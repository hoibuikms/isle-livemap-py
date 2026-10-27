"""The "nearby:" report and the user's "creatures are next to me" mark: what the stream holds around the player, so that a
log can tell "nobody is near" from "somebody is near and the map cannot place them". Synthetic data only."""

from __future__ import annotations

import pytest

from isle_livemap.peers import GamePeerSource

ME = (100_000.0, -250_000.0, 30_000.0)  # world cm


def make_source() -> GamePeerSource:
    g = GamePeerSource()
    g._own_position = lambda: ME  # noqa: SLF001
    return g


def at(metres: float, north: float = 0.0):
    return (ME[0] + metres * 100.0, ME[1] + north * 100.0, 0.0)


def test_report_counts_creatures_per_band_and_names_the_nearest():
    g = make_source()
    g._emit(5001, *at(120.0), "dino", "Allosaurus", "Actor #1")  # noqa: SLF001
    g._emit(5002, *at(400.0), "player", "Carnotaurus", "P1")  # noqa: SLF001
    g._emit(5003, *at(900.0), "dino", "Triceratops", "Actor #3")  # noqa: SLF001
    g._emit(5004, *at(2_000.0), "dino", "Stegosaurus", "Actor #4")  # noqa: SLF001
    line = g.nearby_report()
    assert line.startswith("nearby: own (1000, -2500) m")
    assert "<=250m 1" in line and "<=500m 2" in line and "<=1000m 3" in line
    assert "nearest 120m dino Allosaurus" in line


def test_report_counts_a_spawn_only_position_apart_and_marks_the_nearest():
    g = make_source()
    g._emit(5001, *at(80.0), "dino", "Allosaurus", "Actor #1", "spawn")  # noqa: SLF001
    g._emit(5002, *at(300.0), "dino", "Allosaurus", "Actor #2")  # noqa: SLF001
    line = g.nearby_report()
    assert "<=250m 1 (1 spawn-only)" in line and "<=500m 2 (1 spawn-only)" in line
    assert "nearest 80m dino Allosaurus (spawn-only)" in line


def test_report_keeps_server_animals_out_of_the_creature_counts():
    g = make_source()
    g._emit(5001, *at(50.0), "ai", "Longear", "Actor #1")  # noqa: SLF001
    g._emit(5002, *at(60.0), "ai", "Longear", "Actor #2")  # noqa: SLF001
    line = g.nearby_report()
    assert "<=250m 0" in line and "no creature placed" in line and "server animals<=250m 2" in line


def test_report_distance_is_flat_not_3d():
    g = make_source()
    g._emit(5001, ME[0] + 10_000.0, ME[1], ME[2] + 90_000.0, "dino", "Allosaurus", "Actor #1")  # noqa: SLF001 - 100 m east, 900 m up
    assert "nearest 100m" in g.nearby_report()


def test_report_without_an_own_position_still_says_what_it_holds():
    g = make_source()
    g._own_position = lambda: None  # noqa: SLF001
    g._emit(5001, *at(50.0), "dino", "Allosaurus", "Actor #1")  # noqa: SLF001
    line = g.nearby_report()
    assert line.startswith("nearby: own position unknown") and "creatures placed=1" in line


def test_mark_moment_puts_the_report_into_the_session_log_events_and_returns_it():
    g = make_source()
    g._emit(5001, *at(120.0), "dino", "Allosaurus", "Actor #1")  # noqa: SLF001
    report = g.mark_moment()
    events = g.drain_events()
    assert len(events) == 1 and events[0].startswith("user mark (creatures are next to the player now): own (")
    assert "nearest 120m dino Allosaurus" in events[0] and report.startswith("nearby: ")
    assert g.drain_events() == []


def test_session_log_writes_the_nearby_line_every_interval(tmp_path):
    from isle_livemap.sessionlog import LABELS_INTERVAL_S, SessionLog

    class Pipe:
        server = None

        def add_observer(self, *a): ...
        def remove_observer(self, *a): ...
        def counts(self): return {}

    now = [0.0]
    g = make_source()
    log = SessionLog(tmp_path / "s.log", Pipe(), peers=g, clock=lambda: now[0])
    assert log.start()
    log._stop.set()  # drive tick() by hand

    def lines():
        return [ln for ln in (tmp_path / "s.log").read_text(encoding="utf-8").splitlines() if " nearby: " in ln]

    def tick(t):
        now[0] = t
        log.tick()

    tick(100.0)
    assert lines() == []  # nothing replicated yet, no own pawn: nothing to say
    g._emit(5001, *at(120.0), "dino", "Allosaurus", "Actor #1")  # noqa: SLF001
    tick(101.0)
    assert len(lines()) == 1 and "nearest 120m" in lines()[0]
    tick(101.0 + LABELS_INTERVAL_S - 1)
    assert len(lines()) == 1
    tick(101.0 + LABELS_INTERVAL_S + 1)
    assert len(lines()) == 2
    g.mark_moment()
    tick(101.0 + LABELS_INTERVAL_S + 2)
    text = (tmp_path / "s.log").read_text(encoding="utf-8")
    assert "user mark (creatures are next to the player now)" in text
    log.close()


def test_the_nearby_panel_mark_button_writes_the_mark(tmp_path):
    from isle_livemap.app import NearbyPanel
    from tests.test_peers_ui import make_view

    view = make_view(tmp_path)
    g = make_source()
    g._emit(5001, *at(120.0), "dino", "Allosaurus", "Actor #1")  # noqa: SLF001
    view.peers = g
    panel = NearbyPanel(view)
    panel.mark_btn.click()
    assert panel.status.text().startswith("nearby: ") and any("user mark" in e for e in g.drain_events())
    view.peers = None
    panel.mark_btn.click()
    assert panel.status.text() == "no nearby-players source"


@pytest.mark.parametrize("limit", GamePeerSource.NEARBY_BANDS_M)
def test_the_bands_are_the_minimap_tiers(limit):
    from isle_livemap.geo import priority_tier

    tiers = {250.0: 0, 500.0: 1, 1000.0: 2}
    assert priority_tier(limit) == tiers[limit]
