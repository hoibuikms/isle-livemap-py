"""Headless render tests for other players on the minimap (offscreen Qt)."""

import os
import socket
import threading
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from isle_livemap.geo import Entity, MarkerStore, Settings  # noqa: E402
from isle_livemap.peers import LabPeerSource, PeerEntity, PeerSnapshot  # noqa: E402


class FakePeers:
    def __init__(self, entries=(), status="fake: ok", enabled=True):
        self.entries, self.status, self.enabled, self.version = list(entries), status, enabled, 1

    def set(self, entries):
        self.entries, self.version = list(entries), self.version + 1

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot(self):
        return PeerSnapshot(tuple(self.entries), self.version, self.status, self.enabled)


def make_view(tmp_path, peers=None, fix=True, settings=None):
    from PySide6.QtWidgets import QApplication

    from isle_livemap.capture import TelemetryPipeline
    from isle_livemap.overlay import MapView
    from isle_livemap.replay import SyntheticSource

    QApplication.instance() or QApplication([])
    pipe = TelemetryPipeline()
    if fix:
        for d in SyntheticSource().datagrams(10.0, t0=time.monotonic() - 10.0):  # on the live clock: the fix is fresh
            pipe.handle(d)
    view = MapView(pipe, MarkerStore(tmp_path / "m.json"), settings or Settings(), peers)
    view.tick()
    return view


def peer(view, i, dx_m=0.0, dy_m=0.0, age=0.0, **kw):
    e = Entity(str(i), "player", kw.pop("name", f"P{i}"), "", view.interp.x + dx_m * 100, view.interp.y + dy_m * 100,
               30_000.0, health_pct=kw.pop("hp", 80.0), **kw)
    return PeerEntity(e, time.monotonic() - age)


def tick_twice(view):
    view.tick()
    time.sleep(0.01)
    view.tick()


def px(view, x, y):
    from isle_livemap.geo import project_unclamped

    pt = view.to_screen(*project_unclamped(x, y))
    return int(pt.x()), int(pt.y())


# ---- T13
def test_peers_in_view_are_listed_and_rendered(tmp_path):
    view = make_view(tmp_path)
    fp = FakePeers()
    view.peers = fp
    fp.set([peer(view, 1, 100, 0), peer(view, 2, 0, -300, hp=None)])
    tick_twice(view)
    got = view.nearby()
    assert [n.entity.id for n in got] == ["1", "2"] and view.peer_status == "fake: ok"
    img = view.grab().toImage()
    assert not img.isNull()
    # the marker is the purple peer arrow: blue clearly above red, green low at the peer's pixel
    x, y = px(view, got[0].x, got[0].y)
    c = img.pixelColor(x, y)
    assert c.blue() > 150 and c.red() > 100 and c.green() < 120


def test_local_arrow_is_drawn_over_a_peer_at_the_same_spot(tmp_path):
    view = make_view(tmp_path)
    fp = FakePeers([peer(view, 1, 0, 0)])
    view.peers = fp
    tick_twice(view)
    c = view.grab().toImage().pixelColor(view.width() // 2, view.height() // 2 + 2)
    assert c.red() > 200 and c.blue() < 120  # red local arrow on top


def test_stale_peer_is_dimmed_and_expired_peer_is_gone(tmp_path):
    view = make_view(tmp_path)
    fp = FakePeers([peer(view, 1, 100, 0, age=0.0), peer(view, 2, -100, 0, age=3.5), peer(view, 3, 0, 100, age=0.0)])
    view.peers = fp
    tick_twice(view)
    got = {n.entity.id: n for n in view.nearby()}
    assert not got["1"].stale and got["2"].stale
    img = view.grab().toImage()
    fresh = img.pixelColor(*px(view, got["1"].x, got["1"].y))
    dim = img.pixelColor(*px(view, got["2"].x, got["2"].y))
    assert fresh.blue() > dim.blue()
    later = time.monotonic() + 2.0  # peer 2 is now 5.5 s old: removed
    assert "2" not in {n.entity.id for n in view.nearby(now=later)}


def test_off_view_peer_gets_edge_indicator_and_beyond_radius_gets_nothing(tmp_path):
    s = Settings(zoom=4.0, peer_radius_m=1000.0)  # zoom 4: the minimap edge is ~280 m from you
    view = make_view(tmp_path, settings=s)
    fp = FakePeers([peer(view, 1, 0, -600), peer(view, 2, 0, -5000)])
    view.peers = fp
    tick_twice(view)
    assert [n.entity.id for n in view.nearby()] == ["1"]
    n = view.nearby()[0]
    pt = px(view, n.x, n.y)
    assert ((pt[0] - view.width() / 2) ** 2 + (pt[1] - view.height() / 2) ** 2) ** 0.5 > view.width() / 2  # off view
    # indicator sits straight north of the centre, on the circle edge: compare with the same frame without peers
    def purple_near_top(view):
        img = view.grab().toImage()
        return sum(1 for xx in range(view.width() // 2 - 10, view.width() // 2 + 10) for yy in range(0, 20)
                   if (c := img.pixelColor(xx, yy)).blue() > 200 and c.green() < 120 and c.red() > 110)

    with_peer = purple_near_top(view)
    without = purple_near_top(make_view(tmp_path, settings=Settings(zoom=4.0)))
    assert with_peer >= without + 8, (with_peer, without)


def test_show_players_off_hides_peers_and_edge_indicators(tmp_path):
    s = Settings(zoom=4.0, show_players=False)
    view = make_view(tmp_path, settings=s)
    view.peers = FakePeers([peer(view, 1, 50, 0), peer(view, 2, 0, -600)])
    tick_twice(view)
    assert view.nearby() == []
    assert not view.grab().toImage().isNull()


def test_full_map_renders_everyone(tmp_path):
    view = make_view(tmp_path)
    view.peers = FakePeers([peer(view, 1, 100, 0), peer(view, 2, 0, -4000)])
    tick_twice(view)
    view.toggle_full_map()
    assert {n.entity.id for n in view.nearby(None)} == {"1", "2"}
    assert not view.grab().toImage().isNull()


def test_heading_draws_arrow_and_no_heading_draws_dot(tmp_path):
    view = make_view(tmp_path)
    with_h = peer(view, 1, 100, 0, heading=90.0)
    view.peers = FakePeers([with_h])
    tick_twice(view)
    n = view.nearby()[0]
    assert n.entity.heading == pytest.approx(90.0, abs=1.0)
    assert not view.grab().toImage().isNull()


def test_no_fix_draws_no_peers_and_does_not_raise(tmp_path):
    view = make_view(tmp_path, fix=False)
    view.peers = FakePeers([PeerEntity(Entity("1", "player", "A", "", 0.0, 0.0), time.monotonic())])
    tick_twice(view)
    assert not view.interp.has_fix and view.nearby() == [] and not view.grab().toImage().isNull()


def test_no_source_and_source_error_status(tmp_path):
    plain = make_view(tmp_path)
    assert plain.peers is None and plain.nearby() == [] and plain.peer_status == ""
    assert not plain.grab().toImage().isNull()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as hog:
        hog.bind(("127.0.0.1", 0))
        src = LabPeerSource(hog.getsockname()[1])
        src.start()
        view = make_view(tmp_path, peers=src)
        tick_twice(view)
        assert "unavailable" in view.peer_status and view.nearby() == []
        assert not view.grab().toImage().isNull()
    disabled = make_view(tmp_path, peers=FakePeers(enabled=False))
    assert disabled.peer_status == ""


def test_detail_line_and_empty_snapshot_render(tmp_path):
    view = make_view(tmp_path, settings=Settings())
    view.peers = FakePeers()
    tick_twice(view)
    assert not view.grab().toImage().isNull()
    view.peers.set([peer(view, 1, 80, 0)])
    tick_twice(view)
    assert not view.grab().toImage().isNull()


def test_settings_self_ids_hide_peer_on_the_map(tmp_path):
    s = Settings(peer_self_ids=["1"])
    view = make_view(tmp_path, settings=s)
    view.peers = FakePeers([peer(view, 1, 50, 0), peer(view, 2, 60, 0)])
    tick_twice(view)
    assert [n.entity.id for n in view.nearby()] == ["2"]


# ---- T12: lab loopback -> sniffer -> LabPeerSource -> MapView.nearby()
def test_lab_loopback_to_map_nearby(tmp_path):
    from labsim import telemetry as t

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as k:
        k.bind(("127.0.0.1", 0))
        port = k.getsockname()[1]
    src = LabPeerSource(port)
    src.start()
    try:
        view = make_view(tmp_path, peers=src)
        wx, wz = view.interp.x / 100.0, view.interp.y / 100.0  # lab Y-up metres: x, (height), z
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.sendto(t.encode(t.Node(t.PLAYER_ID, wx, 10.0, wz, 100, "me")), ("127.0.0.1", port))
        tx.sendto(t.encode(t.Node(5, wx + 50.0, 12.0, wz, 64, "Remote")), ("127.0.0.1", port))
        tx.sendto(t.encode(t.Node(6, wx, 10.0, wz - 2000.0, 10, "TooFar")), ("127.0.0.1", port))
        tx.sendto(b"junk", ("127.0.0.1", port))
        tx.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(src.snapshot().entities) < 2:
            time.sleep(0.02)
        tick_twice(view)
        got = view.nearby()
        status = src.status()
    finally:
        src.stop()
    assert [n.entity.id for n in got] == ["5"]  # id 0 is me, id 6 is 2 km away
    n = got[0]
    assert n.entity.name == "Remote" and n.entity.health_pct == 64.0 and n.distance_m == pytest.approx(50.0, abs=0.5)
    assert n.bearing == pytest.approx(90.0, abs=1.0)  # +x is east
    assert "1 malformed" in status and "listening" in status


def test_nearby_panel_builds(tmp_path):
    from isle_livemap.app import NearbyPanel

    view = make_view(tmp_path)
    view.peers = FakePeers([peer(view, 1, 100, 0)])
    tick_twice(view)
    panel = NearbyPanel(view)
    panel.refresh()
    assert panel.table.rowCount() == 1 and panel.table.item(0, 0).text() == "P1"


# ---- T14
@pytest.mark.slow
def test_500_entities_selection_and_paint_budget(tmp_path):
    from isle_livemap.geo import EntitySmoother, visible_entities

    view = make_view(tmp_path)
    es = [peer(view, i, (i % 25 - 12) * 30.0, (i // 25 - 10) * 30.0) for i in range(500)]
    sm = EntitySmoother()
    now = time.monotonic()
    t0 = time.perf_counter()
    for _ in range(20):
        sm.update(es, now)
        sm.step(0.016)
        visible_entities(sm.items_aged(now), view.interp.x, view.interp.y, 1000.0)
    logic_ms = (time.perf_counter() - t0) / 20 * 1000
    assert logic_ms < 4.0, f"{logic_ms:.2f} ms"
    view.peers = FakePeers(es)
    tick_twice(view)
    from PySide6.QtGui import QImage, QPainter

    img = QImage(view.size(), QImage.Format.Format_ARGB32)
    p = QPainter(img)
    view._draw_entities(p)  # warm-up: first use of the font / glyph cache is a one-off cost
    runs = []
    for _ in range(3):  # best of 3: a scheduler hiccup must not fail the budget
        t0 = time.perf_counter()
        for _ in range(10):
            view._draw_entities(p)
        runs.append((time.perf_counter() - t0) / 10 * 1000)
    paint_ms = min(runs)
    p.end()
    print(f"entity layer: logic {logic_ms:.2f} ms, paint {paint_ms:.2f} ms")
    assert paint_ms < 4.0, f"paint {paint_ms:.2f} ms"


def test_overlay_uses_the_sources_own_stale_and_ttl(tmp_path):
    class Slow(FakePeers):
        stale_s, ttl_s = 8.0, 30.0

    view = make_view(tmp_path)
    view.peers = Slow([peer(view, 1, 100, 0, age=6.0), peer(view, 2, -100, 0, age=9.0)])
    tick_twice(view)
    got = {n.entity.id: n for n in view.nearby()}
    assert not got["1"].stale and got["2"].stale  # 6 s old is fresh for this source (3 s for the lab feed)
    assert "2" in {n.entity.id for n in view.nearby(now=time.monotonic() + 15.0)}  # still there 24 s after its last update
    assert "2" not in {n.entity.id for n in view.nearby(now=time.monotonic() + 25.0)}  # 34 s: past 30 s
