"""Demo / offline feeds so the UI works without the game.

* ``SyntheticSource``: a walking player. Movement packets are *encoded* in the
  game's wire layout and then decoded by the real decoder, so the whole
  pipeline is exercised. Vitals come from three real recorded inbound packets
  (static values; replayed on a loop).
* ``pcap_datagrams``: replays a pcap/pcapng capture of your own session.
"""

from __future__ import annotations

import base64
import json
import math
import struct
import threading
import time
from pathlib import Path
from typing import Iterator

from .capture import INBOUND, OUTBOUND, Datagram, TelemetryPipeline, classify_direction
from .geo import ASSET_DIR

SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT = "10.0.0.2", 7777, "192.168.0.10", 50_000


class BitWriter:
    """LSB-first bit packer (inverse of the decoder's reader)."""

    def __init__(self) -> None:
        self.value = 0
        self.nbits = 0

    def write(self, value: int, count: int) -> None:
        self.value |= (value & ((1 << count) - 1)) << self.nbits
        self.nbits += count

    def to_bytes(self) -> bytes:
        return self.value.to_bytes((self.nbits + 7) // 8, "little")


def _needed(v: int) -> int:
    return 1 + (~v if v < 0 else v).bit_length()


def encode_move_payload(x: float, y: float, z: float, unreal_yaw_deg: float, client_ts: float,
                        accel: tuple[int, int, int] | None = None) -> bytes:
    """Build a client->server move payload the decoder understands:
    pad | f32 timestamp | acceleration | FVector_NetQuantize100 location | rotation.
    ``accel``: the acceleration as raw FVector_NetQuantize10 integers (cm/s² × 10); None: zero."""
    raw = [round(v * 100) for v in (x, y, z)]
    comp = max(_needed(r) for r in raw)
    if comp < 23 or comp > 31:
        raise ValueError(f"location needs {comp} bits; decoder accepts 23..31 (use a larger |z|)")
    w = BitWriter()
    w.write(0, 40)
    w.write(struct.unpack("<I", struct.pack("<f", client_ts))[0], 32)
    if accel is None:
        w.write(1 | 64, 7)  # acceleration vector: 1-bit components (all zero)
        w.write(0, 3)
    else:
        bits = max(_needed(a) for a in accel)
        w.write(bits | 64, 7)
        for a in accel:
            w.write(a, bits)
    w.write(comp | 64, 7)
    for r in raw:
        w.write(r, comp)
    w.write(0, 1)  # pitch absent
    w.write(1, 1)
    w.write(int((unreal_yaw_deg % 360.0) / 360.0 * 65536) & 0xFFFF, 16)
    w.write(0, 1)  # roll absent
    return w.to_bytes()


# A loop through the middle of the island, world units (cm).
DEMO_ROUTE = [
    (51_000, -49_000), (120_000, -90_000), (180_000, -20_000), (150_000, 60_000),
    (60_000, 90_000), (-20_000, 40_000), (-40_000, -40_000), (10_000, -90_000),
]


class SyntheticSource:
    """Walks DEMO_ROUTE forever at ``speed`` units/s, 30 packets/s."""

    def __init__(self, speed: float = 2_500.0, rate_hz: float = 30.0, z: float = 30_000.0) -> None:
        self.speed, self.rate, self.z = speed, rate_hz, z
        pts = DEMO_ROUTE + [DEMO_ROUTE[0]]
        self.segs = [(a, b, math.dist(a, b)) for a, b in zip(pts, pts[1:])]
        self.total = sum(s[2] for s in self.segs)

    def pose(self, t: float) -> tuple[float, float, float]:
        """(x, y, unreal_yaw) at time t seconds."""
        d = (t * self.speed) % self.total
        for a, b, length in self.segs:
            if d <= length:
                f = d / length
                dx, dy = b[0] - a[0], b[1] - a[1]
                map_heading = math.degrees(math.atan2(dx, -dy)) % 360.0
                return a[0] + dx * f, a[1] + dy * f, (map_heading - 90.0) % 360.0
            d -= length
        return pts_last(self)

    def datagrams(self, duration_s: float, t0: float = 0.0) -> Iterator[Datagram]:
        """Deterministic stream with simulated timestamps (no sleeping)."""
        vit = load_demo_vitals()
        n = int(duration_s * self.rate)
        next_vitals = 0.0
        vit_idx = 0
        for i in range(n):
            t = i / self.rate
            x, y, yaw = self.pose(t)
            payload = encode_move_payload(x, y, self.z + 400 * math.sin(t / 3), yaw, 40.0 + t)
            yield Datagram(t0 + t, CLIENT_IP, CLIENT_PORT, SERVER_IP, SERVER_PORT, payload, OUTBOUND)
            if t >= next_vitals:
                # first, second, periodic, then keep-alive heartbeats
                pkt = vit[min(vit_idx, 2)] if vit_idx < 3 else vit[1]
                yield Datagram(t0 + t + 0.001, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT, pkt, INBOUND)
                vit_idx += 1
                next_vitals = t + 1.1


def pts_last(src: SyntheticSource) -> tuple[float, float, float]:
    a = src.segs[-1][1]
    return a[0], a[1], 0.0


def load_demo_vitals() -> list[bytes]:
    data = json.loads((ASSET_DIR / "demo_vitals.json").read_text())
    return [base64.b64decode(data[k]) for k in ("FirstHeartbeat", "SecondHeartbeat", "PeriodicAttributes")]


def pcap_datagrams(path: Path, local_port: int | None = None) -> Iterator[Datagram]:
    """Yield UDP datagrams from a capture file, with their capture times.

    ``local_port`` is the game's local UDP port; if omitted, the port on the
    private-address side of the busiest UDP flow is used."""
    import ipaddress

    from scapy.layers.inet import IP, UDP
    from scapy.utils import PcapReader

    def udp_packets():
        # streamed: a 200 MB recording must not be held in memory as scapy packet objects
        with PcapReader(str(path)) as reader:
            for p in reader:
                if UDP in p and IP in p and len(bytes(p[UDP].payload)) > 0:
                    yield p

    t0 = None
    if local_port is None:
        counts: dict[int, int] = {}
        for p in udp_packets():
            if t0 is None:
                t0 = float(p.time)
            for ip, port in ((p[IP].src, p[UDP].sport), (p[IP].dst, p[UDP].dport)):
                if ipaddress.ip_address(ip).is_private:
                    counts[port] = counts.get(port, 0) + len(bytes(p[UDP].payload))
        if not counts:
            if t0 is None:
                return
            raise ValueError("could not infer the local game port; pass --local-port")
        local_port = max(counts, key=counts.get)
    for p in udp_packets():
        if t0 is None:
            t0 = float(p.time)
        direction = classify_direction(int(p[UDP].sport), int(p[UDP].dport), {local_port})
        if direction is None:
            continue
        yield Datagram(
            float(p.time) - t0, p[IP].src, int(p[UDP].sport), p[IP].dst, int(p[UDP].dport),
            bytes(p[UDP].payload), direction,
        )


class ReplayFeeder:
    """Pushes datagrams into a pipeline at (scaled) real time on a thread."""

    def __init__(self, pipeline: TelemetryPipeline, source: Iterator[Datagram], speed: float = 1.0, loop=None) -> None:
        self.pipeline, self.speed, self._loop = pipeline, speed, loop
        self._source = source
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="isle-replay")

    def start(self) -> None:
        self.pipeline.set_status("replay")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        start = time.monotonic()
        while not self._stop.is_set():
            base = None
            for d in self._source:
                if self._stop.is_set():
                    return
                if base is None:
                    base = d.observed_at
                due = start + (d.observed_at - base) / self.speed
                delay = due - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                # re-stamp onto the live monotonic clock the trackers compare
                self.pipeline.handle(
                    Datagram(time.monotonic(), d.src_ip, d.src_port, d.dst_ip, d.dst_port, d.payload, d.direction)
                )
            if self._loop is None:
                return
            self._source = self._loop()
            start = time.monotonic()
