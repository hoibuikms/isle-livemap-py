"""Passive session recorder: ``--record FILE``.

Writes every datagram the capture pipeline is handed (the game's own UDP traffic, both directions) to a standard
pcap file, so that ``--replay FILE`` (or ``tools/inspect_capture.py FILE``) can feed it back through the real
decoders. It starts when the app starts, i.e. BEFORE the player joins a server, which is the only way to capture
actor creation records and object exports (identity is sent once, when an actor first becomes relevant).

File format: classic pcap (magic 0xa1b2c3d4, microsecond timestamps, little-endian), link type 101 (LINKTYPE_RAW,
each record is a bare IPv4 packet). The IPv4 + UDP headers are rebuilt from the Datagram's addresses and ports
(the Ethernet layer is not available to the decoders and is not stored); the UDP payload is the exact bytes the
decoders saw. Timestamps are wall-clock time of arrival at the pipeline. Wireshark opens these files.

Strictly passive and fail-safe: it only observes datagrams the pipeline already has (no memory access, no
sending), it never raises into the capture thread, it flushes about once a second, and it stops writing, with a
visible status, when the disk write fails.

Size cap: without ``rollover`` (explicit ``--record FILE``) writing stops at the cap and the file keeps the OLDEST
data. With ``rollover`` (automatic sessions, see sessionlog.py) the file is closed at the cap and a new part file is
opened, so a long session keeps its NEWEST data; the session retention deletes the oldest parts first.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from pathlib import Path
from typing import Callable

from .capture import INBOUND, Datagram, TelemetryPipeline
from .ifaces import get_logger

DEFAULT_MAX_MB = 200.0
FLUSH_INTERVAL_S = 1.0
LINKTYPE_RAW = 101
_PCAP_HEADER = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, LINKTYPE_RAW)


def _ip_checksum(header: bytes) -> int:
    total = sum(struct.unpack(">10H", header))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def _addr(ip: str) -> bytes:
    try:
        return socket.inet_aton(ip or "0.0.0.0")
    except OSError:
        return b"\x00\x00\x00\x00"


def build_record(d: Datagram, ts: float) -> bytes:
    """One pcap record (record header + IPv4/UDP headers + payload) for ``d`` captured at wall time ``ts``."""
    udp_len = 8 + len(d.payload)
    total = 20 + udp_len
    head = struct.pack(">BBHHHBBH4s4s", 0x45, 0, total, 0, 0x4000, 64, 17, 0, _addr(d.src_ip), _addr(d.dst_ip))
    head = head[:10] + struct.pack(">H", _ip_checksum(head)) + head[12:]
    udp = struct.pack(">HHHH", d.src_port & 0xFFFF, d.dst_port & 0xFFFF, udp_len, 0)  # checksum 0 = none (IPv4)
    sec = int(ts)
    usec = min(int((ts - sec) * 1_000_000), 999_999)
    return struct.pack("<IIII", sec, usec, total, total) + head + udp + d.payload


class PcapRecorder:
    def __init__(self, path: str | Path, max_mb: float = DEFAULT_MAX_MB, *,
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic,
                 rollover: Callable[[], Path] | None = None, on_event: Callable[[str], None] | None = None) -> None:
        self.path = Path(path)
        self._rollover = rollover  # returns the path of the next part file; None = stop at the cap
        self._on_event = on_event  # fn(text): start / roll / cap / stop / failure, for the session log
        self.parts = 1
        self.max_bytes = int(max_mb * 1024 * 1024)
        self._clock, self._mono = clock, mono
        self._lock = threading.Lock()
        self._fh = None
        self._pipeline: TelemetryPipeline | None = None
        self._last_flush = 0.0
        self._anchor: tuple[float, float] | None = None  # (wall clock, datagram clock) of the first datagram
        self.packets = 0
        self.bytes = 0
        self.creation = 0  # creation-kind batches in inbound packets (same classifier as the peers status)
        self.exports = 0
        self.destroyed = 0
        self.capped = False
        self.error = ""

    # -- lifecycle ---------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._fh is not None

    def start(self) -> bool:
        """Create the file now. Returns False (and sets ``error``) instead of raising."""
        with self._lock:
            try:
                self._fh = open(self.path, "wb")
                self._fh.write(_PCAP_HEADER)
                self._fh.flush()
            except Exception as err:  # noqa: BLE001 - recording must never stop the app
                self._fail(f"cannot open {self.path}: {err}")
                return False
            self.bytes = len(_PCAP_HEADER)
            self._last_flush = self._mono()
        get_logger().info("recording game datagrams to %s (cap %.0f MB)", self.path, self.max_bytes / 1048576)
        self._event(f"recorder started: {self.path} (cap {self.max_bytes / 1048576:.0f} MB per file"
                    f"{', rolling to a new file at the cap' if self._rollover else ''})")
        return True

    def _event(self, text: str) -> None:
        if self._on_event is not None:
            try:
                self._on_event(text)
            except Exception:  # noqa: BLE001
                pass

    def attach(self, pipeline: TelemetryPipeline) -> bool:
        if not self.start():
            return False
        self._pipeline = pipeline
        pipeline.add_datagram_observer(self.on_datagram)
        return True

    def close(self) -> None:
        if self._pipeline is not None:
            self._pipeline.remove_datagram_observer(self.on_datagram)
            self._pipeline = None
        with self._lock:
            was_open = self._fh is not None
            self._close_file()
        if was_open:
            self._event(f"recorder stopped: {self.packets} packets, {self.parts} file(s), last {self.path.name}")

    # -- tap (decode worker thread; must not raise) --------------------------
    def on_datagram(self, d: Datagram) -> None:
        try:
            if self._fh is None:
                return
            if d.direction == INBOUND:
                self._count(d)
            if self._anchor is None:
                self._anchor = (self._clock(), d.observed_at)
            # wall time = anchor + the datagram's own capture-time offset, so decode-queue lag never skews spacing
            rec = build_record(d, self._anchor[0] + (d.observed_at - self._anchor[1]))
            with self._lock:
                if self._fh is None:
                    return
                if self.bytes + len(rec) > self.max_bytes and not self._roll():
                    return
                self._fh.write(rec)
                self.packets += 1
                self.bytes += len(rec)
                now = self._mono()
                if now - self._last_flush >= FLUSH_INTERVAL_S:
                    self._fh.flush()
                    self._last_flush = now
        except Exception as err:  # noqa: BLE001
            with self._lock:
                self._fail(f"write failed: {err}")

    def _roll(self) -> bool:
        """Size cap reached (lock held). True = a fresh part file is open and the record should be written to it."""
        if self._rollover is None:
            self.capped = True
            self._close_file()
            get_logger().warning("recording stopped: size cap reached (%d packets)", self.packets)
            self._event(f"recorder stopped: size cap reached ({self.packets} packets, {self.path.name})")
            return False
        old = self.path.name
        self._close_file()
        try:
            nxt = Path(self._rollover())
            self._fh = open(nxt, "wb")
            self._fh.write(_PCAP_HEADER)
            self._fh.flush()
        except Exception as err:  # noqa: BLE001
            self._fail(f"cannot roll to a new file: {err}")
            return False
        self.path, self.bytes, self.parts = nxt, len(_PCAP_HEADER), self.parts + 1
        self._event(f"recorder size cap reached in {old}: continuing in {nxt.name} ({self.packets} packets so far)")
        return True

    def _count(self, d: Datagram) -> None:
        try:
            from .decode import count_batches

            c = count_batches(d.payload)
            self.creation += c.creation
            self.exports += c.exports
            self.destroyed += c.destroyed
        except Exception:  # noqa: BLE001 - counters are informational
            pass

    def _fail(self, message: str) -> None:
        self.error = message
        get_logger().error("recording: %s", message)
        self._close_file()
        self._event(f"recorder FAILED: {message}")

    def _close_file(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.flush()
                fh.close()
            except Exception:  # noqa: BLE001
                pass

    # -- status ------------------------------------------------------------
    def status(self) -> str:
        mb = self.bytes / 1048576
        seen = f"creation records seen: {self.creation}, exports {self.exports}"
        name = self.path.name
        if self.error:
            return f"recording FAILED ({self.error}); {self.packets} pkts kept in {name}"
        if self.capped:
            return (f"recording STOPPED at size cap {self.max_bytes / 1048576:.0f} MB "
                    f"({self.packets} pkts; {seen}); restart with a larger --record-max-mb to keep going")
        return f"recording {name}: {self.packets} pkts, {mb:.1f} MB, {seen}"
