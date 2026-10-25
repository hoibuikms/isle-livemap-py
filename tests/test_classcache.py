"""Identity without the join tables: the class cache, the layout rule for server animals, the early-sniff buffer.

Packets come from the sanitised ``fixtures/recorded_join_payloads.json`` (a capture that started before the join, so it
holds the export tables); "a session that missed the tables" is simulated by ignoring export sections."""

from __future__ import annotations

import json

import pytest

from isle_livemap import species as sp
from isle_livemap.capture import INBOUND, PreCaptureBuffer, Datagram
from isle_livemap.classcache import MAX_AGE_S, SAVE_INTERVAL_S, SCHEMA, ClassCache
from isle_livemap.decode import ACTOR_STATE_HEADER, ACTOR_STATE_HEADER_BITS, IdentityTracker
from isle_livemap.geo import Entity, Nearby, entity_label_lines
from isle_livemap.peers import GamePeerSource
from isle_livemap.replay import BitWriter, CLIENT_IP, CLIENT_PORT, SERVER_IP, SERVER_PORT

PATH_A = "/Game/TheIsle/Core/Characters/Dinosaurs/Alpha/BP_Alpha.Default__BP_Alpha_C"
PATH_B = "/Game/TheIsle/Core/Characters/Dinosaurs/Beta/BP_Beta.Default__BP_Beta_C"


class FakePipeline:
    def add_observer(self, *a):
        pass

    def remove_observer(self, *a):
        pass


class Clock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def learn_from_tables(join_packets, cache) -> IdentityTracker:
    tr = IdentityTracker(cache)
    for r in join_packets:
        tr.feed(r["payload"])
    return tr


def blind_tracker(cache) -> IdentityTracker:
    tr = IdentityTracker(cache)
    tr._record_exports = lambda section: None  # the capture began after the join: no export tables
    return tr


# ---------------------------------------------------------------- the cache itself
def test_learn_lookup_and_conflict_evicts_and_blocks():
    c = ClassCache()
    c.learn(7, PATH_A)
    assert c.lookup(7) == PATH_A and len(c) == 1
    c.learn(7, PATH_A)
    assert c.lookup(7) == PATH_A and c.conflicts == 0
    c.learn(7, PATH_B)  # this connection's tables disagree: nothing is trusted any more for that id
    assert c.lookup(7) is None and c.conflicts == 1
    c.learn(7, PATH_A)
    assert c.lookup(7) is None


def test_only_complete_paths_are_stored():
    c = ClassCache()
    c.learn(1, "Gateway.PersistentLevel.BP_Thing_C_UAID_1")
    assert c.lookup(1) is None and len(c) == 0


def test_round_trip_expiry_and_damaged_file(tmp_path):
    path = tmp_path / "classes.json"
    clock = Clock(10_000.0)
    c = ClassCache(path, clock)
    c.learn(0xB06A2F3D, PATH_A)
    c.learn(2, PATH_B)
    assert c.save(force=True) and json.loads(path.read_text())["schema"] == SCHEMA
    back = ClassCache.load(path, clock)
    assert back.lookup(0xB06A2F3D) == PATH_A and back.lookup(2) == PATH_B
    clock.t += MAX_AGE_S + 1  # unseen for months: the game has probably changed
    assert len(ClassCache.load(path, clock)) == 0
    path.write_text("{ not json", encoding="utf-8")
    broken = ClassCache.load(path)
    assert len(broken) == 0 and broken.error
    path.write_text(json.dumps({"schema": SCHEMA + 1, "entries": {"00000001": {"path": PATH_A, "seen": 1}}}))
    assert len(ClassCache.load(path)) == 0  # another schema is ignored, not misread


def test_save_is_rate_limited_and_never_raises(tmp_path):
    clock = Clock()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    bad = ClassCache(blocker / "classes.json", clock)  # the parent is a file: every write fails
    bad.learn(1, PATH_A)
    assert bad.save(force=True) is False and bad.error
    good = ClassCache(tmp_path / "classes.json", clock)
    good.learn(1, PATH_A)
    assert good.save() is True
    good.learn(2, PATH_B)
    assert good.save() is False  # too soon after the last write
    clock.t += SAVE_INTERVAL_S
    assert good.save() is True


# ---------------------------------------------------------------- identity tracker
def test_tables_teach_the_cache_and_a_blind_session_resolves_from_it(join_packets):
    cache = ClassCache()
    full = learn_from_tables(join_packets, cache)
    assert len(cache) >= 6 and full.table_classes > 0 and not full.tables_missed()

    blind = blind_tracker(cache)
    for r in join_packets:
        blind.feed(r["payload"])
    assert blind.table_classes == 0 and blind.tables_missed()
    resolved = 0
    for handle, info in full.actors.items():
        if info.archetype_handle is None or handle not in blind.actors:
            continue
        expected = full.class_path(handle)
        if expected is None or not expected.startswith("/"):  # a cut-off path could pass a dinosaur off as a world object
            continue
        assert blind.class_path(handle) == expected and blind.is_inferred(handle)
        assert not full.is_inferred(handle)
        resolved += 1
    assert resolved >= 30


def test_without_a_cache_a_blind_session_knows_nothing(join_packets):
    blind = blind_tracker(None)
    for r in join_packets:
        blind.feed(r["payload"])
    assert all(blind.class_path(h) is None and not blind.is_inferred(h) for h in blind.actors)


def test_tables_that_arrive_later_replace_the_cached_class_and_confirm_it(join_packets):
    cache = ClassCache()
    learn_from_tables(join_packets, cache)
    tr = blind_tracker(cache)
    for r in join_packets:
        tr.feed(r["payload"])
    inferred = [h for h in tr.actors if tr.is_inferred(h)]
    assert inferred
    del tr._record_exports  # the tables show up after all (a rejoin): the instance falls back to the class method
    for r in join_packets:
        tr.feed(r["payload"])
    assert tr.table_classes > 0 and cache.conflicts == 0
    assert not any(tr.is_inferred(h) for h in inferred if h in tr.actors)


def test_tables_that_disagree_with_the_cache_win_and_evict_the_entry(join_packets):
    cache = ClassCache()
    learn_from_tables(join_packets, cache)
    archetype_protocol = next(c for c in cache._entries)
    cache._entries[archetype_protocol]["path"] = PATH_B  # a stale / wrong entry
    tr = IdentityTracker(cache)
    for r in join_packets:
        tr.feed(r["payload"])
    assert cache.conflicts >= 1 and cache.lookup(archetype_protocol) is None
    assert all(PATH_B != tr.class_path(h) for h in tr.actors)


# ---------------------------------------------------------------- peer source
def inbound(payload: bytes, at: float) -> Datagram:
    return Datagram(at, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT, payload, INBOUND)


def feed_source(g, join_packets) -> None:
    for r in join_packets:
        g.on_inbound(inbound(r["payload"], r["t"]), None)


def test_blind_session_with_cache_marks_species_as_inferred_and_hides_world_objects(join_packets):
    cache = ClassCache()
    learn_from_tables(join_packets, cache)
    full = GamePeerSource(clock=Clock(), class_cache=cache)
    feed_source(full, join_packets)
    full_ents = {p.entity.id: p.entity for p in full.store.entities()}
    assert full_ents and not any(e.inferred for e in full_ents.values())

    blind = GamePeerSource(clock=Clock(), class_cache=cache)
    blind.identity._record_exports = lambda section: None
    feed_source(blind, join_packets)
    ents = {p.entity.id: p.entity for p in blind.store.entities()}
    assert {e.species for e in ents.values() if e.species} & {"Pteranodon", "Rainbow Fish"}
    assert all(e.inferred for e in ents.values() if e.species)
    assert {e.kind for e in ents.values()} <= {"player", "dino", "ai", "unknown"}  # fruit and plants stay hidden
    assert blind.filtered_world_objects > 20
    assert "from the saved class cache" in blind.summary()

    plain = GamePeerSource(clock=Clock())
    plain.identity._record_exports = lambda section: None
    feed_source(plain, join_packets)
    assert not any(p.entity.species for p in plain.store.entities())


def test_inferred_species_is_marked_in_the_label():
    e = Entity("1", "player", "Rex Fan", "Alpha", 0.0, 0.0, inferred=True)
    head, _ = entity_label_lines(Nearby(e, 0.0, 0.0, 10.0, 0.0))
    assert head == "Rex Fan - Alpha~ - 10 m"
    head, _ = entity_label_lines(Nearby(Entity("1", "player", "Rex Fan", "Alpha", 0.0, 0.0), 0.0, 0.0, 10.0, 0.0))
    assert head == "Rex Fan - Alpha - 10 m"
    nameless = Entity("1", "dino", "Actor #1", "Alpha", 0.0, 0.0, inferred=True)  # no player name: species alone, marked
    assert entity_label_lines(Nearby(nameless, 0.0, 0.0, 10.0, 0.0))[0] == "Alpha~ - 10 m"


def test_class_cache_is_saved_when_the_source_stops(tmp_path, join_packets):
    path = tmp_path / "classes.json"
    g = GamePeerSource(pipeline=FakePipeline(), clock=Clock(), class_cache=ClassCache(path))
    g.start()
    feed_source(g, join_packets)
    g.stop()
    assert len(ClassCache.load(path)) >= 6


# ---------------------------------------------------------------- late-join hint stays accurate
def test_hint_says_what_is_left_after_the_cache_names_some_actors(join_packets):
    cache = ClassCache()
    learn_from_tables(join_packets, cache)
    g = GamePeerSource(pipeline=FakePipeline(), clock=Clock(), class_cache=cache)
    g.identity._record_exports = lambda section: None
    feed_source(g, join_packets)
    g.packets = g.LATE_JOIN_PACKETS
    g.store.update(Entity("999999", "unknown", "Unknown #999999", "", 1.0, 2.0))  # created before the app started
    assert g.late_join()
    status = g.snapshot().status
    assert "unidentified" in status and "saved class cache" in status and "rejoin" in status.lower()
    for p in g.store.entities():
        if p.entity.kind == "unknown":
            g.store.remove(p.entity.id)
    assert not g.late_join()  # nothing unidentified is left: no hint


# ---------------------------------------------------------------- animals without a creation record
def state_batch(handle: int, bits: int):
    from isle_livemap.decode import Bits, IrisBatch

    w = BitWriter()
    w.write(ACTOR_STATE_HEADER, ACTOR_STATE_HEADER_BITS)
    w.write(0, max(0, bits - ACTOR_STATE_HEADER_BITS))
    return Bits(w.to_bytes()), IrisBatch(handle, 0, bits, True, False)


def test_unclassified_actor_with_the_animal_state_size_is_ai_from_its_first_batch():
    g = GamePeerSource(clock=Clock())
    assert g._animal_layout(7, 155) and g._animal_layout(7, 158)  # waiting for more drew every new fish as a creature


def test_animal_verdict_does_not_flap_on_a_stray_batch():
    g = GamePeerSource(clock=Clock())
    assert all(g._animal_layout(7, bits) for bits in (155,) * 5 + (190,) + (155,) * 5)


@pytest.mark.parametrize("sizes", [(81,) * 20, (190, 155, 190, 190, 155, 190, 190, 190), (48, 155, 158)])
def test_other_state_sizes_are_not_taken_for_animals(sizes):
    g = GamePeerSource(clock=Clock())
    assert not any(g._animal_layout(9, s) for s in sizes)


def test_animal_verdict_is_revoked_once_the_actor_clearly_stops_looking_like_one():
    g = GamePeerSource(clock=Clock())
    verdicts = [g._animal_layout(9, s) for s in (155, 158, 48, 81, 190, 155, 158, 70)]
    assert verdicts[0] and not verdicts[-1]  # latched at first, revoked after 8 batches of which only half fit


def test_layout_counters_are_dropped_with_the_actor():
    g = GamePeerSource(clock=Clock())
    for _ in range(4):
        g._animal_layout(5, 155)
    g._drop(5)
    assert 5 not in g._layouts
    assert g._animal_layout(5, 155) and g._layouts[5][:2] == [1, 1]  # a reused handle starts counting again
    g.on_reset()
    assert not g._layouts


# ---------------------------------------------------------------- early sniffing buffer
class Pkt:
    def __init__(self, tag: str) -> None:
        self.tag = tag


def test_buffer_hands_over_packets_of_a_newly_owned_port_in_order_with_their_times():
    buf = PreCaptureBuffer()
    assert buf.offer(1.0, Pkt("a"), 7777, 50_000)
    assert buf.offer(1.1, Pkt("other"), 443, 51_000)
    assert buf.offer(1.2, Pkt("b"), 50_000, 7777)
    got: list[tuple[float, str]] = []
    assert buf.set_ports({50_000}, lambda t, p: got.append((t, p.tag))) == 2
    assert got == [(1.0, "a"), (1.2, "b")] and len(buf) == 1  # the unrelated packet stays held
    assert buf.offer(2.0, Pkt("c"), 7777, 50_000) is False  # owned now: handled by the caller at once
    assert buf.set_ports({50_000}, lambda t, p: got.append((t, p.tag))) == 0


def test_buffer_drops_old_and_excess_packets():
    buf = PreCaptureBuffer(max_age_s=5.0, max_packets=3)
    for i in range(5):
        buf.offer(float(i), Pkt(str(i)), 1, 2)
    assert len(buf) == 3
    buf.offer(20.0, Pkt("late"), 1, 2)
    assert len(buf) == 1
    got = []
    buf.set_ports({2}, lambda t, p: got.append(p.tag))
    assert got == ["late"]


def test_buffer_forgets_ports_and_holds_again():
    buf = PreCaptureBuffer()
    buf.set_ports({50_000}, lambda t, p: None)
    assert buf.offer(1.0, Pkt("x"), 50_000, 7777) is False
    buf.set_ports((), lambda t, p: None)  # the game exited
    assert buf.offer(2.0, Pkt("y"), 50_000, 7777) is True
