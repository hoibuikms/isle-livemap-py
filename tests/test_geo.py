"""Projection (ports of GatewayMapProjectionTests/MapHeadingTests), rings,
markers/waypoints (distance, bearing, JSON persistence), settings, smoothing."""

from __future__ import annotations

import json
import math

import pytest

from isle_livemap.decode import map_heading_from_unreal_yaw
from isle_livemap.geo import (
    DEFAULT_RING_RADII_M, Interpolator, MarkerStore, Settings, bearing_deg, build_rings, clamp_size_percent,
    compass_label, distance_m, format_distance, load_zones, parse_radii, project, project_unclamped, shortest_arc,
    unproject,
)

# ------------------------------------------------------------------ projection


def test_known_world_centre_maps_to_image_centre():
    left, top = project(51_000, -49_000)
    assert left == pytest.approx(0.5, abs=1e-8) and top == pytest.approx(0.5, abs=1e-8)


def test_unproject_round_trips():
    x, y = -329_555.776, 112_985.442
    rx, ry = unproject(*project(x, y))
    assert rx == pytest.approx(x, abs=1e-6) and ry == pytest.approx(y, abs=1e-6)


@pytest.mark.parametrize("x,y,l,t", [(-9e6, -9e6, 0, 0), (9e6, 9e6, 1, 1)])
def test_project_clamps(x, y, l, t):
    assert project(x, y) == (l, t)


def test_project_unclamped_keeps_out_of_bounds():
    left, top = project_unclamped(9_000_000, -9_000_000)
    assert left > 1 and top < 0


def test_unproject_non_finite_goes_to_centre():
    assert unproject(float("nan"), float("inf")) == unproject(0.5, 0.5)


@pytest.mark.parametrize("yaw,expected", [(0, 90), (90, 180), (180, 270), (270, 0), (-90, 0)])
def test_map_heading_from_unreal_yaw(yaw, expected):
    assert map_heading_from_unreal_yaw(yaw) == pytest.approx(expected, abs=1e-8)


# ------------------------------------------------------------------ distance / bearing


def test_distance_in_metres_from_centimetre_units():
    assert distance_m(0, 0, 30_000, 40_000) == pytest.approx(500.0)


@pytest.mark.parametrize(
    "dx,dy,expected",
    [(0, -1000, 0), (1000, 0, 90), (0, 1000, 180), (-1000, 0, 270), (1000, -1000, 45), (-1000, 1000, 225)],
)
def test_bearing_north_is_minus_y(dx, dy, expected):
    assert bearing_deg(5_000, 5_000, 5_000 + dx, 5_000 + dy) == pytest.approx(expected, abs=1e-9)


def test_bearing_agrees_with_screen_projection():
    # a point projected up-right of the player must have a bearing in (0, 90)
    px, py = 0.0, 0.0
    tx, ty = 20_000.0, -20_000.0
    (l0, t0), (l1, t1) = project(px, py), project(tx, ty)
    assert l1 > l0 and t1 < t0
    assert 0 < bearing_deg(px, py, tx, ty) < 90


@pytest.mark.parametrize("b,label", [(0, "N"), (44, "NE"), (90, "E"), (200, "S"), (359, "N"), (292, "W")])
def test_compass_label(b, label):
    assert compass_label(b) == label


@pytest.mark.parametrize("m,text", [(0, "0 m"), (250, "250 m"), (999.6, "1000 m"), (1000, "1 km"), (1500, "1.5 km"), (12_400, "12 km")])
def test_format_distance(m, text):
    assert format_distance(m) == text


# ------------------------------------------------------------------ rings


def test_default_rings_are_250_500_1000():
    rings = build_rings()
    assert [r.radius_m for r in rings] == [250, 500, 1000] == list(DEFAULT_RING_RADII_M)
    assert [r.label for r in rings] == ["250 m", "500 m", "1 km"]


def test_ring_radius_matches_projection_scale():
    ring = build_rings([500])[0]
    # a point exactly 500 m east of the player lands on the ring's right edge
    px, py = 51_000.0, -49_000.0
    l0, t0 = project_unclamped(px, py)
    l1, t1 = project_unclamped(px + 500 * 100, py)
    assert l1 - l0 == pytest.approx(ring.rx, abs=1e-12)
    l2, t2 = project_unclamped(px, py + 500 * 100)
    assert t2 - t0 == pytest.approx(ring.ry, abs=1e-12)


def test_rings_scale_linearly_dedupe_and_drop_invalid():
    rings = build_rings([1000, 250, 250, -5, 0, float("nan"), 500])
    assert [r.radius_m for r in rings] == [250, 500, 1000]
    assert rings[2].rx == pytest.approx(4 * rings[0].rx)


def test_parse_radii():
    assert parse_radii("250, 500, 1km") == [250, 500, 1000]
    assert parse_radii("1.5 km; 300m") == [1500, 300]
    assert parse_radii("abc, , -4, 0") == []


# ------------------------------------------------------------------ markers / waypoints


def test_marker_relative_to_player(tmp_path):
    store = MarkerStore(tmp_path / "m.json")
    m = store.add("Nest", "nest", 30_000, -40_000)  # 300 m east, 400 m north of origin
    d, b = m.relative_to(0, 0)
    assert d == pytest.approx(500.0)
    assert b == pytest.approx(math.degrees(math.atan2(3, 4)), abs=1e-9)  # east of north
    assert "500 m" in m.describe(0, 0) and "NE" in m.describe(0, 0)


def test_marker_json_round_trip(tmp_path):
    path = tmp_path / "sub" / "markers.json"
    s = MarkerStore(path)
    a = s.add("Water hole", "water", 1000.5, -2000.25, notes="east pond")
    b = s.add("Way", "waypoint", 5, 6)
    again = MarkerStore(path)
    assert [(m.id, m.name, m.kind, m.x, m.y, m.notes) for m in again.markers] == [
        (a.id, "Water hole", "water", 1000.5, -2000.25, "east pond"),
        (b.id, "Way", "waypoint", 5, 6, ""),
    ]
    assert json.loads(path.read_text())["version"] == 1


def test_marker_remove_persists(tmp_path):
    path = tmp_path / "m.json"
    s = MarkerStore(path)
    a, b = s.add("a", "other", 0, 0), s.add("b", "nest", 1, 1)
    assert s.remove(a.id) and not s.remove("nope")
    assert [m.id for m in MarkerStore(path).markers] == [b.id]


def test_multiple_waypoints_kept_independent(tmp_path):
    s = MarkerStore(tmp_path / "m.json")
    for i in range(5):
        s.add(f"wp{i}", "waypoint", i * 1000, 0)
    assert len([m for m in s.markers if m.kind == "waypoint"]) == 5
    assert len({m.id for m in s.markers}) == 5


def test_marker_store_tolerates_corrupt_and_bad_entries(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{ not json")
    assert MarkerStore(path).markers == []
    path.write_text(json.dumps({"markers": [{"name": "ok", "x": 1, "y": 2, "kind": "weird"}, {"name": "bad"}, {"name": "nan", "x": "nan", "y": 0}, 7]}))
    ms = MarkerStore(path).markers
    assert len(ms) == 1 and ms[0].kind == "other"


def test_marker_unknown_kind_defaults_and_blank_name(tmp_path):
    s = MarkerStore(tmp_path / "m.json")
    m = s.add("  ", "zzz", 0, 0)
    assert m.kind == "other" and m.name == "Other"


def test_marker_limit(tmp_path):
    s = MarkerStore(tmp_path / "m.json")
    s.MAX_MARKERS = 2
    s.add("1", "other", 0, 0)
    s.add("2", "other", 0, 0)
    with pytest.raises(ValueError):
        s.add("3", "other", 0, 0)


# ------------------------------------------------------------------ settings


@pytest.mark.parametrize("v,expected", [(10, 65), (65, 65), (100, 100), (175, 175), (900, 175)])
def test_size_clamped_to_65_175(v, expected):
    assert clamp_size_percent(v) == expected


def test_settings_round_trip_and_normalise(tmp_path):
    p = tmp_path / "s.json"
    Settings(size_percent=500, zoom=99, ring_radii_m=[100, 2000], species="Triceratops").save(p)
    s = Settings.load(p)
    assert s.size_percent == 175 and s.zoom == 16.0 and s.ring_radii_m == [100, 2000] and s.species == "Triceratops"


def test_settings_missing_or_corrupt_file_gives_defaults(tmp_path):
    assert Settings.load(tmp_path / "none.json").size_percent == 100
    (tmp_path / "bad.json").write_text("[1,2")
    assert Settings.load(tmp_path / "bad.json").ring_radii_m == list(DEFAULT_RING_RADII_M)


# ------------------------------------------------------------------ smoothing


def test_shortest_arc_wraps():
    assert shortest_arc(350, 10) == 20 and shortest_arc(10, 350) == -20


def test_interpolator_converges_and_is_frame_rate_independent():
    def run(fps):
        it = Interpolator(tau_s=0.09)
        it.set_target(0, 0, 0)
        it.set_target(1000, 0, 90)
        for _ in range(int(fps * 0.2)):
            it.step(1 / fps)
        return it.x, it.heading

    x60, h60 = run(60)
    x120, h120 = run(120)
    assert x60 == pytest.approx(x120, rel=1e-6)
    assert 0 < x60 < 1000 and 0 < h60 < 90
    it = Interpolator()
    it.set_target(0, 0, 350)
    it.set_target(0, 0, 10)
    it.step(0.05)
    assert it.heading > 350 or it.heading < 10  # went the short way through north


def test_interpolator_snaps_on_teleport():
    it = Interpolator()
    it.set_target(0, 0, 0)
    it.set_target(500_000, 0, 0)
    assert it.step(1 / 60)[0] == 500_000


# ------------------------------------------------------------------ bundled data


def test_bundled_zones_counts():
    z = load_zones()
    assert len(z["migration"]) == 12 and len(z["patrol"]) == 61 and len(z["food"]) == 13
    for layer in ("migration", "patrol"):
        for zone in z[layer]:
            assert len(zone["points"]) >= 3
            assert all(0 <= a <= 1 and 0 <= b <= 1 for a, b in zone["points"])
