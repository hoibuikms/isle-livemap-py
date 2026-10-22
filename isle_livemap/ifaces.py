"""Capture-interface selection, status derivation and capture logging.

Pure logic (testable on any OS) plus thin adapters over scapy / sockets. Passive
only: the outbound-interface probe uses a UDP socket ``connect`` (which sends no
packet) and reads the local address the OS would route from.
"""

from __future__ import annotations

import logging
import os
import socket
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_SKIP_WORDS = ("loopback", "wan miniport", "bluetooth", "isatap", "teredo", "pseudo-interface")


@dataclass(frozen=True, slots=True)
class IfaceInfo:
    name: str
    description: str = ""
    ips: tuple[str, ...] = field(default_factory=tuple)


def has_real_ip(info: IfaceInfo) -> bool:
    return any(ip and ip != "0.0.0.0" and not ip.startswith(("127.", "169.254.")) for ip in info.ips)


def is_usable(info: IfaceInfo) -> bool:
    """False for loopback, miniport/bluetooth pseudo adapters and adapters with no routable IPv4."""
    text = f"{info.name} {info.description}".lower()
    if any(w in text for w in _SKIP_WORDS):
        return False
    return has_real_ip(info)


def match_outbound(infos: Iterable[IfaceInfo], outbound_ip: str | None) -> IfaceInfo | None:
    if not outbound_ip:
        return None
    for i in infos:
        if outbound_ip in i.ips:
            return i
    return None


def select_interfaces(
    infos: list[IfaceInfo], outbound_ip: str | None, explicit: str | None = None
) -> tuple[list[str], list[str], str]:
    """Return (primary, all_usable, reason). ``primary`` is the interface to try
    first (explicit name, else the one owning the outbound IP; empty if unknown)."""
    all_usable = [i.name for i in infos if is_usable(i)]
    if explicit:
        low = explicit.lower()
        for i in infos:
            if low in (i.name.lower(), i.description.lower()):
                return [i.name], all_usable, f"--iface {explicit}"
        return [explicit], all_usable, f"--iface {explicit} (not in interface list)"
    hit = match_outbound(infos, outbound_ip)
    if hit is not None:
        return [hit.name], all_usable, f"outbound interface {hit.name} ({outbound_ip})"
    return [], all_usable, f"outbound interface unknown (ip={outbound_ip}); using all usable interfaces"


def derive_capture_status(opened: list[str], errors: dict[str, str]) -> str:
    """Supervisor status after trying to open interfaces."""
    if opened:
        return "capturing"
    if errors:
        first = next(iter(errors.values()))
        return f"capture failed: {first}"[:160]
    return "capture failed: no usable capture interface found"


def should_fallback_to_all(udp_total: int, seconds_since_open: float, already_all: bool, timeout: float = 5.0) -> bool:
    return not already_all and udp_total == 0 and seconds_since_open >= timeout


# ----------------------------------------------------------------- adapters


def outbound_ip() -> str | None:
    """Local IPv4 the OS routes to the internet from. UDP connect sends nothing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(1.0)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def collect_interfaces() -> list[IfaceInfo]:
    from scapy.config import conf

    out: list[IfaceInfo] = []
    for i in list(conf.ifaces.values()):
        ips: list[str] = []
        raw_ips = getattr(i, "ips", None)
        try:
            for v4 in (raw_ips.get(4, []) if hasattr(raw_ips, "get") else []):
                ips.append(str(v4))
        except Exception:  # noqa: BLE001
            pass
        single = str(getattr(i, "ip", "") or "")
        if single and single not in ips:
            ips.append(single)
        out.append(IfaceInfo(str(getattr(i, "name", i)), str(getattr(i, "description", "") or ""), tuple(ips)))
    return out


def log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "IsleLiveMap" / "capture.log"


_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    lg = logging.getLogger("isle_livemap.capture")
    lg.setLevel(logging.INFO)
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(p, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(h)
    except OSError:
        lg.addHandler(logging.NullHandler())
    _logger = lg
    return lg
