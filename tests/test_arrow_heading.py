"""The arrow icon points the way the creature faces: orientation of the polygon for every compass direction (yours and
other creatures'), the rotation byte that comes with a creature's position, and the fallbacks when it is missing."""

import math
import os

import pytest

from isle_livemap import species as sp
from isle_livemap.decode import (
    Bits, IrisBatch, ActorLocationTracker, YAW_BITS, YAW_GAP_BITS, map_heading_from_unreal_yaw, yaw_after_vector,
)
from isle_livemap.geo import ARROW_POINTS, PEER_ARROW_SCALE, bearing_deg
from isle_livemap.peers import GamePeerSource
from isle_livemap.replay import BitWriter

COMPASS = {"N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0, "S": 180.0, "SW": 225.0, "W": 270.0, "NW": 315.0}


def screen_angle(x: float, y: float) -> float:
    """Degrees clockwise from screen-up of the vector (x right, y down)."""
    return math.degrees(math.atan2(x, -y)) % 360.0


def circ(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


# ---------------------------------------------------------------- conventions
@pytest.mark.parametrize("name,deg", COMPASS.items())
def test_world_bearing_uses_north_as_minus_y(name, deg):
    # one step of 100 m in the compass direction: east is +X, north is -Y
    dx, dy = math.sin(math.radians(deg)), -math.cos(math.radians(deg))
    assert bearing_deg(0.0, 0.0, dx * 10_000.0, dy * 10_000.0) == pytest.approx(deg, abs=1e-6)


def test_unreal_yaw_zero_is_east_and_turns_clockwise_on_the_map():
    assert map_heading_from_unreal_yaw(0.0) == pytest.approx(90.0)  # +X is east
    assert map_heading_from_unreal_yaw(90.0) == pytest.approx(180.0)  # +Y is south: yaw turns clockwise seen from above
    assert map_heading_from_unreal_yaw(180.0) == pytest.approx(270.0)
    assert map_heading_from_unreal_yaw(-90.0) == pytest.approx(0.0)  # north


def test_the_arrow_polygon_has_its_tip_first_and_is_symmetric_about_its_axis():
    tip = ARROW_POINTS[0]
    assert tip[0] == pytest.approx(0.0) and tip[1] < 0  # drawn pointing up: the tip is the highest point
    right, notch, left = ARROW_POINTS[1], ARROW_POINTS[2], ARROW_POINTS[3]
    assert right[0] == pytest.approx(-left[0]) and right[1] == pytest.approx(left[1]) and notch[0] == pytest.approx(0.0)
    assert notch[1] < right[1]  # the notch is between the wing tips and the tip: the tail is open
    cx = sum(p[0] for p in ARROW_POINTS) / 4
    assert cx == pytest.approx(0.0)  # centred sideways


@pytest.mark.parametrize("name,deg", COMPASS.items())
def test_a_peer_arrow_tip_points_along_its_heading_on_screen(name, deg):
    pytest.importorskip("PySide6")
    from isle_livemap.overlay import _arrow_polygon

    poly = _arrow_polygon(round(deg / 3.0) % 120)
    tip = poly.at(0)
    assert screen_angle(tip.x(), tip.y()) == pytest.approx(deg, abs=1.5)
    assert math.hypot(tip.x(), tip.y()) > max(math.hypot(poly.at(i).x(), poly.at(i).y()) for i in (1, 2, 3)) * 0.8  # the tip leads, not a wing
    # the arrow keeps the same size in every direction
    assert math.hypot(tip.x(), tip.y()) == pytest.approx(math.hypot(ARROW_POINTS[0][0], ARROW_POINTS[0][1]) * PEER_ARROW_SCALE, abs=1e-6)


@pytest.mark.parametrize("name,deg", COMPASS.items())
def test_your_arrow_tip_points_along_the_heading_it_is_rotated_by(name, deg):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QTransform

    # MapView._draw_player does painter.rotate(heading) around the polygon drawn pointing up; QPainter rotates clockwise
    t = QTransform().rotate(deg)
    tip = t.map(QPointF(*ARROW_POINTS[0]))
    assert screen_angle(tip.x(), tip.y()) == pytest.approx(deg, abs=1e-6)


@pytest.mark.parametrize("name,deg", COMPASS.items())
def test_a_drawn_peer_arrow_points_the_way_it_is_headed(tmp_path, name, deg):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from test_peer_marker import _painted

    img, pts = _painted(tmp_path, "Allosaurus", heading=deg)
    cx, cy = img.width() / 2, img.height() / 2
    fx, fy = math.sin(math.radians(deg)), -math.cos(math.radians(deg))  # unit vector of the heading on screen (y down)
    proj = [((x - cx) * fx + (y - cy) * fy) for x, y in pts]
    side = [((x - cx) * -fy + (y - cy) * fx) for x, y in pts]
    assert max(proj) >= -min(proj) + 0.5  # it reaches a little further ahead of the coordinate than behind it

    def width(lo, hi):
        sel = [s_ for p_, s_ in zip(proj, side) if lo <= p_ <= hi]
        return max(sel) - min(sel) + 1 if sel else 0

    assert width(-7.0, -4.0) > width(4.0, 7.0) + 3  # narrow at the front (the tip), wide at the back (the wings): it points forward
    assert abs(max(side) + min(side)) < 2.0  # and it is symmetric about the heading
    mx, my = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    assert math.hypot(mx - cx, my - cy) < 2.5  # the middle of the arrow is on the exact coordinate


# ---------------------------------------------------------------- the rotation byte
HDR = 0x0C0000348


def state_batch(vec, yaw_deg, offset=94, comp=26, total=260, handle=5, pad_bits=None):
    w = BitWriter()
    w.write(HDR, 33)
    w.write(0xA5A5A5A5A5 & ((1 << (offset - 33)) - 1), offset - 33)
    w.write(comp | 64, 7)
    for v in vec:
        w.write(round(v * 100), comp)
    w.write(0b101, YAW_GAP_BITS)
    if yaw_deg is not None:
        w.write(round(yaw_deg / 360.0 * (1 << YAW_BITS)) % (1 << YAW_BITS), YAW_BITS)
    w.write(0, total - w.nbits)
    return Bits(w.to_bytes()), IrisBatch(handle, 0, w.nbits, True, False)


@pytest.mark.parametrize("comp", (25, 26, 27))
def test_the_rotation_byte_is_read_3_bits_after_the_end_of_the_vector_for_every_component_width(comp):
    bits, batch = state_batch((130_000.0, -320_000.0, 36_000.0), 135.0, comp=comp)
    assert yaw_after_vector(bits, batch.data_bit_offset, batch.data_bit_count, 94, comp) == pytest.approx(135.0, abs=360 / 256)


def test_a_learned_location_carries_the_yaw_and_the_map_heading():
    t = ActorLocationTracker()
    t.note_creation(5, (129_000.0, -320_050.0, 36_000.0), 9.0)  # 10 m away after a second: accepted at once
    bits, batch = state_batch((130_000.0, -320_000.0, 36_000.0), 270.0)  # Unreal yaw 270 = looking north (-Y)
    loc = t.update(bits, batch, 10.0, learn=True)
    assert loc is not None and loc.yaw_deg == pytest.approx(270.0, abs=360 / 256)
    assert loc.map_heading == pytest.approx(0.0, abs=2.0)  # north on the map


def test_a_bootstrapped_location_carries_the_yaw_too():
    t = ActorLocationTracker()
    origin = (130_000.0, -320_000.0, 36_000.0)
    a_bits, a_batch = state_batch((130_100.0, -320_000.0, 36_000.0), 0.0)
    b_bits, b_batch = state_batch((130_160.0, -320_000.0, 36_000.0), 0.0)
    assert t.bootstrap(a_bits, a_batch, 10.0, origin) is None  # first sample
    loc = t.bootstrap(b_bits, b_batch, 10.5, origin)  # continues it at walking pace
    assert loc is not None and loc.yaw_deg == pytest.approx(0.0, abs=2.0) and loc.map_heading == pytest.approx(90.0, abs=2.0)


def test_a_location_without_room_for_the_byte_has_no_yaw():
    bits, batch = state_batch((130_000.0, -320_000.0, 36_000.0), None, total=94 + 7 + 78 + YAW_GAP_BITS + 4)
    assert yaw_after_vector(bits, batch.data_bit_offset, batch.data_bit_count, 94, 26) is None


# ---------------------------------------------------------------- heading choice in the peer source
def source():
    return GamePeerSource(pipeline=object())


def emit(g, handle, x_m, y_m, facing=None):
    if facing is not None:
        g._facing[handle] = facing
    g._emit(handle, x_m * 100.0, y_m * 100.0, 0.0, "dino", "Rex", "Actor #1")
    return g.store.get(str(handle))


def test_a_creature_that_never_moved_faces_the_way_its_rotation_byte_says():
    g = source()
    assert emit(g, 1, 0, 0).heading is None  # nothing known: the marker is round
    assert emit(g, 1, 0, 0, facing=225.0).heading == pytest.approx(225.0)  # standing still, but it faces south-west
    assert emit(g, 1, 0.1, 0.1).heading == pytest.approx(225.0)  # held while it is silent / jitters


def test_the_rotation_byte_wins_over_the_direction_of_travel_when_they_differ_a_little():
    g = source()
    emit(g, 1, 0, 0)
    assert emit(g, 1, 5, 0, facing=70.0).heading == pytest.approx(70.0)  # travelled east (90), faces 70: the creature turns while it walks


def test_a_rotation_byte_that_contradicts_the_movement_every_time_is_dropped_for_that_creature():
    g = source()
    emit(g, 1, 0, 0)
    for i in range(1, 9):  # walks east, "faces" west every time
        h = emit(g, 1, 5 * i, 0, facing=270.0).heading
    assert 1 in g._yaw_distrust and h == pytest.approx(90.0)  # the direction of travel from then on
    assert emit(g, 1, 45, 0).heading == pytest.approx(90.0)


def test_a_destroyed_creature_forgets_its_facing():
    g = source()
    emit(g, 1, 0, 0, facing=10.0)
    g._drop(1)
    assert 1 not in g._facing and emit(g, 1, 100, 100).heading is None


def test_server_animals_get_no_heading_from_a_rotation_byte():
    assert sp.KIND_AI == "ai"  # the location loop skips them; the diamond has no direction


# ---------------------------------------------------------------- your own arrow
def run_east(facing, speed_ms, seconds, camera, t0=0.0, x0=0.0, y0=0.0, hz=7.0):
    """Walk east (+X) at ``speed_ms`` for ``seconds``; returns (last heading, last x, last t)."""
    t, x, h = t0, x0, None
    n = int(seconds * hz)
    for _ in range(n):
        t += 1.0 / hz
        x += speed_ms * 100.0 / hz
        h = facing.update(x, y0, t, camera)
    return h, x, t


def test_your_arrow_follows_where_you_run_not_where_the_camera_looks():
    from isle_livemap.geo import OwnFacing

    f = OwnFacing()
    h, _x, _t = run_east(f, 9.0, 3.0, camera=0.0)  # running east with the camera looking north
    assert h == pytest.approx(90.0, abs=1.0)


def test_below_walking_pace_it_is_the_camera():
    from isle_livemap.geo import OwnFacing

    f = OwnFacing()
    h, _x, _t = run_east(f, 1.0, 4.0, camera=200.0)  # creeping east at 1 m/s
    assert h == pytest.approx(200.0)


def test_standing_still_it_keeps_the_running_direction_until_the_camera_turns():
    from isle_livemap.geo import OwnFacing

    f = OwnFacing()
    h, x, t = run_east(f, 9.0, 3.0, camera=0.0)
    for i in range(1, 30):  # stopped, camera did not move
        assert f.update(x, 0.0, t + i / 7.0, 0.0) == pytest.approx(90.0, abs=1.0)
    assert f.update(x, 0.0, t + 5.0, 30.0) == pytest.approx(30.0)  # the camera turned 30 degrees: the arrow follows it


def test_a_teleport_or_a_long_gap_starts_over():
    from isle_livemap.geo import OwnFacing

    f = OwnFacing()
    h, x, t = run_east(f, 9.0, 3.0, camera=0.0)
    assert f.update(x + 100_000.0, 0.0, t + 0.14, 10.0) == pytest.approx(10.0)  # 1 km jump: the camera, not a bearing across the map
    h2, _x, _t = run_east(f, 9.0, 2.0, camera=10.0, t0=t + 10.0, x0=x + 100_000.0)  # after a long silence it follows the new run
    assert h2 == pytest.approx(90.0, abs=1.0)


def test_running_in_every_compass_direction_points_the_arrow_that_way():
    from isle_livemap.geo import OwnFacing

    for name, deg in COMPASS.items():
        f, t, x, y = OwnFacing(), 0.0, 0.0, 0.0
        dx, dy = math.sin(math.radians(deg)), -math.cos(math.radians(deg))  # north is -Y
        for _ in range(30):
            t += 1.0 / 7.0
            x += dx * 900.0 / 7.0
            y += dy * 900.0 / 7.0
            h = f.update(x, y, t, (deg + 100.0) % 360.0)  # the camera is always 100 degrees off
        assert circ(h, deg) < 1.0, name
