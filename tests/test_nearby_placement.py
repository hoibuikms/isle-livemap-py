"""Nearby creatures must be placed and followed: a creature without a class (the join tables were missed) learns its
position layout like a classified one, a creature that rested is picked up again after its anchor went stale, and
a long silence between two batches is bridged when the two candidates continue each other.

Synthetic data only (the shapes come from the recorded sessions)."""

from __future__ import annotations

import pytest

from isle_livemap.decode import ActorLocationTracker, _vector_at, vector_to_cm
from isle_livemap.peers import GamePeerSource

from test_pawn_vitals import ME, vector_batch


def _at(dx_m: float, dy_m: float = 0.0):
    return (ME[0] + dx_m * 100.0, ME[1] + dy_m * 100.0, ME[2])


# ---------------------------------------------------------------- bootstrap across a long silence
def test_two_sparse_batches_a_few_seconds_apart_place_a_resting_creature():
    t = ActorLocationTracker()
    assert t.bootstrap(*vector_batch(_at(200.0)), 10.0, ME) is None
    # 10.5 s later (more than MAX_GAP): the one candidate of each batch continues the other at walking speed
    loc = t.bootstrap(*vector_batch(_at(215.0, 4.0)), 20.5, ME)
    assert loc is not None and (loc.x, loc.y) == pytest.approx(_at(215.0, 4.0)[:2])


def test_a_long_silence_with_a_jump_is_not_bridged():
    t = ActorLocationTracker()
    assert t.bootstrap(*vector_batch(_at(200.0)), 10.0, ME) is None
    assert t.bootstrap(*vector_batch(_at(700.0)), 20.5, ME) is None  # 500 m in 10 s: another vector, not the creature
    assert t.bootstrapped == 0


def test_a_silence_beyond_the_limit_forgets_the_candidate():
    t = ActorLocationTracker()
    assert t.bootstrap(*vector_batch(_at(200.0)), 10.0, ME) is None
    assert t.bootstrap(*vector_batch(_at(200.0)), 10.0 + t.BOOT_LONG_GAP + 5.0, ME) is None


def test_a_creature_whose_anchor_went_stale_is_picked_up_again():
    t = ActorLocationTracker()
    t.note_creation(5, _at(200.0), 0.0)
    assert t.bootstrap(*vector_batch(_at(240.0)), 50.0, ME) is None  # fresh anchor: update() follows it, not the bootstrap
    late = t.ANCHOR_LIFETIME + 30.0
    assert t.bootstrap(*vector_batch(_at(240.0)), late, ME) is None  # stale: the first candidate is only remembered ...
    loc = t.bootstrap(*vector_batch(_at(243.0)), late + 1.0, ME)  # ... the second one continues it
    assert loc is not None and t.last_known(5)[0] == pytest.approx(_at(243.0)[0])


# ---------------------------------------------------------------- creatures without a class
def source() -> GamePeerSource:
    g = GamePeerSource()
    g._last_t = 100.0
    return g


def test_an_actor_with_the_batches_of_a_pawn_counts_as_a_creature_but_a_singleton_never_does():
    g = source()
    for _ in range(g.SHAPE_MIN_BATCHES):
        g._note_shape(100_500, 200)
        g._note_shape(2, 200)  # a low handle: the game's own singleton
    assert g._creature_like(100_500) and not g._creature_like(2)


def test_animal_sized_batches_and_few_batches_are_not_a_creature():
    g = source()
    for _ in range(40):
        g._note_shape(100_501, 155)
    for _ in range(g.SHAPE_MIN_BATCHES - 1):
        g._note_shape(100_502, 200)
    assert not g._creature_like(100_501) and not g._creature_like(100_502)


def test_a_creation_record_with_a_maximum_health_makes_a_creature_at_once():
    g = source()
    g._creatures.add(100_503)
    assert g._creature_like(100_503)
    g._drop(100_503)  # the server destroyed it: the next actor of that handle starts from nothing
    assert not g._creature_like(100_503)


def test_a_low_handle_with_a_pawn_profile_is_not_reported_as_a_hidden_creature():
    g = source()
    for _ in range(40):
        for handle in (2, 100_504):
            g._note_shape(handle, 120)
            g._note_unplaced(handle, 120, 100.0)
    assert g.hidden_creatures() == 1  # only the dynamic handle


# ---------------------------------------------------------------- where a species comes from
def _tracker_with(cache_path: str, votes: dict[str, int]):
    from collections import Counter

    from isle_livemap.classcache import ClassCache
    from isle_livemap.decode import ActorInfo, IdentityTracker

    tr = IdentityTracker(ClassCache(None, seed={0x1234: cache_path}))
    tr.actors[7] = ActorInfo(7, 55, None, 0x1234)
    tr._arch_votes[55] = Counter(votes)
    return tr


STEGO = "/Game/TheIsle/Core/Characters/Dinosaurs/Stegosaurus/BP_Stegosaurus.Default__BP_Stegosaurus_C"


def test_a_remembered_class_that_the_connections_own_assets_confirm_is_not_marked_inferred():
    tr = _tracker_with(STEGO, {"Stegosaurus": 3})
    assert tr.species_source(7) == "asset" and not tr.is_inferred(7)


def test_a_remembered_class_stays_inferred_without_or_against_the_assets():
    assert _tracker_with(STEGO, {}).species_source(7) == "seed"
    assert _tracker_with(STEGO, {"Triceratops": 3}).species_source(7) == "seed"
    assert _tracker_with(STEGO, {"Stegosaurus": 1}).species_source(7) == "seed"  # one creation is not evidence
    assert _tracker_with(STEGO, {}).is_inferred(7)


# ---------------------------------------------------------------- the seed builder
def test_the_seed_builder_derives_a_dinosaur_path_only_where_no_table_gave_one(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("build_class_seed", Path(__file__).resolve().parent.parent / "tools" / "build_class_seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    table_path = "/Game/TheIsle/Core/Characters/Dinosaurs/Allosaurus/BP_Allosaurus.Default__BP_Allosaurus_C"
    answers = {
        "a": ({1: {table_path}}, {1: {"/Game/TheIsle/Core/Characters/Dinosaurs/Allosaurus/BP_Allosaurus.Default__BP_Allosaurus_Cx"}, 2: {STEGO}}),
        "b": ({}, {2: {STEGO}, 3: {STEGO}}),
        "c": ({}, {3: {"/Game/TheIsle/Core/Characters/Dinosaurs/Triceratops/BP_Triceratops.Default__BP_Triceratops_C"}}),
    }
    monkeypatch.setattr(mod, "paths_of", lambda p: answers[p.name])
    seed, ambiguous = mod.build([tmp_path / "a", tmp_path / "b", tmp_path / "c"])
    assert seed[1] == table_path  # the tables win over the derived path
    assert seed[2] == STEGO  # two captures derived the same one
    assert 3 not in seed and ambiguous == 1  # two captures disagree: skipped


# ---------------------------------------------------------------- the same position path for every creature
def test_one_conversion_turns_quantised_integers_into_world_cm():
    assert vector_to_cm((12345, -6789, 100)) == pytest.approx((123.45, -67.89, 1.0))
    assert vector_to_cm((12345, -6789, 100), 16.0) == pytest.approx((1975.2, -1086.24, 16.0))  # a creation record's x16


def test_the_state_vector_decoder_uses_that_conversion():
    bits, batch = vector_batch((150_000.0, -320_000.0, 36_000.0))
    comp, x, y, z = _vector_at(bits, batch.data_bit_offset, batch.data_bit_count, 94, 22)
    assert (x, y, z) == pytest.approx(vector_to_cm((15_000_000, -32_000_000, 3_600_000)))


# ---------------------------------------------------------------- the false-lock guards of the bootstrap
def test_a_loose_continuation_needs_a_third_consistent_sample_but_a_tight_one_does_not():
    t = ActorLocationTracker()
    assert t.bootstrap(*vector_batch(_at(200.0)), 10.0, ME) is None
    # 40 m in 0.5 s (80 m/s): inside the loose radius, outside the tight one (about 25 m): one more sample is needed
    assert t.bootstrap(*vector_batch(_at(240.0)), 10.5, ME) is None
    loc = t.bootstrap(*vector_batch(_at(260.0)), 11.0, ME)  # continues the second sample at walking pace
    assert loc is not None and (loc.x, loc.y) == pytest.approx(_at(260.0)[:2]) and t.bootstrapped == 1


def test_a_loose_match_that_is_not_continued_never_places_the_creature():
    t = ActorLocationTracker()
    assert t.bootstrap(*vector_batch(_at(200.0)), 10.0, ME) is None
    assert t.bootstrap(*vector_batch(_at(240.0)), 10.5, ME) is None
    assert t.bootstrap(*vector_batch(_at(600.0)), 11.0, ME) is None  # 360 m in 0.5 s: the chain is broken
    assert t.bootstrapped == 0


def test_a_vector_at_the_world_origin_is_not_believed_while_the_player_is_far_from_it():
    far = (110_000.0, 0.0, 15_000.0)  # 1.1 km from the origin
    near_origin = (30_000.0, 0.0, 15_000.0)  # 300 m from it, 800 m from the player: inside the search radius
    t = ActorLocationTracker()
    assert t.bootstrap(*vector_batch(near_origin), 10.0, far) is None
    assert t.bootstrap(*vector_batch((30_100.0, 0.0, 15_000.0)), 10.8, far) is None
    assert t.bootstrapped == 0 and t.last_known(5) is None
    spawn = (10_000.0, 0.0, 15_000.0)  # the player is there too: a creature next to them is real
    assert t.bootstrap(*vector_batch((20_000.0, 0.0, 15_000.0)), 20.0, spawn) is None
    assert t.bootstrap(*vector_batch((20_100.0, 0.0, 15_000.0)), 20.8, spawn) is not None


def test_creatures_are_only_searched_for_around_a_locked_own_pawn():
    g = source()
    g._own_position = lambda: ME
    assert g._bootstrap_origin() is None  # the position before the lock is a spawn transition
    g.own_handle = 7
    assert g._bootstrap_origin() == ME


def test_the_store_keeps_the_packet_time_of_the_last_position():
    from isle_livemap.geo import Entity
    from isle_livemap.peers import PeerStore

    clock = [100.0]
    store = PeerStore(clock=lambda: clock[0])
    store.update(Entity("9", "dino", "x", "", 1.0, 2.0), at=97.5)
    clock[0] = 101.0
    store.update(Entity("9", "dino", "x", "", 1.0, 2.0, health_pct=80.0))  # a state batch without a position
    (p,) = store.entities()
    assert p.sample_at == 97.5 and p.last_seen == 101.0
    store.update(Entity("9", "dino", "x", "", 3.0, 2.0), at=100.5)
    assert store.entities()[0].sample_at == 100.5
