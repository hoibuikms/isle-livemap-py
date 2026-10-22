"""Entity maths: distance/bearing, radius filtering, sorting, labels, smoothing, settings."""

import math

from isle_livemap.geo import (
    Entity, EntitySmoother, Settings, entity_label_lines, entity_summary, nearby_entities, place_label, truncate,
)


def E(i, kind="ai", x=0.0, y=0.0, **kw):
    return Entity(i, kind, kw.pop("name", i), kw.pop("species", "Sp"), x, y, **kw)


def test_distance_and_bearing_to_entities():
    items = [(E("n", y=-10_000), 0, -10_000), (E("e", x=20_000), 20_000, 0), (E("s", y=30_000), 0, 30_000)]
    got = {n.entity.id: n for n in nearby_entities(items, 0, 0)}
    assert math.isclose(got["n"].distance_m, 100) and math.isclose(got["n"].bearing, 0, abs_tol=1e-6)
    assert math.isclose(got["e"].distance_m, 200) and math.isclose(got["e"].bearing, 90)
    assert math.isclose(got["s"].distance_m, 300) and math.isclose(got["s"].bearing, 180)


def test_radius_filter_sort_and_kind_toggles():
    items = [(E("far", x=150_000), 150_000, 0), (E("mid", "player", 50_000), 50_000, 0),
             (E("near", x=5_000), 5_000, 0), (E("edge", x=100_000), 100_000, 0)]
    res = nearby_entities(items, 0, 0, radius_m=1000)
    assert [n.entity.id for n in res] == ["near", "mid", "edge"]  # 1000 m inclusive, far dropped
    assert [n.entity.id for n in nearby_entities(items, 0, 0, None)][-1] == "far"
    assert [n.entity.id for n in nearby_entities(items, 0, 0, 1000, show_players=False)] == ["near", "edge"]
    assert [n.entity.id for n in nearby_entities(items, 0, 0, 1000, show_ai=False)] == ["mid"]


def test_labels_and_truncation():
    n = nearby_entities([(E("a", "player", 25_000, name="X" * 40, species="Tyrannosaurus", health_pct=92.4), 25_000, 0)], 0, 0)[0]
    head, tail = entity_label_lines(n)
    assert len(head.split(" (")[0]) <= 16 and head.endswith("(Tyrannosaurus)") and "…" in head
    assert tail == "HP 92% G ? 250 m"
    assert "250 m E" in entity_summary(n)
    assert truncate("abcdef", 4) == "abc…" and truncate("ab", 4) == "ab"


def test_place_label_avoids_overlap_and_bounds():
    bounds = (0, 0, 200, 100)
    placed = []
    r1 = place_label(100, 50, 60, 20, placed, bounds)
    r2 = place_label(100, 50, 60, 20, placed, bounds)  # same anchor: must go elsewhere
    assert r1 and r2 and r1 != r2
    assert all(not (a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])
               for i, a in enumerate(placed) for b in placed[i + 1:])
    assert place_label(5, 5, 300, 20, [], bounds) is None  # wider than the view


def test_smoother_interpolates_snaps_and_drops():
    s = EntitySmoother(tau_s=0.4)
    s.update([E("a", x=0, y=0)])
    s.update([E("a", x=1000, y=0), E("b", x=5, y=5)])
    (_, ax, _), (_, bx, _) = s.items()
    assert ax == 0 and bx == 5  # existing eases, new one appears in place
    s.step(0.4)
    ax = dict((e.id, x) for e, x, _ in s.items())["a"]
    assert 500 < ax < 700
    for _ in range(100):
        s.step(0.1)
    assert abs(dict((e.id, x) for e, x, _ in s.items())["a"] - 1000) < 1
    s.update([E("a", x=900_000)])  # teleport > snap distance
    assert s.items()[0][1] == 900_000
    s.update([])
    assert s.items() == []


def test_settings_roundtrip_and_normalise(tmp_path):
    p = tmp_path / "s.json"
    s = Settings(feed_url=" https://h/f ", feed_token="tok", feed_radius_m=2500, feed_interval_s=2, show_ai=False)
    s.save(p)
    back = Settings.load(p)
    assert (back.feed_url, back.feed_token, back.feed_radius_m, back.feed_interval_s) == ("https://h/f", "tok", 2500, 2)
    assert back.show_players and not back.show_ai
    bad = Settings(feed_radius_m="x", feed_interval_s=0.0001).normalise()
    assert bad.feed_radius_m == 1000.0 and bad.feed_interval_s == 0.25
    assert Settings(feed_radius_m=1e9).normalise().feed_radius_m == 20_000
    assert Settings().feed_radius_m == 1000.0 and Settings().feed_url == ""
