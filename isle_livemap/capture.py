"""Passive capture of the player's OWN game UDP traffic (Windows + Npcap).

What this module does:
  * finds the game's process id by name via a Toolhelp32 process *snapshot*
    (enumeration only: the game process is never opened),
  * asks Windows which UDP ports that pid owns (GetExtendedUdpTable, the same
    call `netstat -ano` uses),
  * sniffs UDP with Npcap through scapy, read-only, and keeps datagrams whose
    local port is one the game owns,
  * feeds payload bytes to the decoders.

What it never does: open/read the game's memory, inject anything, hook APIs,
send or modify packets. Windows-only imports are lazy so this file imports (and
its pure logic is testable) on any OS.
"""

from __future__ import annotations

import collections
import ctypes
import math
import queue
import struct
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable

from .ifaces import (
    collect_interfaces, derive_capture_status, get_logger, outbound_ip, select_interfaces, should_fallback_to_all,
)
from .decode import MovementCandidate, find_server_locations
from .tracking import MovementTracker, Vitals, VitalsObservation, VitalsTracker

GAME_PROCESS_NAME = "TheIsleClient-Win64-Shipping"  # without .exe

GAME_PROCESS_PREFIX = "TheIsleClient"  # fallback for renamed shipping builds

SERVER_SWITCH_DATAGRAMS = 5  # outbound datagrams in a row to a new destination before it counts as the new server
SERVER_SWITCH_SILENCE_S = 1.0  # ... and the old server must have been silent this long

INBOUND = "inbound"  # server -> game
OUTBOUND = "outbound"  # game -> server


# ----------------------------------------------------------------- pure helpers


def classify_direction(src_port: int, dst_port: int, owned_ports: Iterable[int]) -> str | None:
    owned = set(owned_ports)
    s, d = src_port in owned, dst_port in owned
    if s and not d:
        return OUTBOUND
    if d and not s:
        return INBOUND
    return None


def build_capture_filter(ports: Iterable[int], include_inbound: bool = True) -> str:
    ordered = sorted(set(ports))
    if not ordered:
        return "udp and (false)"
    clauses: list[str] = []
    for p in ordered:
        if include_inbound:
            clauses += [f"src port {p}", f"dst port {p}"]
        else:
            clauses.append(f"src port {p}")
    return f"udp and ({' or '.join(clauses)})"


def parse_udp_owner_table(buffer: bytes, pid: int) -> set[int]:
    """Parse a MIB_UDPTABLE_OWNER_PID blob: DWORD count, then rows of
    (DWORD local_addr, DWORD local_port[network order, low 16 bits], DWORD pid)."""
    if len(buffer) < 4:
        return set()
    (count,) = struct.unpack_from("<I", buffer, 0)
    ports: set[int] = set()
    for i in range(min(count, (len(buffer) - 4) // 12)):
        _addr, raw_port, owner = struct.unpack_from("<III", buffer, 4 + i * 12)
        if owner != pid:
            continue
        port = ((raw_port & 0xFF) << 8) | ((raw_port >> 8) & 0xFF)
        if port > 0:
            ports.add(port)
    return ports


def pick_game_process(
    processes: Iterable[tuple[int, str]], name: str = GAME_PROCESS_NAME, prefix: str = GAME_PROCESS_PREFIX
) -> tuple[int, str] | None:
    """Choose the game from (pid, exe_file_name) pairs: exact name first, then
    any exe starting with ``prefix``. Case-insensitive; '.exe' optional."""
    exact, fallback = None, None
    want, pre = name.lower(), prefix.lower()
    for pid, exe in processes:
        low = exe.lower()
        stem = low[:-4] if low.endswith(".exe") else low
        if stem == want:
            exact = exact or (pid, exe)
        elif stem.startswith(pre):
            fallback = fallback or (pid, exe)
    return exact or fallback


def isle_like_names(processes: Iterable[tuple[int, str]]) -> list[str]:
    """Sorted unique exe names containing 'isle' (case-insensitive)."""
    return sorted({exe for _pid, exe in processes if "isle" in exe.lower()}, key=str.lower)


def status_text(status: str, counts: dict, has_position: bool) -> str:
    """Human-readable live status. ``status`` is the supervisor stage."""
    if has_position or status == "tracking":
        return "tracking"
    if status != "capturing":
        return status  # "waiting for game", "game running, no UDP socket yet", "replay", ...
    if not counts.get("udp_total", 0):
        return "capturing, 0 packets yet (wrong interface?)"
    if not (counts.get("outbound", 0) or counts.get("inbound", 0)):
        return "capturing, UDP seen but none on the game's ports"
    return "game packets arriving, no position decoded yet"


@dataclass(frozen=True, slots=True)
class Datagram:
    observed_at: float
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    payload: bytes
    direction: str


@dataclass(frozen=True, slots=True)
class Telemetry:
    """Latest known own-player state. Immutable; replaced atomically."""

    position: MovementCandidate | None = None
    position_at: float = 0.0
    vitals: Vitals | None = None
    vitals_at: float = 0.0
    status: str = "waiting"
    stats: dict = field(default_factory=dict)


# A decoded own position that moves further than this between two accepted moves, faster than any creature, while the client's
# own clock carries on, is a jump: a teleport, or the frame the client reports in changed. Recorded session ``m``: 1.2 km at
# standstill, client clock continuous (see docs/PEERS.md, "Own position frame").
JUMP_CM = 30_000.0
JUMP_MIN_SPEED_CM_S = 10_000.0  # ... at more than 100 m/s: no creature moves like that
JUMP_MAX_GAP_S = 10.0  # the tracker takes a new place over only after a couple of seconds of silence from the old one
JUMP_CLOCK_TOL_S = 1.5  # the client clock advanced by the wall time between the two moves, give or take this
JUMPS_KEPT = 20
# The server's own copy of the own location (decode.find_server_locations), confirmed by a second packet: compared with the
# position decoded from the client's moves. 60 m is more than a fast mover's latency (29 m at 15 m/s, 2 s behind).
SERVER_FIX_REPEAT_S = 1.0
SERVER_FIX_SAME_M = 3.0
SERVER_FIX_MAX_DZ_M = 60.0
SERVER_FIX_MAX_M = 6_000.0
SERVER_FIX_FAR_M = 60.0
FRAME_CHECKS_KEPT = 20
# No accepted own move for this long: the marker is where the last move put it, not necessarily where the player is (dead and
# waiting to respawn, a loading screen, a teleport: recorded gaps of 4 s to 24 s, ending 50 m to 1.4 km from where they began).
MOVE_GAP_S = 3.0
MOVE_GAPS_KEPT = 20


class TelemetryPipeline:
    """Routes datagrams to the decoders: outbound client moves -> position and
    heading, inbound server replication -> own vitals. Thread-safe."""

    def __init__(self, on_update: Callable[[Telemetry], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._movement = MovementTracker()
        self._vitals = VitalsTracker()
        self._server: str | None = None
        self._server_seen = 0.0  # datagram time of the last outbound datagram to ``_server``
        self._rival: tuple[str, int] | None = None  # (destination, consecutive outbound datagrams) that is not _server
        self._on_update = on_update
        # Passive read-only taps for other consumers of the same decoded stream (peers.GamePeerSource).
        # inbound: fn(datagram, own_net_ref_handle | None) for every inbound datagram from the learned server;
        # reset: fn() when handles stop being valid (new server / game restart).
        self._inbound_observers: list[Callable[[Datagram, int | None], None]] = []
        self._reset_observers: list[Callable[[], None]] = []
        # fn(datagram) for EVERY datagram handed to handle(), both directions, before decoding (the recorder).
        self._datagram_observers: list[Callable[[Datagram], None]] = []
        self._counts = {"outbound": 0, "inbound": 0, "dropped": 0, "udp_total": 0, "decoded": 0,
                        "queue_max": 0, "slow_decodes": 0, "capture_errors": 0, "worker_restarts": 0,
                        "server_fixes": 0, "server_fixes_far": 0}
        # fn(where) called from inside an ``except`` block when a decoder/observer raised (the session log
        # records the traceback); the exception itself is still swallowed so capture never stops.
        self.error_hook: Callable[[str], None] | None = None
        self.latest = Telemetry()
        # Manual position alignment (see align_to): world cm added to every decoded own position. The raw position is kept so
        # the alignment can be set, changed and cleared at any time; a jump of the raw position is counted (position_jumps).
        self._align = (0.0, 0.0)
        self._align_stale = False  # the raw position jumped since the alignment was set
        self._raw: MovementCandidate | None = None
        self._raw_at = 0.0
        self.jumps: list[tuple[float, float]] = []  # (datagram time, metres) of the last JUMPS_KEPT position jumps
        # (datagram time of the move that ended the gap, seconds without an accepted move, metres from where it began)
        self.move_gaps: list[tuple[float, float, float]] = []
        # (datagram time, metres between the server's own location and the decoded one, server (x, y, z) cm) of the last
        # FRAME_CHECKS_KEPT confirmed server locations that were further than SERVER_FIX_FAR_M from the decoded position
        self.frame_checks: list[tuple[float, float, tuple[float, float, float]]] = []
        self._srv_prev: tuple[float, list[tuple[float, float, float]]] | None = None

    def set_status(self, status: str) -> None:
        with self._lock:
            self.latest = Telemetry(
                self.latest.position, self.latest.position_at, self.latest.vitals, self.latest.vitals_at,
                status, dict(self._counts),
            )

    def add_observer(self, on_inbound: Callable[[Datagram, int | None], None],
                     on_reset: Callable[[], None] | None = None) -> None:
        with self._lock:
            self._inbound_observers.append(on_inbound)
            if on_reset is not None:
                self._reset_observers.append(on_reset)

    def remove_observer(self, on_inbound: Callable[[Datagram, int | None], None],
                        on_reset: Callable[[], None] | None = None) -> None:
        with self._lock:
            if on_inbound in self._inbound_observers:
                self._inbound_observers.remove(on_inbound)
            if on_reset is not None and on_reset in self._reset_observers:
                self._reset_observers.remove(on_reset)

    def add_datagram_observer(self, fn: Callable[[Datagram], None]) -> None:
        with self._lock:
            self._datagram_observers.append(fn)

    def remove_datagram_observer(self, fn: Callable[[Datagram], None]) -> None:
        with self._lock:
            if fn in self._datagram_observers:
                self._datagram_observers.remove(fn)

    @property
    def server(self) -> str | None:
        """``ip:port`` of the game server learned from outbound traffic (None until then)."""
        return self._server

    def report_error(self, where: str) -> None:
        hook = self.error_hook
        if hook is not None:
            try:
                hook(where)
            except Exception:  # noqa: BLE001
                pass

    def _notify_reset(self, observers: list[Callable[[], None]]) -> None:
        for fn in observers:
            try:
                fn()
            except Exception:  # noqa: BLE001 - an observer must never break own-player decoding
                self.report_error("reset observer")

    def reset(self) -> None:
        with self._lock:
            self._movement.reset()
            self._vitals.reset()
            self._srv_prev = None
            self._server = None
            self._rival = None
            self._align, self._align_stale, self._raw = (0.0, 0.0), False, None
            self.latest = Telemetry(status="waiting", stats=dict(self._counts))
            observers = list(self._reset_observers)
        self._notify_reset(observers)

    def note_dropped(self, n: int = 1) -> None:
        self._counts["dropped"] += n

    def note_gauge(self, name: str, value: int) -> None:
        """Keep the largest value seen (queue depth)."""
        if value > self._counts.get(name, 0):
            self._counts[name] = value

    def note_count(self, name: str, n: int = 1) -> None:
        self._counts[name] = self._counts.get(name, 0) + n

    def note_udp(self, n: int = 1) -> None:
        """Count every UDP packet the sniffer sees (before port filtering)."""
        self._counts["udp_total"] += n

    def counts(self) -> dict:
        return dict(self._counts)

    def status_text(self) -> str:
        cur = self.latest
        text = status_text(cur.status, self._counts, cur.position is not None)
        if cur.position is not None and (self._align[0] or self._align[1]):
            dx, dy = self._align[0] / 100.0, self._align[1] / 100.0
            text += f" · aligned {dx:+.0f}, {dy:+.0f} m" + (" (position jumped since: align again?)" if self._align_stale else "")
        return text

    # -- manual alignment of the own position ---------------------------------------------------------------------
    def _aligned(self, sample: MovementCandidate) -> MovementCandidate:
        dx, dy = self._align
        return sample if not (dx or dy) else replace(sample, x=sample.x + dx, y=sample.y + dy)

    def _note_jump(self, sample: MovementCandidate, at: float) -> None:
        prev = self._raw
        if prev is None or not 0.0 <= at - self._raw_at <= JUMP_MAX_GAP_S:
            return
        far = math.hypot(sample.x - prev.x, sample.y - prev.y)
        gap = at - self._raw_at
        dts = sample.client_timestamp - prev.client_timestamp
        # the client clock carried on with the wall clock: not a respawn (that restarts it)
        if far > JUMP_CM and far / max(gap, 0.05) > JUMP_MIN_SPEED_CM_S and abs(dts - gap) <= JUMP_CLOCK_TOL_S:
            self.jumps.append((at, far / 100.0))
            del self.jumps[:-JUMPS_KEPT]
            self._counts["position_jumps"] = self._counts.get("position_jumps", 0) + 1
            if self._align[0] or self._align[1]:
                self._align_stale = True

    def _note_move_gap(self, sample: MovementCandidate, at: float) -> None:
        prev = self._raw
        if prev is None or at - self._raw_at <= MOVE_GAP_S:
            return
        self.move_gaps.append((at, at - self._raw_at, math.hypot(sample.x - prev.x, sample.y - prev.y) / 100.0))
        del self.move_gaps[:-MOVE_GAPS_KEPT]
        self._counts["own_move_gaps"] = self._counts.get("own_move_gaps", 0) + 1

    def _check_server_location(self, d: Datagram) -> None:
        """Inbound packet of the learned server: does it carry the own location the server holds, and does it agree with the
        position decoded from the client's moves? Counted only (never changes the position): in every recorded connection
        the two agree to a few metres, which is what shows the own marker and the replicated creatures share a frame."""
        locs = find_server_locations(d.payload)
        prev = self._srv_prev
        if locs:
            self._srv_prev = (d.observed_at, locs)  # packets without a triple in between do not forget the last one
        raw = self._raw
        if not locs or prev is None or raw is None or d.observed_at - prev[0] > SERVER_FIX_REPEAT_S or d.observed_at - self._raw_at > 2.0:
            return
        for x, y, z in locs:
            if not any(math.hypot(x - px, y - py) < SERVER_FIX_SAME_M * 100 and abs(z - pz) < SERVER_FIX_SAME_M * 100 for px, py, pz in prev[1]):
                continue
            dz = abs(z - raw.z) / 100.0
            far = math.hypot(x - raw.x, y - raw.y) / 100.0
            if dz > SERVER_FIX_MAX_DZ_M or far > SERVER_FIX_MAX_M:
                continue  # not the own location (another triple of doubles)
            self._counts["server_fixes"] += 1
            if far > SERVER_FIX_FAR_M:
                self._counts["server_fixes_far"] += 1
                self.frame_checks.append((d.observed_at, far, (x, y, z)))
                del self.frame_checks[:-FRAME_CHECKS_KEPT]
            return

    def align_to(self, x: float, y: float) -> bool:
        """The user stands at world (x, y) (cm), e.g. next to another player whose position is known: from now on every
        decoded own position is shifted by the difference, so the own marker, the distances and the peers placed around it
        agree with that spot. Cleared by ``clear_alignment``, a new server and ``reset``. False without a position yet."""
        with self._lock:
            raw = self._raw
            if raw is None:
                return False
            self._align, self._align_stale = (x - raw.x, y - raw.y), False
            cur = self.latest
            if cur.position is not None:
                self.latest = Telemetry(self._aligned(raw), cur.position_at, cur.vitals, cur.vitals_at, cur.status, dict(self._counts))
            snap = self.latest
        if self._on_update:
            self._on_update(snap)
        return True

    def clear_alignment(self) -> None:
        with self._lock:
            self._align, self._align_stale = (0.0, 0.0), False
            cur, raw = self.latest, self._raw
            if cur.position is not None and raw is not None:
                self.latest = Telemetry(raw, cur.position_at, cur.vitals, cur.vitals_at, cur.status, dict(self._counts))
            snap = self.latest
        if self._on_update:
            self._on_update(snap)

    @property
    def alignment_m(self) -> tuple[float, float]:
        return (self._align[0] / 100.0, self._align[1] / 100.0)

    def handle(self, d: Datagram) -> Telemetry | None:
        """Decode one datagram; returns the new snapshot if something changed."""
        for fn in tuple(self._datagram_observers):  # passive raw tap; a failing observer never affects decoding
            try:
                fn(d)
            except Exception:  # noqa: BLE001
                self.report_error("datagram observer")
        tap: tuple[list, int | None] | None = None
        reset_taps: list | None = None
        with self._lock:
            cur = self.latest
            pos, pos_at, vit, vit_at = cur.position, cur.position_at, cur.vitals, cur.vitals_at
            changed = False
            if d.direction == OUTBOUND:
                self._counts["outbound"] += 1
                dest = f"{d.dst_ip}:{d.dst_port}"
                if self._server is None:  # first outbound datagram, or the first one after reset(): nothing to wipe
                    self._server, self._server_seen, self._rival = dest, d.observed_at, None
                elif dest == self._server:
                    self._server_seen, self._rival = d.observed_at, None
                else:
                    # The game also sends the odd datagram from a second socket to another host (a 1300-byte latency
                    # probe in a live capture). That is not a server change: wiping the identity and vitals state for
                    # it lost the own-vitals lock for good. Switch only when the new destination takes over, i.e. it
                    # sent SERVER_SWITCH_DATAGRAMS in a row and the old one has been silent for SERVER_SWITCH_SILENCE s.
                    n = self._rival[1] + 1 if self._rival and self._rival[0] == dest else 1
                    self._rival = (dest, n)
                    if n >= SERVER_SWITCH_DATAGRAMS and d.observed_at - self._server_seen >= SERVER_SWITCH_SILENCE_S:
                        self._server, self._server_seen, self._rival = dest, d.observed_at, None
                        self._vitals.reset()
                        vit, vit_at = None, 0.0
                        self._align, self._align_stale, self._raw, self._srv_prev = (0.0, 0.0), False, None, None
                        reset_taps = list(self._reset_observers)
                    else:
                        return None  # a stray datagram: not game traffic of the learned server
                sample = self._movement.track(d.payload, d.observed_at)
                if sample is not None:
                    self._counts["decoded"] += 1
                    self._note_jump(sample, d.observed_at)
                    self._note_move_gap(sample, d.observed_at)
                    self._raw, self._raw_at = sample, d.observed_at
                    pos, pos_at, changed = self._aligned(sample), d.observed_at, True
            elif d.direction == INBOUND:
                self._counts["inbound"] += 1
                if f"{d.src_ip}:{d.src_port}" == self._server:
                    obs: VitalsObservation | None = self._vitals.track(d.payload, d.observed_at)
                    if obs is not None:
                        vit, vit_at, changed = obs.vitals, d.observed_at, True
                    self._check_server_location(d)
                    if self._inbound_observers:
                        tap = (list(self._inbound_observers), self._vitals.self_handle)
            if not changed:
                snap = None
            else:
                status = "tracking" if pos is not None else cur.status
                self.latest = Telemetry(pos, pos_at, vit, vit_at, status, dict(self._counts))
                snap = self.latest
        if reset_taps:
            self._notify_reset(reset_taps)
        if tap is not None:
            for fn in tap[0]:
                try:
                    fn(d, tap[1])
                except Exception:  # noqa: BLE001 - an observer must never break own-player decoding
                    self.report_error("inbound observer")
        if snap is not None and self._on_update:
            self._on_update(snap)
        return snap


# ----------------------------------------------------------------- Windows plumbing


class CaptureUnavailable(RuntimeError):
    pass


def is_windows() -> bool:
    return sys.platform == "win32"


def list_processes() -> list[tuple[int, str]]:
    """(pid, exe file name) for all processes, from a Toolhelp32 snapshot. This
    only reads the system's process *list*; it does not open any process."""
    if not is_windows():
        return []
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x2

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, ctypes.c_void_p(-1).value):
        return []
    out: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def find_game_pid(process_name: str = GAME_PROCESS_NAME) -> int | None:
    """Game pid by exact name, falling back to any ``TheIsleClient*`` exe."""
    found = pick_game_process(list_processes(), process_name)
    return found[0] if found else None


def owned_udp_ports(pid: int) -> set[int]:
    """UDP ports (IPv4) owned by ``pid`` (WindowsUdpPortOwnerResolver.cs)."""
    if not is_windows():
        return set()
    from ctypes import wintypes

    AF_INET, UDP_TABLE_OWNER_PID, ERROR_INSUFFICIENT_BUFFER = 2, 1, 122
    iphlp = ctypes.WinDLL("iphlpapi")
    iphlp.GetExtendedUdpTable.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG,
    ]
    iphlp.GetExtendedUdpTable.restype = wintypes.DWORD
    size = wintypes.DWORD(0)
    if iphlp.GetExtendedUdpTable(None, ctypes.byref(size), True, AF_INET, UDP_TABLE_OWNER_PID, 0) != ERROR_INSUFFICIENT_BUFFER:
        return set()
    for _ in range(3):  # table can grow between the two calls
        buf = ctypes.create_string_buffer(size.value)
        rc = iphlp.GetExtendedUdpTable(buf, ctypes.byref(size), True, AF_INET, UDP_TABLE_OWNER_PID, 0)
        if rc == 0:
            return parse_udp_owner_table(buf.raw[: size.value], pid)
        if rc != ERROR_INSUFFICIENT_BUFFER:
            break
    return set()


def npcap_available() -> bool:
    if not is_windows():
        return False
    for name in (r"C:\Windows\System32\Npcap\wpcap.dll", "wpcap.dll"):
        try:
            ctypes.WinDLL(name)
            return True
        except OSError:
            continue
    return False


def datagram_from_scapy(pkt, owned_ports: set[int], now: float) -> Datagram | None:
    """Turn a scapy packet into a Datagram if it belongs to the game's sockets."""
    from scapy.layers.inet import IP, UDP  # lazy: scapy is only needed for capture

    if UDP not in pkt:
        return None
    udp = pkt[UDP]
    payload = bytes(udp.payload)
    if not payload:
        return None
    direction = classify_direction(int(udp.sport), int(udp.dport), owned_ports)
    if direction is None:
        return None
    ip = pkt[IP] if IP in pkt else None
    return Datagram(
        now, ip.src if ip else "", int(udp.sport), ip.dst if ip else "", int(udp.dport), payload, direction
    )


class PreCaptureBuffer:
    """Recent UDP packets whose ports no game socket owns (yet).

    The sniffers run from the moment the app starts, before the game's process or its UDP socket is known (finding
    them and opening Npcap takes seconds, and the server's join tables are sent only once). Packets of unowned ports
    wait here for ``MAX_AGE_S``; when ``set_ports`` learns a port, the packets of that port are handed over first, in
    arrival order, with their original timestamps, so a connection started during the delay is decoded from its first
    datagram.

    The hand-over runs OUTSIDE the lock (parsing thousands of packets used to keep the sniffer thread out of ``offer``
    for 100 ms and more). Until it has caught up, the new ports stay "unowned": ``offer`` keeps holding their packets,
    and the hand-over loop takes them too, so a packet that arrives during the hand-over still comes after the older
    ones. Only then do the ports become owned."""

    MAX_AGE_S = 15.0
    MAX_PACKETS = 4096

    def __init__(self, max_age_s: float = MAX_AGE_S, max_packets: int = MAX_PACKETS) -> None:
        self.max_age_s, self.max_packets = max_age_s, max_packets
        self._lock = threading.Lock()
        self._handover = threading.Lock()  # one set_ports at a time
        self._held: collections.deque = collections.deque()  # (time, packet, source port, destination port)
        self.owned: frozenset[int] = frozenset()
        self._draining: frozenset[int] = frozenset()
        self.replayed = 0

    def __len__(self) -> int:
        return len(self._held)

    @property
    def ports(self) -> frozenset[int]:
        """Ports whose packets may be turned into datagrams now: owned, or being handed over."""
        return self.owned | self._draining

    def offer(self, at: float, packet, sport: int, dport: int) -> bool:
        """True: held (nobody owns either port). False: the caller handles the packet now."""
        with self._lock:
            if sport in self.owned or dport in self.owned:
                return False
            held = self._held
            held.append((at, packet, sport, dport))
            while held and (len(held) > self.max_packets or at - held[0][0] > self.max_age_s):
                held.popleft()
            return True

    def set_ports(self, ports: Iterable[int], emit: Callable[[float, object], None]) -> int:
        """Replace the owned ports; ``emit(time, packet)`` gets every held packet of a newly owned port. Returns
        how many were handed over."""
        ports = frozenset(ports)
        with self._handover:
            with self._lock:
                self.owned = self.owned & ports  # a port the game closed is unowned again at once
                new = ports - self.owned
                if not new:
                    return 0
                self._draining = new
            handed = 0
            try:
                while True:
                    with self._lock:
                        mine = [item for item in self._held if item[2] in new or item[3] in new]
                        if not mine:
                            self.owned = self.owned | new
                            self._draining = frozenset()
                            break
                        self._held = collections.deque(i for i in self._held if not (i[2] in new or i[3] in new))
                    for item in mine:
                        try:
                            emit(item[0], item[1])
                        except Exception:  # noqa: BLE001 - one bad packet must not leave the ports half handed over
                            pass
                        handed += 1
            finally:
                with self._lock:
                    if self._draining:  # emit raised something worse than an Exception: still end the hand-over
                        self.owned = self.owned | self._draining
                        self._draining = frozenset()
            self.replayed += handed
            return handed


class LiveCapture:
    """Background capture: supervisor thread + sniffer + decode worker."""

    def __init__(self, pipeline: TelemetryPipeline, iface: str | None = None) -> None:
        self.pipeline = pipeline
        self.iface = iface
        self._stop = threading.Event()
        self._q: queue.Queue[Datagram] = queue.Queue(maxsize=8192)
        self._precap = PreCaptureBuffer()
        self._early_retry_at = 0.0
        self._threads: list[threading.Thread] = []
        self._sniffers: list = []
        self._force_all = False
        self._all_mode = False
        self._opened_at = 0.0
        self._udp_at_open = 0
        self._tracked_pid: int | None = None
        self._stall_udp = -1
        self._stall_at = 0.0
        self._reopened_at = 0.0
        self._work_thread: threading.Thread | None = None
        self._packet_errors = 0

    RETRY_STEP_S = 1.0  # after a failed supervisor step: wait this much per failure in a row ...
    RETRY_MAX_S = 5.0  # ... up to this
    STALL_S = 10.0  # the game owns UDP ports but not one UDP packet was seen this long: the interface is not the right one
    STALL_COOLDOWN_S = 30.0
    SLOW_DECODE_S = 0.05  # a datagram that takes the worker longer than this is counted ("slow decodes")

    def start(self) -> None:
        if not is_windows():
            raise CaptureUnavailable("Live capture needs Windows + Npcap. Use --replay on this OS.")
        if not npcap_available():
            raise CaptureUnavailable("Npcap not found. Install it from https://npcap.com (WinPcap API-compatible mode).")
        self._start_worker()
        t = threading.Thread(target=self._supervise, daemon=True, name="isle-_supervise")
        t.start()
        self._threads.append(t)

    def _start_worker(self) -> None:
        t = threading.Thread(target=self._work, daemon=True, name="isle-_work")
        t.start()
        self._work_thread = t
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._close_sniffers()

    def _on_packet(self, pkt) -> None:
        try:
            from scapy.layers.inet import UDP

            if UDP not in pkt:
                return
            self.pipeline.note_udp()
            now = time.monotonic()
            if self._precap.offer(now, pkt, int(pkt[UDP].sport), int(pkt[UDP].dport)):
                return  # no game socket owns this port yet: kept for the moment one does
            d = datagram_from_scapy(pkt, self._precap.ports, now)
            if d is not None:
                self._q.put_nowait(d)
        except queue.Full:
            self.pipeline.note_dropped()
        except Exception:  # noqa: BLE001 - malformed traffic must not stop capture
            self._packet_errors += 1
            self.pipeline.note_count("capture_errors")
            if self._packet_errors <= 3:
                get_logger().exception("sniffer callback failed (%d so far)", self._packet_errors)

    def _work(self) -> None:
        """Decode worker. Nothing a datagram does may end this loop; if it ends anyway the supervisor starts it again."""
        depth_seen = 0
        while not self._stop.is_set():
            try:
                d = self._q.get(timeout=0.25)
            except queue.Empty:
                continue
            depth = self._q.qsize()
            if depth > depth_seen:
                depth_seen = depth
                self.pipeline.note_gauge("queue_max", depth)
            began = time.perf_counter()
            try:
                self.pipeline.handle(d)
            except Exception:  # noqa: BLE001
                self.pipeline.report_error("pipeline.handle")
            if time.perf_counter() - began > self.SLOW_DECODE_S:
                self.pipeline.note_count("slow_decodes")

    # -- sniffer management ------------------------------------------------

    def _open_sniffers(self, names: list[str]) -> tuple[list[str], dict[str, str]]:
        """Start one read-only AsyncSniffer per interface; keep those that are
        confirmed running. A failing interface never aborts the others."""
        from scapy.sendrecv import AsyncSniffer

        log = get_logger()
        opened: list[str] = []
        errors: dict[str, str] = {}
        started = []
        for name in names:
            try:
                s = AsyncSniffer(iface=name, filter="udp", prn=self._on_packet, store=False)
                s.start()
                started.append((name, s))
            except Exception as e:  # noqa: BLE001
                errors[name] = f"{type(e).__name__}: {e}"
        deadline = time.monotonic() + 1.5
        while started and time.monotonic() < deadline:
            if all(getattr(s, "running", False) or not _thread_alive(s) for _n, s in started):
                break
            time.sleep(0.05)
        for name, s in started:
            err = getattr(s, "exception", None)
            if getattr(s, "running", False) and _thread_alive(s) and err is None:
                opened.append(name)
                self._sniffers.append((name, s))
            else:
                errors[name] = f"{type(err).__name__}: {err}" if err else "sniffer thread exited right after start"
                try:
                    s.stop()
                except Exception:  # noqa: BLE001
                    pass
        for n in opened:
            log.info("opened interface %s", n)
        for n, e in errors.items():
            log.error("could not open interface %s: %s", n, e)
        return opened, errors

    def _close_sniffers(self) -> None:
        for _n, s in self._sniffers:
            try:
                s.stop()
            except Exception:  # noqa: BLE001
                pass
        self._sniffers = []

    def _emit_held(self, at: float, pkt) -> None:
        d = datagram_from_scapy(pkt, self._precap.ports, at)  # the ports being handed over are not owned yet
        if d is None:
            return
        try:
            self._q.put_nowait(d)
        except queue.Full:
            self.pipeline.note_dropped()

    def _ensure_early(self) -> None:
        """Keep sniffers open while the game is not (fully) up. A failure is retried every 10 s, silently: the
        normal path reports it once the game runs."""
        if self._sniffers and not any(not (getattr(s, "running", False) and _thread_alive(s)) for _n, s in self._sniffers):
            return
        if time.monotonic() < self._early_retry_at:
            return
        self._ensure_capture(quiet=True)
        if not self._sniffers:
            self._early_retry_at = time.monotonic() + 10.0

    def _ensure_capture(self, quiet: bool = False) -> None:
        """Open sniffers if none are healthy: preferred interface first, then all. ``quiet``: the game is not
        running yet, so leave the status alone (it keeps saying what the app is waiting for)."""
        log = get_logger()
        dead = [n for n, s in self._sniffers if not (getattr(s, "running", False) and _thread_alive(s))]
        if dead and self._sniffers:
            log.error("sniffer(s) died: %s; restarting", dead)
            self._close_sniffers()
        if self._sniffers:
            return
        try:
            infos = collect_interfaces()
        except Exception as e:  # noqa: BLE001
            log.error("cannot list interfaces: %s", e)
            infos = []
        try:
            ip = outbound_ip()
        except Exception:  # noqa: BLE001
            ip = None
        primary, everything, reason = select_interfaces(infos, ip, self.iface)
        log.info("interfaces: %s", [(i.name, i.description, i.ips) for i in infos])
        log.info("selection: %s", reason)
        errors: dict[str, str] = {}
        opened: list[str] = []
        self._all_mode = False
        if primary and not self._force_all:
            opened, errors = self._open_sniffers(primary)
        if not opened:
            self._all_mode = True
            more, errs = self._open_sniffers([n for n in everything if n not in errors])
            opened, errors = more, {**errors, **errs}
        self._opened_at = time.monotonic()
        self._udp_at_open = self.pipeline.counts().get("udp_total", 0)
        if quiet:
            log.info("sniffing before the game starts (opened=%s, errors=%s)", opened, errors)
            return
        self.pipeline.set_status(derive_capture_status(opened, errors))
        log.info("status after open: %s (opened=%s)", self.pipeline.latest.status, opened)

    def _watchdog(self) -> None:
        """No UDP at all a few seconds after opening one interface: widen to all."""
        udp = self.pipeline.counts().get("udp_total", 0) - self._udp_at_open
        if self._sniffers and should_fallback_to_all(udp, time.monotonic() - self._opened_at, self._all_mode):
            get_logger().warning("no UDP seen on %s after 5 s; restarting on all interfaces", [n for n, _ in self._sniffers])
            self._close_sniffers()
            self._force_all = True  # one-shot widening

    def _supervise(self) -> None:
        """Supervisor thread: one step every second or so. A step that raises is logged and retried (the old loop
        ended for good, leaving the app with a minimap that never updated again and a status line saying why only once)."""
        log = get_logger()
        failures = 0
        try:
            from scapy.config import conf

            conf.use_pcap = True  # sniff through Npcap
        except Exception as e:  # noqa: BLE001
            log.exception("scapy unavailable")
            self.pipeline.set_status(f"capture failed: {type(e).__name__}: {e}"[:160])
            return
        while not self._stop.is_set():
            try:
                wait = self._step()
                failures = 0
            except Exception as e:  # noqa: BLE001
                failures += 1
                self.pipeline.note_count("capture_errors")
                if failures <= 3 or failures % 30 == 0:
                    log.exception("supervisor step failed (%d in a row)", failures)
                self.pipeline.set_status(f"capture error, retrying: {type(e).__name__}: {e}"[:160])
                wait = min(failures * self.RETRY_STEP_S, self.RETRY_MAX_S)
            self._stop.wait(wait)
        self._close_sniffers()

    def _ensure_worker(self) -> None:
        t = self._work_thread
        if t is not None and not t.is_alive() and not self._stop.is_set():
            get_logger().error("decode worker died; starting a new one")
            self.pipeline.note_count("worker_restarts")
            self._start_worker()

    def _check_stall(self, now: float) -> None:
        """The game has a UDP socket but no UDP packet at all has been seen for ``STALL_S``: the sniffers listen on an
        interface the traffic no longer uses (Wi-Fi to cable, VPN up or down). Reopen on every interface."""
        udp = self.pipeline.counts().get("udp_total", 0)
        if udp != self._stall_udp:
            self._stall_udp, self._stall_at = udp, now
        elif self._sniffers and now - self._stall_at >= self.STALL_S and now - self._reopened_at >= self.STALL_COOLDOWN_S:
            get_logger().warning("no UDP seen for %.0f s on %s although the game has a socket; reopening on all interfaces",
                                 now - self._stall_at, [n for n, _ in self._sniffers])
            self._close_sniffers()
            self._force_all = True
            self._reopened_at = now
            self._stall_at = now

    def _step(self) -> float:
        """One supervisor round; returns how long to wait before the next."""
        self._ensure_worker()
        pid = find_game_pid()
        if pid is None:
            if self._tracked_pid is not None:
                self.pipeline.reset()
                self._tracked_pid = None
                self._precap.set_ports((), self._emit_held)
            self.pipeline.set_status("waiting for game")
            self._ensure_early()  # already sniffing when the game starts and joins
            return 1.0
        ports = owned_udp_ports(pid)
        if not ports:
            self.pipeline.set_status("game running, no UDP socket yet")
            self._ensure_early()
            return 0.5
        if pid != self._tracked_pid:
            self.pipeline.reset()
            self._tracked_pid = pid
            self._opened_at = time.monotonic()  # the 5 s "no UDP at all" check starts with the game
            self._udp_at_open = self.pipeline.counts().get("udp_total", 0)
            self._stall_udp, self._stall_at = -1, time.monotonic()
        self._precap.set_ports(ports, self._emit_held)  # packets held for a newly owned port go first
        self._watchdog()
        self._ensure_capture()
        self._check_stall(time.monotonic())
        if self._sniffers and self.pipeline.latest.status not in ("capturing", "tracking"):
            self.pipeline.set_status("capturing")
        return 2.0


def _thread_alive(sniffer) -> bool:
    t = getattr(sniffer, "thread", None)
    return t is not None and t.is_alive()
