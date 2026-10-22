"""Lab probe: capture local UDP/TCP packets on one port and decode a fixed-layout node.

Assumed payload layout (little-endian by default, see ENDIAN):

    offset size  field
    0      4     object id          int32
    4      4     x                  float32
    8      4     y                  float32
    12     4     z                  float32
    16     2     status / health    int16
    18     ...   name               UTF-8, rest of the payload

Usage:
    pip install scapy
    python tools/payload_probe.py --port 7777 --iface lo0          # capture
    python tools/payload_probe.py --simulate --port 7777           # lab traffic generator

Capture runs in a daemon thread (scapy's sniff() blocks); packets cross to the asyncio
side through a thread-safe handoff so parsing and printing never stall the sniffer.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import socket
import struct
import threading
from dataclasses import dataclass

# struct format: first char picks byte order ("<" little, ">" big, "!" network = big).
# "=" / "@" would add native alignment padding, so they are avoided on purpose.
# i = int32, f = float32, h = int16. Standard sizes (4/4/4/4/2) apply with "<", ">", "!".
ENDIAN = "<"
HEADER = struct.Struct(f"{ENDIAN}ifffh")  # 4 + 4 + 4 + 4 + 2 = 18 bytes
assert HEADER.size == 18


@dataclass(frozen=True, slots=True)
class Node:
    obj_id: int
    x: float
    y: float
    z: float
    status: int
    name: str


def parse_node(payload: bytes) -> Node:
    """Decode one node from a raw payload. Raises ValueError on malformed input."""
    if len(payload) < HEADER.size:
        raise ValueError(f"payload too short: {len(payload)} < {HEADER.size}")

    # unpack_from reads HEADER.size bytes at the given offset without slicing/copying.
    # It returns a tuple, one item per format code: (int, float, float, float, int).
    obj_id, x, y, z, status = HEADER.unpack_from(payload, 0)

    # Everything after the fixed header is the name. errors="replace" keeps the probe
    # alive on bytes that are not valid UTF-8 (which is itself a useful debug signal).
    # Strip trailing NULs in case the sender pads a C-style string.
    name = payload[HEADER.size:].decode("utf-8", errors="replace").rstrip("\x00")

    if not all(map(math.isfinite, (x, y, z))):
        raise ValueError("non-finite coordinate (wrong endianness or wrong layout?)")
    return Node(obj_id, x, y, z, status, name)


def build_node(node: Node) -> bytes:
    """Inverse of parse_node; used by the simulator and the self-test."""
    return HEADER.pack(node.obj_id, node.x, node.y, node.z, node.status) + node.name.encode("utf-8")


# ---------------------------------------------------------------- capture (thread)

def sniff_thread(port: int, iface: str | None, loop: asyncio.AbstractEventLoop,
                 queue: asyncio.Queue[tuple[str, bytes]], stop: threading.Event) -> None:
    from scapy.all import TCP, UDP, Raw, sniff  # lazy: parser works without scapy

    def on_packet(pkt) -> None:
        if Raw not in pkt:
            return  # SYN/ACK etc. carry no payload
        proto = "UDP" if UDP in pkt else "TCP" if TCP in pkt else "?"
        # call_soon_threadsafe is the only safe way to touch the loop from this thread.
        # put_nowait raises QueueFull when the consumer lags; swallow it = drop packet,
        # so a slow printer can never back-pressure the capture.
        loop.call_soon_threadsafe(_offer, queue, (proto, bytes(pkt[Raw].load)))

    sniff(
        iface=iface,
        filter=f"(udp or tcp) and port {port}",  # BPF, evaluated in the kernel
        prn=on_packet,
        store=False,  # do not accumulate packets in memory
        stop_filter=lambda _p: stop.is_set(),
    )


def _offer(queue: asyncio.Queue, item: tuple[str, bytes]) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass


# ---------------------------------------------------------------- consumer (asyncio)

async def consume(queue: asyncio.Queue[tuple[str, bytes]]) -> None:
    while True:
        proto, payload = await queue.get()
        try:
            n = parse_node(payload)
            print(f"[{proto}] id={n.obj_id} pos=({n.x:.2f}, {n.y:.2f}, {n.z:.2f}) "
                  f"status={n.status} name={n.name!r}")
        except ValueError as e:
            print(f"[{proto}] undecodable ({e}); head={payload[:24].hex(' ')}")


async def run_capture(port: int, iface: str | None) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue(maxsize=1024)
    stop = threading.Event()
    threading.Thread(target=sniff_thread, args=(port, iface, loop, queue, stop),
                     daemon=True).start()
    try:
        await consume(queue)
    finally:
        stop.set()


# ---------------------------------------------------------------- lab traffic generator

async def run_simulator(port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t = 0.0
    while True:
        node = Node(42, 1000 * math.cos(t), 1000 * math.sin(t), 25.0, 100 - int(t) % 100, "lab-node")
        sock.sendto(build_node(node), ("127.0.0.1", port))
        t += 0.1
        await asyncio.sleep(0.05)


def self_test() -> None:
    n = Node(7, 1.5, -2.25, 300.0, 87, "Ánh-01")
    assert parse_node(build_node(n)) == n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--iface", default=None, help="e.g. lo0 (macOS loopback), lo (Linux)")
    ap.add_argument("--simulate", action="store_true", help="send synthetic nodes to 127.0.0.1:PORT")
    args = ap.parse_args()
    self_test()
    try:
        asyncio.run(run_simulator(args.port) if args.simulate else run_capture(args.port, args.iface))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
