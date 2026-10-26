"""Health and stamina of other creatures between their (rare) full blocks, positions of creatures without an anchor,
the steadier overlay line and the stamina on the label.

Synthetic data only. The shapes come from the recorded sessions: a full 8-slot block (health, maxHealth, stamina,
maxHealth, half, half, maxHealth, maxHealth) now and then, and in between a 179-bit state batch carrying the pair
health + stamina 66 bits apart (health 2192.47 -> 2274.58 in steps of ~0.56 over 80 s for a 2620.49 maximum)."""

from __future__ import annotations

import struct

import pytest

from isle_livemap.decode import (
    ACTOR_STATE_HEADER_BITS, ActorLocationTracker, Bits, IrisBatch, PawnVitalsTracker, attribute_slots,
)
from isle_livemap.geo import Entity, Nearby, entity_label_lines
from isle_livemap.replay import BitWriter

HDR = 0x0C0000348
SLOT = 66


def _raw(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def attribute_batch(slots: dict[int, float], total: int = 700, handle: int = 9, header: int = HDR):
    """A state batch with GAS attribute slots (float, flag, float; 66 bit stride) at the given offsets."""
    w = BitWriter()
    w.write(header, ACTOR_STATE_HEADER_BITS)
    pos = ACTOR_STATE_HEADER_BITS
    for off in sorted(slots):
        w.write(0, off - pos)
        raw = _raw(slots[off])
        w.write(raw, 32)
        w.write(1, 1)
        w.write(raw, 32)
        pos = off + 65
    w.write(0, total - w.nbits)
    return Bits(w.to_bytes()), IrisBatch(handle, 0, w.nbits, True, False)


def block8(at: int, health: float, max_health: float, stamina: float) -> dict[int, float]:
    seq = [health, max_health, stamina, max_health, max_health / 2, max_health / 2, max_health, max_health]
    return {at + SLOT * i: v for i, v in enumerate(seq)}


def block9(at: int, health: float, max_health: float, stamina: float, max_stamina: float) -> dict[int, float]:
    seq = [health, max_health, stamina, max_stamina, max_health, max_health / 2, max_health / 2, max_health, max_health]
    return {at + SLOT * i: v for i, v in enumerate(seq)}


def pair(at: int, health: float, stamina: float | None = None, total: int = 179) -> tuple[Bits, IrisBatch]:
    slots = {at: health}
    if stamina is not None:
        slots[at + SLOT] = stamina
    return attribute_batch(slots, total)


# ---------------------------------------------------------------- full blocks
def test_attribute_slots_finds_the_flagged_duplicated_floats():
    bits, batch = attribute_batch({47: 2192.46875, 113: 533.84625})
    assert {off: round(v, 2) for off, v in attribute_slots(bits, batch)} == {47: 2192.47, 113: 533.85}


def test_eight_slot_block_gives_health_and_names_no_stamina_percentage():
    t = PawnVitalsTracker()
    bits, batch = attribute_batch(block8(100, 2255.09, 2620.49, 623.95))
    v = t.update(bits, batch)
    assert v is not None and v.health_pct == pytest.approx(86.06, abs=0.05)
    assert v.stamina == pytest.approx(623.95, abs=0.01) and v.stamina_pct is None  # the maximum was not in the block
    assert t.blocks == 1


def test_nine_slot_block_gives_both_percentages():
    t = PawnVitalsTracker()
    bits, batch = attribute_batch(block9(100, 144.16, 163.32, 306.56, 938.28))
    v = t.update(bits, batch)
    assert v.health_pct == pytest.approx(88.27, abs=0.05) and v.stamina_pct == pytest.approx(32.67, abs=0.05)


def test_five_unrelated_slots_are_not_a_block():
    t = PawnVitalsTracker()
    bits, batch = attribute_batch({100 + SLOT * i: 10.0 + i for i in range(8)})
    assert t.update(bits, batch) is None


# ---------------------------------------------------------------- following the pair
def seeded(stamina_max: bool = True) -> PawnVitalsTracker:
    t = PawnVitalsTracker()
    bits, batch = attribute_batch(block9(100, 2192.47, 2620.49, 533.85, 640.0) if stamina_max
                                  else block8(100, 2192.47, 2620.49, 533.85))
    assert t.update(bits, batch) is not None
    return t


def test_the_pair_is_followed_by_continuity():
    t = seeded()
    v = t.update(*pair(47, 2193.03, 535.82))
    assert v.health == pytest.approx(2193.03, abs=0.01) and v.stamina == pytest.approx(535.82, abs=0.01)
    assert v.health_pct == pytest.approx(83.69, abs=0.05) and v.stamina_pct == pytest.approx(83.7, abs=0.1)
    assert t.followed == 1


def test_a_lone_health_slot_close_to_the_last_value_is_followed():
    t = seeded()
    v = t.update(*pair(296, 2195.29, None, total=362))
    assert v.health == pytest.approx(2195.29, abs=0.01) and v.stamina == pytest.approx(533.85, abs=0.01)


def test_values_that_do_not_continue_the_last_ones_are_ignored():
    t = seeded()
    assert t.update(*pair(47, 100.0, None)) is None  # a lone slot far from the health
    assert t.update(*pair(47, 9999.0, 5.0)) is None  # above the maximum
    assert t.get(9).health == pytest.approx(2192.47, abs=0.01)


def test_a_confirmed_offset_takes_a_big_drop_a_stranger_does_not():
    t = seeded()
    bite = pair(47, 1500.0, 500.0)  # 26 % of the maximum in one step: not "close" to the last health
    assert t.update(*bite) is None  # first time at this offset: not confirmed
    for h in (2193.0, 2194.0):  # two small steps confirm (header, offset) as a health slot
        assert t.update(*pair(47, h, 536.0)) is not None
    v = t.update(*bite)
    assert v is not None and v.health_pct == pytest.approx(57.2, abs=0.1)


def test_nothing_is_followed_before_the_first_block():
    t = PawnVitalsTracker()
    assert t.update(*pair(47, 2193.03, 535.82)) is None and t.get(9) is None


def test_forget_and_reset():
    t = seeded()
    t.forget(9)
    assert t.get(9) is None and t.update(*pair(47, 2193.03, 535.82)) is None
    seeded_again = seeded()
    seeded_again.reset()
    assert seeded_again.blocks == 0 and seeded_again.get(9) is None


# ---------------------------------------------------------------- position of a creature without an anchor
def vector_batch(vec, offset=94, comp=26, total=200, handle=5):
    w = BitWriter()
    w.write(HDR, ACTOR_STATE_HEADER_BITS)
    w.write(0xA5A5A5A5A5 & ((1 << (offset - ACTOR_STATE_HEADER_BITS)) - 1), offset - ACTOR_STATE_HEADER_BITS)
    w.write(comp | 64, 7)
    for v in vec:
        w.write(round(v * 100), comp)
    w.write(0, total - w.nbits)
    return Bits(w.to_bytes()), IrisBatch(handle, 0, w.nbits, True, False)


ME = (130_000.0, -320_000.0, 36_000.0)


def test_bootstrap_places_an_unanchored_creature_on_its_second_batch_near_the_player():
    t = ActorLocationTracker()
    first = t.bootstrap(*vector_batch((130_000.0 + 20_000.0, -320_000.0, 36_000.0)), 10.0, ME)
    assert first is None  # one candidate proves nothing
    loc = t.bootstrap(*vector_batch((130_000.0 + 20_100.0, -320_050.0, 36_000.0)), 10.8, ME)
    assert loc is not None and (loc.x, loc.y) == pytest.approx((150_100.0, -320_050.0))
    assert t.last_known(5) == pytest.approx((150_100.0, -320_050.0, 36_000.0)) and t.bootstrapped == 1


def test_bootstrap_rejects_far_from_the_player_and_jumps_between_batches():
    t = ActorLocationTracker()
    assert t.bootstrap(*vector_batch((ME[0] + 300_000.0, ME[1], ME[2])), 10.0, ME) is None  # 3 km away
    assert t.bootstrap(*vector_batch((ME[0] + 300_100.0, ME[1], ME[2])), 10.8, ME) is None
    assert t.bootstrap(*vector_batch((ME[0] + 20_000.0, ME[1], ME[2])), 11.0, ME) is None
    assert t.bootstrap(*vector_batch((ME[0] - 40_000.0, ME[1], ME[2])), 11.5, ME) is None  # 600 m in 0.5 s
    assert t.bootstrap(*vector_batch((ME[0] + 20_000.0, ME[1], ME[2])), 12.0, None) is None  # own position unknown
    assert t.bootstrapped == 0 and t.last_known(5) is None


def test_bootstrap_leaves_an_anchored_creature_alone():
    t = ActorLocationTracker()
    t.note_creation(5, (ME[0] + 20_000.0, ME[1], ME[2]), 9.0)
    assert t.bootstrap(*vector_batch((ME[0] + 20_000.0, ME[1], ME[2])), 10.0, ME) is None


# ---------------------------------------------------------------- what the label says
def test_label_tail_carries_health_and_stamina():
    e = Entity("7", "dino", "Actor #7", "Allosaurus", 0.0, 0.0, health_pct=86.0, stamina_pct=33.4)
    head, tail = entity_label_lines(Nearby(e, 0.0, 0.0, 100.0, 90.0))
    assert head == "Allosaurus - 100 m" and tail == "86% - HP – - ST 33%"  # no maximum health known: the points are an en dash
    sized = Entity("7", "dino", "Actor #7", "Allosaurus", 0.0, 0.0, health_pct=86.0, stamina_pct=33.4, max_health=1650.0)
    assert entity_label_lines(Nearby(sized, 0.0, 0.0, 100.0, 90.0))[1] == "86% - HP 1419 - ST 33%"  # 86 % of 1650
    bare = Entity("7", "dino", "Actor #7", "Allosaurus", 0.0, 0.0)
    tail = entity_label_lines(Nearby(bare, 0.0, 0.0, 100.0, 90.0))[1]
    assert tail == ""  # never read: no second line, nothing guessed
