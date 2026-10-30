"""Identity, species, positions and vitals of other creatures, from a live capture that started BEFORE joining.

``fixtures/recorded_join_payloads.json`` holds 46 inbound datagrams of that capture (payload bytes only; account
ids, player names and SteamID64 strings were overwritten in place). Numbers asserted here were measured on the whole
193 s capture (3003 inbound datagrams, 5419 creation records, 431 destruction records) and are repeated in
docs/trd/other-players-on-minimap.md, section "Identity, species and positions".

Synthetic batches are built with the repo's own ``replay.BitWriter``."""

from __future__ import annotations

from isle_livemap.decode import STATE_VECTOR_SCALE, _bits_needed

import math
from collections import Counter

import pytest

from isle_livemap import species as sp
from isle_livemap.capture import INBOUND, Datagram, TelemetryPipeline
from isle_livemap.decode import (
    ACTOR_LAYOUTS, CREATION_LOCATION_SCALE, ActorLocationTracker, Bits, IdentityTracker, IrisBatch, IrisPacket,
    decode_actor_location, decode_creation, decode_net_string, decode_player_name, decode_vitals_block, batch_kind,
    parse_iris_packet, KIND_CREATION,
)
from isle_livemap.geo import peer_label_lines, visible_entities
from isle_livemap.peers import GamePeerSource
from isle_livemap.replay import BitWriter, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT
from isle_livemap.tracking import VitalsTracker, _find_heartbeat


class Clock:
    t = 100.0

    def __call__(self) -> float:
        return self.t


def group(packets, name):
    return [r for r in packets if r["group"] == name]


def one(packets, cap_packet):
    return next(r for r in packets if r["cap_packet"] == cap_packet)


def replay(packets, **kw) -> GamePeerSource:
    clock = Clock()
    g = GamePeerSource(clock=clock, **kw)
    for r in packets:
        clock.t = r["t"]
        g.on_inbound(Datagram(r["t"], SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT, r["payload"], INBOUND), None)
    return g


def feed_all(packets) -> IdentityTracker:
    tr = IdentityTracker()
    for r in packets:
        tr.feed(r["payload"])
    return tr


def varint_encode(text: str) -> bytes:
    """Inverse of decode_net_string(encoded=True): big-endian base-128 groups, bit 7 set on all but the last."""
    out = bytearray()
    for ch in text:
        v = ord(ch)
        groups = [v & 0x7F]
        v >>= 7
        while v:
            groups.append(v & 0x7F)
            v >>= 7
        groups.reverse()  # most significant group first
        out += bytes([g | 0x80 for g in groups[:-1]] + [groups[-1]])
    return bytes(out)


# ---------------------------------------------------------------- envelope: destruction records
def test_destruction_record_is_a_handle_and_one_bit_and_ends_on_the_last_batch(join_packets):
    """The old reading (has-info bit, optional second handle, trailing bit) produced garbage handles such as
    3590495952 and aborted the walk at an 'inline destruction' (0 batches parsed) in 245 of 3003 datagrams."""
    a = parse_iris_packet(one(join_packets, 289)["payload"])
    assert a.destroyed_handles == (83968,) and a.is_complete and a.incomplete_reason is None and len(a.batches) == 10
    b = parse_iris_packet(one(join_packets, 1007)["payload"])
    # four records: the objects 64800/64804 and their sub-objects 64802/64806, then 10 batches, then the end
    assert b.destroyed_handles == (64800, 64802, 64804, 64806) and b.is_complete and len(b.batches) == 10


def test_every_fixture_datagram_parses_completely_with_small_destroyed_handles(join_packets):
    for r in join_packets:
        pkt = parse_iris_packet(r["payload"])
        assert pkt is not None and pkt.is_complete, r["cap_packet"]
        assert all(0 < h < 200_000 for h in pkt.destroyed_handles)


# ---------------------------------------------------------------- net strings
@pytest.mark.parametrize("text", ["Plain Name 42", "Thành Thông Thái", "Đầu Khất", "Cường Sinh Tồn", "x"])
def test_encoded_net_strings_are_base128_code_points(text):
    assert decode_net_string(True, varint_encode(text)) == text


def test_encoded_net_string_examples_from_the_capture_and_malformed_input():
    assert decode_net_string(True, bytes([0x54, 0x81, 0x61, 0x6E, 0x20, 0x54, 0x75])) == "Tán Tu"  # one real name
    assert decode_net_string(True, bytes([0x82, 0x10])) == "Đ"
    assert decode_net_string(True, bytes([0x80, 0xBD, 0x27])) == "ầ"
    assert decode_net_string(False, b"/Game/TheIsle/Core/Player/BP_PlayerState") == "/Game/TheIsle/Core/Player/BP_PlayerState"
    assert decode_net_string(True, bytes([0x41, 0x81])) == "A�"  # truncated group: replacement, no exception
    assert decode_net_string(True, b"") == ""


# ---------------------------------------------------------------- export sections and class paths
def test_export_sections_resolve_archetype_handles_to_asset_paths(join_packets):
    tr = feed_all(join_packets)
    assert tr.class_path(85760) == "/Game/TheIsle/Core/Player/BP_PlayerController.Default__BP_PlayerController_C"
    assert tr.class_path(2) == "/Game/TheIsle/Core/GameModes/BP_SurvivalGameState.Default__BP_SurvivalGameState_C"
    assert tr.class_path(61004) == "/Game/TheIsle/Core/AI/Characters/Fish/BP_Longear.Default__BP_Longear_C"
    assert tr.class_path(86612) == "/Game/TheIsle/Core/Characters/Dinosaurs/Gallimimus/BP_Gallimimus.Default__BP_Gallimimus_C"
    assert tr.object_path(1483).endswith("BP_PlayerController.Default__BP_PlayerController_C")  # the object chain, outer last
    assert tr.object_path(999_999_999) is None


def test_gameplay_tag_token_exports_carry_a_type_id(join_packets):
    pkt = parse_iris_packet(one(join_packets, 1745)["payload"])
    tokens = [t for b in pkt.batches if b.exports for t in b.exports.tokens]
    assert [(t.type_id, t.text) for t in tokens] == [(2, "Status.DisableHeadtracking"), (2, "Ability.Skill.SpearAttack")]


def test_a_name_token_is_defined_once_and_resolved_by_later_references(join_packets):
    tr = feed_all(join_packets)
    assert tr.tokens[(2, 3)] == "Status.DisableHeadtracking"
    # object names are sent inline the first time and by token afterwards. The names that cannot be resolved from the
    # fixture are the three default sub-objects of every pawn (tokens 414..416, defined before the capture's first
    # datagram, so unresolvable even in the full capture) plus one token whose datagram is not part of this subset.
    unresolved = {o.name_token for o in tr.objects.values()
                  if o.name_token and not (o.name or tr._object_names.get(o.name_token))}  # noqa: SLF001
    assert {414, 415, 416} <= unresolved <= {414, 415, 416, 1283}
    assert sum(1 for o in tr.objects.values() if o.name) >= 30


def test_unexported_archetype_has_no_class_path_and_no_invented_species(join_packets):
    tr = feed_all(join_packets)
    assert tr.actors[73832].archetype_handle == 2221 and tr.class_path(73832) is None
    assert tr.object_path(2221) is None


# ---------------------------------------------------------------- creation records
def test_creation_record_fields_on_a_real_batch(join_packets):
    payload = one(join_packets, 668)["payload"]
    pkt = parse_iris_packet(payload)
    bits = Bits(payload)
    batch = next(b for b in pkt.batches if b.net_ref_handle == 61004)
    assert batch_kind(bits, batch) == KIND_CREATION
    c = decode_creation(bits, batch)
    assert (c.net_ref_handle, c.archetype_handle) == (61004, 4279)
    assert c.location == pytest.approx((449_925.6 * STATE_VECTOR_SCALE, -178_420.8 * STATE_VECTOR_SCALE, 38_290.88 * STATE_VECTOR_SCALE), abs=0.01)
    later = one(join_packets, 824)["payload"]
    lbits = Bits(later)
    kinds = [batch_kind(lbits, b) for b in parse_iris_packet(later).batches]
    assert "state" in kinds  # state batches have no creation record, creation batches always do
    assert all((decode_creation(lbits, b) is not None) == (batch_kind(lbits, b) == KIND_CREATION)
               for b in parse_iris_packet(later).batches)


def test_longear_state_positions_equal_creation_position_scaled(join_packets):
    """BP_Longear: 193 later state vectors (the verified 0x20E822A8 layout) have x and y equal to 16 x the creation
    vector, to the last digit; z bobs by +-1 m around the same 16 x value."""
    assert CREATION_LOCATION_SCALE == 16.0 * STATE_VECTOR_SCALE  # 12.5: the state vector is 16 x the creation vector
    g = replay(group(join_packets, "rainbow_class") + group(join_packets, "longear"))
    e = g.store.get("61004")
    assert e is not None and e.kind == "ai" and e.species == "Longear"
    assert (e.x, e.y) == pytest.approx((449_925.6 * STATE_VECTOR_SCALE, -178_420.8 * STATE_VECTOR_SCALE), abs=0.01)
    assert e.z == pytest.approx(38_290.88 * STATE_VECTOR_SCALE, abs=300)  # creation z; the state z moves about +-1 m
    assert g.locator.fixed >= 4 and g.locator.learned == 0


def test_class_rules_and_species_names():
    d = "/Game/TheIsle/Core/Characters/Dinosaurs/Gallimimus/BP_Gallimimus.Default__BP_Gallimimus_C"
    assert sp.describe(d) == ("dino", "Gallimimus")
    assert sp.describe(d, has_player_state=True) == ("player", "Gallimimus")
    assert sp.describe("/Game/TheIsle/Characters/Dinosaurs/Triceratops/BP_Triceratops.Default__BP_Triceratops_C") == ("dino", "Triceratops")
    assert sp.describe("/Game/TheIsle/Core/AI/Characters/Fish/BP_Longear.Default__BP_Longear_C") == ("ai", "Longear")
    assert sp.describe("/Game/TheIsle/Core/Characters/Fishes/Schools/BP_Fish_RainbowFish.Default__BP_Fish_RainbowFish_C") == ("ai", "Rainbow Fish")
    for world in ("/Game/TheIsle/Core/Spawnables/Fruits/BP_FruitMangoStatic.Default__BP_FruitMangoStatic_C",
                  "/Game/TheIsle/Core/PickablePieces/BP_GoreHeart.Default__BP_GoreHeart_C",
                  "/Game/TheIsle/Core/Player/BP_PlayerState.Default__BP_PlayerState_C",
                  "/Game/TheIsle/Maps/Game/Gateway/Gateway.Gateway.PersistentLevel.BP_EdiblePlantsSpawnable_C_UAID_5811"):
        assert sp.describe(world) == ("other", "")
    assert sp.describe(None) == ("unknown", "") and sp.describe(None, has_player_state=True) == ("player", "")
    assert sp.describe(None, "Pteranodon") == ("dino", "Pteranodon")  # unexported class, but dinosaur assets named
    # a path that is known always wins over an asset hint
    assert sp.describe("/Game/TheIsle/Core/Spawnables/Fruits/BP_X.Default__BP_X_C", "Pteranodon") == ("other", "")


def test_unknown_creature_classes_show_their_cleaned_name_never_an_invented_one():
    assert sp.clean_class_name("Default__BP_Triceratops_C") == "Triceratops"
    assert sp.clean_class_name("/Game/TheIsle/Characters/Dinosaurs/Triceratops/BP_Triceratops") == "Triceratops"
    assert sp.clean_class_name("BP_SomethingNew_C") == "SomethingNew"
    assert sp.describe("/Game/TheIsle/Core/AI/Characters/Fish/BP_BrandNewFish.Default__BP_BrandNewFish_C") == ("ai", "BrandNewFish")
    assert sp.describe("/Game/TheIsle/Characters/Dinosaurs/Newosaurus/BP_Newosaurus.Default__BP_Newosaurus_C") == ("dino", "Newosaurus")
    assert sp.display_name("TyrannosaurusRex") == "Tyrannosaurus" and sp.display_name("Whatever") == "Whatever"


def test_species_folder_ignores_ability_assets():
    assert sp.folder_species("/Game/TheIsle/Core/Characters/Dinosaurs/Abilities/Effects/GE_TakeoffCooldown") is None
    assert sp.folder_species("/Game/TheIsle/Characters/Dinosaurs/Pteranodon/Animations/AM_Pteranodon_Adult_Attack") == "Pteranodon"
    assert sp.folder_species(None) is None and sp.folder_species("/Game/Other/Thing") is None


# ---------------------------------------------------------------- species from assets for an unexported archetype
def test_unexported_pawn_archetype_gets_its_species_from_the_assets_of_its_creations(join_packets):
    tr = feed_all(join_packets)
    # archetype 2221 is never exported, but five creation records name Pteranodon animation montages and none
    # names anything else
    assert tr._arch_votes[2221] == {"Pteranodon": 5}
    for h in (73832, 74400, 84764, 80376):
        assert tr.class_path(h) is None and tr.species_hint(h) == "Pteranodon"
    assert tr.species_hint(86612) is None  # its class path is known: the path decides, not the assets


def test_thin_or_contradictory_asset_evidence_gives_no_species(join_packets):
    tr = IdentityTracker()
    tr._arch_votes[7] = Counter({"Pteranodon": 1})
    tr.actors[1] = type("A", (), {"archetype_handle": 7})()
    assert tr.archetype_species(7) is None  # one creation is not evidence
    tr._arch_votes[7].update({"Pteranodon": 1, "Triceratops": 3})
    assert tr.archetype_species(7) is None  # two species disagree
    tr._arch_votes[8] = Counter({"Triceratops": 2})
    assert tr.archetype_species(8) == "Triceratops"


# ---------------------------------------------------------------- player names
def test_player_state_creation_carries_the_player_name(join_packets):
    tr = feed_all(join_packets)
    assert len(tr.player_names) >= 40
    assert all(sp.describe(tr.class_path(h)) == ("other", "") for h in tr.player_names)  # a PlayerState is never drawn
    # the fixture's names were replaced by synthetic ones of the same length; the structure is what is tested
    assert all(n.startswith("p") for n in tr.player_names.values())


def test_player_name_scan_decodes_a_synthetic_player_state():
    for name in ("tunghuy911", "Thành Thông Thái", "A"):
        raw = varint_encode(name)
        w = BitWriter()
        w.write(0b11000, 5)
        w.write(0, 460 - 5)
        w.write(0, 2)
        w.write(len(raw), 8)
        for byte in raw:
            w.write(byte, 8)
        w.write(0, 40)
        bits, batch = Bits(w.to_bytes()), IrisBatch(1, 0, w.nbits, True, False)
        assert decode_player_name(bits, batch) == name


def test_player_name_scan_rejects_noise():
    bits, batch = Bits(bytes(range(256)) * 4), IrisBatch(1, 0, 1000, True, False)
    assert decode_player_name(bits, batch) is None


def test_pawn_creation_references_its_player_state_and_gets_its_name(join_packets):
    tr = feed_all(join_packets)
    assert tr.actors[86612].player_state == 8456 and tr.name_of(86612) == tr.player_names[8456]
    assert tr.actors[84764].player_state == 84648 and tr.actors[74400].player_state == 66576
    assert tr.actors[73832].player_state is None  # its PlayerState was created before the capture started: no name
    assert tr.name_of(73832) is None


# ---------------------------------------------------------------- handle reuse and destruction
def test_destruction_forgets_the_actor_and_a_reused_handle_starts_afresh(join_packets):
    tr = feed_all(group(join_packets, "rainbow_class") + group(join_packets, "longear"))
    assert 61004 in tr.actors and tr.class_path(61004).endswith("BP_Longear.Default__BP_Longear_C")
    tr.feed(b"", IrisPacket(0, True, True, [], None, (61004,)))
    assert 61004 not in tr.actors and tr.class_path(61004) is None and tr.destroyed_known == 1
    tr.feed(b"", IrisPacket(0, True, True, [], None, (61004,)))
    assert tr.destroyed_unknown == 1
    tr.feed(one(join_packets, 668)["payload"])  # the same handle is created again (the server reuses handles)
    assert tr.class_path(61004).endswith("BP_Longear.Default__BP_Longear_C")


def test_gamepeersource_drops_and_forgets_on_destruction(join_packets):
    g = replay(group(join_packets, "rainbow_class") + group(join_packets, "longear")[:2])
    assert g.store.get("61004") is not None
    g.on_inbound(Datagram(500.0, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT, one(join_packets, 289)["payload"], INBOUND), None)
    assert g.store.get("61004") is not None  # 83968 was destroyed, not the fish
    pkt = IrisPacket(0, True, True, [], None, (61004,))
    g.identity.feed(b"", pkt)
    g._drop(61004)  # noqa: SLF001 - what on_inbound does for packet.destroyed_handles
    assert g.store.get("61004") is None and g.locator.last_known(61004) is None


# ---------------------------------------------------------------- positions
def test_rainbow_fish_layout_x_y_equal_sixteen_times_the_creation_position(join_packets):
    pk = group(join_packets, "rainbow_class") + group(join_packets, "rainbow_fish")
    g = replay(pk)
    assert any(l.header == 0x1680A4328 for l in ACTOR_LAYOUTS)
    fish = g.store.get("86082")
    assert fish is not None and fish.kind == "ai" and fish.species == "Rainbow Fish"
    creation = g.identity.actors[86082].location
    assert (fish.x, fish.y) == pytest.approx((creation[0], creation[1]), abs=200)  # 2 m of swimming in 5 s
    assert fish.z is None  # z of this layout runs away at ~8 m/s: x and y are claimed, z is not


def test_second_fixed_layout_decodes_192_bit_batches(join_packets):
    payload = one(join_packets, 1113)["payload"]
    pkt = parse_iris_packet(payload)
    bits = Bits(payload)
    locs = [decode_actor_location(bits, b) for b in pkt.batches if b.net_ref_handle == 86082]
    assert len(locs) == 1 and locs[0] is not None and locs[0].component_bit_count == 26 and not locs[0].z_reliable


def test_learned_layout_tracks_a_pteranodon_with_continuity(join_packets):
    pk = group(join_packets, "pteranodon_creations")[:6] + group(join_packets, "player_states") + group(join_packets, "pteranodon_moving")
    pk.sort(key=lambda r: r["cap_packet"])
    clock = Clock()
    g = GamePeerSource(clock=clock)
    track, e = [], None
    for r in pk:
        clock.t = r["t"]
        g.on_inbound(Datagram(r["t"], SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT, r["payload"], INBOUND), None)
        if r["cap_packet"] >= 3018 and r["cap_packet"] < 5000:
            e = g.store.get("84764")
            track.append((r["t"], e.x, e.y, e.z))
    assert len(track) >= 12 and g.locator.learned >= 8 and g.locator.trusted_layouts >= 1
    steps = [math.dist(a[1:], b[1:]) / max(b[0] - a[0], 1e-3) for a, b in zip(track, track[1:])]
    assert max(steps) < 6_000  # never faster than 60 m/s between consecutive packets: a continuous flight path
    assert track[-1][1] > track[0][1] + 2_000  # and it really moves: > 20 m east over 2 s of data
    assert (e.kind, e.species) == ("player", "Pteranodon") and e.name.startswith("p")  # synthetic name, see module doc


def synthetic_state_batch(header: int, vec: tuple[float, float, float], offset: int, comp: int = 26, total: int = 200, handle: int = 5):
    w = BitWriter()
    comp = max(_bits_needed(round(v * 128)) for v in vec)  # the decoder believes only the canonical width
    w.write(header, 33)
    w.write(0xA5A5A5A5A5 & ((1 << (offset - 33)) - 1), offset - 33)
    w.write(comp | 64, 7)
    for v in vec:
        w.write(round(v * 128), comp)  # replicated vectors are raw / 128 cm
    w.write(0, total - w.nbits)
    return Bits(w.to_bytes()), IrisBatch(handle, 0, w.nbits, True, False)


HDR = 0x0C0000348  # a pawn-style state header (bit 3 = has state, bit 4 clear = not a creation)


def test_learned_layout_needs_an_anchor_continuity_and_repeated_confirmation():
    t = ActorLocationTracker()
    bits, batch = synthetic_state_batch(HDR, (130_000.0, -320_000.0, 36_000.0), 94)
    assert t.update(bits, batch, 10.0, learn=True) is None  # no anchor: a plausible vector alone proves nothing
    t.note_creation(5, (122_000.0, -320_000.0, 36_000.0), 9.0)  # 80 m away: inside the loose radius, outside the tight one
    assert t.update(bits, batch, 10.0, learn=False) is None  # learning is only for creature classes
    assert t.update(bits, batch, 10.0, learn=True) is None  # first sighting of (header, offset): a coincidence so far
    assert t.trusted_layouts == 0
    loc = t.update(bits, batch, 10.5, learn=True)  # second: confirmed and accepted
    assert loc is not None and (loc.x, loc.y) == pytest.approx((130_000.0, -320_000.0)) and t.trusted_layouts == 1
    # from now on the layout is used directly, but still only when the position is continuous
    near, nb = synthetic_state_batch(HDR, (130_300.0, -320_000.0, 36_000.0), 94)
    assert t.update(near, nb, 11.0, learn=True) is not None
    far, fb = synthetic_state_batch(HDR, (190_000.0, -320_000.0, 36_000.0), 94)  # 600 m in 0.5 s
    assert t.update(far, fb, 11.5, learn=True) is None
    assert t.last_known(5) == pytest.approx((130_300.0, -320_000.0, 36_000.0))


def test_a_vector_right_next_to_the_anchor_needs_no_second_sighting():
    t = ActorLocationTracker()
    bits, batch = synthetic_state_batch(HDR, (130_000.0, -320_000.0, 36_000.0), 94)
    t.note_creation(5, (129_000.0, -320_050.0, 36_000.0), 9.0)  # 10 m away after one second: a walking creature
    loc = t.update(bits, batch, 10.0, learn=True)
    assert loc is not None and (loc.x, loc.y) == pytest.approx((130_000.0, -320_000.0))
    assert t.trusted_layouts == 0  # accepted, not yet a trusted layout


def test_stale_anchor_and_creation_far_away_are_rejected():
    t = ActorLocationTracker()
    bits, batch = synthetic_state_batch(HDR, (130_000.0, -320_000.0, 36_000.0), 94)
    t.note_creation(5, (130_000.0, -320_000.0, 36_000.0), 0.0)
    assert t.update(bits, batch, 500.0, learn=True) is None  # anchor older than ANCHOR_LIFETIME
    t.note_creation(5, (10_000.0, -20_000.0, 36_000.0), 499.0)  # the actor was somewhere else
    assert t.update(bits, batch, 500.0, learn=True) is None


def test_anchor_table_is_bounded():
    t = ActorLocationTracker()
    for h in range(ActorLocationTracker.MAX_ANCHORS + 5):
        t.note_creation(h, (1.0, 2.0, 3.0), 0.0)
    t.note_creation(10**9, (1.0, 2.0, 3.0), 10_000.0)  # everything older than the anchor lifetime is dropped
    assert len(t._last) <= 2 and t.last_known(10**9) == (1.0, 2.0, 3.0)  # noqa: SLF001


def test_fixed_layout_synthetic_second_layout_round_trips():
    layout = ACTOR_LAYOUTS[1]
    w = BitWriter()
    w.write(layout.header, 33)
    w.write(0x12345, layout.offset - 33)
    w.write(26 | 64, 7)
    for v in (124_086.5, -255_433.5, 25_629.0):
        w.write(round(v * 128), 26)
    w.write(0x2AAAAAAAA, layout.tail_bits)
    bits, batch = Bits(w.to_bytes()), IrisBatch(9, 0, w.nbits, True, False)
    assert batch.data_bit_count == 192
    loc = decode_actor_location(bits, batch)
    assert loc is not None and (loc.x, loc.y, loc.z) == pytest.approx((124_086.5, -255_433.5, 25_629.0)) and not loc.z_reliable
    assert decode_actor_location(bits, IrisBatch(9, 0, 193, True, False)) is None  # size equation broken


# ---------------------------------------------------------------- vitals of other creatures
def vitals_block_batch(health, max_health, stamina, max_stamina, lead=70, tail=30, handle=9):
    w = BitWriter()
    w.write(0b01000, 5)
    w.write(0x155, lead - 5)
    from isle_livemap.decode import to_f32
    import struct

    def slot(v):
        raw = struct.unpack("<I", struct.pack("<f", v))[0]
        w.write(raw, 32)
        w.write(1, 1)
        w.write(raw, 32)
        w.write(0, 1)  # slots sit 66 bits apart

    for v in (health, max_health, stamina, max_stamina, max_health, max_health / 2, max_health / 2, max_health, max_health):
        slot(to_f32(v))
    w.write(0x2AA, tail)
    return Bits(w.to_bytes()), IrisBatch(handle, 0, w.nbits, True, False)


def test_vitals_block_gives_health_and_stamina_percentages():
    v = decode_vitals_block(*vitals_block_batch(11.55, 14.72, 242.68, 357.92))
    assert v is not None and v.net_ref_handle == 9
    assert v.health_pct == pytest.approx(78.46, abs=0.05) and v.stamina_pct == pytest.approx(67.80, abs=0.05)
    full = decode_vitals_block(*vitals_block_batch(9.34, 9.34, 282.05, 282.05))
    assert full.health_pct == 100.0 and full.stamina_pct == 100.0


def test_vitals_block_rejects_broken_invariants_and_denormal_noise():
    bits, batch = vitals_block_batch(11.55, 14.72, 242.68, 357.92)
    assert decode_vitals_block(bits, IrisBatch(9, 0, 300, True, False)) is None  # too short for nine slots
    assert decode_vitals_block(*vitals_block_batch(20.0, 14.72, 242.68, 357.92)) is None  # health above its maximum
    assert decode_vitals_block(*vitals_block_batch(11.55, 14.72, 400.0, 357.92)) is None  # stamina above its maximum
    assert decode_vitals_block(*vitals_block_batch(0.0, 0.0, 0.0, 0.0)) is None  # all-zero maxima are not a creature
    assert decode_vitals_block(Bits(bytes(200)), IrisBatch(9, 0, 1600, True, False)) is None


def test_vitals_blocks_of_a_real_pteranodon_are_continuous(join_packets):
    seq = []
    for cap in (1014, 1659, 2345):
        r = one(join_packets, cap)
        pkt = parse_iris_packet(r["payload"])
        bits = Bits(r["payload"])
        v = next(decode_vitals_block(bits, b) for b in pkt.batches if b.net_ref_handle == 74400 and b.data_bit_count >= 593)
        seq.append((r["t"], v.health_pct, v.stamina_pct, v.max_health))
    assert [round(h, 1) for _, h, _, _ in seq] == [78.4, 79.2, 79.9]  # health regenerating, 0.8 %/16 s
    assert seq[0][3] < seq[1][3] < seq[2][3] and 14 < seq[0][3] and seq[2][3] < 16  # max health grows with growth
    assert all(60 < s < 70 for _, _, s, _ in seq)


def test_peer_entity_carries_vitals_species_and_name(join_packets):
    pk = [r for r in join_packets if r["cap_packet"] <= 1014]
    g = replay(pk)
    e = g.store.get("74400")
    assert e is not None and (e.kind, e.species) == ("player", "Pteranodon")
    assert e.health_pct == pytest.approx(78.4, abs=0.1) and e.stamina_pct == pytest.approx(67.8, abs=0.1)
    assert e.name == g.identity.player_names[66576]
    assert e.health == pytest.approx(e.health_pct / 100.0 * e.max_health, rel=1e-3) and 10 < e.health < e.max_health  # raw points
    assert e.max_src == "block" and e.max_t is not None and e.stamina is not None and e.max_stamina  # a full block: real maxima
    near = visible_entities([(e, e.x + 50_000, e.y)], e.x, e.y, 1_000.0)  # 500 m east of the viewer
    head, tail = peer_label_lines(near[0])
    assert head.endswith(" - Pteranodon - 500 m") and tail.startswith("HP 12/") and "  ST 243/" in tail  # the points the game sent, not a percentage


# ---------------------------------------------------------------- GamePeerSource: only creatures reach the map
def test_world_objects_never_become_peers(join_packets):
    g = replay(join_packets)
    assert g.filtered_world_objects > 50  # plants, fruit, gore, player states, level actors: batches skipped
    for p in g.store.entities():
        assert p.entity.kind in ("player", "dino", "ai", "unknown")
    kinds = {p.entity.id: (p.entity.kind, p.entity.species) for p in g.store.entities()}
    assert kinds["86612"] == ("player", "Gallimimus")
    assert not {"85760", "2"} & set(kinds)  # PlayerController, GameState


def test_status_and_summary_report_what_was_decoded(join_packets):
    g = replay(join_packets)
    g.pipeline = TelemetryPipeline()
    st = g.status()
    assert "with species" in st and "not available" in st and not st.startswith("game: unavailable")
    s = g.summary()
    assert "positions fixed=" in s and "learned=" in s and "world-object batches skipped=" in s and "player names=" in s


def test_unknown_class_with_a_verified_layout_is_still_shown_but_not_labelled(join_packets):
    """A creature whose creation record was not captured (join before the app started) keeps the old behaviour."""
    g = replay([one(join_packets, 1220)])  # a rainbow-fish state batch without its creation record
    assert g.store.entities() and all(p.entity.kind == "unknown" and p.entity.species == "" for p in g.store.entities())


# ---------------------------------------------------------------- own vitals: heartbeat with an extra slot
def test_heartbeat_with_an_extra_slot_is_recognised_and_locks_the_own_handle(join_packets):
    hb = group(join_packets, "heartbeat")
    assert [r["cap_packet"] for r in hb] == [5119, 5159, 5195]
    plain, extra1, extra2 = hb
    pkt = parse_iris_packet(extra1["payload"])
    batch = next(b for b in pkt.batches if b.net_ref_handle == 85864 and b.data_bit_count > 300)
    h = _find_heartbeat(Bits(extra1["payload"]), batch, None)
    assert h is not None and h.hunger == pytest.approx(0.22201, abs=1e-4) and h.thirst == pytest.approx(712.377, abs=1e-2)
    # the lock needs two heartbeats; both of these are the 4-slot form that the triple-only match never saw, so the
    # own handle could not be re-learned after it timed out (the user's "own vitals froze")
    vt = VitalsTracker()
    vt.track(extra1["payload"], extra1["t"])
    assert vt.self_handle is None
    vt.track(extra2["payload"], extra2["t"])
    assert vt.self_handle == 85864


# ---------------------------------------------------------------- PlayerState reference variants
def _ref_batch(handle, filler, nbytes=3, lead=100, marker=0b10100011):
    """A synthetic pawn record: noise, the reference marker, 28 filler bits, then byte count - 1 and the handle."""
    w = BitWriter()
    w.write(0, lead)
    w.write(marker, 8)
    w.write(filler, 28)
    w.write(nbytes - 1, 3)
    w.write(handle, nbytes * 8)
    w.write(0, 40)
    return Bits(w.to_bytes()), IrisBatch(1, 0, w.nbits, False, False)


@pytest.mark.parametrize("filler", [0x0000001, 0xA000001, 0x4000001, 0x8000000])
def test_player_state_reference_does_not_depend_on_the_bits_before_the_count(filler):
    tr = IdentityTracker()
    bits, batch = _ref_batch(179074, filler)
    assert tr._reference_candidates(bits, batch, 0) == (179074,)  # noqa: SLF001


def test_player_state_reference_two_byte_handle_and_non_canonical_width_rejected():
    tr = IdentityTracker()
    assert tr._reference_candidates(*_ref_batch(36450, 5, nbytes=2), 0) == (36450,)  # noqa: SLF001
    assert tr._reference_candidates(*_ref_batch(36450, 5, nbytes=3), 0) == ()  # noqa: SLF001 - padded, not a handle


def test_pawn_links_to_a_player_state_that_arrives_after_it():
    tr = IdentityTracker()
    from isle_livemap.decode import ActorInfo
    info = tr.actors[500] = ActorInfo(500, None, None, 0)
    info.ref_candidates = (179074, 42)
    tr._waiting.add(500)  # noqa: SLF001
    tr._resolve_player_state(info)  # noqa: SLF001
    assert info.player_state is None and tr.name_of(500) is None
    tr.player_names[179074] = "pSynthetic"
    tr._resolve_player_state(info)  # noqa: SLF001
    assert info.player_state == 179074 and tr.name_of(500) == "pSynthetic" and 500 not in tr._waiting  # noqa: SLF001


def test_pawn_with_two_known_player_state_candidates_is_not_linked():
    from isle_livemap.decode import ActorInfo
    tr = IdentityTracker()
    tr.player_names.update({100001: "pA", 100002: "pB"})
    info = ActorInfo(7, None, None, 0)
    info.ref_candidates = (100001, 100002)
    tr._resolve_player_state(info)  # noqa: SLF001
    assert info.player_state is None


# ---------------------------------------------------------------- the variant marker (two of the eight bits are not constant)
VARIANT = 0b10001011  # stream bits 1,1,0,1,0,0,0,1: recorded on a Giganotosaurus whose player reference is a 2-byte handle


def test_the_variant_marker_gives_loose_candidates_inside_the_recorded_window_only():
    tr = IdentityTracker()
    bits, batch = _ref_batch(35744, 5, nbytes=2, lead=700, marker=VARIANT)
    assert tr._reference_candidates(bits, batch, 0) == ()  # noqa: SLF001 - the strict marker is not there
    assert tr._loose_reference_candidates(bits, batch, ()) == (35744,)  # noqa: SLF001
    bits, batch = _ref_batch(35744, 5, nbytes=2, lead=100, marker=VARIANT)  # too early in the record
    assert tr._loose_reference_candidates(bits, batch, ()) == ()  # noqa: SLF001
    bits, batch = _ref_batch(35744, 5, nbytes=2, lead=1200, marker=VARIANT)  # too late
    assert tr._loose_reference_candidates(bits, batch, ()) == ()  # noqa: SLF001


def test_loose_candidates_take_two_or_three_byte_handles_and_skip_the_strict_marker():
    tr = IdentityTracker()
    assert tr._loose_reference_candidates(*_ref_batch(179074, 5, nbytes=3, lead=700, marker=VARIANT), ()) == (179074,)  # noqa: SLF001
    assert tr._loose_reference_candidates(*_ref_batch(200, 5, nbytes=1, lead=700, marker=VARIANT), ()) == ()  # noqa: SLF001
    bits, batch = _ref_batch(35744, 5, nbytes=2, lead=700)  # the strict marker: not a loose candidate
    assert tr._reference_candidates(bits, batch, 0) == (35744,) and tr._loose_reference_candidates(bits, batch, (35744,)) == ()  # noqa: SLF001


def test_a_loose_candidate_links_only_when_no_strict_candidate_is_a_known_player_state():
    from isle_livemap.decode import ActorInfo
    tr = IdentityTracker()
    tr.player_names.update({35744: "pLoose", 100001: "pStrict"})
    only_loose = ActorInfo(1, None, None, 0)
    only_loose.ref_candidates, only_loose.ref_candidates_loose = (42,), (35744,)
    tr._resolve_player_state(only_loose)  # noqa: SLF001
    assert only_loose.player_state == 35744
    both = ActorInfo(2, None, None, 0)
    both.ref_candidates, both.ref_candidates_loose = (100001,), (35744,)
    tr._resolve_player_state(both)  # noqa: SLF001
    assert both.player_state == 100001  # the strict one wins; the loose one never overrides it
    two_loose = ActorInfo(3, None, None, 0)
    tr.player_names[35745] = "pOther"
    two_loose.ref_candidates_loose = (35744, 35745)
    tr._resolve_player_state(two_loose)  # noqa: SLF001
    assert two_loose.player_state is None  # two known candidates: no link


def test_a_pawn_record_with_the_variant_marker_is_linked_end_to_end():
    from isle_livemap.decode import ActorInfo
    tr = IdentityTracker()
    tr.player_names[35744] = "pSynthetic"
    info = tr.actors[900] = ActorInfo(900, None, None, 0)
    bits, batch = _ref_batch(35744, 5, nbytes=2, lead=700, marker=VARIANT)
    info.ref_candidates = tr._reference_candidates(bits, batch, 0)  # noqa: SLF001
    info.ref_candidates_loose = tr._loose_reference_candidates(bits, batch, info.ref_candidates)  # noqa: SLF001
    tr._resolve_player_state(info)  # noqa: SLF001
    assert tr.name_of(900) == "pSynthetic"
