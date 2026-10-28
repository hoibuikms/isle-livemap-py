"""The world position of another creature is read from its own packets, in world coordinates: nothing about the local
player (position, lock state, handle) may change it. ``tools/tier_check.py --invariance`` proves it on whole recordings;
this is the same replay on the recorded join, plus the facts the decoder relies on instead.

Synthetic data and the sanitised recorded join only."""

from __future__ import annotations

import pytest

from isle_livemap.capture import INBOUND, Datagram
from isle_livemap.decode import PLAYABLE_X_CM, PLAYABLE_Y_CM
from isle_livemap.peers import GamePeerSource
from isle_livemap.replay import CLIENT_IP, CLIENT_PORT, SERVER_IP, SERVER_PORT

OWN_HANDLE = 103_940
FAR_AWAY = (-450_000.0, 450_000.0, 0.0)


def _fixes(join_packets, variant: str) -> set:
    clock = lambda: 100.0  # noqa: E731 - the peer source only needs a steady clock here
    g = GamePeerSource(clock=clock)
    if variant == "far":
        g._own_position = lambda: FAR_AWAY
    elif variant == "shift":
        g._own_position = lambda: (442_020.0 + 500_000.0, -162_148.0 + 500_000.0, 26_009.0)
    elif variant == "nolock":
        g._own_position = lambda: None
    out: set = set()
    emit = g._emit

    def spy(handle, x, y, z, kind, species, name, source=""):
        out.add((handle, round(g._last_t * 1000), round(x, 1), round(y, 1), source))
        emit(handle, x, y, z, kind, species, name, source)

    g._emit = spy
    for rec in join_packets:
        d = Datagram(1_000.0 + rec["t"], SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT, rec["payload"], INBOUND)
        g.on_inbound(d, None if variant == "nolock" else OWN_HANDLE)
    return {f for f in out if f[0] != OWN_HANDLE}


def test_every_position_of_another_creature_is_the_same_whatever_the_own_position_or_lock(join_packets):
    normal = _fixes(join_packets, "normal")
    assert normal, "the recorded join must put creatures on the map for this test to mean anything"
    for variant in ("far", "shift", "nolock"):
        assert _fixes(join_packets, variant) == normal, variant


def test_the_playable_map_is_the_gateway_map_with_a_kilometre_to_spare():
    from isle_livemap import geo

    assert PLAYABLE_X_CM == pytest.approx(((-geo.X_OFFSET) * geo.WORLD_SCALE - 100_000.0, (geo.MAP_WIDTH - geo.X_OFFSET) * geo.WORLD_SCALE + 100_000.0))
    assert PLAYABLE_Y_CM == pytest.approx(((-geo.Y_OFFSET) * geo.WORLD_SCALE - 100_000.0, (geo.MAP_HEIGHT - geo.Y_OFFSET) * geo.WORLD_SCALE + 100_000.0))


def test_nothing_in_the_decoder_or_the_source_reads_the_own_position_to_place_another_creature():
    import inspect

    from isle_livemap import decode, peers

    assert "origin" not in inspect.signature(decode.ActorLocationTracker.bootstrap).parameters
    assert not hasattr(peers.GamePeerSource, "_bootstrap_origin")
    body = inspect.getsource(peers.GamePeerSource._on_inbound)
    assert "_own_position" not in body  # the own position is only read to describe (distance, bearing), never to decode


def test_the_own_position_is_not_computed_from_other_creatures(join_packets, hexb):
    """The other direction: the same outbound moves give the same own marker with and without the creatures' packets."""
    import base64  # noqa: F401
    from conftest import retimed
    from isle_livemap.capture import OUTBOUND, TelemetryPipeline

    move = hexb("StationaryPayload")

    def run(with_creatures: bool):
        pipe = TelemetryPipeline()
        inbound_packets = iter(join_packets if with_creatures else [])
        pending = next(inbound_packets, None)
        for i in range(60):
            at = 1_000.0 + i * 0.05
            pipe.handle(Datagram(at, CLIENT_IP, CLIENT_PORT, SERVER_IP, SERVER_PORT, retimed(move, i * 0.05), OUTBOUND))
            while pending is not None and 1_000.0 + pending["t"] % 3.0 <= at:
                pipe.handle(Datagram(at, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT, pending["payload"], INBOUND))
                pending = next(inbound_packets, None)
        pos = pipe.latest.position
        return None if pos is None else (round(pos.x, 2), round(pos.y, 2), round(pos.z, 2))

    alone = run(False)
    assert alone is not None
    assert run(True) == alone
