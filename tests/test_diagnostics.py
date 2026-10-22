from isle_livemap.capture import (
    TelemetryPipeline, isle_like_names, pick_game_process, status_text,
)
from isle_livemap.diagnose import IfaceResult, choose_verdict
from isle_livemap.geo import Settings


def test_pick_exact_preferred_over_prefix():
    procs = [(1, "TheIsleClient-Win64-Shipping-Test.exe"), (2, "thEISLEclient-win64-shipping.EXE")]
    assert pick_game_process(procs) == (2, "thEISLEclient-win64-shipping.EXE")


def test_pick_prefix_fallback_and_none():
    assert pick_game_process([(5, "TheIsleClient-Renamed.exe")]) == (5, "TheIsleClient-Renamed.exe")
    assert pick_game_process([(1, "notepad.exe"), (2, "Isle.exe")]) is None


def test_isle_like_names():
    procs = [(1, "notepad.exe"), (2, "Isle.exe"), (3, "TheISLEx.exe"), (4, "Isle.exe")]
    assert isle_like_names(procs) == ["Isle.exe", "TheISLEx.exe"]


def test_status_text_stages():
    assert status_text("waiting for game", {}, False) == "waiting for game"
    assert status_text("game running, no UDP socket yet", {}, False).startswith("game running")
    assert "0 packets" in status_text("capturing", {"udp_total": 0}, False)
    assert "none on the game" in status_text("capturing", {"udp_total": 9}, False)
    assert "no position decoded" in status_text("capturing", {"udp_total": 9, "outbound": 3}, False)
    assert status_text("capturing", {"udp_total": 9, "outbound": 3}, True) == "tracking"


def test_pipeline_counters_and_status():
    p = TelemetryPipeline()
    p.set_status("capturing")
    assert "0 packets" in p.status_text()
    p.note_udp(3)
    assert p.counts()["udp_total"] == 3
    assert "none on the game" in p.status_text()


def _r(name, udp=0, matched=0, decoded=0, error=""):
    return IfaceResult(name, udp_packets=udp, matched=matched, decoded=decoded, error=error)


def test_verdict_precedence():
    ok = [_r("a", 5, 2, 1)]
    assert "Npcap" in choose_verdict(False, True, 1, {1}, ok)[1]
    assert "no game process" in choose_verdict(True, True, None, set(), ok)[1]
    assert "no UDP socket" in choose_verdict(True, True, 1, set(), ok)[1]
    assert "administrator" in choose_verdict(True, False, 1, {1}, [_r("a")])[1]
    assert choose_verdict(True, True, 1, {1}, [_r("a")])[0] is None
    assert "none on the game's ports" in choose_verdict(True, True, 1, {1}, [_r("a", 4)])[1]
    best, msg = choose_verdict(True, True, 1, {1}, [_r("a", 4, 2, 0)])
    assert best == "a" and "nothing decoded" in msg


def test_verdict_best_interface():
    best, msg = choose_verdict(True, True, 1, {1}, [_r("a", 9, 5, 0), _r("b", 9, 5, 3), _r("c", 50, 0, 0)])
    assert best == "b" and msg.startswith("Best interface: b")


def test_settings_iface_roundtrip(tmp_path):
    s = Settings(iface="Ethernet")
    s.save(tmp_path / "s.json")
    assert Settings.load(tmp_path / "s.json").iface == "Ethernet"
