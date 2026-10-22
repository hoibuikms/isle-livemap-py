"""Automated network diagnostic tool (Windows).

Flow:
  1. Find the target process by name -> PID -> its UDP sockets (local/remote port).
  2. Feed the discovered port into a scapy BPF filter and sniff in a background thread.
  3. Decode payloads with struct, compute 3D Euclidean distance to a root node,
     and render a live table sorted nearest -> farthest.

Requirements:  pip install psutil scapy tabulate   (+ Npcap installed)
Run from an elevated (Administrator) terminal.

PLACEHOLDER PAYLOAD LAYOUT (little-endian), repeated until the payload ends:
    Value_1  int32   node id
    Value_2  float32 x
    Value_3  float32 y
    Value_4  float32 z
Replace RECORD_FMT / decode_payload() with the real layout when known.
"""

import ctypes
import math
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

try:
    import psutil
    from scapy.all import UDP, Raw, sniff  # type: ignore
    from tabulate import tabulate
except ImportError as exc:  # pragma: no cover
    sys.exit(f"[!] Missing dependency: {exc.name}. Run: pip install psutil scapy tabulate")

# ----------------------------- configuration -----------------------------
PROCESS_NAME = "TheIsle-Win64-Shipping.exe"
RECORD_FMT = struct.Struct("<i3f")       # Value_1 (int), Value_2..4 (float)
ROOT_NODE_ID: Optional[int] = None       # None -> first record seen becomes the root
RESCAN_INTERVAL = 5.0                    # seconds between process/port rescans while waiting
REFRESH_INTERVAL = 1.0                   # table refresh period
NODE_TTL = 5.0                           # drop nodes not updated for this long
# -------------------------------------------------------------------------


@dataclass
class Node:
    node_id: int
    x: float
    y: float
    z: float
    seen: float


class NodeStore:
    """Thread-safe latest-position store shared by sniffer and renderer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nodes: Dict[int, Node] = {}
        self._root_id: Optional[int] = ROOT_NODE_ID
        self.packets = 0
        self.bad_packets = 0

    def update(self, records: List[tuple]) -> None:
        now = time.time()
        with self._lock:
            self.packets += 1
            for node_id, x, y, z in records:
                if not all(math.isfinite(v) for v in (x, y, z)):
                    continue
                if self._root_id is None:
                    self._root_id = node_id
                self._nodes[node_id] = Node(node_id, x, y, z, now)

    def mark_bad(self) -> None:
        with self._lock:
            self.bad_packets += 1

    def snapshot(self):
        now = time.time()
        with self._lock:
            for nid in [n for n, v in self._nodes.items() if now - v.seen > NODE_TTL]:
                del self._nodes[nid]
            return self._root_id, dict(self._nodes), self.packets, self.bad_packets


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


# ------------------------- 1. PID / port discovery -------------------------
def find_pid(name: str) -> Optional[int]:
    target = name.lower()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info["name"] or "").lower() == target:
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def get_udp_endpoints(pid: int):
    """Return (list of connection dicts, set of local ports) for the PID."""
    proc = psutil.Process(pid)
    # `connections` is deprecated since psutil 6.0 in favour of `net_connections`.
    getter = getattr(proc, "net_connections", None) or proc.connections
    conns = getter(kind="udp")
    rows, ports = [], set()
    for c in conns:
        local = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "-"
        remote = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "-"
        rows.append({"local": local, "remote": remote, "status": c.status or "NONE"})
        if c.laddr:
            ports.add(c.laddr.port)
        if c.raddr:
            ports.add(c.raddr.port)
    return rows, ports


def discover_ports() -> Optional[Set[int]]:
    """Locate the process and its UDP ports; print debug info. None if not ready yet."""
    try:
        pid = find_pid(PROCESS_NAME)
        if pid is None:
            print(f"[~] '{PROCESS_NAME}' not running. Waiting...")
            return None
        rows, ports = get_udp_endpoints(pid)
    except psutil.AccessDenied:
        print("[!] Access denied reading connections. Run as Administrator.")
        return None
    except psutil.NoSuchProcess:
        print("[~] Process exited during scan. Retrying...")
        return None
    except Exception as exc:
        print(f"[!] Port discovery failed: {exc!r}")
        return None

    print(f"[+] PID {pid}: {len(rows)} UDP socket(s)")
    if rows:
        print(tabulate(rows, headers="keys", tablefmt="simple"))
    if not ports:
        print("[~] No active UDP sockets yet. Retrying...")
        return None
    # Note: UDP sockets are usually unconnected, so remote is often '-';
    # the local port is then the one that matters for the capture filter.
    return ports


# ------------------------------ 2. sniff & parse ------------------------------
def decode_payload(payload: bytes) -> List[tuple]:
    """Decode consecutive (int, float, float, float) records; ignore a partial tail."""
    size = RECORD_FMT.size
    count = len(payload) // size
    if count == 0:
        raise struct.error(f"payload too short ({len(payload)} bytes)")
    return [RECORD_FMT.unpack_from(payload, i * size) for i in range(count)]


class SnifferThread(threading.Thread):
    def __init__(self, ports: Set[int], store: NodeStore) -> None:
        super().__init__(daemon=True, name="sniffer")
        self.bpf = "udp and (" + " or ".join(f"port {p}" for p in sorted(ports)) + ")"
        self.store = store
        self.stop_event = threading.Event()
        self.error: Optional[BaseException] = None

    def _on_packet(self, pkt) -> None:
        try:
            if UDP in pkt and Raw in pkt:
                self.store.update(decode_payload(bytes(pkt[Raw].load)))
        except struct.error:
            self.store.mark_bad()
        except Exception:
            self.store.mark_bad()

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                # Short timeout so stop_event is honoured promptly.
                sniff(filter=self.bpf, prn=self._on_packet, store=False, timeout=1)
        except Exception as exc:  # e.g. Npcap missing, no permission
            self.error = exc

    def stop(self) -> None:
        self.stop_event.set()


# ------------------------------ 3. distance & display ------------------------------
def distance3d(a: Node, b: Node) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def render(store: NodeStore, bpf: str) -> None:
    root_id, nodes, packets, bad = store.snapshot()
    os.system("cls" if os.name == "nt" else "clear")
    print(f"Filter: {bpf} | packets: {packets} | undecodable: {bad}")
    root = nodes.get(root_id) if root_id is not None else None
    if root is None:
        print("Waiting for data...")
        return
    print(f"Root node: {root.node_id} @ ({root.x:.1f}, {root.y:.1f}, {root.z:.1f})\n")
    rows = sorted(
        ((n, distance3d(root, n)) for n in nodes.values() if n.node_id != root.node_id),
        key=lambda t: t[1],
    )
    table = [
        [i, n.node_id, f"{n.x:.1f}", f"{n.y:.1f}", f"{n.z:.1f}", f"{d:.2f}", f"{time.time() - n.seen:.1f}s"]
        for i, (n, d) in enumerate(rows, 1)
    ]
    print(tabulate(table, headers=["#", "Node", "X", "Y", "Z", "Distance", "Age"], tablefmt="github"))


def main() -> int:
    if os.name != "nt":
        print("[!] This tool targets Windows (Npcap); behaviour elsewhere is untested.")
    elif not is_admin():
        print("[!] Not running as Administrator: connection lookup and capture may fail.")

    store = NodeStore()
    sniffer: Optional[SnifferThread] = None
    try:
        while True:
            # (Re)acquire ports whenever there is no healthy sniffer.
            if sniffer is None or not sniffer.is_alive():
                if sniffer is not None and sniffer.error:
                    print(f"[!] Sniffer stopped: {sniffer.error!r}")
                    return 1
                ports = discover_ports()
                if ports is None:
                    time.sleep(RESCAN_INTERVAL)
                    continue
                sniffer = SnifferThread(ports, store)
                sniffer.start()
                print(f"[+] Sniffing with filter: {sniffer.bpf}")
                time.sleep(1)
                continue
            render(store, sniffer.bpf)
            time.sleep(REFRESH_INTERVAL)
    except KeyboardInterrupt:
        print("\n[*] Stopping...")
    finally:
        if sniffer is not None:
            sniffer.stop()
            sniffer.join(timeout=3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
