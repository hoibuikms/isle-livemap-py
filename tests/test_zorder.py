"""Nothing covers an arrow that matters: players are painted last (above fish, label pills and captions), and the
250 / 500 / 1000 m tiers have exact, inclusive boundaries."""

import os
import time

import pytest

from isle_livemap.geo import Entity, Nearby, PRIORITY_RADII_M, priority_key, priority_tier


@pytest.mark.parametrize("metres,tier", [(0, 0), (249.9, 0), (250.0, 0), (250.01, 1), (499.99, 1), (500.0, 1), (500.01, 2),
                                         (999.99, 2), (1000.0, 2), (1000.01, 3), (5000, 3)])
def test_tier_boundaries_are_inclusive(metres, tier):
    assert PRIORITY_RADII_M == (250, 500, 1000) or tuple(PRIORITY_RADII_M) == (250, 500, 1000)
    assert priority_tier(metres) == tier


def _nearby(kind, metres, i="1"):
    return Nearby(Entity(i, kind, "n", "", 0.0, 0.0), 0.0, 0.0, metres, 0.0)


def test_priority_order_players_before_fish_and_near_tiers_before_far():
    order = sorted([_nearby("ai", 10, "a"), _nearby("player", 700, "b"), _nearby("player", 400, "c"), _nearby("player", 240, "d"),
                    _nearby("dino", 100, "e"), _nearby("player", 260, "f")], key=priority_key)
    ids = [n.entity.id for n in order]
    assert ids.index("d") < ids.index("c") < ids.index("b")  # 250 m tier, then 500 m, then 1000 m
    assert ids.index("a") == len(ids) - 1  # a fish 10 m away still ranks below every creature
    assert ids.index("d") < ids.index("f") and ids.index("e") > ids.index("d") - 5


def _scene(tmp_path, entities):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from test_peers_ui import FakePeers, make_view, tick_twice  # noqa: PLC0415

    from isle_livemap.peers import PeerEntity

    view = make_view(tmp_path)
    ents = []
    for i, (kind, species, dx, dy) in enumerate(entities, start=1):
        e = Entity(str(i), kind, f"Name{i}", species, view.interp.x + dx * 100.0, view.interp.y + dy * 100.0, 30_000.0,
                   health_pct=90.0, stamina_pct=50.0, heading=45.0, max_health=2000.0)
        ents.append(PeerEntity(e, time.monotonic()))
    view.peers = FakePeers(ents)
    tick_twice(view)
    return view


def _arrow_visible(view, x_m, y_m):
    """The purple of an arrow at (x_m, y_m) from the local player is still on screen after everything is painted."""
    from isle_livemap.geo import PEER_COLOR, project_unclamped

    img = view.grab().toImage()
    pt = view.to_screen(*project_unclamped(view.interp.x + x_m * 100.0, view.interp.y + y_m * 100.0))
    target = [int(PEER_COLOR[i:i + 2], 16) for i in (1, 3, 5)]
    best = 1e9
    for dx in range(-4, 5):
        for dy in range(-4, 5):
            c = img.pixelColor(int(pt.x()) + dx, int(pt.y()) + dy)
            best = min(best, abs(c.red() - target[0]) + abs(c.green() - target[1]) + abs(c.blue() - target[2]))
    return best


def test_an_arrow_in_the_500_m_tier_is_not_covered_by_the_label_of_a_nearer_creature(tmp_path):
    # the label of the creature at 150 m is placed right where the arrow 400 m away is
    view = _scene(tmp_path, [("player", "Allosaurus", 60.0, -120.0), ("player", "Carnotaurus", 62.0, -118.0),
                             ("player", "Stegosaurus", 120.0, -300.0), ("dino", "Pteranodon", 125.0, -298.0),
                             ("player", "Triceratops", -80.0, 330.0)])
    for x, y in ((120.0, -300.0), (-80.0, 330.0)):
        assert _arrow_visible(view, x, y) <= 40, "an arrow was painted over"


def test_arrows_stay_visible_when_the_labels_of_a_crowd_have_to_overlap_them(tmp_path):
    # eight players inside 250 m: the 250 m tier always keeps its full labels, even over other creatures
    import math

    ring = [("player", "Allosaurus", 200.0 * math.cos(a), 200.0 * math.sin(a)) for a in [k * math.tau / 8 for k in range(8)]]
    view = _scene(tmp_path, ring)
    worst = max(_arrow_visible(view, x, y) for _k, _s, x, y in ring)
    assert worst <= 40, f"an arrow is covered (colour distance {worst})"


def test_players_are_painted_above_fish_labels_and_ghosts(tmp_path):
    view = _scene(tmp_path, [("ai", "Catfish", 300.0, 10.0), ("ai", "Longear", 302.0, 12.0), ("ai", "Hoplo", 298.0, 8.0),
                             ("player", "Allosaurus", 300.0, 10.0)])
    assert _arrow_visible(view, 300.0, 10.0) <= 40


def test_labels_are_drawn_before_glyphs():
    import inspect

    from isle_livemap import overlay

    src = inspect.getsource(overlay.MapView._draw_entities)
    assert src.index("for r, lines, alpha, color, head_alpha in label_ops") < src.index("self._draw_entity_marker(p, n, pt")
