"""Ports of UnrealMovementPacketDecoderTests.cs (recorded payloads + tracker)."""

from __future__ import annotations

import pytest

from isle_livemap.decode import MovementCandidate, decode_movement
from isle_livemap.tracking import MovementTracker

from conftest import retimed

T0 = 1_000_000.0


def cand(x, y, z, timestamp, bits=26):
    return MovementCandidate(x, y, z, 0.0, timestamp, 65, 380, bits)


def near(a, b, tol=0.01):
    return abs(a - b) < tol


def test_decode_finds_real_location_and_yaw(hexb):
    cs = decode_movement(hexb("MovingPayload"))
    hits = [c for c in cs if near(c.x, 153_610.82) and near(c.y, 283_609.52)]
    assert len(hits) == 1
    m = hits[0]
    assert m.z == pytest.approx(20_389.74, abs=0.01)
    assert m.unreal_yaw_degrees == pytest.approx(172.71, abs=0.01)
    assert m.map_heading_degrees == pytest.approx(262.71, abs=0.01)
    assert m.location_bit_offset == 380
    assert m.component_bit_count == 26


def test_decode_rejects_overlapping_false_vector(hexb):
    cs = decode_movement(hexb("StationaryPayload"))
    assert any(near(c.x, 442_020.33) and near(c.y, -162_148.37) and near(c.z, 26_009.46) for c in cs)
    assert not any(near(c.x, 180_719.49) and near(c.y, 89_980.69) for c in cs)


def test_decode_reads_yaw_while_stationary(hexb):
    hits = [c for c in decode_movement(hexb("StationaryPayload")) if near(c.x, 442_020.33)]
    assert len(hits) == 1
    assert hits[0].unreal_yaw_degrees == pytest.approx(60.62, abs=0.01)
    assert hits[0].map_heading_degrees == pytest.approx(150.62, abs=0.01)


def test_decode_empty_and_garbage():
    assert decode_movement(b"") == []
    assert decode_movement(b"\x00" * 64) == []
    assert decode_movement(b"\xff" * 64) == []


def test_tracker_locks_after_bootstrap_window(hexb):
    tr = MovementTracker()
    payload = hexb("StationaryPayload")
    for i in range(12):
        assert tr.track(retimed(payload, i * 0.05), T0 + i * 0.05) is None
    s = tr.track(retimed(payload, 0.6), T0 + 0.6)
    assert s is not None
    assert s.x == pytest.approx(442_020.33, abs=0.01)
    assert s.y == pytest.approx(-162_148.37, abs=0.01)


def test_tracker_locks_real_vector_from_live_payload(hexb):
    tr = MovementTracker()
    payload = hexb("CompetingCandidatePayload")
    assert any(near(c.x, 137_939.16) and near(c.y, 285_822.42) for c in decode_movement(payload))
    tracked = None
    for i in range(13):
        tracked = tr.track(retimed(payload, i * 0.05), T0 + i * 0.05) or tracked
    assert tracked is not None
    assert tracked.x == pytest.approx(137_939.16, abs=0.01)
    assert tracked.y == pytest.approx(285_822.42, abs=0.01)
    assert tracked.unreal_yaw_degrees == pytest.approx(22.38, abs=0.01)


def test_rejects_retransmitted_movement_with_older_timestamp():
    cur = cand(100, 100, 100, 40.0)
    old = cand(450, 100, 100, 39.5)
    assert MovementTracker.continue_track([old], cur, T0 - 0.05, T0) is None


def test_prefers_newest_forward_movement_over_older_saved_move():
    cur = cand(100, 100, 100, 40.0)
    old = cand(450, 100, 100, 39.5)
    new = cand(130, 110, 100, 40.05)
    assert MovementTracker.continue_track([old, new], cur, T0 - 0.05, T0) == new


def test_rejects_timestamp_regression_after_wall_clock_delay():
    cur = cand(100, 100, 100, 229_678.0)
    assert MovementTracker.continue_track([cand(140, 120, 100, 134.0)], cur, T0 - 0.3, T0) is None


def test_rejects_implausible_timestamp_jump():
    cur = cand(100, 100, 100, 134.0)
    assert MovementTracker.continue_track([cand(140, 120, 100, 229_678.0)], cur, T0 - 0.05, T0) is None


def test_rejects_different_position_with_duplicate_timestamp():
    cur = cand(138_577, -209_973, 26_537, 333.630)
    dup = cand(138_673, -209_973, 26_537, 333.630)
    assert MovementTracker.continue_track([dup], cur, T0 - 0.06, T0) is None


def test_does_not_rebootstrap_distant_layout_after_long_gap(hexb):
    tr = MovementTracker()
    p1 = hexb("MovingPayload")
    for i in range(13):
        tr.track(p1, T0 + i * 0.05)
    p2 = hexb("StationaryPayload")
    for i in range(13):
        assert tr.track(p2, T0 + 31 + i * 0.05) is None


def test_reacquires_stable_nearby_canonical_movement_after_losing_continuity():
    tr = MovementTracker()
    cur = cand(152_631, -255_094, 37_011, 40.0)
    for i in range(12):
        c = cand(159_018 + i * 40, -271_844 + i * 50, 33_040, 172.0 + i * 0.05)
        assert tr.recover_track([c], cur, T0 + i * 0.05) is None
    rec = cand(159_498, -271_244, 33_040, 172.6)
    assert tr.recover_track([rec], cur, T0 + 0.6) == rec


def test_does_not_recover_from_stable_low_bit_overlap():
    tr = MovementTracker()
    cur = cand(138_577, -209_973, 26_537, 336.745)
    for i in range(21):
        ov = cand(137_000 + i, -208_000 + i, 4_085, 337.0 + i * 0.05, bits=20)
        assert tr.recover_track([ov], cur, T0 + i * 0.05) is None


def test_replaces_distant_stale_lock_after_sustained_canonical_chain():
    tr = MovementTracker()
    wrong = cand(34_079, -28_844, 4_085, 40.0, bits=23)
    rec = None
    for i in range(16):  # tracking._REQUIRED_DISTANT_HITS
        c = cand(111_344.3, -246_597.7, 30_289.4, 159.0 + i * 0.1)
        rec = tr.recover_distant([c], wrong, T0, T0 + 2 + i * 0.1)
        if i < 15:
            assert rec is None
    assert rec is not None
    assert rec.x == pytest.approx(111_344.3, abs=0.1)
    assert rec.y == pytest.approx(-246_597.7, abs=0.1)


def test_reacquires_lower_bit_layout_after_respawn():
    tr = MovementTracker()
    old = cand(140_424.3, -210_803.2, 26_262.5, 900.0, bits=26)
    rec = None
    for i in range(16):
        c = cand(53_330.12, -32_143.69, 38_831.91, 65.62 + i * 0.21, bits=24)
        rec = tr.recover_distant([c], old, T0, T0 + 2 + i * 0.1)
        if i < 15:
            assert rec is None
    assert rec is not None
    assert rec.component_bit_count == 24
    assert rec.x == pytest.approx(53_330.12, abs=0.1)


def test_does_not_replace_distant_lock_while_still_fresh():
    tr = MovementTracker()
    cur = cand(140_424, -210_803, 26_263, 900.0)
    assert tr.recover_distant([cand(53_330, -32_144, 38_832, 65.62, bits=24)], cur, T0 - 0.5, T0) is None


def test_does_not_replace_distant_lock_from_replayed_duplicate_packet():
    tr = MovementTracker()
    cur = cand(140_424, -210_803, 26_263, 900.0)
    dup = cand(53_330, -32_144, 38_832, 65.62, bits=24)
    for i in range(40):
        assert tr.recover_distant([dup], cur, T0, T0 + 2 + i * 0.075) is None


def test_does_not_replace_distant_lock_from_short_or_low_bit_chain():
    tr = MovementTracker()
    cur = cand(111_344, -246_598, 30_289, 159.0, bits=23)
    for i in range(40):
        bits = 26 if i < 16 else 20
        ov = cand(34_079, -28_844, 4_085, 160.0 + i * 0.075, bits=bits)
        assert tr.recover_distant([ov], cur, T0, T0 + 2 + i * 0.075) is None


def test_distant_recovery_follows_a_chain_across_changing_layouts():
    """The bit layout of a move changes from datagram to datagram (what else the datagram carries moves the vector): the
    chain is what continues, not the layout. Recorded session ``m``: 13 s with the old place on the map after a 1.2 km jump."""
    tr = MovementTracker()
    old = cand(84_800, 295_900, 20_200, 49.0, bits=26)
    rec = None
    for i in range(30):
        c = cand(170_300 + i * 20, 207_100, 20_300, 50.6 + i * 0.1, bits=26)
        c = MovementCandidate(c.x, c.y, c.z, c.unreal_yaw_degrees, c.client_timestamp, 60 + (i % 5) * 4, 340 + (i % 7) * 11, 26)
        rec = tr.recover_distant([c], old, T0, T0 + 2.0 + i * 0.1)
        if rec is not None:
            break
    assert rec is not None and i <= 17 and rec.x == pytest.approx(170_300 + i * 20, abs=1)


def test_a_chain_needs_the_client_clock_to_run_forward():
    tr = MovementTracker()
    old = cand(84_800, 295_900, 20_200, 49.0, bits=26)
    for i in range(40):  # same timestamp over and over: a constant vector, not a move
        c = cand(170_300, 207_100, 20_300, 50.6, bits=26)
        assert tr.recover_distant([c], old, T0, T0 + 2.0 + i * 0.1) is None
