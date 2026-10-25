"""What a nearby creature's label says, who gets a label first, and the direction of travel (heading)."""

import os
import time

import pytest

from isle_livemap.geo import (
    AI_LABELS_MINIMAP, CROWDED_LABELS, LABELS_FULL, LABELS_MINIMAP, Entity, Nearby, Settings, entity_label_lines,
    label_limits, label_sort_key,
)
from isle_livemap.peers import GamePeerSource, PeerEntity


def near(kind, name, species="", dist=100.0, **kw):
    return Nearby(Entity(name, kind, name, species, 0.0, 0.0, **kw), 0.0, 0.0, dist, 0.0)


# ---------------------------------------------------------------- label text
def test_creature_without_a_player_name_is_labelled_by_species_not_by_handle():
    head, tail = entity_label_lines(near("dino", "Actor #171102", "Deinosuchus"))
    assert head == "Deinosuchus" and "#" not in head
    assert entity_label_lines(near("ai", "Actor #5", "Longear"))[0] == "Longear"
    assert entity_label_lines(near("dino", "Actor #171102", "Deinosuchus", inferred=True))[0] == "Deinosuchus~"


def test_named_player_keeps_name_and_species():
    assert entity_label_lines(near("player", "Rex Fan", "Deinosuchus"))[0] == "Rex Fan (Deinosuchus)"
    assert entity_label_lines(near("player", "Rex Fan", "Deinosuchus", inferred=True))[0] == "Rex Fan (Deinosuchus~)"


def test_unidentified_creature_still_shows_its_id():
    assert entity_label_lines(near("unknown", "Unknown #9"))[0] == "Unknown #9"
    assert entity_label_lines(near("dino", "Actor #9"))[0] == "Actor #9"


# ---------------------------------------------------------------- who gets a label
def test_players_are_labelled_before_dinosaurs_unknowns_and_fish_whatever_the_distance():
    es = [near("ai", "f1", dist=10), near("unknown", "u", dist=20), near("dino", "d", dist=900), near("player", "p", dist=800),
          near("player", "p2", dist=700)]
    assert [n.entity.id for n in sorted(es, key=label_sort_key)] == ["p2", "p", "d", "u", "f1"]


def test_fish_never_use_up_the_label_budget_of_players():
    assert label_limits(2, 10, full_map=False) == (LABELS_MINIMAP, AI_LABELS_MINIMAP)
    assert AI_LABELS_MINIMAP < LABELS_MINIMAP
    assert label_limits(2, 30, full_map=False)[1] == 0  # a shoal: no fish labels at all, players keep theirs
    assert label_limits(2, 30, full_map=False)[0] == LABELS_MINIMAP
    assert label_limits(25, 0, full_map=False)[0] == CROWDED_LABELS
    assert label_limits(2, 0, full_map=True)[0] == LABELS_FULL


def test_minimap_labels_the_player_first_even_when_many_fish_are_nearer(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from test_peers_ui import FakePeers, make_view, peer, tick_twice  # noqa: PLC0415

    import isle_livemap.overlay as overlay

    view = make_view(tmp_path)
    fish = [peer(view, 100 + i, 40 + i * 10, 30 + i * 10) for i in range(12)]
    fish = [PeerEntity(Entity(p.entity.id, "ai", f"Actor #{p.entity.id}", "Longear", p.entity.x, p.entity.y), p.last_seen)
            for p in fish]
    dino = PeerEntity(Entity("7", "player", "Bi Long", "Deinosuchus", view.interp.x + 70_000, view.interp.y - 20_000, 30_000.0),
                      time.monotonic())
    view.peers = FakePeers([*fish, dino])
    tick_twice(view)
    asked = []
    real = overlay.peer_label_lines
    monkeypatch.setattr(overlay, "peer_label_lines", lambda n, *a, **k: asked.append(n.entity) or real(n, *a, **k))
    assert not view.grab().toImage().isNull()
    assert asked and asked[0].id == "7" and asked[0].name == "Bi Long"  # the first label placed is the player's


# ---------------------------------------------------------------- heading from movement
def source() -> GamePeerSource:
    return GamePeerSource()


def emit(g, handle, x_m, y_m, kind="dino"):
    g._emit(handle, x_m * 100.0, y_m * 100.0, 0.0, kind, "Rex", "Actor #1")
    return g.store.get(str(handle))


def test_heading_is_the_direction_of_travel_clockwise_from_north():
    g = source()
    assert emit(g, 1, 0, 0).heading is None  # a single sample has no direction
    assert emit(g, 1, 5, 0).heading == pytest.approx(90.0)  # east
    assert emit(g, 1, 5, -5).heading == pytest.approx(0.0)  # north is -Y
    assert emit(g, 1, 0, -5).heading == pytest.approx(270.0)  # west
    assert emit(g, 1, 0, 0).heading == pytest.approx(180.0)  # south


def test_slow_walkers_get_a_heading_and_standing_creatures_keep_it():
    g = source()
    emit(g, 1, 0, 0)
    assert emit(g, 1, 0.4, 0).heading is None  # 40 cm: still inside the noise filter
    assert emit(g, 1, 0.8, 0).heading is None
    assert emit(g, 1, 1.2, 0).heading == pytest.approx(90.0)  # 1.2 m from the anchor
    assert emit(g, 1, 1.25, 0.02).heading == pytest.approx(90.0)  # jitter does not turn the arrow


def test_a_respawn_jump_does_not_turn_the_heading_and_a_destroyed_creature_starts_over():
    g = source()
    emit(g, 1, 0, 0)
    assert emit(g, 1, 3, 0).heading == pytest.approx(90.0)
    assert emit(g, 1, 3, 500).heading == pytest.approx(90.0)  # 500 m in one step is a teleport
    g._drop(1)
    assert emit(g, 1, 100, 100).heading is None  # the handle is reused by a new actor


def test_status_says_where_heading_comes_from():
    g = GamePeerSource(pipeline=object())
    g.packets = 1
    emit(g, 1, 0, 0)
    st = g.status()
    assert "heading from movement" in st and "not available" in st


# ---------------------------------------------------------------- label placement
def test_place_label_honours_an_accept_filter():
    from isle_livemap.geo import place_label

    bounds = (0.0, 0.0, 200.0, 200.0)
    no_left = lambda r: r[0] >= 50.0  # noqa: E731 - pretend the left part of the view is clipped away
    r = place_label(60.0, 100.0, 80.0, 20.0, [], bounds, no_left)
    assert r is not None and r[0] >= 50.0
    assert place_label(60.0, 100.0, 80.0, 20.0, [], bounds, lambda r: False) is None


def test_labels_stay_inside_the_round_minimap_and_off_player_dots(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import math

    from PySide6.QtGui import QImage, QPainter
    from test_peers_ui import FakePeers, make_view  # noqa: PLC0415

    import isle_livemap.overlay as overlay
    from isle_livemap.geo import project_unclamped

    view = make_view(tmp_path)
    dinos = [PeerEntity(Entity(str(i), "player", f"Player number {i}", "Deinosuchus", view.interp.x + dx * 100,
                               view.interp.y + dy * 100, 30_000.0), time.monotonic())
             for i, (dx, dy) in enumerate([(780, -100), (700, -400), (-700, -300), (0, 850)])]
    view.peers = FakePeers(dinos)
    time.sleep(0.01)
    view.tick()
    time.sleep(0.01)
    view.tick()

    placed = []
    real = overlay.place_label
    monkeypatch.setattr(overlay, "place_label", lambda *a, **k: placed.append(real(*a, **k)) or placed[-1])
    img = QImage(view.size(), QImage.Format.Format_ARGB32)
    painter = QPainter(img)
    view._draw_entities(painter)
    painter.end()
    rects = [r for r in placed if r]
    assert len(rects) == 4
    cx = cy = view.width() / 2
    for r in rects:
        assert all(math.hypot(x - cx, y - cy) <= view.width() / 2 for x in (r[0], r[0] + r[2]) for y in (r[1], r[1] + r[3]))
        for n in view.nearby():
            pt = view.to_screen(*project_unclamped(n.x, n.y))
            assert not (r[0] < pt.x() < r[0] + r[2] and r[1] < pt.y() < r[1] + r[3]), "a label covers a player dot"


def test_two_creatures_standing_together_both_get_a_label():
    from isle_livemap.geo import place_label

    bounds, placed = (0.0, 0.0, 360.0, 360.0), []
    first = place_label(180.0, 180.0, 150.0, 26.0, placed, bounds)
    second = place_label(183.0, 182.0, 150.0, 26.0, placed, bounds)  # 4 px away: the first label sits where its own would go
    third = place_label(181.0, 181.0, 150.0, 26.0, placed, bounds)
    assert first and second and third and len(placed) == 3
    for i, a in enumerate(placed):
        assert all(not (a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])
                   for b in placed[i + 1:])
