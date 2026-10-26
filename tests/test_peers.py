"""Peer source: adapter, store, backends, self-exclusion, static guard (pure Python, no Qt)."""

import ast
import asyncio
import logging
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from isle_livemap import peers
from isle_livemap.geo import Entity, local_entity, vital_lines, visible_entities
from isle_livemap.nearby import nearby_entities as lab_nearby
from isle_livemap.peers import (
    LAB_PLAYER_ID, GamePeerSource, LabPeerSource, PeerStore, SyntheticPeers, create_peer_source, from_record,
)
from labsim import payload_sniffer as ps
from labsim import telemetry as t


def rec(id_=7, x=1.5, y=-2.25, z=300.0, status=42, name="Alpha"):
    return ps.NodeRecord(id_, x, y, z, status, name)


def ent(i, x=0.0, y=0.0, **kw):
    return Entity(str(i), "player", kw.pop("name", f"P{i}"), "", x, y, **kw)


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


# ---- T1
def test_from_record_fields():
    e = from_record(rec(id_=7, x=10.0, y=3.0, z=20.0, status=87, name="Alpha"))
    assert (e.id, e.kind, e.name, e.species) == ("7", "player", "Alpha", "")
    assert e.health_pct == 87.0
    assert (e.growth_pct, e.stamina_pct, e.hunger_pct, e.thirst_pct, e.heading) == (None,) * 5


def test_from_record_clamps_and_rejects():
    assert from_record(rec(status=500)).health_pct == 100.0
    assert from_record(rec(status=0xFFFB)).health_pct == 0.0  # int16 -5 read through the unsigned parser
    assert from_record(rec(x=float("nan"))) is None
    assert from_record(rec(z=float("inf"))) is None
    assert from_record(rec(x=5e9)) is None  # wrong layout / endianness
    assert from_record(NS(id=1)) is None  # not a record at all


def test_name_cleanup_and_fallback():  # T9 (adapter part)
    assert from_record(rec(name="Bob\x00\x00")).name == "Bob"
    assert from_record(rec(id_=9, name="")).name == "Player #9"
    assert from_record(rec(id_=9, name="\x00\x00  ")).name == "Player #9"
    assert "�" in from_record(rec(name="a�b")).name  # replacement char from bad UTF-8 is kept


# ---- T2
def test_real_bytes_round_trip_signed_unsigned_parity():
    n = t.Node(5, 12.5, 4.0, -30.0, 77, "Hươu")
    e = from_record(ps.parse_payload(t.encode(n)))
    assert (e.id, e.name, e.health_pct) == ("5", "Hươu", 77.0)
    neg = from_record(ps.parse_payload(t.encode(t.Node(-1, 0, 0, 0, -5, "n"))))  # signed telemetry vs unsigned sniffer
    assert neg.id == "-1" and neg.health_pct == 0.0


def test_lab_constants_match_labsim():
    assert LAB_PLAYER_ID == t.PLAYER_ID and peers.LAB_PORT == t.PORT


# ---- T3
def test_store_latest_wins_ttl_and_version():
    clk = Clock()
    s = PeerStore(clock=clk)
    s.update(ent(1, x=1))
    s.update(ent(1, x=2))
    snap = s.snapshot()
    assert [p.entity.x for p in snap.entities] == [2] and snap.version == 2
    v = s.version
    clk.t += 1
    s.update(ent(1, x=2))  # identical packet: last_seen refreshes, version does not move
    assert s.version == v and s.entities()[0].last_seen == 101.0
    assert s.snapshot() is s.snapshot()  # cached while nothing changed
    clk.t += 4.9
    assert len(s.entities()) == 1  # 4.9 s since last packet: still alive
    clk.t += 0.2
    assert s.entities() == () and s.version == v + 1  # expired at 5 s, change counted once
    assert s.snapshot().entities == ()


def test_snapshot_is_immutable():
    snap = PeerStore().snapshot()
    with pytest.raises(AttributeError):
        snap.version = 3  # type: ignore[misc]
    assert isinstance(snap.entities, tuple)


def test_store_thread_safety_smoke():
    s = PeerStore()
    stop = threading.Event()

    def writer(base):
        for i in range(2000):
            s.update(ent(base + i % 50, x=i))

    def reader():
        while not stop.is_set():
            s.snapshot()

    ws = [threading.Thread(target=writer, args=(b,)) for b in (0, 1000, 2000)]
    r = threading.Thread(target=reader)
    r.start()
    [w.start() for w in ws]
    [w.join() for w in ws]
    stop.set()
    r.join()
    assert len(s.entities()) == 150


# ---- T4
def test_axes_and_units_match_nearby_radar():
    e = from_record(rec(x=10.0, y=3.0, z=20.0))  # lab: x, y = height, z
    assert (e.x, e.y, e.z) == (1000.0, 2000.0, pytest.approx(300.0))
    raw = {"id": 7, "pos": {"x": 10.0, "y": 3.0, "z": 20.0}}
    via_radar = lab_nearby({"x": 0.0, "y": 0.0, "z": 0.0}, [raw], 1e6)[0]
    mine = visible_entities([(e, e.x, e.y)], 0.0, 0.0)[0]
    assert mine.distance_m == pytest.approx(via_radar.distance_m)
    assert mine.bearing == pytest.approx(via_radar.bearing)


# ---- T5
def test_self_id_zero_never_enters_entities():
    src = LabPeerSource(port=1)
    src._on_record(rec(id_=LAB_PLAYER_ID, name="me"))
    src._on_record(rec(id_=3))
    assert [p.id for p in src.snapshot().entities] == ["3"]
    assert src.me is not None and src.me.name == "me"
    s = PeerStore()  # the generic store only knows "local"; id 0 is the lab's self id, added by the lab backends
    assert s.update(ent("local")) is False and s.entities() == ()
    s.add_self_id(LAB_PLAYER_ID)
    assert s.update(ent(0)) is False and s.entities() == ()
    assert SyntheticPeers(lambda: None).store.update(ent(0)) is False


def test_peer_self_ids_and_signed_unsigned_spelling():
    s = PeerStore(self_ids=["-1"])
    assert s.update(from_record(rec(id_=0xFFFFFFFF))) is False  # same id as -1
    assert s.update(from_record(rec(id_=8))) is True
    s.set_self_ids(["8"])
    assert s.entities() == ()


def test_same_coordinates_different_id_is_kept():
    mine = ent(0, 5.0, 5.0)
    other = ent(9, 5.0, 5.0)
    got = visible_entities([(mine, 5.0, 5.0), (other, 5.0, 5.0)], 5.0, 5.0, me_id="0")
    assert [n.entity.id for n in got] == ["9"]


def test_provisional_peer_purged_when_own_handle_locks():
    s = PeerStore()
    s.update(ent(4242))
    s.update(ent(7))
    v = s.version
    s.add_self_id(4242)
    assert [p.id for p in s.entities()] == ["7"] and s.version > v
    assert s.update(ent(4242)) is False


# ---- backends
def test_game_without_pipeline_returns_nothing_and_says_so(caplog):
    g = GamePeerSource()
    with caplog.at_level(logging.WARNING, logger="isle_livemap.peers"):
        g.start()
    snap = g.snapshot()
    assert snap.entities == () and snap.enabled and "no capture pipeline" in snap.status
    assert "no capture pipeline" in caplog.text and peers.status_is_warning(snap.status)


def test_factory():
    assert create_peer_source("off") is None and create_peer_source("bogus") is None
    assert isinstance(create_peer_source("lab"), LabPeerSource)
    assert isinstance(create_peer_source("game"), GamePeerSource)
    assert isinstance(create_peer_source("off", synthetic=True), SyntheticPeers)


def test_no_user_facing_source_setting():
    from isle_livemap.geo import Settings
    assert not hasattr(Settings(), "peer_source")


def test_game_peers_flag_is_gone():
    from isle_livemap.__main__ import main
    with pytest.raises(SystemExit) as ei:
        main(["--game-peers"])
    assert ei.value.code == 2


def test_game_status_waiting_texts_and_failing_pipeline_never_raises():
    g = GamePeerSource(pipeline=object())  # object() has no add_observer: attaching must fail softly
    g.start()
    assert "unavailable" in g.status() and peers.status_is_warning(g.status())
    class P:
        def add_observer(self, *a): pass
        def remove_observer(self, *a): pass
    g2 = GamePeerSource(pipeline=P())
    g2.start()
    assert g2.status() == "game: waiting for server traffic"
    g2.stop()


def test_synthetic_peers_exercise_self_id_name_and_ttl():
    clk = Clock(0.0)
    where = [None]
    s = SyntheticPeers(lambda: where[0], clock=clk)
    assert s.snapshot().entities == () and "waiting" in s.snapshot().status
    where[0] = (0.0, 0.0)
    clk.t = 0.6
    snap = s.snapshot()
    ids = {p.id for p in snap.entities}
    assert str(LAB_PLAYER_ID) not in ids and {"101", "105", "106", "107"} <= ids  # self entry dropped
    assert next(p for p in snap.entities if p.id == "105").entity.name == ""  # shown as "Player #105" by the label
    assert next(p for p in snap.entities if p.id == "106").entity.heading is not None
    clk.t = 15.0  # ghost is silent from t=14 s...
    s.snapshot()
    clk.t = 21.0  # ...and gone after the 5 s TTL
    assert "107" not in {p.id for p in s.snapshot().entities}


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as k:
        k.bind(("127.0.0.1", 0))
        return k.getsockname()[1]


def _run_simulator(port, seconds):
    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(t.simulator(stop, port=port))
        await asyncio.sleep(seconds)
        stop.set()
        await task

    asyncio.run(go())


def test_lab_source_loopback_end_to_end():  # core of T12
    port = _free_port()
    src = LabPeerSource(port)
    src.start()
    try:
        th = threading.Thread(target=_run_simulator, args=(port, 0.7))
        th.start()
        th.join()
        deadline = time.monotonic() + 2
        while len(src.snapshot().entities) < 8 and time.monotonic() < deadline:
            time.sleep(0.02)
        snap = src.snapshot()
    finally:
        src.stop()
    assert sorted(p.id for p in snap.entities) == [str(i) for i in range(1, 9)]  # 8 peers, "me" (0) excluded
    assert src.me is not None and "listening" in snap.status and "0 malformed" in snap.status
    assert all(p.entity.name.startswith("Node_") for p in snap.entities)


def test_lab_source_port_in_use_reports_error_without_raising():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as hog:
        hog.bind(("127.0.0.1", 0))
        src = LabPeerSource(hog.getsockname()[1])
        src.start()  # must not raise
        snap = src.snapshot()
        src.stop()
    assert snap.entities == () and snap.enabled and "unavailable" in snap.status and peers.status_is_warning(snap.status)


def test_lab_source_counts_rejected_records():
    src = LabPeerSource(port=1)
    src._on_record(rec(x=float("nan")))
    assert src.rejected == 1 and src.snapshot().entities == ()


# ---- T8 (vitals parity, pure part)
def test_vitals_parity_with_local_player():
    from isle_livemap.tracking import Vitals

    me = local_entity(NS(x=0.0, y=0.0, z=0.0), Vitals(health=50, max_health=200))
    other = from_record(rec(status=25))
    assert other.health_pct == me.health_pct == 25.0
    assert vital_lines(other) == vital_lines(me) == ["Health 25%"]


# ---- T15
FORBIDDEN_NAMES = {"FeedPoller", "DemoFeed", "parse_feed", "resolve_feed_config", "feed_url", "feed_token",
                   "feed_interval_s", "ISLE_FEED_TOKEN", "ISLE_FEED_URL"}
ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("rel", ["isle_livemap/peers.py", "isle_livemap/overlay.py", "isle_livemap/app.py",
                                 "isle_livemap/__main__.py", "isle_livemap/nearby.py"])
def test_no_feed_or_http_dependency(rel):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [("." * node.level) + (node.module or "")] + [a.name for a in node.names]
        else:
            mods = []
        for m in mods:
            assert m.split(".")[-1] != "feed" and not m.startswith(("urllib", "http", "requests")), (rel, m)
        names = {node.id} if isinstance(node, ast.Name) else {node.attr} if isinstance(node, ast.Attribute) else set()
        assert not names & FORBIDDEN_NAMES, (rel, names)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 60:
            assert "--feed" not in node.value and "ISLE_FEED" not in node.value, (rel, node.value)


def test_geo_has_no_feed_import_or_network():
    tree = ast.parse((ROOT / "isle_livemap/geo.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            text = ast.unparse(node)
            assert "feed" not in text and "urllib" not in text, text
