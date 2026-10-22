"""Plaintext-leak debugging capture for the lab simulator (loopback only).

Scope: this module only ever listens on 127.0.0.1 and only on the lab port
(labsim.telemetry.PORT, 7777). The record layout below is the *assumed,
simulated* Node format used by labsim -- it is not any real game's protocol.

Two interchangeable backends feed the same parser:
  * "scapy":  passive sniff of the loopback interface (needs scapy + privileges;
              BPF filter "udp and host 127.0.0.1 and port N"). Sees traffic that
              labsim's own receiver also consumes.
  * "socket": plain UDP socket bound to 127.0.0.1:N (no privileges). It takes
              the port, so use it instead of, not next to, another receiver.

Capture runs on a daemon thread and never blocks the asyncio simulator; every
failure in the parse/callback path is counted, never raised into the loop.

Run against the simulator:  python -m labsim.payload_sniffer
"""
from __future__ import annotations

import socket
import struct
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from .telemetry import HOST, PORT

# Little-endian, no padding. Each format char unpacks as follows:
#   "<"  little-endian byte order, standard sizes, no alignment padding
#   "I"  unsigned 32-bit int  (4 B) -> object ID
#   "f"  IEEE-754 float32     (4 B) -> X   (the three "f" = X, Y, Z;
#   "f"                                     Y is height in the lab's Y-up world)
#   "f"                                  -> Z
#   "H"  unsigned 16-bit int  (2 B) -> status / health
# Header = 4 + 4 + 4 + 4 + 2 = 18 bytes; everything after it is the UTF-8 name.
RECORD = struct.Struct("<IfffH")

LOOPBACK_ADDR = HOST  # 127.0.0.1 -- the only address this module will use


@dataclass(frozen=True)
class NodeRecord:
    id: int
    x: float
    y: float
    z: float
    status: int
    name: str


def parse_payload(data: bytes) -> Optional[NodeRecord]:
    """Decode one payload into a NodeRecord, or None if it is too short.

    Invalid UTF-8 in the name never fails the record: bad bytes become U+FFFD.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) < RECORD.size:
        return None
    id_, x, y, z, status = RECORD.unpack_from(data, 0)
    name = bytes(data[RECORD.size:]).decode("utf-8", errors="replace")
    return NodeRecord(id_, x, y, z, status, name)


def loopback_iface() -> str:
    """Loopback interface name for scapy (macOS 'lo0', Linux 'lo')."""
    if sys.platform == "darwin":
        return "lo0"
    if sys.platform.startswith("linux"):
        return "lo"
    raise RuntimeError("loopback capture is only wired for macOS/Linux")


class PayloadSniffer:
    """Background capture of lab datagrams; parsed records go to `on_record`."""

    def __init__(self, on_record: Callable[[NodeRecord], None], port: int = PORT,
                 backend: str = "scapy") -> None:
        if backend not in ("scapy", "socket"):
            raise ValueError(f"unknown backend {backend!r}")
        self.on_record, self.port, self.backend = on_record, port, backend
        self.captured = 0   # datagrams seen
        self.malformed = 0  # datagrams the parser rejected or the callback failed on
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._error: Optional[BaseException] = None

    # -- shared datagram path ------------------------------------------------
    def _handle(self, data: bytes) -> None:
        self.captured += 1
        try:
            rec = parse_payload(data)
            if rec is None:
                self.malformed += 1
            else:
                self.on_record(rec)
        except Exception:  # a bad record or callback must not kill the capture loop
            self.malformed += 1

    # -- lifecycle -----------------------------------------------------------
    def start(self, timeout: float = 3.0) -> "PayloadSniffer":
        target = self._run_scapy if self.backend == "scapy" else self._run_socket
        self._thread = threading.Thread(target=target, name="payload-sniffer", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise RuntimeError("sniffer did not start in time")
        if self._error is not None:
            raise RuntimeError(f"sniffer failed to start: {self._error}") from self._error
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def __enter__(self) -> "PayloadSniffer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- backends ------------------------------------------------------------
    def _run_socket(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind((LOOPBACK_ADDR, self.port))
            self._sock.settimeout(0.2)
        except OSError as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:  # socket closed by stop()
                break
            self._handle(data)

    def _run_scapy(self) -> None:
        try:
            from scapy.all import Raw, UDP, sniff  # type: ignore
            iface = loopback_iface()
        except (ImportError, RuntimeError) as exc:
            self._error = exc
            self._ready.set()
            return

        def on_packet(pkt) -> None:
            if UDP in pkt and Raw in pkt:
                self._handle(bytes(pkt[Raw].load))

        # Kernel-level filter keeps capture on loopback traffic for this port only.
        flt = f"udp and host {LOOPBACK_ADDR} and port {self.port}"
        self._ready.set()
        try:
            sniff(iface=iface, filter=flt, prn=on_packet, store=False,
                  stop_filter=lambda _p: self._stop.is_set())
        except Exception as exc:  # e.g. no privileges: surface via .error, don't raise in thread
            self._error = exc

    @property
    def error(self) -> Optional[BaseException]:
        return self._error


def main() -> None:  # pragma: no cover - manual lab run
    """Run labsim's simulator and print every decoded record via the sniffer."""
    import asyncio

    from .telemetry import simulator

    def show(r: NodeRecord) -> None:
        print(f"id={r.id:<3} x={r.x:8.2f} y={r.y:8.2f} z={r.z:8.2f} status={r.status:<3} {r.name}")

    async def go() -> None:
        stop = asyncio.Event()
        # The simulator sends to 127.0.0.1:PORT; the socket backend is the listener.
        with PayloadSniffer(show, backend="socket"):
            task = asyncio.create_task(simulator(stop))
            await asyncio.sleep(3)
            stop.set()
            await task

    asyncio.run(go())


if __name__ == "__main__":  # pragma: no cover
    main()
