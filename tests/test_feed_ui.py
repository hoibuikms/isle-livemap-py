"""Headless render smoke test with feed entities (offscreen Qt)."""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_overlay_draws_feed_entities(tmp_path):
    from PySide6.QtWidgets import QApplication

    from isle_livemap.capture import TelemetryPipeline
    from isle_livemap.feed import DemoFeed
    from isle_livemap.geo import MarkerStore, Settings
    from isle_livemap.overlay import MapView
    from isle_livemap.replay import SyntheticSource

    QApplication.instance() or QApplication([])
    pipe = TelemetryPipeline()
    for d in SyntheticSource().datagrams(10.0):
        pipe.handle(d)

    def anchor():
        pos = pipe.latest.position
        return (pos.x, pos.y) if pos else None

    settings = Settings()
    view = MapView(pipe, MarkerStore(tmp_path / "m.json"), settings, DemoFeed(anchor))
    view.tick()
    time.sleep(0.02)
    view.tick()
    ids = {n.entity.id for n in view.nearby()}
    assert {"p1", "p2", "a1"} <= ids and "p4" not in ids  # p4 is ~3 km away: outside 1 km radius
    assert "p4" in {n.entity.id for n in view.nearby(None)}
    assert "feed: demo" in view.feed_status
    img = view.grab().toImage()
    assert not img.isNull()
    settings.show_ai = False
    assert all(n.entity.kind == "player" for n in view.nearby())
    settings.show_players = settings.show_ai = True
    view.toggle_full_map()
    assert not view.grab().toImage().isNull()
    # no feed at all still renders
    plain = MapView(pipe, MarkerStore(tmp_path / "m2.json"), Settings())
    plain.tick()
    assert plain.nearby() == [] and not plain.grab().toImage().isNull()


def test_zoom_to_radius_edge_is_radius_metres():
    from isle_livemap.geo import MAP_WIDTH, UNITS_PER_METER, WORLD_SCALE, Settings
    from isle_livemap.overlay import BASE_VIEW_FRACTION, MapView

    settings = Settings()
    MapView.zoom_to_radius(type("V", (), {"settings": settings})(), 1000)
    span = BASE_VIEW_FRACTION / settings.zoom
    assert abs(span * MAP_WIDTH * WORLD_SCALE / UNITS_PER_METER / 2 - 1000) < 1
