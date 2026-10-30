"""Real-game peer backend (TRD-051): other actors decoded from the same inbound stream as the own vitals.

Golden numbers come from the recorded inbound packets in tests/fixtures (TRD-050 findings); synthetic
batches are built with the repo's own ``replay.BitWriter`` / ``_needed``."""

from __future__ import annotations

import logging
import math
import re

import pytest

from isle_livemap import peers
from isle_livemap.capture import INBOUND, OUTBOUND, SERVER_SWITCH_DATAGRAMS, Datagram, TelemetryPipeline
from isle_livemap.decode import (
    ACTOR_STATE_HEADER, ACTOR_STATE_HEADER_BITS, ACTOR_STATE_TAIL_BITS, Bits, IrisBatch, PacketCounts, batch_kind,
    count_batches, decode_actor_location, decode_actor_locations, parse_iris_packet,
)
from isle_livemap.geo import GAME_PEER_STALE_S, GAME_PEER_TTL_S, peer_label_lines, visible_entities
from isle_livemap.peers import GamePeerSource, create_peer_source
from isle_livemap.replay import CLIENT_IP, CLIENT_PORT, SERVER_IP, SERVER_PORT, BitWriter, _needed, encode_move_payload

T0 = 1_000.0
OWN = 103_940  # the own pawn in FirstHeartbeat/SecondHeartbeat (see test_vitals)


class Clock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def inbound(payload: bytes, at: float = T0) -> Datagram:
    return Datagram(at, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT, payload, INBOUND)


def outbound(at: float = T0) -> Datagram:
    return Datagram(at, CLIENT_IP, CLIENT_PORT, SERVER_IP, SERVER_PORT, encode_move_payload(1e5, 2e5, 3e4, 10.0, 41.0), OUTBOUND)


def actor_batch(x: float, y: float, z: float, handle: int = 7, header: int = ACTOR_STATE_HEADER, comp: int | None = None):
    """A batch in the recorded location layout, built with the repo's BitWriter."""
    raw = [round(v * 100) for v in (x, y, z)]
    comp = comp or max(_needed(r) for r in raw)
    w = BitWriter()
    w.write(header, ACTOR_STATE_HEADER_BITS)
    w.write(comp | 64, 7)
    for r in raw:
        w.write(r, comp)
    w.write(0x1ABCDEF123 & ((1 << ACTOR_STATE_TAIL_BITS) - 1), ACTOR_STATE_TAIL_BITS)
    return Bits(w.to_bytes()), IrisBatch(handle, 0, w.nbits, True, False)


# ---------------------------------------------------------------- decoder on recorded packets
def test_recorded_packet_yields_other_actor_positions(b64):
    locs = {l.net_ref_handle: l for l in decode_actor_locations(b64("SecondHeartbeat"))}
    assert len(locs) == 15 and OWN not in locs  # the own pawn has no such batch
    a = locs[93_362]  # 155-bit batch, 26-bit components
    assert (a.x, a.y, a.z, a.component_bit_count) == (pytest.approx(201_852.74), pytest.approx(-249_768.68), pytest.approx(30_398.88), 26)
    b = locs[84_466]  # 158-bit batch, 27-bit components (|y|... a component beyond 335 544 cm)
    assert b.component_bit_count == 27 and b.x == pytest.approx(413_642.88)


def test_every_recorded_location_batch_is_a_canonical_world_position(b64, recorded):
    total = 0
    for name in ("FirstHeartbeat", "SecondHeartbeat", "PeriodicAttributes", "PteranodonHeartbeat1", "PteranodonHeartbeat2",
                 "SparseMaximumAttributes", "SparseMaximumHeartbeat2", "TrailingAttributesHeartbeat1", "TrailingAttributesHeartbeat2"):
        pkt = parse_iris_packet(b64(name))
        locs = decode_actor_locations(b64(name), pkt)
        bits = Bits(b64(name))
        sized = [b for b in pkt.batches if b.data_bit_count in (155, 158)
                 or (b.data_bit_count in (192, 195) and bits.read(b.data_bit_offset, 33) == 0x1680A4328)]
        # every 155/158-bit batch (first layout) and every 192/195-bit batch of the second layout decodes, nothing else
        assert len(locs) == len({b.net_ref_handle for b in sized})
        for l in locs:
            assert -800_000 <= l.x <= 800_000 and -800_000 <= l.y <= 800_000
            assert not l.z_reliable or 1_000 < l.z < 100_000  # the second layout's z runs away; only x and y are claimed
        total += len(locs)
    assert total >= 40


def test_same_handle_moves_continuously_between_packets(b64):
    a = {l.net_ref_handle: l for l in decode_actor_locations(b64("TrailingAttributesHeartbeat1"))}[206_308]
    b = {l.net_ref_handle: l for l in decode_actor_locations(b64("TrailingAttributesHeartbeat2"))}[206_308]
    assert math.dist((a.x, a.y, a.z), (b.x, b.y, b.z)) < 500  # < 5 m between two packets: same actor, same layout


def test_destruction_record_is_exposed_by_the_envelope_walk(b64):
    pkt = parse_iris_packet(b64("DetachedDamagedAttributes"))
    assert pkt is not None and pkt.destroyed_handles == (86_078,)
    assert parse_iris_packet(b64("SecondHeartbeat")).destroyed_handles == ()


# ---------------------------------------------------------------- decoder on synthetic batches
@pytest.mark.parametrize("pos", [(201_852.74, -249_768.68, 30_398.88), (413_642.88, -232_348.96, 37_593.69),
                                 (-45_000.5, 12_000.25, 20_000.0), (1.0, 1.0, 20_000.0), (-5.0, 7.0, 9_000.0)])
def test_synthetic_batch_round_trips(pos):
    bits, batch = actor_batch(*pos)
    loc = decode_actor_location(bits, batch)
    assert loc is not None and loc.net_ref_handle == 7
    assert (loc.x, loc.y, loc.z) == pytest.approx(pos, abs=0.006)


def test_synthetic_batch_matches_recorded_sizes():
    assert actor_batch(201_852.74, -249_768.68, 30_398.88)[1].data_bit_count == 155
    assert actor_batch(413_642.88, -232_348.96, 37_593.69)[1].data_bit_count == 158


def test_decoder_rejects_other_layouts():
    bits, batch = actor_batch(1e5, 2e5, 3e4)
    assert decode_actor_location(bits, batch) is not None
    assert decode_actor_location(bits, IrisBatch(7, 0, batch.data_bit_count + 1, True, False)) is None  # size != 77 + 3*comp
    assert decode_actor_location(bits, IrisBatch(7, 1, batch.data_bit_count, True, False)) is None  # shifted: header mismatch
    assert decode_actor_location(*actor_batch(1e5, 2e5, 3e4, header=ACTOR_STATE_HEADER ^ 1)) is None  # another state class
    assert decode_actor_location(*actor_batch(1e5, 2e5, 3e4, comp=27)) is None  # non-canonical component width
    assert decode_actor_location(*actor_batch(9e5, 2e5, 3e4)) is None  # outside the world
    assert decode_actor_location(*actor_batch(1e5, 2e5, 3e6)) is None


def test_decoder_never_raises_on_junk():
    assert decode_actor_locations(b"") == [] and decode_actor_locations(bytes(range(200))) == []


# ---------------------------------------------------------------- GamePeerSource
def make(**kw):
    clk = Clock()
    kw.setdefault("pipeline", TelemetryPipeline())  # attached but not started: on_inbound is driven by hand
    return GamePeerSource(clock=clk, **kw), clk


def test_peers_come_from_inbound_batches_with_unknown_fields_none(b64):
    g, _ = make()
    g.on_inbound(inbound(b64("SecondHeartbeat")), None)
    snap = g.snapshot()
    assert len(snap.entities) == 15 and g.locations == 15
    e = {p.id: p.entity for p in snap.entities}["93362"]
    # these fixtures hold no creation record for the handle, and every state batch is the animal size (155/158 bits):
    # a server animal ("ai"), neither a player nor a dino, from its very first batch
    assert (e.kind, e.name, e.species) == ("ai", "Actor #93362", "")
    assert (e.x, e.y, e.z) == (pytest.approx(201_852.74), pytest.approx(-249_768.68), pytest.approx(30_398.88))
    assert (e.health_pct, e.growth_pct, e.stamina_pct, e.hunger_pct, e.thirst_pct, e.heading) == (None,) * 6
    assert "not available" in snap.status and not peers.status_is_warning(snap.status)


def test_game_labels_say_animal_for_an_unknown_server_animal_and_never_show_growth_hunger_thirst(b64):
    g, _ = make()
    g.on_inbound(inbound(b64("SecondHeartbeat")), None)
    near = visible_entities([(p.entity, p.entity.x, p.entity.y) for p in g.snapshot().entities], 200_000.0, -250_000.0, 5_000.0)
    assert near
    lines = peer_label_lines(near[0])
    assert len(lines) == 1 and re.fullmatch(r"AI - Animal - \d+ m", lines[0]), lines  # a server animal with no class: "AI - Animal"
    assert "HP" not in lines[0] and "G " not in lines[0]  # no vitals line for fish, no growth / hunger / thirst


def test_provisional_peer_is_purged_when_own_handle_locks(b64):
    g, _ = make()
    g.on_inbound(inbound(b64("SecondHeartbeat")), None)
    assert "93362" in {p.id for p in g.snapshot().entities}
    v = g.store.version
    g.on_inbound(inbound(b64("SecondHeartbeat")), 93_362)  # heartbeat lock says: that handle is me
    ids = {p.id for p in g.snapshot().entities}
    assert "93362" not in ids and len(ids) == 14 and g.store.version > v and g.own_handle == 93_362
    g.on_inbound(inbound(b64("SecondHeartbeat")), 93_362)
    assert "93362" not in {p.id for p in g.snapshot().entities}  # stays excluded


def test_own_handle_change_replaces_the_exclusion(b64):
    g, _ = make(self_ids=["84466"])
    g.on_inbound(inbound(b64("SecondHeartbeat")), 93_362)
    g.on_inbound(inbound(b64("SecondHeartbeat")), 82_008)  # respawn: new pawn, old handle may belong to someone else
    ids = {p.id for p in g.snapshot().entities}
    assert "82008" not in ids and "84466" not in ids  # configured self id kept
    assert "93362" not in ids  # the old pawn is my own body: never a "nearby creature" (a dot that never moves again)


def test_destruction_record_removes_a_tracked_peer_only(b64):
    g, _ = make()
    g.store.update(peers.Entity("86078", "player", "Actor #86078", "", 1.0, 2.0, 3.0))
    g.store.update(peers.Entity("5", "player", "Actor #5", "", 1.0, 2.0, 3.0))
    g.on_inbound(inbound(b64("DetachedDamagedAttributes")), None)
    ids = {p.id for p in g.snapshot().entities}
    assert "86078" not in ids and "5" in ids and g.destroyed == 1  # the packet also carries locations of other actors
    g.on_inbound(inbound(b64("DetachedDamagedAttributes")), None)  # untracked handle: no effect, no error
    assert g.destroyed == 1


def test_silent_peer_expires_after_ttl(b64):
    # Iris sends changes only, so an idle actor is silent: game peers are dimmed late and kept long (destruction
    # records remove them at once), unlike the 3 s / 5 s of the lab feed.
    g, clk = make()
    assert (g.stale_s, g.TTL_AI_S) == (GAME_PEER_STALE_S, GAME_PEER_TTL_S) and g.TTL_AI_S > peers.PEER_TTL_S
    assert g.ttl_s > g.TTL_AI_S  # creatures rest for minutes without a report: they wait for the destruction record
    g.on_inbound(inbound(b64("SecondHeartbeat")), None)
    clk.t += peers.PEER_TTL_S + 1.0  # past the lab TTL: a stationary game actor is still there
    sn = g.snapshot()
    assert len(sn.entities) == 15 and all(clk.t - p.last_seen < g.stale_s for p in sn.entities)  # not even dimmed yet
    clk.t += g.TTL_AI_S
    assert g.snapshot().entities == ()  # server animals (these fixtures): the short limit


def test_silent_creature_outlives_the_animal_limit_but_not_a_missed_destruction_record():
    g, clk = make()
    g.store.update(peers.Entity("7", "dino", "Actor #7", "Deinosuchus", 1.0, 2.0, 3.0))
    g.store.update(peers.Entity("8", "ai", "Actor #8", "", 1.0, 2.0, 3.0))
    clk.t += g.TTL_AI_S + 1.0
    assert [p.id for p in g.snapshot().entities] == ["7"]  # resting dino stays, the fish is gone
    clk.t += g.ttl_s
    assert g.snapshot().entities == ()


def test_creature_still_replicated_without_a_position_stays_on_the_map(b64):
    # Live capture: a creature's position stops changing (no vector in its state batches) but the server keeps
    # sending its state for minutes and never destroys it. It must stay at its last position, not expire.
    g, clk = make()
    g.on_inbound(inbound(b64("SecondHeartbeat")), None)
    before = {p.entity.id: p.entity for p in g.snapshot().entities}
    assert len(before) == 15
    g.locator.update = lambda *a, **k: None  # same state batches, no decodable position
    for _ in range(4):
        clk.t += g.TTL_AI_S * 0.6  # these actors are server animals: the shorter limit applies
        g.on_inbound(inbound(b64("SecondHeartbeat")), None)
    after = {p.entity.id: p.entity for p in g.snapshot().entities}
    assert after == before  # past the TTL since the last position, same ids, same positions


def test_garbage_and_non_replication_datagrams_are_ignored():
    g, _ = make()
    for junk in (b"", b"\x00", bytes(range(200)), encode_move_payload(1e5, 2e5, 3e4, 10.0, 41.0)):
        g.on_inbound(inbound(junk), None)
    assert g.snapshot().entities == () and "waiting" in g.snapshot().status and peers.status_is_warning(g.snapshot().status)


# ---------------------------------------------------------------- wired into the real capture pipeline
def test_pipeline_tap_uses_heartbeat_self_and_leaves_local_decoding_alone(b64):
    pipe = TelemetryPipeline()
    g, _ = make(pipeline=pipe)
    g.start()
    pipe.handle(outbound(T0 - 0.1))  # learn the server, as in live capture
    pipe.handle(inbound(b64("FirstHeartbeat"), T0))
    assert g.own_handle is None and len(g.snapshot().entities) > 0  # provisional peers before the lock
    pipe.handle(inbound(b64("SecondHeartbeat"), T0 + 1.056))
    pipe.handle(inbound(b64("PeriodicAttributes"), T0 + 2))
    assert g.own_handle == OWN and str(OWN) not in {p.id for p in g.snapshot().entities}
    assert g.snapshot().entities  # others still shown
    v = pipe.latest.vitals  # own vitals decoded exactly as without the tap
    assert v is not None and v.health == 4.601197242736816 and v.growth == 0.10464542359113693
    assert f"own handle {OWN}" in g.snapshot().status


def test_pipeline_ignores_other_endpoints_and_stop_detaches(b64):
    pipe = TelemetryPipeline()
    g, _ = make(pipeline=pipe)
    g.start()
    pipe.handle(outbound(T0 - 0.1))
    pipe.handle(Datagram(T0, "6.6.6.6", 7777, CLIENT_IP, CLIENT_PORT, b64("SecondHeartbeat"), INBOUND))
    assert g.packets == 0 and g.snapshot().entities == ()
    g.stop()
    pipe.handle(inbound(b64("SecondHeartbeat"), T0 + 1))
    assert g.packets == 0


def test_server_change_and_game_restart_clear_the_handles(b64):
    pipe = TelemetryPipeline()
    g, _ = make(pipeline=pipe, self_ids=["9"])
    g.start()
    pipe.handle(outbound(T0 - 0.1))
    pipe.handle(inbound(b64("SecondHeartbeat"), T0))
    assert g.snapshot().entities
    # joined another server: the old one goes silent and the new destination sends datagram after datagram
    for i in range(SERVER_SWITCH_DATAGRAMS):
        pipe.handle(Datagram(T0 + 2 + i * 0.03, CLIENT_IP, CLIENT_PORT, "10.9.9.9", 7777, b"\x00" * 8, OUTBOUND))
    assert g.snapshot().entities == () and g.own_handle is None
    pipe.handle(inbound(b64("SecondHeartbeat"), T0 + 3))
    assert g.snapshot().entities == ()  # old server's datagrams are not trusted any more
    pipe.reset()  # game exit: supervisor resets the pipeline
    assert g.snapshot().entities == ()


def test_a_stray_datagram_to_another_host_is_not_a_server_change(b64):
    """Live capture: a 1300-byte probe from a second socket to another host arrived mid-session. It used to wipe the
    own-vitals lock, the identity tables and every peer (two resets within 30 ms, the second one when the real server
    was 'learned' again)."""
    pipe = TelemetryPipeline()
    resets = []
    pipe.add_observer(lambda d, own: None, lambda: resets.append(1))
    g, _ = make(pipeline=pipe)
    g.start()
    pipe.handle(outbound(T0 - 0.1))
    pipe.handle(inbound(b64("FirstHeartbeat"), T0))
    pipe.handle(inbound(b64("SecondHeartbeat"), T0 + 1.056))
    assert g.own_handle == OWN and g.snapshot().entities
    n = len(resets)
    pipe.handle(Datagram(T0 + 2.0, CLIENT_IP, 65_396, "10.9.9.9", 27_038, b"\x01\x02sdping" + bytes(1290), OUTBOUND))
    pipe.handle(outbound(T0 + 2.026))  # the next game datagram, 26 ms later
    assert len(resets) == n and pipe.server == f"{SERVER_IP}:{SERVER_PORT}"
    assert g.own_handle == OWN and g.snapshot().entities  # nothing was wiped
    assert pipe._vitals.self_handle == OWN  # noqa: SLF001 - the heartbeat lock survived too


def test_learning_the_server_after_a_reset_does_not_notify_twice(b64):
    pipe = TelemetryPipeline()
    resets = []
    pipe.add_observer(lambda d, own: None, lambda: resets.append(1))
    pipe.handle(outbound(T0))
    assert pipe.server and resets == []  # first server: nothing to wipe
    pipe.reset()
    assert resets == [1]
    pipe.handle(outbound(T0 + 1))  # the same server again: state is already clear
    assert resets == [1]


def test_a_failing_observer_cannot_break_own_player_decoding(b64):
    pipe = TelemetryPipeline()

    def boom(*_a):
        raise RuntimeError("observer bug")

    pipe.add_observer(boom, boom)
    pipe.handle(outbound(T0 - 0.1))
    pipe.handle(inbound(b64("FirstHeartbeat"), T0))
    assert pipe.handle(inbound(b64("SecondHeartbeat"), T0 + 1.056)) is not None


def test_factory_wires_the_pipeline_and_start_without_one_logs(caplog):
    pipe = TelemetryPipeline()
    g = create_peer_source("game", pipeline=pipe)
    assert isinstance(g, GamePeerSource) and g.pipeline is pipe
    with caplog.at_level(logging.WARNING, logger="isle_livemap.peers"):
        GamePeerSource().start()
    assert "no capture pipeline" in caplog.text


# ---------------------------------------------------------------- batch kind classifier and join-data counters
def kind_batch(prefix_bits: list[int], total: int = 40, offset: int = 0):
    """Batch whose payload starts with ``prefix_bits`` (LSB-first), built with BitWriter."""
    w = BitWriter()
    w.write(0, offset)
    for b in prefix_bits:
        w.write(b, 1)
    w.write(0, total - len(prefix_bits))
    return Bits(w.to_bytes()), IrisBatch(1, offset, total, False, False)


def test_batch_kind_on_synthetic_prefixes():
    assert batch_kind(*kind_batch([0, 0, 0, 1, 0])) == "state"
    assert batch_kind(*kind_batch([0, 0, 0, 1, 1])) == "creation"
    assert batch_kind(*kind_batch([0, 0, 0, 1, 1], offset=13)) == "creation"  # honours data_bit_offset
    assert batch_kind(*kind_batch([0, 0, 0, 0, 0])) == "unknown"  # no state bit
    assert batch_kind(*kind_batch([1, 0, 0, 1, 0])) == "unknown"  # skipped bits not zero
    assert batch_kind(*kind_batch([0, 0, 1, 1, 1])) == "unknown"
    assert batch_kind(*kind_batch([0, 0, 0, 1], total=4)) == "unknown"  # shorter than the prefix


def test_batch_kind_of_the_recorded_location_batch_is_state():
    bits, batch = actor_batch(1e5, 2e5, 3e4)
    assert batch_kind(bits, batch) == "state"


def test_every_recorded_batch_is_a_state_update(recorded, b64):
    total = states = creations = exports = 0
    for name in recorded:
        try:
            payload = b64(name)
        except Exception:  # entries stored as hex are not Iris payloads
            continue
        pkt = parse_iris_packet(payload)
        if pkt is None:
            continue
        c = count_batches(payload, pkt)
        assert c.unknown == 0 and c.creation + c.state == len(pkt.batches)
        creations += c.creation
        exports += c.exports
        total += len(pkt.batches)
        states += c.state
    # one creation record, in the packet that also carries a destruction record. It used to be invisible: the old
    # destruction-record layout ended that packet's walk before its first batch (0 of 36 parsed).
    assert (total, states, creations, exports) == (162, 161, 1, 2)


def test_counts_per_packet_on_the_destruction_fixture(b64):
    c = count_batches(b64("DetachedDamagedAttributes"))
    assert (c.state, c.creation, c.exports, c.destroyed) == (35, 1, 2, 1) and c.unknown == 0
    assert count_batches(b"") == PacketCounts() and count_batches(bytes(range(200))) == PacketCounts()


def test_source_counters_on_recorded_traffic_and_status_text(b64, recorded):
    g, _ = make()
    for name in recorded:
        try:
            payload = b64(name)
        except Exception:
            continue
        g.on_inbound(inbound(payload), None)
    assert (g.creation_records, g.export_batches, g.destruction_records) == (1, 2, 1)
    st = g.snapshot().status
    assert "creation 1, exports 2, destroyed 1" in st and not peers.status_is_warning(st)
    assert len(st) < 170


def test_destruction_records_count_even_when_the_handle_is_untracked(b64):
    g, _ = make()
    g.on_inbound(inbound(b64("DetachedDamagedAttributes")), None)
    assert g.destruction_records == 1 and g.destroyed == 0



def test_late_join_labels_actors_unidentified_and_hints_to_rejoin(b64):
    g, _ = make()
    g.on_inbound(inbound(b64("SecondHeartbeat")), None)
    g.store.update(peers.Entity("999999", "unknown", "Unknown #999999", "", 1.0, 2.0, 3.0))  # a creature with no class
    assert not g.late_join() and "rejoin" not in g.snapshot().status.lower()  # too little traffic to say the tables were missed
    g.packets = g.LATE_JOIN_PACKETS  # many packets, still no export table, no player name, no class
    g._first_packet_t = -1000.0  # noqa: SLF001 - old enough for the class tables to have arrived
    snap = g.snapshot()
    assert g.late_join() and "rejoin" in snap.status.lower() and peers.status_is_warning(snap.status)
    assert "LATE JOIN" in g.summary()


def test_late_join_hint_is_dropped_once_a_player_name_is_known(b64):
    g, _ = make()
    g.on_inbound(inbound(b64("SecondHeartbeat")), None)
    g.packets = g.LATE_JOIN_PACKETS
    g._first_packet_t = -1000.0  # noqa: SLF001 - old enough for the class tables to have arrived
    g.identity.player_names[1] = "Someone"
    g.identity.table_classes = 1  # the export tables were seen: names can only come from them (or the class cache)
    assert not g.late_join() and "rejoin" not in g.snapshot().status.lower() and "LATE JOIN" not in g.summary()


def test_summary_counts_actors_with_a_class_path_not_just_tracked_actors(b64):
    g, _ = make()
    g.on_inbound(inbound(b64("SecondHeartbeat")), None)
    tracked = len(g.identity.actors)
    resolved = g.identity_resolved()
    assert resolved <= tracked
    assert f"actors with class path={resolved}/{tracked}" in g.summary()
    assert "classes known" not in g.summary()
