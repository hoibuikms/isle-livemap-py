from isle_livemap.ifaces import (
    IfaceInfo, derive_capture_status, is_usable, select_interfaces, should_fallback_to_all,
)
from isle_livemap.capture import TelemetryPipeline

INFOS = [
    IfaceInfo("Loopback", "Npcap Loopback Adapter", ("127.0.0.1",)),
    IfaceInfo("Ethernet", "Intel I219", ("192.168.1.5",)),
    IfaceInfo("VPN", "WireGuard Tunnel", ("10.8.0.2",)),
    IfaceInfo("Bluetooth", "Bluetooth Device", ("192.168.9.9",)),
    IfaceInfo("Down", "Realtek", ("169.254.3.4",)),
    IfaceInfo("NoIP", "Ghost", ()),
]


def test_usable_filter():
    assert [i.name for i in INFOS if is_usable(i)] == ["Ethernet", "VPN"]


def test_outbound_match():
    primary, allu, _ = select_interfaces(INFOS, "10.8.0.2")
    assert primary == ["VPN"] and allu == ["Ethernet", "VPN"]


def test_outbound_unknown_falls_to_all():
    primary, allu, reason = select_interfaces(INFOS, None)
    assert primary == [] and allu == ["Ethernet", "VPN"] and "unknown" in reason
    assert select_interfaces(INFOS, "1.2.3.4")[0] == []


def test_explicit_iface_wins_by_name_or_description():
    assert select_interfaces(INFOS, "10.8.0.2", "ethernet")[0] == ["Ethernet"]
    assert select_interfaces(INFOS, None, "Intel I219")[0] == ["Ethernet"]
    assert select_interfaces(INFOS, None, "Nope")[0] == ["Nope"]


def test_status_derivation():
    assert derive_capture_status(["Ethernet"], {"VPN": "x"}) == "capturing"
    assert derive_capture_status([], {"Ethernet": "OSError: denied"}) == "capture failed: OSError: denied"
    assert derive_capture_status([], {}).startswith("capture failed")


def test_failure_status_passes_through_pipeline():
    p = TelemetryPipeline()
    p.set_status(derive_capture_status([], {"E": "PermissionError: x"}))
    assert p.status_text() == "capture failed: PermissionError: x"


def test_fallback_rule():
    assert should_fallback_to_all(0, 5.1, False)
    assert not should_fallback_to_all(0, 4.0, False)
    assert not should_fallback_to_all(3, 9.0, False)
    assert not should_fallback_to_all(0, 9.0, True)
