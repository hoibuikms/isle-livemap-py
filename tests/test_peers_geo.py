"""geo.py additions for other players: visible_entities, smoother TTL/heading, edge_anchor, settings."""

import json
import math

import pytest

from isle_livemap.geo import (
    ENTITY_STYLE, PEER_STALE_S, PEER_TTL_S, Entity, EntitySmoother, Settings, canonical_id, edge_anchor,
    entity_label_lines, is_self, local_entity, nearby_3d, nearby_entities, peer_label_lines, shortest_arc,
    visible_entities,
)
from isle_livemap.peers import PeerEntity


def E(i, x=0.0, y=0.0, kind="player", **kw):
    return Entity(str(i), kind, kw.pop("name", f"P{i}"), "", x, y, **kw)


def items(*es):
    return [(e, e.x, e.y) for e in es]


# ---- T6
def test_visible_entities_filters_orders_and_matches_nearby_entities():
    es = [E("far", 150_000), E("mid", 50_000), E("near", 5_000), E("edge", 100_000), E("ai", 1_000, kind="ai")]
    got = visible_entities(items(*es), 0, 0, 1000)
    assert [n.entity.id for n in got] == ["ai", "near", "mid", "edge"]  # 1000 m inclusive in 2D, far dropped
    assert [n.entity.id for n in got] == [n.entity.id for n in nearby_entities(items(*es), 0, 0, 1000)]
    assert [n.entity.id for n in visible_entities(items(*es), 0, 0, None)][-1] == "far"
    assert [n.entity.id for n in visible_entities(items(*es), 0, 0, 1000, show_players=False)] == ["ai"]
    assert [n.entity.id for n in visible_entities(items(*es), 0, 0, 1000, show_ai=False)] == ["near", "mid", "edge"]


def test_visible_entities_and_nearby_3d_agree_in_3d():
    me = local_entity(type("P", (), {"x": 0.0, "y": 0.0, "z": 0.0})(), None)
    es = [E("a", 3_000, z=0.0), E("b", 2_000, z=4_000.0), E("c", 10_000), E("edge", 10_000)]
    by_3d = nearby_3d(me, es, 100.0)  # strict: c and edge sit exactly at 100 m
    assert [n.entity.id for n in by_3d] == ["a", "b"]
    mine = visible_entities(items(*es), 0, 0, 100.0, my_z=0.0, use_3d=True)
    assert [n.entity.id for n in mine] == [n.entity.id for n in by_3d]
    assert [n.distance_m for n in mine] == [n.distance_m for n in by_3d]


def test_self_filter_local_me_id_and_configured_ids():
    es = [E("local", 1), E("me", 2), E("7", 3), E("-1", 4), E("ok", 5)]
    got = visible_entities(items(*es), 0, 0, me_id="me", self_ids=["7", "4294967295"])  # unsigned spelling of -1
    assert [n.entity.id for n in got] == ["ok"]
    me = local_entity(None, None)
    assert [n.entity.id for n in nearby_3d(me, [E("local"), E("x")], 1e9)] == ["x"]  # id "local" is self
    assert [n.entity.id for n in nearby_3d(me, [E("9"), E("x")], 1e9, self_ids=["9"])] == ["x"]
    assert is_self("local") and is_self("5", self_ids={"5"}) and not is_self("5")
    assert canonical_id(" 4294967295 ") == "-1" and canonical_id("abc") == "abc"


def test_visible_entities_age_ttl_and_stale_flag():
    e = E("a", 100)
    got = visible_entities([(e, 100, 0, 1.0), (E("b", 200), 200, 0, PEER_STALE_S), (E("c", 300), 300, 0, PEER_TTL_S)], 0, 0)
    assert [(n.entity.id, n.stale) for n in got] == [("a", False), ("b", True)]  # c is past the TTL


# ---- T7
def test_smoother_ttl_stale_reappear_teleport():
    s = EntitySmoother(tau_s=0.4)
    a = E("a", 0)
    s.update([PeerEntity(a, 0.0)], now=0.0)
    s.update([], now=3.0)  # a missed update does not drop it
    ((e, x, y, age),) = s.items_aged(3.0)
    assert e.id == "a" and age == 3.0 and age >= PEER_STALE_S
    s.update([], now=4.9)
    assert len(s.items_aged(4.9)) == 1
    s.update([], now=5.0)
    assert s.items_aged(5.0) == [] and s.items() == []
    # reappearing inside the TTL resets the age without a position jump
    s.update([PeerEntity(E("b", 0), 10.0)], now=10.0)
    s.update([PeerEntity(E("b", 1000), 14.0)], now=14.0)
    ((_, bx, _, age),) = s.items_aged(14.0)
    assert bx == 0 and age == 0.0
    s.step(0.4)
    assert 500 < s.items()[0][1] < 700
    s.update([PeerEntity(E("b", 900_000), 14.5)], now=14.5)  # teleport > 200 000 units
    assert s.items()[0][1] == 900_000


def test_smoother_heading_shortest_arc_and_none():
    s = EntitySmoother(tau_s=0.15)
    s.update([E("a", heading=350.0)])
    s.update([E("a", heading=10.0)])
    s.step(0.15)
    h = s.items_aged(0.0 + 0)[0][0].heading if s.last_seen["a"] == 0.0 else None
    assert h is not None and abs(shortest_arc(h, 350 + 20 * (1 - math.exp(-1)))) < 0.5  # went through 0, not 180
    assert not 20 < h < 340
    s.update([E("a")])  # heading gone: no arrow
    assert s.items_aged(0.0)[0][0].heading is None


def test_entity_heading_normalised():
    assert Entity("a", "player", "", "", 0, 0, heading=-90.0).heading == 270.0
    assert Entity("a", "player", "", "", 0, 0, heading=float("nan")).heading is None
    assert Entity("a", "player", "", "", 0, 0).heading is None


# ---- T8 / T9
def test_labels_unknown_fields_print_question_mark_and_fallbacks():
    n = visible_entities(items(E("12", 25_000, name="", health_pct=40.0)), 0, 0)[0]
    head, tail = entity_label_lines(n)
    assert head == "12" and tail == "HP 40% G ? 250 m"  # geo falls back to the id; peers.from_record uses "Player #id"
    assert peer_label_lines(n) == [head, tail]
    detail = peer_label_lines(n, detail=True)
    assert detail[2] == "Health 40%" and len(detail) == 3
    bare = visible_entities(items(E("1", 10)), 0, 0)[0]
    assert peer_label_lines(bare, detail=True) == list(entity_label_lines(bare))  # no vitals: no detail line
    long = visible_entities(items(E("1", 10, name="X" * 40)), 0, 0)[0]
    assert len(entity_label_lines(long)[0]) <= 16 and entity_label_lines(long)[0].endswith("…")


# ---- T10
def test_edge_anchor():
    x, y = edge_anchor(100, 100, 400, 500, 50)
    assert math.hypot(x - 100, y - 100) == pytest.approx(50)
    assert math.atan2(y - 100, x - 100) == pytest.approx(math.atan2(400, 300))
    assert edge_anchor(0, 0, 10, 0, 5) == (5, 0)
    assert edge_anchor(3, 3, 3, 3, 5) is None
    assert edge_anchor(0, 0, float("nan"), 0, 5) is None


def test_style_table_keeps_self_distinct_and_bigger():
    assert ENTITY_STYLE["self"].color != ENTITY_STYLE["player"].color
    assert ENTITY_STYLE["self"].radius > ENTITY_STYLE["player"].radius


# ---- T11
def test_settings_roundtrip_and_migration(tmp_path):
    p = tmp_path / "s.json"
    s = Settings(peer_source="lab", peer_radius_m=2500, peer_self_ids=[" 5 ", "4294967295", "5"], show_vitals_detail=True)
    s.save(p)
    back = Settings.load(p)
    assert (back.peer_source, back.peer_radius_m, back.show_vitals_detail) == ("lab", 2500.0, True)
    assert back.peer_self_ids == ["-1", "5"]
    assert Settings(peer_radius_m="x").normalise().peer_radius_m == 1000.0
    assert Settings(peer_radius_m=1e9).normalise().peer_radius_m == 20_000
    assert Settings(peer_self_ids="oops").normalise().peer_self_ids == []


def test_old_settings_file_migrates_feed_radius_and_ignores_unknown_keys(tmp_path):
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"feed_url": "http://x/", "feed_token": "t", "feed_radius_m": 750, "zoom": 2.0,
                             "some_removed_key": 1}))
    s = Settings.load(p)
    assert s.peer_radius_m == 750.0 and s.zoom == 2.0 and s.peer_source == "off"
    p.write_text(json.dumps({"feed_radius_m": 750, "peer_radius_m": 300}))
    assert Settings.load(p).peer_radius_m == 300.0  # an explicit new value wins
