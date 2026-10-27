"""Every dinosaur / player of another person is one purple arrow, the same shape as the local (red) arrow: no species,
diet or size categories, no dots. Server animals stay a small muted diamond."""

import os
import time

import pytest

from isle_livemap import species as sp
from isle_livemap.geo import (
    AI_RADIUS_PX, ARROW_POINTS, ENTITY_STYLE, LEGEND_ENTRIES, PEER_ARROW_SCALE, PEER_COLOR, Entity, marker_extent, marker_radius,
    peer_style,
)


def ent(species, kind="dino", **kw):
    return Entity("1", kind, "n", species, 0.0, 0.0, **kw)


def test_species_size_and_diet_categories_are_gone():
    for name in ("SIZE_CLASSES", "size_class", "DIET_SPECIES", "diet_of", "DEFAULT_SIZE"):
        assert not hasattr(sp, name)


def test_every_dinosaur_and_player_has_one_purple_arrow_style():
    styles = {id(peer_style(ent(s, kind=k))) for s in ("Tyrannosaurus", "Troodon", "", "Deinosuchus~") for k in ("dino", "player", "unknown")}
    assert len(styles) == 1
    st = peer_style(ent("Tyrannosaurus"))
    assert st.color == PEER_COLOR and st.glyph == "arrow" and st is ENTITY_STYLE["player"]
    assert ENTITY_STYLE["self"].glyph == "arrow" and ENTITY_STYLE["self"].color != PEER_COLOR  # yours stays red
    assert peer_style(ent("Longear", kind="ai")) is ENTITY_STYLE["ai"] and ENTITY_STYLE["ai"].glyph == "diamond"


def test_marker_size_does_not_depend_on_the_species():
    sizes = {marker_radius(ent(s)) for s in ("Tyrannosaurus", "Troodon", "", "Zzzsaurus")}
    extents = {marker_extent(ent(s, heading=h)) for s in ("Tyrannosaurus", "Troodon") for h in (None, 0.0, 90.0)}
    assert len(sizes) == 1 and len(extents) == 1
    assert marker_extent(ent("Tyrannosaurus")) == pytest.approx(-min(y for _x, y in ARROW_POINTS) * PEER_ARROW_SCALE)  # the tip of the arrow
    assert marker_radius(ent("Longear", kind="ai")) == AI_RADIUS_PX and marker_extent(ent("Longear", kind="ai")) == AI_RADIUS_PX + 1


def test_the_legend_has_no_diet_entries():
    names = [n for n, _c, _h in LEGEND_ENTRIES]
    assert names == ["You", "Another player's dinosaur", "Animal / fish"]


# ---------------------------------------------------------------- drawing
def _painted(tmp_path, species, kind="dino", heading=None):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from test_peers_ui import FakePeers, make_view, tick_twice  # noqa: PLC0415

    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QImage, QPainter

    from isle_livemap.peers import PeerEntity

    view = make_view(tmp_path)
    e = Entity("1", kind, "", species, view.interp.x + 20_000.0, view.interp.y, 30_000.0, heading=heading)
    view.peers = FakePeers([PeerEntity(e, time.monotonic())])
    tick_twice(view)
    img = QImage(view.size(), QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    view._draw_entity_marker(p, view.nearby()[0], QPointF(view.width() / 2, view.height() / 2))
    p.end()
    pts = [(x, y) for x in range(img.width()) for y in range(img.height()) if img.pixelColor(x, y).alpha() > 0]
    return img, pts


def test_a_big_and_a_small_species_are_painted_identically(tmp_path):
    a, pa = _painted(tmp_path, "Tyrannosaurus", heading=90.0)
    b, pb = _painted(tmp_path, "Troodon", heading=90.0)
    assert pa == pb and pa
    assert all(a.pixelColor(x, y) == b.pixelColor(x, y) for x, y in pa)


def test_a_dinosaur_is_a_round_marker_until_its_heading_is_known_then_an_arrow(tmp_path):
    img, pts = _painted(tmp_path, "Allosaurus", heading=None)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    width, height = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
    assert abs(width - height) <= 1  # as wide as tall: it does not claim to face anywhere (it used to be an arrow pointing north)
    cx, cy = img.width() / 2, img.height() / 2
    assert abs((min(xs) + max(xs)) / 2 - cx) < 1.5 and abs((min(ys) + max(ys)) / 2 - cy) < 1.5  # centred on the coordinate
    north, pn = _painted(tmp_path, "Allosaurus", heading=0.0)
    nys = [p[1] for p in pn]
    nxs = [p[0] for p in pn]
    assert max(nys) - min(nys) + 1 > max(nxs) - min(nxs) + 1  # known and facing north: an arrow, taller than wide
    top = [x for x, y in pn if y == min(nys)]
    bottom = [x for x, y in pn if y == max(nys)]
    assert max(bottom) - min(bottom) > max(top) - min(top)  # the wings are at the back
    east, pe = _painted(tmp_path, "Allosaurus", heading=90.0)
    assert max(p[0] for p in pe) - min(p[0] for p in pe) == max(nys) - min(nys)  # turned a quarter: the long side is horizontal


def test_the_arrow_is_purple(tmp_path):
    img, pts = _painted(tmp_path, "Allosaurus", heading=0.0)
    r, g, b = (lambda c: (c.red(), c.green(), c.blue()))(img.pixelColor(*pts[len(pts) // 2]))
    assert b > r > g or b > g  # a purple / violet fill, not the red of the local arrow
    assert not (r > 200 and g < 100 and b < 100)


def test_a_label_is_kept_clear_of_the_arrow(tmp_path):
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
    assert reserved, "the arrow's clearance box is part of the space a label avoids"
    assert min(reserved, key=lambda r: r[2])[2] / 2 >= marker_extent(e) + 3.0 - 1e-6


def test_the_arrow_keeps_its_last_direction_while_the_creature_rests(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from test_peers_ui import FakePeers, make_view, tick_twice  # noqa: PLC0415

    from isle_livemap.peers import PeerEntity

    view = make_view(tmp_path)
    e = Entity("1", "dino", "", "Allosaurus", view.interp.x + 20_000.0, view.interp.y, 30_000.0, heading=135.0)
    peers = FakePeers([PeerEntity(e, time.monotonic())])
    peers.heading_from_movement = True  # the game source says its heading is the direction of travel
    view.peers = peers
    tick_twice(view)
    assert view.ents.heading_only_when_moving is False  # no hiding of the arrow once the creature stopped
    assert view.nearby()[0].entity.heading == pytest.approx(135.0)
