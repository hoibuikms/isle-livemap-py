"""Lab telemetry: simulator + asyncio UDP receiver + live nearest-entities table.

Self-contained: the simulator is the only sender, the receiver binds to loopback.
Wire format (little-endian) per datagram:
    i  int32   node id          (4 B)
    f  float32 x                (4 B)
    f  float32 y (height)       (4 B)
    f  float32 z                (4 B)
    h  int16   health / status  (2 B)
    -  UTF-8 name, the remaining bytes
Node id PLAYER_ID is the local player ("me"); every other id is an entity.
"""
import asyncio
import math
import random
import time
from dataclasses import dataclass

import struct

HOST, PORT = "127.0.0.1", 7777
PLAYER_ID = 0
NODE_TTL = 5.0

# "<" = little-endian, no padding. unpack_from returns (id, x, y, z, health).
HEADER = struct.Struct("<i3fh")


@dataclass
class Node:
    id: int
    x: float
    y: float
    z: float
    health: int
    name: str


def encode(n: Node) -> bytes:
    return HEADER.pack(n.id, n.x, n.y, n.z, n.health) + n.name.encode("utf-8")


def decode(data: bytes) -> Node:
    if len(data) < HEADER.size:
        raise ValueError(f"payload {len(data)}B shorter than header {HEADER.size}B")
    id_, x, y, z, health = HEADER.unpack_from(data, 0)
    # errors="replace": a malformed name must not drop the whole record.
    name = data[HEADER.size:].decode("utf-8", errors="replace")
    return Node(id_, x, y, z, health, name)


def nearby(me: Node, nodes, max_distance: float = 100.0):
    """[(distance, delta_y, node)] with distance < max_distance, nearest first.

    distance = sqrt(dx^2 + dy^2 + dz^2); delta_y = node.y - me.y (>0 means higher).
    """
    rows = []
    for n in nodes:
        dx, dy, dz = n.x - me.x, n.y - me.y, n.z - me.z
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if d < max_distance:
            rows.append((d, dy, n))
    rows.sort(key=lambda r: r[0])
    return rows


def render(rows) -> str:
    head = ["Name", "Health", "Distance (m)", "Delta Y (m)"]
    body = [[n.name, str(n.health), f"{d:.1f}",
             f"{dy:+.1f} ({'higher' if dy > 0 else 'lower' if dy < 0 else 'level'})"]
            for d, dy, n in rows]
    widths = [max(len(r[i]) for r in [head, *body]) for i in range(len(head))]
    line = lambda r: " | ".join(c.ljust(w) for c, w in zip(r, widths))
    return "\n".join([line(head), "-+-".join("-" * w for w in widths), *map(line, body)])


class Store:
    """Latest node per id, with expiry. Written by the receiver, read by the UI."""

    def __init__(self, ttl: float = NODE_TTL, clock=time.monotonic) -> None:
        self.ttl, self.clock = ttl, clock
        self.me: Node | None = None
        self._seen: dict[int, tuple[Node, float]] = {}

    def update(self, n: Node) -> None:
        if n.id == PLAYER_ID:
            self.me = n
        else:
            self._seen[n.id] = (n, self.clock())

    def entities(self) -> list[Node]:
        cutoff = self.clock() - self.ttl
        self._seen = {i: v for i, v in self._seen.items() if v[1] >= cutoff}
        return [v[0] for v in self._seen.values()]


class Receiver(asyncio.DatagramProtocol):
    def __init__(self, store: Store) -> None:
        self.store = store
        self.errors = 0

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self.store.update(decode(data))
        except ValueError:
            self.errors += 1


async def simulator(stop: asyncio.Event, host=HOST, port=PORT, count=8, hz=5.0) -> None:
    rng = random.Random(1)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, remote_addr=(host, port))
    ents = [Node(i, rng.uniform(0, 200), rng.uniform(0, 100), rng.uniform(100, 300),
                 rng.randint(10, 100), f"Node_{i}") for i in range(1, count + 1)]
    me = Node(PLAYER_ID, 100.0, 50.0, 200.0, 100, "me")
    try:
        while not stop.is_set():
            for n in (me, *ents):
                n.x += rng.uniform(-2, 2); n.y += rng.uniform(-0.5, 0.5); n.z += rng.uniform(-2, 2)
                transport.sendto(encode(n))
            await asyncio.sleep(1 / hz)
    finally:
        transport.close()


async def ui(store: Store, stop: asyncio.Event, max_distance=100.0, interval=1.0, out=print) -> None:
    while not stop.is_set():
        if store.me is not None:
            out("\033[2J\033[H" + render(nearby(store.me, store.entities(), max_distance)))
        await asyncio.sleep(interval)


async def run(duration: float | None = None, port=PORT, max_distance=100.0) -> Store:
    store, stop = Store(), asyncio.Event()
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: Receiver(store), local_addr=(HOST, port))
    tasks = [asyncio.create_task(simulator(stop, port=port)),
             asyncio.create_task(ui(store, stop, max_distance))]
    try:
        await asyncio.sleep(duration) if duration else await asyncio.Event().wait()
    finally:
        stop.set()
        await asyncio.gather(*tasks)
        transport.close()
    return store


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
