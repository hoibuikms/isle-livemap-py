"""The minimap must be steady: no stuck or ghost dots, no stop-and-go, no frozen frame, no flicker.

Synthetic data and a simulated clock (``time.monotonic`` is replaced), so every number here is exact and independent of
the machine. The same budgets are measured on recorded sessions by ``tools/stability_replay.py``."""

from __future__ import annotations

import math
import os
import threading
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from isle_livemap import peers  # noqa: E402
from isle_livemap.geo import (  # noqa: E402
    DR_CORR_TAU_S, DR_FF_GRACE_S, SLOT_SLACK_PX, Entity, EntitySmoother, place_label,
)
from isle_livemap.peers import GamePeerSource, PeerEntity, PeerSnapshot, PeerStore  # noqa: E402

FPS = 60.0


class SimClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


_VIEWS: list = []


@pytest.fixture
def clock(monkeypatch):
    c = SimClock()
    monkeypatch.setattr(time, "monotonic", c)
    yield c
    monkeypatch.undo()
    import shiboken6

    while _VIEWS:  # a widget left to the garbage collector is destroyed in the middle of some later test's Qt call
        v = _VIEWS.pop()
        v.timer.stop()
        shiboken6.delete(v)


def ent(eid="1", x=0.0, y=0.0, kind="dino", **kw) -> Entity:
    return Entity(eid, kind, kw.pop("name", f"Actor #{eid}"), kw.pop("species", "Deinosuchus"), x, y, 30_000.0, **kw)


def run_creature(path, send_times, seconds, tau=0.09, ttl=180.0):
    """Drive an EntitySmoother at 60 fps; ``path(t)`` is the creature's x (cm) and a sample is delivered at every time in
    ``send_times``. Returns [(t, dot_x, truth_x)] per frame."""
    sm = EntitySmoother(tau_s=tau, ttl_s=ttl)
    out, sends, last = [], iter(send_times), None
    nxt = next(sends, None)
    t = 0.0
    while t <= seconds:
        while nxt is not None and nxt <= t:
            last = ent(x=path(nxt))
            sm.update([PeerEntity(last, nxt, 1)], nxt)
            nxt = next(sends, None)
        sm.step(1.0 / FPS, t)
        if last is not None:
            out.append((t, sm.pos["1"][0], path(t)))
        t += 1.0 / FPS
    return out


# ------------------------------------------------------------------ dots move steadily and stay close to the creature
def test_dot_moves_at_a_steady_pace_between_sparse_samples():
    v = 1000.0  # 10 m/s, samples about every 0.8 s like the server's
    sends = [0.8 * i + (0.05 if i % 2 else -0.05) for i in range(1, 30)]
    frames = run_creature(lambda t: v * t, sends, 20.0)
    steps = [b[1] - a[1] for a, b in zip(frames, frames[1:]) if a[0] > 3.0]
    median = sorted(steps)[len(steps) // 2]
    assert max(steps) < 2.0 * median and min(steps) > 0.0  # never stops and never jumps: a constant pace on screen
    err = sorted(abs(d - truth) for t, d, truth in frames if t > 3.0)
    assert err[int(len(err) * 0.95)] < 150.0  # within 1.5 m of the creature at a steady pace, p95


def test_dot_beats_waiting_on_the_last_sample_by_a_wide_margin():
    v = 800.0
    sends = [0.8 * i for i in range(1, 30)]
    frames = run_creature(lambda t: v * t, sends, 20.0)
    mine = sorted(abs(d - truth) for t, d, truth in frames if t > 3.0)
    hold = sorted(v * ((t - 0.8 * (t // 0.8))) for t, _d, _x in frames if t > 3.0)  # trailing the newest sample
    assert mine[len(mine) // 2] < 0.3 * hold[len(hold) // 2]


def test_a_late_sample_does_not_stop_the_dot_but_the_overshoot_is_bounded():
    v = 1000.0
    sends = [0.8, 1.6, 2.4, 3.8]  # the fourth is 1.4 s late
    frames = run_creature(lambda t: v * min(t, 2.4), sends, 6.0)  # the creature stopped at 2.4 s
    by = {round(t, 2): d for t, d, _ in frames}
    assert by[3.0] > by[2.6] > 0  # still moving on after the last sample: no stop-and-go waiting for the next one
    peak = max(d for _t, d, _x in frames)
    assert peak - v * 2.4 < v * (1.0 + DR_FF_GRACE_S + DR_CORR_TAU_S)  # bounded: horizon + grace + correction
    assert abs(frames[-1][1] - v * 2.4) < 5.0  # the late sample says it had stopped: back on the creature


def test_a_stopped_creature_is_reached_and_the_dot_settles():
    v = 1000.0
    sends = [0.8 * i for i in range(1, 8)] + [5.6 + 0.8 * i for i in range(1, 8)]  # moving, then re-sent at the same spot
    frames = run_creature(lambda t: v * min(t, 5.6), sends, 12.0)
    assert abs(frames[-1][1] - v * 5.6) < 30.0  # settled on the creature within 30 cm
    settled = [d for t, d, _ in frames if t > 9.0]
    assert max(settled) - min(settled) < 30.0


def test_new_sample_blends_in_without_a_jump():
    # the creature was 6 m further on than the dot assumed; two more samples say it is standing there
    frames = run_creature(lambda t: 0.0 if t < 3.0 else 600.0, [0.8, 1.6, 2.4, 3.2, 4.0, 4.8], 7.0)
    steps = [abs(b[1] - a[1]) for a, b in zip(frames, frames[1:])]
    assert max(steps) < 600.0 / (DR_CORR_TAU_S * FPS) * 2.5  # corrected over DR_CORR_TAU_S, not in one frame
    assert abs(frames[-1][1] - 600.0) < 5.0


# ------------------------------------------------------------------ ids that disappear or come back
def test_destroyed_creature_leaves_after_the_grace_even_when_nothing_else_changes():
    sm = EntitySmoother(ttl_s=180.0)
    sm.missing_grace_s = 1.5
    sm.update([PeerEntity(ent("1"), 10.0, 1), PeerEntity(ent("2", 5.0), 10.0, 2)], 10.0)
    sm.update([PeerEntity(ent("2", 5.0), 10.1, 2)], 10.1)  # the server destroyed #1 (the source stops listing it)
    for k in range(1, 200):  # no further update: the source is unchanged, time just passes
        sm.prune(10.1 + k / FPS)
        gone = "1" not in sm.pos
        assert gone == (10.1 + k / FPS - 10.1 >= 1.5)
        if gone:
            break
    assert "1" not in sm.pos and "1" not in sm.entities and "2" in sm.pos and not sm._missing


def test_without_a_grace_a_missing_id_still_waits_for_its_ttl():
    sm = EntitySmoother(ttl_s=5.0)  # the lab feeds: one dropped packet must not make a dot flicker
    sm.update([PeerEntity(ent("1"), 10.0, 1)], 10.0)
    sm.update([], 10.5)
    sm.prune(14.0)
    assert "1" in sm.pos
    sm.prune(15.1)
    assert "1" not in sm.pos


def test_an_id_that_comes_back_inside_the_grace_stays_and_keeps_its_dot():
    sm = EntitySmoother(ttl_s=180.0)
    sm.missing_grace_s = 1.5
    sm.update([PeerEntity(ent("1"), 10.0, 1)], 10.0)
    sm.update([], 10.2)
    sm.prune(10.9)
    sm.update([PeerEntity(ent("1", 100.0), 11.0, 1)], 11.0)
    sm.prune(14.0)
    assert "1" in sm.pos and not sm._missing


def test_a_reused_handle_is_a_new_creature_and_does_not_glide_from_the_old_one():
    sm = EntitySmoother(ttl_s=180.0)
    sm.update([PeerEntity(ent("1", 0.0), 10.0, 7)], 10.0)
    sm.update([PeerEntity(ent("1", 0.0), 10.8, 7)], 10.8)
    sm.update([PeerEntity(ent("1", 5000.0, species="Longear", kind="ai"), 11.0, 8)], 11.0)  # 50 m away, a new store entry
    assert sm.pos["1"] == [5000.0, 0.0] and "1" not in sm._vel  # on the new creature at once, with no velocity of the old


def test_store_gives_a_new_serial_to_a_removed_and_re_added_id():
    st = PeerStore(clock=SimClock())
    st.update(ent("1"))
    first = st.entities()[0].born
    st.update(ent("1", 5.0))
    assert st.entities()[0].born == first  # the same creature moving
    st.remove("1")
    st.update(ent("1", 5.0))
    assert st.entities()[0].born != first


# ------------------------------------------------------------------ the view never freezes
def view_with(tmp_path, clock, entries=(), fake_cls=None):
    from test_peers_ui import FakePeers, make_view

    fp = (fake_cls or FakePeers)(entries)
    view = make_view(tmp_path, fp)
    _VIEWS.append(view)
    view._last = clock.t
    return view, fp


def advance(view, clock, seconds: float, per_frame=None) -> None:
    for _ in range(int(seconds * FPS)):
        clock.t += 1.0 / FPS
        view.tick()
        if per_frame:
            per_frame()


def test_repaint_is_requested_on_every_tick_even_when_nothing_arrives(tmp_path, clock, monkeypatch):
    view, _fp = view_with(tmp_path, clock)
    calls = []
    monkeypatch.setattr(view, "update", lambda *a: calls.append(1))
    advance(view, clock, 1.0)
    assert len(calls) == int(FPS)
    assert view.timer.isActive() and view.timer.interval() <= 17


def test_a_failing_peer_snapshot_does_not_freeze_the_frame(tmp_path, clock, monkeypatch):
    from test_peers_ui import FakePeers, peer

    view, fp = view_with(tmp_path, clock)
    fp.set([peer(view, 1, 100, 0)])
    advance(view, clock, 0.2)
    assert len(view.nearby()) == 1
    calls = []
    monkeypatch.setattr(view, "update", lambda *a: calls.append(1))
    monkeypatch.setattr(FakePeers, "snapshot", lambda self: (_ for _ in ()).throw(RuntimeError("dictionary changed size")))
    advance(view, clock, 0.5)
    assert len(calls) == int(FPS * 0.5)  # still repainting
    assert view.errors == int(FPS * 0.5) and "RuntimeError" in view.last_error
    assert len(view.nearby()) == 1  # and still showing the last good picture
    monkeypatch.undo()
    advance(view, clock, 0.1)


def test_a_failing_tick_still_schedules_the_repaint(tmp_path, clock, monkeypatch):
    view, _fp = view_with(tmp_path, clock)
    monkeypatch.setattr(view.ents, "step", lambda *a: (_ for _ in ()).throw(ValueError("boom")))
    calls = []
    monkeypatch.setattr(view, "update", lambda *a: calls.append(1))
    advance(view, clock, 0.1)
    assert len(calls) == 6 and view.errors == 6


def test_one_failing_layer_does_not_stop_the_other_layers_or_the_painter(tmp_path, clock, monkeypatch):
    view, _fp = view_with(tmp_path, clock)
    drawn = []
    monkeypatch.setattr(view, "_draw_entities", lambda p: (_ for _ in ()).throw(RuntimeError("bad peer")))
    real = view._draw_player
    monkeypatch.setattr(view, "_draw_player", lambda p: (drawn.append("player"), real(p)))
    monkeypatch.setattr(view, "_draw_border", lambda p: drawn.append("border"))
    img = view.grab().toImage()
    assert not img.isNull() and drawn == ["player", "border"] and view.errors == 1
    assert not view.grab().toImage().isNull()  # the next frame is fine: the painter was ended


def test_destroyed_creature_disappears_from_the_view_within_the_grace(tmp_path, clock):
    from test_peers_ui import FakePeers, peer

    class GamePeers(FakePeers):
        gone_grace_s = peers.GamePeerSource.GONE_GRACE_S
        ttl_s = 180.0

    view, fp = view_with(tmp_path, clock, fake_cls=GamePeers)
    fp.set([peer(view, 1, 100, 0), peer(view, 2, 0, -300)])
    advance(view, clock, 0.5)
    assert {n.entity.id for n in view.nearby()} == {"1", "2"}
    fp.set([peer(view, 2, 0, -300)])  # one destruction record, then silence: the snapshot never changes again
    advance(view, clock, 1.0)
    assert {n.entity.id for n in view.nearby()} == {"1", "2"}  # inside the short grace
    advance(view, clock, 0.7)
    assert {n.entity.id for n in view.nearby()} == {"2"}


def test_own_marker_gives_up_its_old_position_when_the_pipeline_is_reset(tmp_path, clock):
    view, fp = view_with(tmp_path, clock)
    assert view.interp.has_fix
    view.pipeline.reset()  # a new server, or the game closed
    advance(view, clock, 0.1)
    assert not view.interp.has_fix and view.nearby() == []


# ------------------------------------------------------------------ labels do not hop or swap
def test_a_label_keeps_the_side_it_has_while_it_still_fits():
    bounds = (0.0, 0.0, 400.0, 400.0)
    first = place_label(200, 200, 80, 26, [], bounds)
    assert first == (160, 208, 80, 26)  # best side: under the marker
    slot = [0]
    r = place_label(200, 200, 80, 26, [], bounds, None, 3, slot)  # last time it was on the left
    assert slot == [3] and r[0] < 200 - 80  # still on the left although "under" is free
    slot = [0]
    blocked = [(160, 208, 80, 26)]
    assert place_label(200, 200, 80, 26, blocked, bounds, None, 0, slot) is not None and slot != [0]  # taken: another side


def test_a_label_tolerates_a_few_pixels_where_a_new_one_would_not_fit():
    bounds = (0.0, 0.0, 400.0, 400.0)
    slot = [0]
    r = place_label(86.0, 200, 80, 26, [], bounds, None, 3, slot)  # its left side sticks out of the view by 2 px
    assert slot == [3] and r[0] == -2.0
    slot = [0]
    r = place_label(79.0, 200, 80, 26, [], bounds, None, 3, slot)  # 9 px: more than the slack, another side is taken
    assert slot != [3] and r[0] >= 0.0


def test_a_peer_that_has_a_label_keeps_it_over_a_newcomer_at_almost_the_same_distance(tmp_path, clock, monkeypatch):
    from test_peers_ui import peer

    monkeypatch.setattr("isle_livemap.geo.LABEL_CAPS_MINIMAP", (0, 0, 2, 0))  # two labels in the 1000 m tier (the inner tiers are guaranteed), so a newcomer has to take one over

    def at(i, metres, degrees):
        a = math.radians(degrees)
        return peer(view, i, metres * math.cos(a), metres * math.sin(a), name="")  # nameless: named players have extra labels

    view, fp = view_with(tmp_path, clock)
    fp.set([at(1, 600.0, 0.0), at(2, 700.0, 120.0)])
    advance(view, clock, 0.3)
    view.grab()
    assert view._labelled == {"1", "2"}
    fp.set([at(1, 600.0, 0.0), at(2, 700.0, 120.0), at(4, 695.0, 240.0)])  # P4 is 1 % nearer than P2, but P2 has the label
    advance(view, clock, 0.3)
    view.grab()
    assert view._labelled == {"1", "2"}
    fp.set([at(1, 600.0, 0.0), at(2, 700.0, 120.0), at(4, 520.0, 240.0)])  # clearly nearer: now it takes the label
    advance(view, clock, 0.3)
    view.grab()
    assert "4" in view._labelled


# ------------------------------------------------------------------ the game source
def test_the_own_old_body_is_never_a_nearby_creature(b64):
    from test_game_peers import inbound, make

    g, _ = make()
    g.on_inbound(inbound(b""), 100)
    g.on_inbound(inbound(b""), 200)  # respawn
    assert g.own_handle == 200 and g._past_own == {100}
    assert g.store.update(Entity("100", "dino", "Actor #100", "Deinosuchus", 1.0, 2.0)) is False
    g._drop(100)  # the server removed the body
    assert g._past_own == set()
    assert g.store.update(Entity("100", "ai", "Actor #100", "", 1.0, 2.0)) is True  # the handle may be someone else's now


def test_a_decode_error_is_counted_shown_and_does_not_stop_the_stream(b64):
    from test_game_peers import inbound, make

    g, _ = make()
    real = g._on_inbound
    calls = []

    def flaky(d, own=None):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("bad batch")
        real(d, own)

    g._on_inbound = flaky
    g.on_inbound(inbound(b64("SecondHeartbeat")), None)  # raises inside: swallowed
    assert g.decode_errors == 1 and g.headline() == "Decode errors: see log" and "DECODE ERRORS" in g.summary()
    g.on_inbound(inbound(b64("SecondHeartbeat")), None)  # the next datagram is decoded as usual
    assert len(g.snapshot().entities) == 15


def test_status_can_be_read_from_the_ui_thread_while_the_decoder_changes_its_tables():
    g = GamePeerSource(pipeline=type("P", (), {"add_observer": lambda *a: None})(), clock=SimClock())
    g.packets = g.LATE_JOIN_PACKETS
    g._last_t = 500.0
    stop = threading.Event()
    errors = []

    def decoder():
        i = 0
        while not stop.is_set():
            i += 1
            g._profile[i] = [40, 40, 0, 500.0]  # unplaced creatures come and go while the UI reads
            g.identity.actors[i] = type("A", (), {"archetype_handle": None, "protocol_id": None, "player_state": None,
                                                  "asset_species": ()})()
            if i > 300:
                g._profile.pop(i - 300, None)
                g.identity.actors.pop(i - 300, None)

    t = threading.Thread(target=decoder)
    t.start()
    try:
        deadline = time.perf_counter() + 0.6
        while time.perf_counter() < deadline:
            g._hidden_cache = (-1e9, 0)
            try:
                g.status(), g.summary(), g.snapshot()
            except Exception as err:  # noqa: BLE001
                errors.append(err)
                break
    finally:
        stop.set()
        t.join()
    assert not errors, errors


def test_the_game_source_exposes_a_short_grace_and_a_long_creature_ttl():
    assert peers.GamePeerSource.gone_grace_s <= 2.0  # a destroyed creature is gone within two seconds
    assert peers.GamePeerSource.ttl_s > peers.GamePeerSource.TTL_AI_S >= 30.0  # resting dinosaurs outlive fish
    snap = PeerSnapshot()
    assert snap.entities == ()
