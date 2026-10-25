"""Dot size follows the species size class (growth of other creatures is not decoded; a growth fraction scales when given)."""

import os
import time

import pytest

from isle_livemap.geo import (
    AI_RADIUS_PX, DOT_MAX_PX, DOT_MIN_PX, JUVENILE_SCALE, SIZE_RADIUS_PX, Entity, marker_extent, marker_radius,
)
from isle_livemap.species import DEFAULT_SIZE, SIZE_CLASSES, SIZE_ORDER, size_class


def ent(species, kind="dino", **kw):
    return Entity("1", kind, "n", species, 0.0, 0.0, **kw)


def test_size_classes_are_ordered_and_radius_grows_with_them():
    radii = [SIZE_RADIUS_PX[s] for s in SIZE_ORDER]
    assert radii == sorted(radii) and len(set(radii)) == len(radii)
    assert set(SIZE_RADIUS_PX) == set(SIZE_ORDER) == set(SIZE_CLASSES)


def test_every_radius_is_clamped_to_a_readable_range():
    assert all(DOT_MIN_PX <= r <= DOT_MAX_PX for r in SIZE_RADIUS_PX.values())
    tiny_juvenile = marker_radius(ent("Troodon", growth_pct=0.0))
    assert tiny_juvenile >= DOT_MIN_PX
    assert marker_radius(ent("Tyrannosaurus", growth_pct=1000.0)) == SIZE_RADIUS_PX["huge"] <= DOT_MAX_PX


def test_big_dinosaurs_get_bigger_dots_than_small_ones():
    assert marker_radius(ent("Tyrannosaurus")) > marker_radius(ent("Deinosuchus")) > marker_radius(ent("Carnotaurus")) \
        > marker_radius(ent("Utahraptor")) > marker_radius(ent("Troodon"))


def test_species_lookup_ignores_case_marks_and_rex():
    assert size_class("Tyrannosaurus") == size_class("TyrannosaurusRex") == size_class("tyrannosaurus rex") == "huge"
    assert size_class("Deinosuchus~") == size_class("Deinosuchus") == "large"  # an inferred (~) species scales too


def test_unknown_species_and_missing_species_get_the_default_size():
    assert size_class("Zzzsaurus") == size_class("") == size_class(None) == DEFAULT_SIZE
    assert marker_radius(ent("Zzzsaurus")) == marker_radius(ent("")) == SIZE_RADIUS_PX[DEFAULT_SIZE]
    assert marker_radius(ent("", kind="unknown")) == SIZE_RADIUS_PX[DEFAULT_SIZE]


def test_fish_stay_small_whatever_their_name():
    assert marker_radius(ent("Longear", kind="ai")) == marker_radius(ent("Tyrannosaurus", kind="ai")) == AI_RADIUS_PX
    assert AI_RADIUS_PX < SIZE_RADIUS_PX["medium"]


def test_players_use_their_species_size():
    assert marker_radius(ent("Tyrannosaurus", kind="player")) == SIZE_RADIUS_PX["huge"]


def test_a_known_growth_fraction_scales_between_juvenile_and_adult():
    adult = marker_radius(ent("Tyrannosaurus"))
    assert marker_radius(ent("Tyrannosaurus", growth_pct=100.0)) == adult
    assert marker_radius(ent("Tyrannosaurus", growth_pct=0.0)) == pytest.approx(adult * JUVENILE_SCALE, abs=0.01)
    half = marker_radius(ent("Tyrannosaurus", growth_pct=50.0))
    assert adult * JUVENILE_SCALE < half < adult
    assert marker_radius(ent("Tyrannosaurus", growth_pct=float("nan"))) == adult


def test_extent_covers_the_glyph():
    big = ent("Tyrannosaurus")
    assert marker_extent(big) == marker_radius(big)
    arrow = ent("Tyrannosaurus", heading=90.0)
    assert marker_extent(arrow) > marker_radius(arrow) * 0.9  # an arrow reaches about as far as its radius or more
    assert marker_extent(ent("Longear", kind="ai")) == AI_RADIUS_PX + 1  # the diamond is one px larger than the dot
    assert marker_extent(ent("Tyrannosaurus", heading=0.0)) > marker_extent(ent("Troodon", heading=0.0))


# ---------------------------------------------------------------- drawing
def _dot_width(tmp_path, species, kind="dino", heading=None):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from test_peers_ui import FakePeers, make_view, tick_twice  # noqa: PLC0415

    from isle_livemap.peers import PeerEntity

    view = make_view(tmp_path)
    e = Entity("1", kind, "", species, view.interp.x + 20_000.0, view.interp.y, 30_000.0, heading=heading)
    view.peers = FakePeers([PeerEntity(e, time.monotonic())])
    tick_twice(view)
    from PySide6.QtGui import QColor, QImage, QPainter

    img = QImage(view.size(), QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    n = view.nearby()[0]
    from PySide6.QtCore import QPointF

    pt = QPointF(view.width() / 2, view.height() / 2)
    view._draw_entity_marker(p, n, pt)
    p.end()
    xs = [x for x in range(img.width()) for y in range(img.height()) if img.pixelColor(x, y).alpha() > 0]
    return max(xs) - min(xs) + 1


def test_the_drawn_dot_is_wider_for_a_big_dinosaur(tmp_path):
    small, big = _dot_width(tmp_path, "Troodon"), _dot_width(tmp_path, "Tyrannosaurus")
    assert big >= small + 4  # 3.0 vs 6.8 px radius, outline included
    fish = _dot_width(tmp_path, "Longear", kind="ai")
    assert fish < big


def test_the_drawn_arrow_grows_with_the_dinosaur_too(tmp_path):
    assert _dot_width(tmp_path, "Tyrannosaurus", heading=90.0) > _dot_width(tmp_path, "Troodon", heading=90.0)


def test_a_label_is_kept_clear_of_a_big_dinosaurs_dot(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from test_peers_ui import FakePeers, make_view, tick_twice  # noqa: PLC0415

    import isle_livemap.overlay as overlay
    from isle_livemap.peers import PeerEntity

    view = make_view(tmp_path)
    e = Entity("1", "dino", "", "Tyrannosaurus", view.interp.x + 20_000.0, view.interp.y, 30_000.0)
    view.peers = FakePeers([PeerEntity(e, time.monotonic())])
    tick_twice(view)
    seen = {}
    real = overlay.place_label

    def spy(x, y, w, h, taken, *a, **k):
        seen["taken"] = list(taken)
        seen["xy"] = (x, y)
        return real(x, y, w, h, taken, *a, **k)

    overlay.place_label = spy
    try:
        from PySide6.QtGui import QImage, QPainter

        img = QImage(view.size(), QImage.Format.Format_ARGB32)
        p = QPainter(img)
        view._draw_entities(p)
        p.end()
    finally:
        overlay.place_label = real
    x, y = seen["xy"]
    reserved = [r for r in seen["taken"] if r[0] < x < r[0] + r[2] and r[1] < y < r[1] + r[3]]
    assert reserved, "the dot's clearance box is part of the space a label avoids"
    box = min(reserved, key=lambda r: r[2])
    assert box[2] / 2 >= marker_extent(e) + 3.0 - 1e-6  # the clearance grew with the dot
