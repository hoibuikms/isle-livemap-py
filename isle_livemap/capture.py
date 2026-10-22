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

import ctypes
import queue
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .decode import MovementCandidate
from .tracking import MovementTracker, Vitals, VitalsObservation, VitalsTracker

GAME_PROCESS_NAME = "TheIsleClient-Win64-Shipping"  # without .exe

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


class TelemetryPipeline:
    """Routes datagrams to the decoders: outbound client moves -> position and
    heading, inbound server replication -> own vitals. Thread-safe."""

    def __init__(self, on_update: Callable[[Telemetry], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._movement = MovementTracker()
        self._vitals = VitalsTracker()
        self._server: str | None = None
        self._on_update = on_update
        self._counts = {"outbound": 0, "inbound": 0, "dropped": 0}
        self.latest = Telemetry()

    def set_status(self, status: str) -> None:
        with self._lock:
            self.latest = Telemetry(
                self.latest.position, self.latest.position_at, self.latest.vitals, self.latest.vitals_at,
                status, dict(self._counts),
            )

    def reset(self) -> None:
        with self._lock:
            self._movement.reset()
            self._vitals.reset()
            self._server = None
            self.latest = Telemetry(status="waiting", stats=dict(self._counts))

    def note_dropped(self, n: int = 1) -> None:
        self._counts["dropped"] += n

    def handle(self, d: Datagram) -> Telemetry | None:
        """Decode one datagram; returns the new snapshot if something changed."""
        with self._lock:
            cur = self.latest
            pos, pos_at, vit, vit_at = cur.position, cur.position_at, cur.vitals, cur.vitals_at
            changed = False
            if d.direction == OUTBOUND:
                self._counts["outbound"] += 1
                server = f"{d.dst_ip}:{d.dst_port}"
                if server != self._server:
                    self._server = server
                    self._vitals.reset()
                    vit, vit_at = None, 0.0
                sample = self._movement.track(d.payload, d.observed_at)
                if sample is not None:
                    pos, pos_at, changed = sample, d.observed_at, True
            elif d.direction == INBOUND:
                self._counts["inbound"] += 1
                if f"{d.src_ip}:{d.src_port}" == self._server:
                    obs: VitalsObservation | None = self._vitals.track(d.payload, d.observed_at)
                    if obs is not None:
                        vit, vit_at, changed = obs.vitals, d.observed_at, True
            if not changed:
                return None
            status = "tracking" if pos is not None else cur.status
            self.latest = Telemetry(pos, pos_at, vit, vit_at, status, dict(self._counts))
            snap = self.latest
        if self._on_update:
            self._on_update(snap)
        return snap


# ----------------------------------------------------------------- Windows plumbing


class CaptureUnavailable(RuntimeError):
    pass


def is_windows() -> bool:
    return sys.platform == "win32"


def find_game_pid(process_name: str = GAME_PROCESS_NAME) -> int | None:
    """Look the game up in a Toolhelp32 snapshot. This only reads the system's
    process *list* (name + pid); it does not open the game process."""
    if not is_windows():
        return None
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
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        wanted = (process_name + ".exe").lower()
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == wanted:
                return int(entry.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return None


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


class LiveCapture:
    """Background capture: supervisor thread + sniffer + decode worker."""

    def __init__(self, pipeline: TelemetryPipeline, iface: str | None = None) -> None:
        self.pipeline = pipeline
        self.iface = iface
        self._stop = threading.Event()
        self._q: queue.Queue[Datagram] = queue.Queue(maxsize=8192)
        self._ports: set[int] = set()
        self._threads: list[threading.Thread] = []
        self._sniffer = None

    def start(self) -> None:
        if not is_windows():
            raise CaptureUnavailable("Live capture needs Windows + Npcap. Use --replay on this OS.")
        if not npcap_available():
            raise CaptureUnavailable("Npcap not found. Install it from https://npcap.com (WinPcap API-compatible mode).")
        for target in (self._supervise, self._work):
            t = threading.Thread(target=target, daemon=True, name=f"isle-{target.__name__}")
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        s = self._sniffer
        if s is not None:
            try:
                s.stop()
            except Exception:  # noqa: BLE001 - shutting down
                pass

    def _on_packet(self, pkt) -> None:
        try:
            d = datagram_from_scapy(pkt, self._ports, time.monotonic())
            if d is not None:
                self._q.put_nowait(d)
        except queue.Full:
            self.pipeline.note_dropped()
        except Exception:  # noqa: BLE001 - malformed traffic must not stop capture
            pass

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                d = self._q.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self.pipeline.handle(d)
            except Exception:  # noqa: BLE001
                pass

    def _supervise(self) -> None:
        from scapy.config import conf
        from scapy.sendrecv import AsyncSniffer

        conf.use_pcap = True  # sniff through Npcap
        tracked_pid: int | None = None
        while not self._stop.is_set():
            pid = find_game_pid()
            if pid is None:
                if tracked_pid is not None:
                    self.pipeline.reset()
                    tracked_pid = None
                self.pipeline.set_status("waiting for game")
                self._stop.wait(2.0)
                continue
            ports = owned_udp_ports(pid)
            if not ports:
                self.pipeline.set_status("game running, no UDP socket yet")
                self._stop.wait(1.0)
                continue
            if pid != tracked_pid:
                self.pipeline.reset()
                tracked_pid = pid
            self._ports = ports  # atomic replace; read by the sniffer callback
            if self._sniffer is None:
                # Read-only sniffing. BPF is applied in user mode via the port set
                # because the game's sockets can be re-bound at any time.
                self._sniffer = AsyncSniffer(iface=self.iface, filter="udp", prn=self._on_packet, store=False)
                self._sniffer.start()
                self.pipeline.set_status("capturing")
            self._stop.wait(2.0)
