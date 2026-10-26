"""Entity maths: distance/bearing, radius filtering, sorting, labels, smoothing, settings."""

import json
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
    assert len(head.split(" - ")[0]) <= 16 and head == "XXXXXXXXXXXXXXX… - Tyrannosaurus - 250 m"
    assert tail == "HP 92%  ST –"  # growth unknown: omitted; health points and stamina were never read: en dashes
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


def test_place_label_prefers_centred_under_then_above_right_left():
    bounds = (0, 0, 200, 100)
    placed = []
    assert place_label(100, 40, 60, 10, placed, bounds) == (70, 48, 60, 10)  # centred, just under the dot
    assert place_label(100, 40, 60, 10, placed, bounds) == (70, 22, 60, 10)  # under taken: above
    assert place_label(100, 40, 60, 10, placed, bounds) == (108, 35, 60, 10)  # both taken: right
    assert place_label(100, 40, 60, 10, placed, bounds) == (32, 35, 60, 10)  # then left


def test_place_label_clamps_centred_text_into_bounds():
    bounds = (0, 0, 200, 100)
    assert place_label(5, 40, 60, 20, [], bounds) == (0, 48, 60, 20)
    assert place_label(198, 40, 60, 20, [], bounds) == (140, 48, 60, 20)
    # no room below: falls back to above
    assert place_label(100, 90, 60, 20, [], bounds) == (70, 62, 60, 20)


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
    s = Settings(peer_radius_m=2500, peer_self_ids=["7", "7", "3"], show_ai=False)
    s.save(p)
    back = Settings.load(p)
    assert (back.peer_radius_m, back.peer_self_ids) == (2500, ["3", "7"])
    assert back.show_players and not back.show_ai
    bad = Settings(peer_radius_m="x").normalise()
    assert bad.peer_radius_m == 1000.0
    assert Settings(peer_radius_m=1e9).normalise().peer_radius_m == 20_000
    assert Settings(peer_radius_m=1).normalise().peer_radius_m == 50
    assert Settings().peer_radius_m == 1000.0
    assert not any(k.startswith("feed_") for k in json.loads(p.read_text()))  # no legacy keys are written


def test_summary_shows_vitals_and_height_delta():
    e = E("p", "player", 25_000, stamina_pct=70, hunger_pct=60, thirst_pct=80, z=13_000)
    n = nearby_entities([(e, 25_000, 0)], 0, 0)[0]
    s = entity_summary(n, my_z=10_000)
    assert "ST 70% HU 60% TH 80%" in s and "▲30 m" in s
    bare = entity_summary(nearby_entities([(E("q", x=25_000), 25_000, 0)], 0, 0)[0], my_z=10_000)
    assert "ST" not in bare and "▲" not in bare and "▼" not in bare


def test_row_and_3d_distance():
    from isle_livemap.geo import distance_3d_m, entity_row
    e = E("p", "player", 30_000, name="Alpha", species="Rex", health_pct=100, growth_pct=80, z=14_000)
    n = nearby_entities([(e, 30_000, 0)], 0, 0)[0]  # 300 m east, 40 m up
    assert math.isclose(distance_3d_m(n, 10_000), 50 * 0 + math.hypot(300, 40))
    row = entity_row(n, my_z=10_000)
    assert row[0] == "Alpha" and row[2] == "100%" and row[3] == "80%" and row[-1] == "▲40 m higher"
    assert entity_row(n)[-1] == "?" and distance_3d_m(n, None) == n.distance_m


def test_label_shows_height_delta():
    e = E("p", "player", 25_000, name="Alpha", z=13_000)
    n = nearby_entities([(e, 25_000, 0)], 0, 0)[0]
    # the minimap label has distance only; compass direction and height are in the summary (tooltips, logs) and the table
    assert "▲" not in " ".join(entity_label_lines(n, my_z=10_000)) and " E " not in entity_label_lines(n, my_z=10_000)[0]
    assert entity_summary(n, my_z=10_000).endswith("250 m E (90°) ▲30 m")
    assert "▲" not in entity_summary(n)
    below = nearby_entities([(E("q", "player", 25_000, name="Beta", z=7_000), 25_000, 0)], 0, 0)[0]
    assert entity_summary(below, my_z=10_000).endswith("250 m E (90°) ▼30 m")
    level = nearby_entities([(E("r", "player", 25_000, name="Gamma", z=10_020), 25_000, 0)], 0, 0)[0]
    assert entity_summary(level, my_z=10_000).endswith("250 m E (90°) level")


def test_local_player_shares_entity_path():
    from types import SimpleNamespace as NS
    from isle_livemap.geo import entity_row, local_entity, vital_lines
    from isle_livemap.tracking import Vitals
    pos = NS(x=1000.0, y=-2000.0, z=5000.0)
    v = Vitals(growth=0.5, health=50, max_health=200, stamina=10, max_stamina=10, hunger=None, max_hunger=100)
    me = local_entity(pos, v, "Rex")
    assert (me.x, me.y, me.z, me.growth_pct, me.health_pct, me.stamina_pct) == (1000.0, -2000.0, 5000.0, 50.0, 25.0, 100.0)
    assert me.hunger_pct is None and me.thirst_pct is None
    assert vital_lines(me) == ["Growth 50%", "Health 25%", "Stamina 100%"]
    # a peer entity with the same values renders identically
    twin = E("t", "player", 1000.0, name="You", species="Rex", growth_pct=50.0, health_pct=25.0, stamina_pct=100.0)
    assert vital_lines(twin) == vital_lines(me)
    n = nearby_entities([(twin, 1000.0, 0)], 0, 0)[0]
    assert entity_row(n)[2:7] == ("25%", "50%", "100%", "?", "?")
    assert local_entity(None, None).health_pct is None and vital_lines(local_entity(None, None)) == []


def test_peer_vitals_use_the_same_decoder_as_the_local_player():
    from types import SimpleNamespace as NS
    from isle_livemap.geo import local_entity, vital_pct
    from isle_livemap.tracking import Vitals
    me = local_entity(NS(x=0.0, y=0.0, z=0.0), Vitals(health=37.0, max_health=100.0, growth=0.5))
    peer = E("a", "player", health_pct=vital_pct(37.0, 100.0), growth_pct=vital_pct(0.5, 1.0))
    assert (peer.health_pct, peer.growth_pct) == (me.health_pct, me.growth_pct) == (37.0, 50.0)
    assert vital_pct(-5, 100.0) == 0.0 and vital_pct(500, 100.0) == 100.0  # clamped
    assert vital_pct(None, 100.0) is None
