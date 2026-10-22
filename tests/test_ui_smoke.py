"""Headless Qt smoke test (offscreen platform): builds the real overlay, feeds
the synthetic stream, renders frames. Skipped if PySide6 is unavailable."""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_overlay_renders_with_synthetic_feed(tmp_path):
    from PySide6.QtWidgets import QApplication

    from isle_livemap.capture import TelemetryPipeline
    from isle_livemap.geo import MarkerStore, Settings
    from isle_livemap.overlay import MapView
    from isle_livemap.replay import SyntheticSource

    qa = QApplication.instance() or QApplication([])
    pipe = TelemetryPipeline()
    for d in SyntheticSource().datagrams(10.0):
        pipe.handle(d)
    store = MarkerStore(tmp_path / "m.json")
    view = MapView(pipe, store, Settings())
    view.tick()
    pos = view.player_world()
    assert pos is not None
    store.add("wp", "waypoint", pos[0] + 20_000, pos[1])
    view.set_size_percent(1000)
    assert view.width() == round(360 * 1.75)
    view.set_size_percent(1)
    assert view.width() == round(360 * 0.65)
    img = view.grab().toImage()
    assert not img.isNull()
    view.toggle_full_map()
    assert not view.grab().toImage().isNull()
    # player is drawn at the widget centre in minimap mode
    view.toggle_full_map()
    c = view._player_screen()
    assert abs(c.x() - view.width() / 2) < 1 and abs(c.y() - view.height() / 2) < 1
