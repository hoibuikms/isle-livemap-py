"""Two things that drew "players" 650-850 m away that never moved (recorded session l, a user report: "players right next to me
are shown at 600-800 m"):

* a dead creature stays in the world as a carcass: a pawn actor created exactly where a TICorpseSmell actor sits, that sends
  a ~1 Hz 163 / 172 / 179 bit state batch (a timer) and never a position. It was drawn as a player-owned dinosaur at its
  spawn point. A carcass is not drawn.
* a creature drawn only at its creation record's spawn point that keeps sending state is active, so the spawn point (where it
  entered the relevancy range) is not where it is: it is listed as "position unknown", not drawn there.

Synthetic data only; the real ``_on_inbound`` loop is driven."""

from __future__ import annotations

import types

import pytest

import isle_livemap.decode as decode
from isle_livemap.capture import INBOUND, Datagram
from isle_livemap.decode import ActorInfo, Creation, IrisBatch, IrisPacket, KIND_STATE
from isle_livemap.peers import GamePeerSource
from isle_livemap.replay import BitWriter

from test_resume_follow import ME, east, feed, source  # noqa: F401 - the fixture and the position feeder

SMELL = "/Game/TheIsle/Core/TICorpseSmell.Default__TICorpseSmell"
ALLO = "/Game/TheIsle/Core/Characters/Dinosaurs/Allosaurus/BP_Allosaurus.Default__BP_Allosaurus_C"


MOVING = GamePeerSource.MOVE_HEADER_MASK  # a state header with the movement member dirty (see GamePeerSource.MOVE_HEADER_MASK)


def feed_bits(g: GamePeerSource, monkeypatch, handle: int, nbits: int, t: float, header: int = 0) -> None:
    """One state batch of ``nbits`` bits (no position vector in it) from ``handle``; ``header`` is the 33-bit state header."""
    w = BitWriter()
    w.write(header, 33)
    w.write(0, nbits - 33)
    packet = IrisPacket(1, True, True, [IrisBatch(handle, 0, nbits, True, False)])
    monkeypatch.setattr(decode, "parse_iris_packet", lambda p: packet)
    g._on_inbound(Datagram(t, "10.0.0.2", 7777, "192.168.0.10", 50_000, w.to_bytes(), INBOUND))  # noqa: SLF001


def creation_packet(g: GamePeerSource, monkeypatch, creations: list[tuple[int, str, tuple[float, float, float]]], t: float) -> None:
    """A datagram whose identity tracker reports these creation records: (handle, class path, spawn position)."""
    paths = {}
    made = []
    for handle, path, loc in creations:
        paths[handle] = path
        g.identity.actors[handle] = ActorInfo(handle, None, loc, 100 + handle)
        made.append(Creation(handle, None, 100 + handle, loc, 0))
    g.identity.class_path = lambda h: paths.get(h)
    g.identity.feed = lambda payload, packet: made
    packet = IrisPacket(1, True, True, [])
    monkeypatch.setattr(decode, "parse_iris_packet", lambda p: packet)
    g._on_inbound(Datagram(t, "10.0.0.2", 7777, "192.168.0.10", 50_000, b"\x00" * 8, INBOUND))  # noqa: SLF001
    g.identity.feed = lambda payload, packet: []


# ---------------------------------------------------------------- carcass: created on a corpse-smell actor
def test_a_creature_created_exactly_where_a_corpse_smell_actor_sits_is_a_carcass_and_is_not_drawn(source, monkeypatch):
    g = source
    spot = (ME[0] + 70_000.0, ME[1], ME[2])
    creation_packet(g, monkeypatch, [(9001, SMELL, spot)], 10.0)  # the smell actors come first (the join burst)
    creation_packet(g, monkeypatch, [(9002, ALLO, (spot[0] + 1.4, spot[1] - 0.6, spot[2]))], 90.0)  # the body, 1.5 cm away
    assert 9002 in g._corpses and g.store.get("9002") is None  # noqa: SLF001
    feed_bits(g, monkeypatch, 9002, 300, 91.0)
    assert g.filtered_corpses == 1 and g.store.get("9002") is None
    assert 9002 not in {r["handle"] for r in g.unplaced_creatures()}


def test_a_creature_created_a_few_metres_from_a_corpse_smell_actor_is_alive(source, monkeypatch):
    g = source
    spot = (ME[0] + 70_000.0, ME[1], ME[2])
    creation_packet(g, monkeypatch, [(9001, SMELL, spot)], 10.0)
    creation_packet(g, monkeypatch, [(9002, ALLO, (spot[0] + 500.0, spot[1], spot[2]))], 90.0)  # 5 m: a dinosaur eating it
    assert 9002 not in g._corpses  # noqa: SLF001
    e = g.store.get("9002")
    assert e is not None and e.kind == "dino" and e.pos_source == "spawn"


def test_a_new_creation_on_a_reused_handle_is_not_a_carcass_any_more(source, monkeypatch):
    g = source
    spot = (ME[0] + 70_000.0, ME[1], ME[2])
    creation_packet(g, monkeypatch, [(9001, SMELL, spot), (9002, ALLO, spot)], 10.0)
    assert 9002 in g._corpses  # noqa: SLF001
    creation_packet(g, monkeypatch, [(9002, ALLO, (spot[0] + 20_000.0, spot[1], spot[2]))], 200.0)  # a live creature on the handle
    assert 9002 not in g._corpses and g.store.get("9002") is not None  # noqa: SLF001


# ---------------------------------------------------------------- carcass: the timer-sized batches
def test_an_actor_that_only_sends_the_timer_sized_batch_becomes_a_carcass(source, monkeypatch):
    g = source
    g._last_t = 5.0  # noqa: SLF001
    g._emit(9100, *east(700.0), "dino", "Pteranodon", "", "spawn")  # noqa: SLF001
    assert g.store.get("9100") is not None
    for i, nbits in enumerate((172, 172, 179, 172, 163, 172, 172)):
        feed_bits(g, monkeypatch, 9100, nbits, 10.0 + i)
    assert g.store.get("9100") is not None  # seven batches are not enough
    feed_bits(g, monkeypatch, 9100, 172, 17.0)
    assert 9100 in g._corpses and g.store.get("9100") is None  # noqa: SLF001
    feed_bits(g, monkeypatch, 9100, 172, 18.0)
    assert g.filtered_corpses == 2  # the batch that decided it and the next one are both skipped


def test_an_actor_whose_batches_are_mostly_other_sizes_is_not_a_carcass(source, monkeypatch):
    g = source
    g._last_t = 5.0  # noqa: SLF001
    g._emit(9101, *east(700.0), "dino", "Allosaurus", "", "spawn")  # noqa: SLF001
    for i in range(16):
        feed_bits(g, monkeypatch, 9101, 172 if i % 2 else 300, 10.0 + i * 0.4)  # half of them: a pawn that is just resting
    assert 9101 not in g._corpses  # noqa: SLF001


def test_a_creature_that_moved_needs_a_long_run_of_timer_batches_before_it_is_called_a_carcass(source, monkeypatch):
    g = source
    g._last_t = 1.0  # noqa: SLF001
    g._creatures.add(9102)  # noqa: SLF001 - a creature: its position layout is learned
    g._emit(9102, *east(300.0), "dino", "Allosaurus", "")  # noqa: SLF001
    g.locator.note_creation(9102, east(300.0), 1.0)
    feed(g, monkeypatch, 9102, east(302.0), 2.0)  # a decoded position: it is alive
    assert g.store.get("9102") is not None and g.store.get("9102").pos_source == "" and g.locator.fixed + g.locator.learned >= 1
    t = 3.0
    for _ in range(11):
        feed_bits(g, monkeypatch, 9102, 172, t)
        t += 1.0
    assert 9102 not in g._corpses  # noqa: SLF001 - 11 in a row: a resting pawn
    feed_bits(g, monkeypatch, 9102, 172, t)
    assert 9102 in g._corpses and g.store.get("9102") is None  # noqa: SLF001 - 12: it died and lies there


# ---------------------------------------------------------------- stale spawn point
def test_a_creature_drawn_only_at_its_spawn_point_that_moved_and_keeps_sending_state_is_taken_off_the_map_and_listed(source, monkeypatch):
    g = source
    g._last_t = 5.0  # noqa: SLF001
    g._creatures.add(9200)  # noqa: SLF001 - its creation record carried a maxHealth
    g._emit(9200, *east(750.0), "unknown", "", "", "spawn")  # noqa: SLF001
    for i in range(16):
        feed_bits(g, monkeypatch, 9200, 220, 10.0 + i, MOVING)  # 1 Hz, the movement member dirty: its initial state (grace)
    assert g.store.get("9200") is not None and g.stale_spawns == 0  # inside the grace the header says nothing
    feed_bits(g, monkeypatch, 9200, 220, 26.0, MOVING)  # 16 s after it appeared: this one is movement
    assert g.store.get("9200") is None and g.stale_spawns == 1
    feed_bits(g, monkeypatch, 9200, 220, 27.0, MOVING)
    rows = {r["handle"]: r for r in g.unplaced_creatures()}
    assert 9200 in rows and rows[9200]["batches"] >= 10  # listed as "position unknown": the Nearby table shows "?"


def test_a_pawn_that_stands_still_and_only_animates_stays_where_it_was_created(source, monkeypatch):
    """Recorded: a Spinosaurus sent 1 738 state batches (3 Hz) in 9 minutes and never a movement-bearing one after its first
    seconds; its handle was the same 1.5 hours earlier in another session. It is where its creation record says."""
    g = source
    g._last_t = 5.0  # noqa: SLF001
    g._creatures.add(9210)  # noqa: SLF001
    g._emit(9210, *east(750.0), "player", "Spinosaurus", "P1", "spawn")  # noqa: SLF001
    for i in range(6):
        feed_bits(g, monkeypatch, 9210, 70, 5.0 + i * 0.3, MOVING)  # its initial state: movement member dirty, no position
    for i in range(200):
        feed_bits(g, monkeypatch, 9210, 190, 8.0 + i * 0.33)  # animation / stance state at 3 Hz for a minute
    e = g.store.get("9210")
    assert e is not None and e.pos_source == "rest" and e.kind == "player"
    assert (e.x, e.y) == pytest.approx(east(750.0)[:2])
    assert g.stale_spawns == 0 and 9210 not in {r["handle"] for r in g.unplaced_creatures()}
    assert "(at rest)" in g.nearby_report()


def test_the_initial_state_of_a_pawn_does_not_count_as_movement(source, monkeypatch):
    g = source
    g._last_t = 5.0  # noqa: SLF001
    g._creatures.add(9211)  # noqa: SLF001
    g._emit(9211, *east(750.0), "unknown", "", "", "spawn")  # noqa: SLF001
    for i in range(30):
        feed_bits(g, monkeypatch, 9211, 220, 10.0 + i * 0.5, MOVING)  # 0.5 s .. 15 s after it appeared
    assert 9211 not in g._moved and g.store.get("9211") is not None  # noqa: SLF001
    feed_bits(g, monkeypatch, 9211, 220, 25.5, MOVING)  # first one after the grace
    assert 9211 in g._moved  # noqa: SLF001


def test_a_rest_is_replaced_by_a_decoded_position_and_a_spawn_does_not_replace_a_rest(source, monkeypatch):
    g = source
    g._last_t = 5.0  # noqa: SLF001
    g._creatures.add(9212)  # noqa: SLF001
    g._emit(9212, *east(750.0), "unknown", "", "", "spawn")  # noqa: SLF001
    for i in range(80):
        feed_bits(g, monkeypatch, 9212, 220, 10.0 + i)
    assert g.store.get("9212").pos_source == "rest"
    g._emit(9212, *east(900.0), "unknown", "", "", "spawn")  # noqa: SLF001 - a creation record on the handle again would reset it elsewhere
    assert g.store.get("9212").pos_source == "rest" and g.store.get("9212").x == pytest.approx(east(750.0)[0])
    g.locator.note_creation(9212, east(750.0), 90.0)
    feed(g, monkeypatch, 9212, east(752.0), 91.0)  # it walks: a decoded position
    e = g.store.get("9212")
    assert e.pos_source == "" and (e.x, e.y) == pytest.approx(east(752.0)[:2])


def test_a_resting_creature_that_sends_a_batch_now_and_then_keeps_its_spawn_dot_and_becomes_a_rest(source, monkeypatch):
    g = source
    g._last_t = 5.0  # noqa: SLF001
    g._creatures.add(9201)  # noqa: SLF001
    g._emit(9201, *east(750.0), "unknown", "", "", "spawn")  # noqa: SLF001
    for i in range(5):
        feed_bits(g, monkeypatch, 9201, 220, 10.0 + i * 5.0)  # one batch every 5 s for 25 s
    assert g.store.get("9201").pos_source == "spawn"  # not yet 30 s since it appeared
    for i in range(5, 12):
        feed_bits(g, monkeypatch, 9201, 220, 10.0 + i * 5.0)
    e = g.store.get("9201")
    assert e is not None and e.pos_source == "rest" and g.stale_spawns == 0


def test_a_decoded_position_puts_a_taken_off_creature_back_on_the_map(source, monkeypatch):
    g = source
    g._last_t = 5.0  # noqa: SLF001
    g._creatures.add(9202)  # noqa: SLF001
    g._emit(9202, *east(750.0), "unknown", "", "", "spawn")  # noqa: SLF001
    for i in range(20):
        feed_bits(g, monkeypatch, 9202, 220, 10.0 + i, MOVING)
    assert g.store.get("9202") is None
    g.locator.note_creation(9202, east(750.0), 31.0)
    feed(g, monkeypatch, 9202, east(752.0), 32.0)  # a position at last: where it really is
    e = g.store.get("9202")
    assert e is not None and e.pos_source == "" and (e.x, e.y) == pytest.approx(east(752.0)[:2])
    assert 9202 not in {r["handle"] for r in g.unplaced_creatures()}


def test_a_followed_creature_is_never_taken_off_for_being_active(source, monkeypatch):
    g = source
    g._last_t = 1.0  # noqa: SLF001
    g._creatures.add(9203)  # noqa: SLF001
    g._emit(9203, *east(300.0), "dino", "Allosaurus", "")  # noqa: SLF001
    g.locator.note_creation(9203, east(300.0), 1.0)
    feed(g, monkeypatch, 9203, east(301.0), 2.0)
    for i in range(15):
        feed_bits(g, monkeypatch, 9203, 220, 3.0 + i)  # no position in these, but it was placed by a decoded one
    e = g.store.get("9203")
    assert e is not None and g.stale_spawns == 0


def test_a_reset_forgets_carcasses_smells_and_activity(source, monkeypatch):
    g = source
    spot = (ME[0] + 70_000.0, ME[1], ME[2])
    creation_packet(g, monkeypatch, [(9001, SMELL, spot), (9002, ALLO, spot)], 10.0)
    assert g._corpses and g._smells  # noqa: SLF001
    g.on_reset()
    assert not g._corpses and not g._smells and not g._timer and not g._active  # noqa: SLF001
    assert not g._first_seen and not g._moved and g._first_packet_t is None  # noqa: SLF001


# ---------------------------------------------------------------- how a rest is drawn
def test_a_rest_is_drawn_solid_and_its_distance_has_no_tilde_while_a_spawn_point_is_dimmed_with_one():
    from isle_livemap.geo import Entity, Nearby, entity_label_lines

    def nearby(source: str) -> Nearby:
        return Nearby(Entity("1", "player", "P1", "Spinosaurus", 0.0, 0.0, pos_source=source), 0.0, 0.0, 480.0, 0.0)

    assert nearby("spawn").stale and not nearby("rest").stale and not nearby("").stale
    assert "~" in entity_label_lines(nearby("spawn"))[0]
    assert "~" not in entity_label_lines(nearby("rest"))[0] and "480 m" in entity_label_lines(nearby("rest"))[0]
