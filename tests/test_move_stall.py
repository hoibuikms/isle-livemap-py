"""The own marker must not stand still while the player moves, and must say so when it can't know: a fast mover's move
records carry an acceleration above the old 100 000 cap (recorded: 105 000 to 110 000), and a gap in the moves (dead, loading,
teleport) is logged and drawn as uncertain."""

import os

import pytest

from isle_livemap.capture import MOVE_GAP_S, OUTBOUND, Datagram, TelemetryPipeline
from isle_livemap.decode import decode_movement
from isle_livemap.geo import OWN_STALE_S, own_marker_stale, own_stale_text
from isle_livemap.replay import encode_move_payload

CLIENT, SERVER = ("192.168.0.10", 50_000), ("203.0.113.5", 7777)
FAST = (1_096_956, -800_055, -103_722)  # raw NetQuantize10: 109 696 cm/s² on the first axis


def move(pipe, t, x_m, y_m, z_m=203.0, ts=40.0, accel=None):
    payload = encode_move_payload(x_m * 100.0, y_m * 100.0, z_m * 100.0, 10.0, ts, accel)
    pipe.handle(Datagram(t, *CLIENT, *SERVER, payload, OUTBOUND))


def test_acceleration_above_the_old_cap_still_decodes():
    cands = decode_movement(encode_move_payload(224_300.0, 62_500.0, 38_200.0, 214.0, 195.8, FAST))
    assert any(abs(c.x - 224_300.0) < 1 and abs(c.y - 62_500.0) < 1 and abs(c.client_timestamp - 195.8) < 0.01 for c in cands)


def test_zero_acceleration_still_decodes_as_before():
    cands = decode_movement(encode_move_payload(224_300.0, 62_500.0, 38_200.0, 214.0, 195.8))
    assert any(abs(c.x - 224_300.0) < 1 for c in cands)


def test_the_marker_follows_a_flyer_whose_acceleration_is_above_100k():
    pipe = TelemetryPipeline()
    t, x = 100.0, 2000.0
    for i in range(80):  # 25 m/s, a move every 0.1 s, all with a big acceleration
        move(pipe, t + i * 0.1, x + i * 2.5, 600.0, ts=40.0 + i * 0.1, accel=FAST)
    pos = pipe.latest.position
    assert pos is not None and abs(pos.x / 100.0 - (x + 79 * 2.5)) < 1.0
    assert pipe.latest.position_at == pytest.approx(t + 79 * 0.1)  # the newest move, not one from seconds ago


def test_a_gap_in_the_moves_is_recorded_with_its_length_and_distance():
    pipe = TelemetryPipeline()
    for i in range(30):
        move(pipe, 100.0 + i * 0.1, 2000.0 + i, 600.0, ts=40.0 + i * 0.1)
    for i in range(30):  # 5 s without a move, then 300 m further on, the client clock carries on
        move(pipe, 108.0 + i * 0.1, 2300.0 + i, 600.0, ts=45.0 + i * 0.1)
    assert len(pipe.move_gaps) == 1
    at, secs, metres = pipe.move_gaps[0]
    assert secs == pytest.approx(5.1, abs=0.2) and 270.0 < metres < 272.0
    assert pipe.counts()["own_move_gaps"] == 1


def test_a_short_pause_is_not_a_gap():
    pipe = TelemetryPipeline()
    for i in range(30):
        move(pipe, 100.0 + i * 0.1, 2000.0 + i, 600.0, ts=40.0 + i * 0.1)
    for i in range(30):  # 2.9 s without a move: below MOVE_GAP_S
        move(pipe, 105.8 + i * 0.1, 2100.0 + i, 600.0, ts=43.0 + i * 0.1)
    assert pipe.move_gaps == [] and MOVE_GAP_S == 3.0


def test_the_marker_is_uncertain_after_three_seconds_without_a_move():
    assert not own_marker_stale(None) and not own_marker_stale(0.0) and not own_marker_stale(OWN_STALE_S)
    assert own_marker_stale(OWN_STALE_S + 0.1) and own_marker_stale(24.0)
    assert own_stale_text(12.4) == "? no moves 12 s"


def test_the_overlay_ages_the_marker_and_clears_it_on_a_new_move(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from test_peers_ui import make_view  # noqa: PLC0415

    view = make_view(tmp_path)
    now = view.pipeline.latest.position_at + 0.2  # the synthetic fix is stamped on its own clock
    view._advance(now, 0.016)
    assert view.own_age_s is not None and not own_marker_stale(view.own_age_s)
    view._advance(now + 20.0, 0.016)  # nothing new for 20 s
    assert own_marker_stale(view.own_age_s) and view.own_age_s > 15.0
    assert not view.grab().toImage().isNull()  # the hollow arrow and its "?" paint without error
    view._advance(view.pipeline.latest.position_at + 0.5, 0.016)  # a move half a second ago: not uncertain any more
    assert not own_marker_stale(view.own_age_s)


def _record_with_a_false_short_prefix(x_m=1104.8, y_m=-2507.7, z_m=355.0, ts=187.763):
    """A move record whose true acceleration is 20 bits wide, laid out so that reading it with 4-bit components also looks
    like a valid prefix - with a 'timestamp' of 5.6e-17 (recorded session ``jj``, 12 s of moves ignored because of it)."""
    from isle_livemap.replay import BitWriter, _needed

    tiny = 0x2481CFC9  # the 32 bits the 4-bit reading takes for its timestamp
    rx = ((tiny & 0x7FF) << 9) | 0x0A5  # bits 9..19 of x are the top of the fake timestamp
    ry = (tiny >> 11) & 0xFFFFF
    rz = (4 << 16) | (0 << 12) | (5 << 8) | 0x88  # low bits: a 4-bit header (value 4, scale flag) and 4-bit components 5, 0, 4
    raw = [round(v * 100) for v in (x_m * 100.0, y_m * 100.0, z_m * 100.0)]
    comp = max(_needed(r) for r in raw)
    w = BitWriter()
    w.write(0, 40)
    w.write(int.from_bytes(__import__("struct").pack("<f", ts), "little"), 32)
    w.write(20 | 64, 7)
    for r in (rx, ry, rz):
        w.write(r, 20)
    w.write(comp | 64, 7)
    for r in raw:
        w.write(r, comp)
    w.write(0, 1)
    w.write(1, 1)
    w.write(int(10.0 / 360.0 * 65536) & 0xFFFF, 16)
    w.write(0, 1)
    return w.to_bytes()


def test_a_false_short_prefix_with_a_tiny_timestamp_does_not_hide_the_true_one():
    payload = _record_with_a_false_short_prefix()
    cands = [c for c in decode_movement(payload) if abs(c.x - 110_480.0) < 2 and abs(c.y + 250_770.0) < 2]
    assert cands and all(abs(c.client_timestamp - 187.763) < 0.01 for c in cands)


def test_the_regression_test_really_fails_on_the_old_prefix_rule(monkeypatch):
    import isle_livemap.decode as decode

    monkeypatch.setattr(decode, "_MIN_CLIENT_TS", 1e-300)  # the rule before: any finite positive timestamp
    cands = [c for c in decode.decode_movement(_record_with_a_false_short_prefix()) if abs(c.x - 110_480.0) < 2]
    assert cands and all(c.client_timestamp < 1e-3 for c in cands)
