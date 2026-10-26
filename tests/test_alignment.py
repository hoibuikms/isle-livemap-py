"""Manual alignment of the own position, and detection of a position jump (decoded position moves kilometres at standstill
while the client clock carries on): recorded session ``m`` (a crocodile), where the own marker and the other players
were not in one frame for the user."""

import pytest

from isle_livemap.capture import INBOUND, OUTBOUND, Datagram, TelemetryPipeline
from isle_livemap.replay import encode_move_payload

CLIENT, SERVER = ("192.168.0.10", 50_000), ("203.0.113.5", 7777)


def feed(pipe, t0, n, x, y, ts0, dt=0.1, z=300.0, yaw=10.0):
    """n moves at (x, y) metres, dt apart, the client clock running with the wall clock."""
    for i in range(n):
        payload = encode_move_payload(x * 100.0, y * 100.0, z * 100.0, yaw, ts0 + i * dt)
        pipe.handle(Datagram(t0 + i * dt, *CLIENT, *SERVER, payload, OUTBOUND))
    return t0 + n * dt, ts0 + n * dt


def locked(x=1000.0, y=-2000.0):
    pipe = TelemetryPipeline()
    t, ts = feed(pipe, 100.0, 20, x, y, 40.0)
    assert pipe.latest.position is not None
    return pipe, t, ts


def test_align_to_moves_the_own_position_and_it_stays_aligned():
    pipe, t, ts = locked()
    raw = pipe.latest.position
    assert pipe.align_to(raw.x + 50_000.0, raw.y - 20_000.0)
    assert (pipe.latest.position.x, pipe.latest.position.y) == pytest.approx((raw.x + 50_000.0, raw.y - 20_000.0))
    assert pipe.alignment_m == pytest.approx((500.0, -200.0))
    feed(pipe, t, 5, 1000.0, -2000.0, ts)  # the next moves are shifted the same way
    assert (pipe.latest.position.x, pipe.latest.position.y) == pytest.approx((raw.x + 50_000.0, raw.y - 20_000.0))
    assert "aligned +500, -200 m" in pipe.status_text()


def test_clear_alignment_and_reset_restore_the_decoded_position():
    pipe, t, ts = locked()
    raw = pipe.latest.position
    pipe.align_to(raw.x + 10_000.0, raw.y)
    pipe.clear_alignment()
    assert (pipe.latest.position.x, pipe.latest.position.y) == pytest.approx((raw.x, raw.y))
    assert pipe.alignment_m == (0.0, 0.0) and "aligned" not in pipe.status_text()
    pipe.align_to(raw.x + 10_000.0, raw.y)
    pipe.reset()
    assert pipe.alignment_m == (0.0, 0.0) and pipe.latest.position is None


def test_align_to_needs_a_position():
    assert TelemetryPipeline().align_to(1.0, 2.0) is False


def test_a_jump_at_standstill_with_the_client_clock_running_is_counted():
    pipe, t, ts = locked(1000.0, -2000.0)
    assert pipe.jumps == []
    t2, ts2 = feed(pipe, t + 3.0, 60, 1850.0, -1100.0, ts + 3.0)  # 1.2 km away after 3 s of silence; the client clock carried on
    assert pipe.latest.position is not None and abs(pipe.latest.position.x / 100.0 - 1850.0) < 1.0  # and the marker follows
    assert len(pipe.jumps) == 1 and pipe.jumps[0][1] == pytest.approx(1237.0, abs=30.0)
    assert pipe.counts()["position_jumps"] == 1


def test_a_respawn_restarts_the_client_clock_and_is_not_a_jump():
    pipe, t, ts = locked(1000.0, -2000.0)
    feed(pipe, t + 3.0, 60, 1850.0, -1100.0, 2.0)  # the clock starts again from ~0
    assert pipe.jumps == []


def test_an_alignment_is_marked_stale_when_the_position_jumps_afterwards():
    pipe, t, ts = locked(1000.0, -2000.0)
    raw = pipe.latest.position
    pipe.align_to(raw.x + 10_000.0, raw.y)
    feed(pipe, t + 3.0, 60, 1850.0, -1100.0, ts + 3.0)
    assert "align again" in pipe.status_text()


def test_a_constant_vector_with_a_constant_or_zero_clock_is_never_the_own_position():
    pipe = TelemetryPipeline()
    for ts in (0.0, 9472.25):  # state that looks like a location, not a move
        for i in range(40):
            payload = encode_move_payload(-52_700.0, -47_700.0, 31_900.0, 10.0, ts)
            pipe.handle(Datagram(100.0 + i * 0.05, *CLIENT, *SERVER, payload, OUTBOUND))
        assert pipe.latest.position is None
    pipe.handle(Datagram(110.0, *SERVER, *CLIENT, b"\x00" * 8, INBOUND))
