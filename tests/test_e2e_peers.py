"""End-to-end: recorded packets -> real ``python -m isle_livemap`` wiring (offscreen Qt) -> rendered MapView.

Nothing here fakes the pipeline, the observer or the peer source: ``__main__.main`` builds them, a pcap built
from the REAL recorded payloads is replayed through ``ReplayFeeder`` / ``TelemetryPipeline.handle``, and the
widget is grabbed and inspected pixel-wise.

The pcap uses the recorded outbound ``StationaryPayload`` (own position 442020.33, -162148.37 cm, z 26009.46)
and recorded inbound replication packets. Whether those were captured in the same session is unknown; the
test only needs them to share a frame (see test_actor_positions_are_on_the_island_in_the_local_frame)."""

from __future__ import annotations

import base64
import json
import math
import os
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("scapy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from isle_livemap.decode import decode_actor_locations  # noqa: E402
from isle_livemap.geo import Entity, project_unclamped  # noqa: E402
from conftest import retimed  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
OWN_FIX = (442_020.33, -162_148.37)
# actors within 1000 m of OWN_FIX in the three replayed packets: 233 and 357 m away, south of it (bearing 158-177 deg). The
# recorded vectors are raw / 128 cm (decode.STATE_VECTOR_SCALE); read with the /100 of the move packets they were 752-838 m
# away, NNW, which is the 650-850 m "ring" of every nearby creature in the live captures.
NEAR_IDS = {"105810", "105148"}
CYAN = (164, 16, 28)  # geo.ENTITY_STYLE["player"].color: the deep red of another player's dinosaur (name kept from the cyan days)
ORANGE = (143, 166, 179)  # geo.ENTITY_STYLE["ai"].color (muted grey-blue): the recorded actors only ever send the 155/158 bit animal state


def build_pcap(path: Path, inbound=("FirstHeartbeat", "SecondHeartbeat", "PeriodicAttributes")) -> Path:
    from scapy.layers.inet import IP, UDP
    from scapy.layers.l2 import Ether
    from scapy.utils import wrpcap

    rec = json.loads((FIXTURES / "recorded_payloads.json").read_text())
    move = bytes.fromhex(rec["StationaryPayload"])
    inb = [base64.b64decode(rec[k]) for k in inbound]
    client, server, cport, sport = "192.168.0.10", "203.0.113.5", 50_000, 7777  # public server: pcap_datagrams infers the local side
    pkts, t = [], 0.0
    for i in range(160):
        for up, payload in [(True, retimed(move, t))] + ([(False, inb[i // 10])] if i % 10 == 5 and i // 10 < len(inb) else []):
            a, b, sp, dp = (client, server, cport, sport) if up else (server, client, sport, cport)
            p = Ether() / IP(src=a, dst=b) / UDP(sport=sp, dport=dp) / payload
            p.time = 1_700_000_000 + t
            pkts.append(p)
        t += 0.05
    wrpcap(str(path), pkts)
    return path


def run_main(monkeypatch, argv, until, timeout=8.0):
    """Run the real ``__main__.main(argv)``; the Qt loop is replaced by a poll until ``until(app)``, then the
    widget is grabbed. Returns (app, QImage) - the app (and its view) stay alive for assertions."""
    from PySide6 import QtWidgets
    from PySide6.QtGui import QColor, QImage, QPainter

    import isle_livemap.app as appmod
    from isle_livemap.__main__ import main

    real = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    box: dict = {}

    class Capture(appmod.App):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            box["app"] = self

    class Shim:  # the real QApplication, except exec() returns after a poll
        def __init__(self, *_a):
            pass

        def __getattr__(self, name):
            return getattr(real, name)

        def exec(self):
            app, t0 = box["app"], time.monotonic()
            while time.monotonic() - t0 < timeout and not until(app):
                real.processEvents()
                time.sleep(0.01)
            real.processEvents()
            img = app.view.grab().toImage()
            bg = QImage(img.size(), QImage.Format.Format_ARGB32)
            bg.fill(QColor(40, 40, 40))
            p = QPainter(bg)
            p.drawImage(0, 0, img)
            p.end()
            box["img"] = bg
            real.aboutToQuit.emit()  # what a real quit does: stops the capture source and the peer source
            return 0

    monkeypatch.setattr(appmod, "App", Capture)
    monkeypatch.setattr(QtWidgets, "QApplication", Shim)
    assert main(argv) == 0
    return box["app"], box["img"]


def near_color(img, x, y, rgb=CYAN, r=4, tol=120) -> bool:
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            px, py = int(round(x)) + dx, int(round(y)) + dy
            if 0 <= px < img.width() and 0 <= py < img.height():
                c = img.pixelColor(px, py)
                if abs(c.red() - rgb[0]) + abs(c.green() - rgb[1]) + abs(c.blue() - rgb[2]) < tol:
                    return True
    return False


def settings_dir(tmp_path, **kw) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    (d / "settings.json").write_text(json.dumps(kw))
    return d


def screen_of(view, x, y):
    pt = view.to_screen(*project_unclamped(x, y))
    return pt.x(), pt.y()


def ready(app) -> bool:
    return app.pipeline.latest.position is not None and len({n.entity.id for n in app.view.nearby()}) >= len(NEAR_IDS) \
        and app.view.interp.has_fix


# ---------------------------------------------------------------- game path through __main__
def test_default_game_peers_through_real_main_draw_dots_at_their_decoded_positions(tmp_path, monkeypatch):
    pcap = build_pcap(tmp_path / "real.pcap")
    data = settings_dir(tmp_path)  # defaults: radius 1000 m, zoom 1
    app, img = run_main(monkeypatch, ["--replay", str(pcap), "--speed", "3", "--data-dir", str(data)], ready)

    # wiring: the factory got the real pipeline, the observer is the one that decoded, own handle was learned
    assert type(app.peers).__name__ == "GamePeerSource" and app.peers.pipeline is app.pipeline
    assert app.peers.packets > 0 and app.peers.locations > 0
    assert app.peers.own_handle == 103_940  # locked from the recorded survival heartbeat, via the real VitalsTracker
    assert "103940" not in {p.id for p in app.peers.snapshot().entities}  # self excluded by identity

    # frame: the local fix is the decoded outbound position; peers are in the same cm frame
    pos = app.pipeline.latest.position
    assert (pos.x, pos.y) == (pytest.approx(OWN_FIX[0], abs=0.01), pytest.approx(OWN_FIX[1], abs=0.01))
    near = {n.entity.id: n for n in app.view.nearby()}
    assert set(near) == NEAR_IDS
    for n in near.values():
        assert 200 < n.distance_m < 400 and 150 < n.bearing < 185  # both just south of the local player
        assert n.entity.kind == "ai" and n.entity.name.startswith("Actor #")
        assert n.entity.health_pct is None  # a server animal: no vitals (its heading may be derived from its movement)

    # rendering: a dot sits at the projected position of every in-view actor (same projection as the arrow)
    cx, cy = screen_of(app.view, pos.x, pos.y)
    assert near_color(img, cx, cy, (255, 64, 64), r=6)  # the red local arrow is at the player's position
    drawn = 0
    for n in near.values():
        sx, sy = screen_of(app.view, n.x, n.y)
        if math.hypot(sx - cx, sy - cy) < app.view.width() / 2 - 8:
            drawn += near_color(img, sx, sy, ORANGE)
    assert drawn >= len(NEAR_IDS), "actors inside the circle must be drawn at their projected positions"


def test_out_of_view_actor_in_radius_gets_an_edge_pointer_through_real_main(tmp_path, monkeypatch):
    from isle_livemap.geo import edge_anchor

    pcap = build_pcap(tmp_path / "real.pcap")
    data = settings_dir(tmp_path, zoom=6.0)  # view edge ~185 m, radius still 1000 m -> the 233 and 357 m actors are off view
    app, img = run_main(monkeypatch, ["--replay", str(pcap), "--speed", "3", "--data-dir", str(data)], ready)
    pos = app.pipeline.latest.position
    cx, cy = screen_of(app.view, pos.x, pos.y)
    hit = outside = 0
    for n in app.view.nearby():
        sx, sy = screen_of(app.view, n.x, n.y)
        if math.hypot(sx - cx, sy - cy) <= app.view.width() / 2:
            continue  # inside the circle: drawn as a dot, not a pointer
        outside += 1
        ax, ay = edge_anchor(cx, cy, sx, sy, app.view.width() / 2 - 7)
        hit += near_color(img, ax, ay, ORANGE, r=5)
    assert outside == len(NEAR_IDS) and hit == outside


def test_actors_beyond_the_radius_are_not_drawn(tmp_path, monkeypatch):
    pcap = build_pcap(tmp_path / "real.pcap")
    data = settings_dir(tmp_path, peer_radius_m=500)
    app, _ = run_main(monkeypatch, ["--replay", str(pcap), "--speed", "3", "--data-dir", str(data)],
                      lambda a: a.peers.locations > 20)
    assert app.view.nearby() == [] and len(app.peers.snapshot().entities) > 10  # decoded, but outside 500 m


def test_game_peers_survive_settings_edits_and_a_server_change(tmp_path, monkeypatch):
    pcap = build_pcap(tmp_path / "real.pcap")
    data = settings_dir(tmp_path)
    app, _ = run_main(monkeypatch, ["--replay", str(pcap), "--speed", "3", "--data-dir", str(data)], ready)
    # configured self ids replace only the USER ids, never the learned own handle
    app.peers.store.set_self_ids(["999"])
    app.peers.store.update(Entity("103940", "player", "me", "", 1.0, 1.0))
    assert "103940" not in {p.id for p in app.peers.snapshot().entities}
    # server change / game restart: handles are meaningless, the store empties, the exclusion is dropped
    app.peers.start()  # run_main's quit detached the observer; re-attach it to exercise the reset tap
    app.pipeline.reset()
    assert app.peers.snapshot().entities == () and app.peers.own_handle is None


def test_no_flags_starts_game_source_and_survives_without_server(tmp_path, monkeypatch):
    """Plain start (no --replay needed for the wiring): the default source is the game backend on the real pipeline,
    and an old settings file with peer_source is accepted and ignored. Live capture is replaced by an inert source."""
    import isle_livemap.capture as capmod

    class Idle:
        def __init__(self, *a, **k): pass
        def start(self): pass
        def stop(self): pass

    monkeypatch.setattr(capmod, "LiveCapture", Idle)
    data = settings_dir(tmp_path, peer_source="lab")
    app, _ = run_main(monkeypatch, ["--data-dir", str(data)], lambda a: True)
    assert type(app.peers).__name__ == "GamePeerSource" and app.peers.pipeline is app.pipeline
    assert app.view.peer_status == "game: waiting for server traffic" and app.view.nearby() == []


# ---------------------------------------------------------------- other sources through __main__
def test_synthetic_replay_through_real_main_draws_demo_peers(tmp_path, monkeypatch):
    data = settings_dir(tmp_path)
    app, img = run_main(monkeypatch, ["--replay", "--data-dir", str(data)], lambda a: len(a.view.nearby()) >= 5)
    ids = {n.entity.id for n in app.view.nearby()}
    assert {"101", "102", "103", "105", "106"} <= ids and "0" not in ids  # id 0 is the self entry and is dropped
    pos = app.pipeline.latest.position
    cx, cy = screen_of(app.view, pos.x, pos.y)
    assert sum(near_color(img, *screen_of(app.view, n.x, n.y)) for n in app.view.nearby()
               if math.hypot(screen_of(app.view, n.x, n.y)[0] - cx, screen_of(app.view, n.x, n.y)[1] - cy) < 160) >= 4
    app.peers.store.set_self_ids(["999"])  # the demo "me" (id 0) must stay hidden
    time.sleep(0.6)  # SyntheticPeers re-emits every 0.5 s, including the self entry
    assert "0" not in {p.id for p in app.peers.snapshot().entities}


def test_lab_loopback_through_real_main_uses_geo_axes(tmp_path, monkeypatch):
    from labsim.telemetry import Node, encode

    pcap = build_pcap(tmp_path / "real.pcap")
    data = settings_dir(tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    lx, lz = OWN_FIX[0] / 100, OWN_FIX[1] / 100  # lab metres: x east, y height, z = geo y (north is -z)
    nodes = [Node(0, lx, 260, lz, 100, "me"), Node(5, lx + 300, 262, lz, 80, "East"), Node(6, lx, 255, lz - 400, 55, "North")]
    stop = threading.Event()

    def send():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while not stop.is_set():
            for n in nodes:
                s.sendto(encode(n), ("127.0.0.1", port))
            time.sleep(0.1)

    threading.Thread(target=send, daemon=True).start()
    try:
        app, img = run_main(monkeypatch, ["--replay", str(pcap), "--lab-peers", str(port), "--speed", "3", "--data-dir", str(data)],
                            lambda a: a.view.interp.has_fix and len(a.view.nearby()) >= 2)
    finally:
        stop.set()
    near = {n.entity.id: n for n in app.view.nearby()}
    assert set(near) == {"5", "6"}  # lab "me" (id 0) excluded
    assert near["5"].bearing == pytest.approx(90, abs=0.5) and near["5"].distance_m == pytest.approx(300, abs=0.5)  # east
    assert near["6"].bearing == pytest.approx(0, abs=0.5) or near["6"].bearing == pytest.approx(360, abs=0.5)  # north = up
    pos = app.pipeline.latest.position
    cx, cy = screen_of(app.view, pos.x, pos.y)
    sx, sy = screen_of(app.view, near["5"].x, near["5"].y)
    assert sx > cx and abs(sy - cy) < 1 and near_color(img, sx, sy)
    ux, uy = screen_of(app.view, near["6"].x, near["6"].y)
    assert uy < cy and abs(ux - cx) < 1 and near_color(img, ux, uy)


# ---------------------------------------------------------------- coordinate frame
def test_actor_positions_are_on_the_island_in_the_local_frame():
    """Land/sea test with the bundled map (same projection as the local arrow): the decoded actors sit on land as
    they are; mirroring x (or both axes) puts most of them in the sea or off the map. Mirroring only y is NOT
    excluded by this data (documented limitation), so it is not asserted."""
    from PySide6.QtGui import QImage

    from isle_livemap.geo import ASSET_DIR

    img = QImage(str(ASSET_DIR / "gateway_map.jpg"))
    rec = json.loads((FIXTURES / "recorded_payloads.json").read_text())
    pts = {}
    for k, v in rec.items():
        if k in ("MovingPayload", "StationaryPayload", "CompetingCandidatePayload"):
            continue
        for loc in decode_actor_locations(base64.b64decode(v)):
            pts[loc.net_ref_handle] = (loc.x, loc.y)
    assert len(pts) >= 50

    def land(x, y):
        left, top = project_unclamped(x, y)
        if not (0 <= left < 1 and 0 <= top < 1):
            return False
        c = img.pixelColor(int(left * img.width()), int(top * img.height()))
        return not (c.blue() > c.red() + 30 and c.blue() > c.green() + 20)

    as_is = sum(land(x, y) for x, y in pts.values()) / len(pts)
    x_flip = sum(land(-x, y) for x, y in pts.values()) / len(pts)
    both = sum(land(-x, -y) for x, y in pts.values()) / len(pts)
    assert as_is > 0.9 and as_is - x_flip > 0.3 and as_is - both > 0.1  # the island, not its mirror images (the map is mostly land)
