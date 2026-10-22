"""``python -m isle_livemap --diagnose``: passive capture diagnostics, plain text.

Capture-only: lists processes by name, reads the UDP owner table, and sniffs UDP
read-only through Npcap. No memory reads, no injection, nothing is sent.
Windows-only parts are lazily imported / guarded; the verdict logic is pure.
"""

from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import dataclass

from .capture import (
    GAME_PROCESS_NAME, TelemetryPipeline, datagram_from_scapy, is_windows, isle_like_names, list_processes,
    npcap_available, owned_udp_ports, pick_game_process,
)


@dataclass(slots=True)
class IfaceResult:
    name: str
    description: str = ""
    ip: str = ""
    udp_packets: int = 0
    udp_bytes: int = 0
    matched: int = 0  # packets on the game's owned ports
    decoded: int = 0  # packets/datagrams that produced a position
    error: str = ""


def is_admin() -> bool:
    if not is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def choose_verdict(
    npcap: bool, admin: bool, pid: int | None, ports: set[int], results: list[IfaceResult]
) -> tuple[str | None, str]:
    """Return (best_interface_name or None, verdict line). Pure."""
    if not npcap:
        return None, "Most likely cause: Npcap not found. Install it from https://npcap.com (WinPcap API-compatible mode)."
    if pid is None:
        return None, "Most likely cause: no game process found. Start the game, or check the exe name listed above."
    if not ports:
        return None, "Most likely cause: game process has no UDP socket yet. Join a server, then run --diagnose again."
    usable = [r for r in results if not r.error]
    if not usable:
        return None, "Most likely cause: no interface could be opened for capture" + (
            "; try running as administrator." if not admin else ".")
    decoded = [r for r in usable if r.decoded > 0]
    if decoded:
        best = max(decoded, key=lambda r: (r.decoded, r.matched))
        return best.name, f"Best interface: {best.name} (use --iface \"{best.name}\")"
    matched = [r for r in usable if r.matched > 0]
    if matched:
        best = max(matched, key=lambda r: r.matched)
        return best.name, (f"Packets seen on the game's ports on {best.name} but nothing decoded: "
                           "move around in game during the test, or the packet format may differ.")
    if any(r.udp_packets > 0 for r in usable):
        return None, ("UDP seen but none on the game's ports: the game may have re-bound its sockets, "
                      "or traffic uses another interface/VPN. Re-run while playing.")
    return None, "No UDP packets on any interface" + (
        ": not running as administrator, try an elevated terminal." if not admin
        else ": check Npcap install, firewall/VPN, and try --iface.")


def list_interfaces() -> list[IfaceResult]:
    from scapy.config import conf

    conf.use_pcap = True
    out: list[IfaceResult] = []
    try:
        ifaces = list(conf.ifaces.values())
    except Exception:  # noqa: BLE001
        from scapy.arch import get_if_list

        return [IfaceResult(n) for n in get_if_list()]
    for i in ifaces:
        out.append(IfaceResult(
            name=str(getattr(i, "name", i)), description=str(getattr(i, "description", "") or ""),
            ip=str(getattr(i, "ip", "") or ""),
        ))
    return out


def _sniff_all(results: list[IfaceResult], ports: set[int], seconds: float) -> None:
    from scapy.layers.inet import UDP
    from scapy.sendrecv import AsyncSniffer

    pipes = {r.name: TelemetryPipeline() for r in results}
    sniffers = []

    def make_cb(r: IfaceResult):
        pipe = pipes[r.name]

        def cb(pkt) -> None:
            try:
                if UDP not in pkt:
                    return
                r.udp_packets += 1
                r.udp_bytes += len(pkt)
                d = datagram_from_scapy(pkt, ports, time.monotonic())
                if d is not None:
                    r.matched += 1
                    pipe.handle(d)
                    r.decoded = pipe.counts()["decoded"]
            except Exception:  # noqa: BLE001
                pass

        return cb

    for r in results:
        try:
            s = AsyncSniffer(iface=r.name, filter="udp", prn=make_cb(r), store=False)
            s.start()
            sniffers.append((r, s))
        except Exception as e:  # noqa: BLE001
            r.error = f"{type(e).__name__}: {e}"
    time.sleep(seconds)
    for r, s in sniffers:
        try:
            s.stop()
        except Exception as e:  # noqa: BLE001
            if not r.error and "not running" not in str(e).lower():
                r.error = f"{type(e).__name__}: {e}"


def run_diagnose(iface: str | None = None, seconds: float = 10.0) -> int:
    print("Isle LiveMap diagnostics (passive; nothing is sent, no game memory is read)")
    print(f"Platform: {sys.platform}")
    npcap = npcap_available()
    admin = is_admin()
    print(f"Npcap found: {'yes' if npcap else 'NO'}")
    print(f"Running as administrator: {'yes' if admin else 'NO'}")
    procs = list_processes()
    found = pick_game_process(procs)
    pid, ports = None, set()
    if found:
        pid = found[0]
        note = "" if found[1].lower() == GAME_PROCESS_NAME.lower() + ".exe" else f" (fallback match; expected {GAME_PROCESS_NAME}.exe)"
        print(f"Game process: {found[1]}{note}  PID {pid}")
        ports = owned_udp_ports(pid)
        print(f"UDP ports owned by game: {sorted(ports) if ports else 'none'}")
    else:
        print(f"Game process: NOT FOUND (looked for {GAME_PROCESS_NAME}.exe and TheIsleClient*)")
        names = isle_like_names(procs)
        print("Running exes containing 'isle': " + (", ".join(names) if names else "none"))

    results: list[IfaceResult] = []
    if npcap:
        try:
            results = list_interfaces()
        except Exception as e:  # noqa: BLE001
            print(f"Could not list interfaces: {type(e).__name__}: {e}")
    print("Capture interfaces:")
    for r in results:
        print(f"  {r.name}  desc={r.description or '-'}  ip={r.ip or '-'}")
    if iface:
        results = [r for r in results if iface.lower() in (r.name.lower(), r.description.lower())] or [IfaceResult(iface)]
    if npcap and results and pid is not None and ports:
        print(f"Sniffing UDP on {len(results)} interface(s) for {seconds:g}s... (play / move in game now)")
        _sniff_all(results, ports, seconds)
        for r in results:
            line = f"  {r.name}: udp={r.udp_packets} game_ports={r.matched} decoded={r.decoded} bytes={r.udp_bytes}"
            print(line + (f"  ERROR {r.error}" if r.error else ""))
    best, verdict = choose_verdict(npcap, admin, pid, ports, results)
    print()
    print("VERDICT: " + verdict)
    return 0 if best else 1
