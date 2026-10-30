"""The scale of replicated positions: raw / 128 cm for the server's state and creation vectors, raw / 100 cm for the client's
own move packets. Read with the /100 of the move packets every other creature was 1.28 x too far from the world origin, so a
player standing next to the user was drawn 650-850 m away (see docs/PEERS.md, "Replicated positions are raw / 128")."""

from __future__ import annotations

import math

import pytest

from isle_livemap.decode import (
    ACTOR_STATE_HEADER_BITS, CREATION_LOCATION_SCALE, STATE_VECTOR_SCALE, ActorLocationTracker, Bits, IrisBatch, _bits_needed,
    decode_movement, vector_to_cm,
)
from isle_livemap.replay import BitWriter, encode_move_payload

HDR = 0x0C0000348
# A user standing 3.0 km from the world origin (east 2 440 m, north 1 770 m), the distance of the recorded cases.
USER_CM = (244_000.0, -177_000.0, 22_500.0)


def state_batch(vec_cm, handle=5, offset=94, total=260):
    """A pawn state batch whose position vector holds ``vec_cm`` as the server sends it: raw = cm * 128."""
    raw = [round(v * 128) for v in vec_cm]
    comp = max(_bits_needed(r) for r in raw)  # the decoder believes only the canonical width
    w = BitWriter()
    w.write(HDR, ACTOR_STATE_HEADER_BITS)
    w.write(0xA5A5A5A5A5 & ((1 << (offset - ACTOR_STATE_HEADER_BITS)) - 1), offset - ACTOR_STATE_HEADER_BITS)
    w.write(comp | 64, 7)
    for r in raw:
        w.write(r, comp)
    w.write(0, total - w.nbits)
    return Bits(w.to_bytes()), IrisBatch(handle, 0, w.nbits, True, False)


def test_the_scales_are_the_recorded_ones():
    assert STATE_VECTOR_SCALE == pytest.approx(100.0 / 128.0)
    assert CREATION_LOCATION_SCALE == pytest.approx(12.5)  # the creation vector is 1/16 of the state vector, same actor
    assert vector_to_cm((12_800, -25_600, 128)) == pytest.approx((128.0, -256.0, 1.28))  # the move packets' own /100
    assert vector_to_cm((12_800, -25_600, 128), STATE_VECTOR_SCALE) == pytest.approx((100.0, -200.0, 1.0))  # replicated: /128


def test_a_creature_at_the_users_place_decodes_to_the_users_position():
    """The user's move packet (raw / 100) and a creature standing on the same spot (raw / 128) give the same world position;
    with the /100 reading the creature came out 28 % further from the origin (a 3.0 km user: 850 m away)."""
    own = decode_movement(encode_move_payload(*USER_CM, 10.0, 41.0))
    assert own and (own[0].x, own[0].y, own[0].z) == pytest.approx(USER_CM, abs=0.01)

    t = ActorLocationTracker()
    t.note_creation(5, (USER_CM[0] + 1_000.0, USER_CM[1] - 500.0, USER_CM[2]), 9.0)  # an anchor 11 m away, a second ago
    bits, batch = state_batch(USER_CM)
    loc = t.update(bits, batch, 10.0, learn=True)
    assert loc is not None
    assert (loc.x, loc.y, loc.z) == pytest.approx(USER_CM, abs=1.0)
    assert math.hypot(loc.x - USER_CM[0], loc.y - USER_CM[1]) / 100.0 < 1.0  # next to the user, not 850 m away


def test_the_old_reading_put_a_neighbour_850_m_away():
    """The failure this guards: the same raw numbers read with 1/100 instead of 1/128."""
    raw = [round(v * 128) for v in USER_CM]
    wrong = vector_to_cm(raw)  # the move-packet scale applied to a replicated vector
    away = math.hypot(wrong[0] - USER_CM[0], wrong[1] - USER_CM[1]) / 100.0
    assert 800 < away < 900
    right = vector_to_cm(raw, STATE_VECTOR_SCALE)
    assert math.hypot(right[0] - USER_CM[0], right[1] - USER_CM[1]) / 100.0 < 0.01
