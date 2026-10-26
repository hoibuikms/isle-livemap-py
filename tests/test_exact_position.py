"""Where a creature is drawn is where it is: exact pixels for known offsets, the arrow's middle on the coordinate, no
bias in motion, an exact stop, the bold first line measured with the bold font, and the debug crosshair."""

import math
import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_other_players_data import colour_near  # noqa: E402
from test_peers_ui import FakePeers, make_view, tick_twice  # noqa: E402

import isle_livemap.overlay as overlay  # noqa: E402
from isle_livemap.geo import (  # noqa: E402
    ARROW_POINTS, MAP_WIDTH, PEER_OUTLINE_W, WORLD_SCALE, Entity, EntitySmoother, Interpolator, Settings,
    project_unclamped,
)
from isle_livemap.peers import PeerEntity  # noqa: E402


def entity(i, view, dx_m=0.0, dy_m=0.0, kind="dino", **kw):
    return Entity(str(i), kind, kw.pop("name", ""), kw.pop("species", "Allosaurus"), view.interp.x + dx_m * 100.0,
                  view.interp.y + dy_m * 100.0, 30_000.0, **kw)


def shown(tmp_path, build, **settings):
    view = make_view(tmp_path, settings=Settings(**settings))
    view.resize(400, 400)
    view.peers = FakePeers([PeerEntity(e, time.monotonic()) for e in build(view)])
    view.trace_frames = True
    tick_twice(view)
    img = view.grab().toImage()
    return view, img


def metres_to_pixels(view, metres):
    """Pixels for ``metres`` on the map, from the constants only: the span of the view is BASE_VIEW_FRACTION of the map width."""
    span = overlay.BASE_VIEW_FRACTION / view.settings.zoom
    return metres * 100.0 * (view.width() / span) / (MAP_WIDTH * WORLD_SCALE)


# ---------------------------------------------------------------- the arrow's middle is the coordinate
def test_the_arrow_is_drawn_around_its_area_centroid():
    n = len(ARROW_POINTS)
    twice_area = sum(ARROW_POINTS[i][0] * ARROW_POINTS[(i + 1) % n][1] - ARROW_POINTS[(i + 1) % n][0] * ARROW_POINTS[i][1] for i in range(n))
    cx = sum((ARROW_POINTS[i][0] + ARROW_POINTS[(i + 1) % n][0]) * (ARROW_POINTS[i][0] * ARROW_POINTS[(i + 1) % n][1] - ARROW_POINTS[(i + 1) % n][0] * ARROW_POINTS[i][1]) for i in range(n)) / (3 * twice_area)
    cy = sum((ARROW_POINTS[i][1] + ARROW_POINTS[(i + 1) % n][1]) * (ARROW_POINTS[i][0] * ARROW_POINTS[(i + 1) % n][1] - ARROW_POINTS[(i + 1) % n][0] * ARROW_POINTS[i][1]) for i in range(n)) / (3 * twice_area)
    assert abs(cx) < 1e-9 and abs(cy) < 1e-9
    assert min(y for _x, y in ARROW_POINTS) < -10.0 < 0.0 < max(y for _x, y in ARROW_POINTS) < 8.0  # tip up, a little longer than the tail


# ---------------------------------------------------------------- exact pixels
def test_known_offsets_land_on_exact_pixels_for_you_and_for_others(tmp_path):
    view, _img = shown(tmp_path, lambda v: [entity("1", v, 100.0, 0.0), entity("2", v, 0.0, -250.0), entity("3", v, -100.0, 250.0)])
    c = view._player_screen()
    assert (c.x(), c.y()) == pytest.approx((view.width() / 2, view.height() / 2), abs=1e-9)  # you: the middle of the view, to the sub-pixel
    east, north, sw = (view.world_to_screen(view.interp.x + 10_000.0, view.interp.y),
                       view.world_to_screen(view.interp.x, view.interp.y - 25_000.0),
                       view.world_to_screen(view.interp.x - 10_000.0, view.interp.y + 25_000.0))
    assert east.x() - c.x() == pytest.approx(metres_to_pixels(view, 100.0), abs=1e-6) and east.y() == pytest.approx(c.y(), abs=1e-9)
    assert c.y() - north.y() == pytest.approx(metres_to_pixels(view, 250.0), abs=1e-6) and north.x() == pytest.approx(c.x(), abs=1e-9)  # north is up
    assert (sw.x() - c.x(), sw.y() - c.y()) == pytest.approx((-metres_to_pixels(view, 100.0), metres_to_pixels(view, 250.0)), abs=1e-6)
    # the same metre is the same number of pixels east-west and north-south
    assert (east.x() - c.x()) / 100.0 == pytest.approx((c.y() - north.y()) / 250.0, rel=1e-9)


def test_the_layer_draws_every_creature_where_world_to_screen_says(tmp_path):
    view, _img = shown(tmp_path, lambda v: [entity(i, v, 37.0 * i - 90.0, 41.0 * i - 120.0) for i in range(1, 6)])
    near = {n.entity.id: n for n in view.nearby(None)}
    drawn = view.frame_trace["pos"]
    assert set(drawn) == set(near) and len(drawn) == 5
    for eid, n in near.items():
        want = view.world_to_screen(n.x, n.y)
        assert (drawn[eid][0], drawn[eid][1]) == pytest.approx((want.x(), want.y()), abs=1e-6)


def test_two_creatures_at_one_world_position_are_drawn_on_one_pixel(tmp_path):
    view, _img = shown(tmp_path, lambda v: [entity("1", v, 80.0, -60.0), entity("2", v, 80.0, -60.0, kind="player", name="P1")])
    drawn = view.frame_trace["pos"]
    assert (drawn["1"][0], drawn["1"][1]) == (drawn["2"][0], drawn["2"][1])


def test_a_creature_at_rest_is_drawn_exactly_on_its_last_position():
    s = EntitySmoother(tau_s=0.15)
    e = Entity("a", "dino", "", "", 123_456.0, -654_321.0)
    s.update([PeerEntity(e, 0.0)], now=0.0)
    s.update([PeerEntity(Entity("a", "dino", "", "", 123_856.0, -654_321.0), 0.5)], now=0.5)  # moved 4 m east in 0.5 s, then stopped
    for i in range(1, 900):
        s.step(1 / 60, 0.5 + i / 60)
    assert s.pos["a"] == pytest.approx([123_856.0, -654_321.0], abs=0.01)  # within a millimetre of the last decoded sample
    it = Interpolator(tau_s=0.09)
    it.set_target(10.0, 20.0, 0.0, 0.0)
    it.set_target(710.0, 20.0, 0.0, 0.14)
    for i in range(900):
        it.step(1 / 60, 0.14 + i / 60)
    assert (it.x, it.y) == pytest.approx((710.0, 20.0), abs=0.01)


# ---------------------------------------------------------------- no bias in motion
def _bias(make, step, speed_cm_s, interval_s, seconds=40.0):
    """Signed error along the path (cm, + = ahead of the creature) of a dot following samples that are exact positions of a
    steady walker, one every ``interval_s``, measured once the filters have settled."""
    errs = []
    t, next_sample, dt = 0.0, 0.0, 1 / 60
    obj = make()
    while t < seconds:
        if t >= next_sample:
            obj.feed(speed_cm_s * t, 0.0, t)
            next_sample += interval_s
        step(obj, dt, t)
        if t > 5.0:
            errs.append(obj.x() - speed_cm_s * t)
        t += dt
    mean = sum(errs) / len(errs)
    return mean, max(abs(e) for e in errs)


class _Peer:
    def __init__(self):
        self.s, self.n = EntitySmoother(tau_s=0.15), 0

    def feed(self, x, y, t):
        self.n += 1
        self.s.update([PeerEntity(Entity("a", "dino", "", "", x, y), t, sample_at=t)], now=t)

    def x(self):
        return self.s.pos["a"][0]


class _Own:
    def __init__(self):
        self.i = Interpolator(tau_s=0.09)

    def feed(self, x, y, t):
        self.i.set_target(x, y, 90.0, t)

    def x(self):
        return self.i.x


def test_a_steadily_walking_creature_is_drawn_neither_ahead_nor_behind():
    mean, worst = _bias(_Peer, lambda o, dt, t: o.s.step(dt, t), 800.0, 0.5)  # 8 m/s, a position every half second
    assert abs(mean) < 25.0 and worst < 150.0  # centimetres: a quarter of a metre on average, never more than 1.5 m
    mean, worst = _bias(_Own, lambda o, dt, t: o.i.step(dt, t), 800.0, 0.14)
    assert abs(mean) < 25.0 and worst < 150.0


# ---------------------------------------------------------------- the bold first line is measured bold
def _advance(text, bold, size=8):
    from PySide6.QtGui import QFont, QFontMetricsF

    f = QFont("Segoe UI", size)
    f.setBold(bold)
    return QFontMetricsF(f).horizontalAdvance(text)


def test_a_players_first_line_is_bold_and_its_label_is_as_wide_as_the_bold_text(tmp_path):
    view, _img = shown(tmp_path, lambda v: [entity("1", v, 120.0, 0.0, kind="player", name="Player One"),
                                            entity("9", v, -120.0, 0.0, kind="ai", species="Catfish")])
    lines, rect, small = view.frame_trace["labels"]["1"]
    assert not small and lines[0].startswith("Player One - Allosaurus - ")
    assert rect[2] == pytest.approx(max([_advance(lines[0], True), *[_advance(t, False) for t in lines[1:]]]) + 2, abs=1e-6)
    assert _advance(lines[0], True) > _advance(lines[0], False)  # bold really is wider, so measuring it regular would clip
    ai_lines, ai_rect, _ = view.frame_trace["labels"]["9"]
    assert ai_lines[0].startswith("AI - Catfish - ") and ai_rect[2] == pytest.approx(_advance(ai_lines[0], False) + 2, abs=1e-6)  # the fish's stays regular


def test_the_arrow_of_a_player_has_a_thicker_outline_than_a_fish(tmp_path):
    view, _img = shown(tmp_path, lambda v: [entity("1", v, 120.0, 0.0), entity("9", v, -120.0, 0.0, kind="ai", species="Catfish")])
    widths = sorted({round(pen.widthF(), 2) for pen, _brush in view._marker_tools.values() if pen.style() != 0})
    assert PEER_OUTLINE_W in widths and 1.5 in widths and PEER_OUTLINE_W > 1.5


# ---------------------------------------------------------------- the debug crosshair
def test_the_debug_crosshair_is_off_by_default_and_marks_the_exact_decoded_position(tmp_path, monkeypatch):
    monkeypatch.delenv("ISLE_DEBUG_POSITIONS", raising=False)
    view, img = shown(tmp_path, lambda v: [entity("1", v, 100.0, 0.0)])
    assert view.debug_positions is False
    ex = view.world_to_screen(view.interp.x + 10_000.0, view.interp.y)
    yellow = (255, 235, 60)
    assert not colour_near(img, ex.x(), ex.y(), yellow, r=8, tol=40)
    view.debug_positions = True
    img2 = view.grab().toImage()
    assert colour_near(img2, ex.x(), ex.y(), yellow, r=8, tol=40)  # the exact decoded position, yellow
    c = view._player_screen()
    assert colour_near(img2, c.x(), c.y(), (255, 60, 220), r=8, tol=40)  # and yours, magenta
    assert view.errors == 0


def test_the_environment_variable_turns_the_debug_crosshair_on(tmp_path, monkeypatch):
    monkeypatch.setenv("ISLE_DEBUG_POSITIONS", "1")
    assert make_view(tmp_path).debug_positions is True


def test_the_distance_on_a_label_ignores_height(tmp_path):
    view, _img = shown(tmp_path, lambda v: [Entity("1", "dino", "", "Allosaurus", v.interp.x + 30_000.0, v.interp.y, 900_000.0)])  # 300 m east, 9 km up
    (n,) = view.nearby(None)
    assert n.distance_m == pytest.approx(300.0, abs=0.01) and math.isfinite(n.distance_m)
    assert view.frame_trace["labels"]["1"][0][0].endswith("- 300 m")
