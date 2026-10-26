"""A dinosaur is named by ANY asset of its species (animation montage, sound, mesh folder), not only by its class table.

Evidence from the recorded captures (see docs/PEERS.md, "Species from assets"): a T-Rex pawn exports
``/Game/TheIsle/Characters/Dinosaurs/Tyrannosaurus/Animations/AM_Tyrannosaurus_Adult_Swallow`` in its own batches, with or
without a creation record, while the join tables that name its class were missed. Synthetic batches only."""

from __future__ import annotations

from isle_livemap import species as sp
from isle_livemap.classcache import ClassCache
from isle_livemap.decode import ActorInfo, ExportSection, IdentityTracker, IrisBatch, IrisPacket, ObjectExport
from isle_livemap.geo import Entity, Nearby, entity_label_lines, entity_summary
from isle_livemap.peers import GamePeerSource

ANIM = "/Game/TheIsle/Characters/Dinosaurs/{s}/Animations/AM_{s}_{stage}_Swallow"
SOUND = "/Game/TheIsle/Characters/Dinosaurs/{s}/Sound/MS_{s}Rex_Call_Broadcast"


def export_packet(handle: int, *paths: str, destroyed: tuple[int, ...] = ()) -> IrisPacket:
    objs = tuple(ObjectExport(handle=9000 + i, name=p) for i, p in enumerate(paths))
    return IrisPacket(0, True, True, [IrisBatch(handle, 0, 0, False, True, ExportSection(objects=objs))], destroyed_handles=destroyed)


def feed(tr: IdentityTracker, packet: IrisPacket) -> None:
    tr.feed(b"\x00" * 16, packet)


def test_asset_species_reads_the_folder_and_the_asset_names():
    assert sp.asset_species(ANIM.format(s="Tyrannosaurus", stage="Adult")) == "Tyrannosaurus"
    assert sp.asset_species("/Game/X/AM_Tyrannosaurus_Subadult_Swallow") == "Tyrannosaurus"
    assert sp.asset_species("/Game/X/MS_TyrannosaurusRex_Call_Danger") == "Tyrannosaurus"
    assert sp.asset_species("/Game/TheIsle/Characters/Dinosaurs/Abilities/Bite") is None  # not a species folder
    assert sp.asset_species("/Game/X/AM_Fish_Swim_Adult_A") is None  # a name only counts for a species of the game
    assert sp.asset_species("/Game/X/MS_Ambient_Call_Wind") is None
    assert sp.asset_species(None) is None


def test_growth_stage_only_when_the_asset_states_it():
    assert sp.stage_of_path("/Game/X/AM_Tyrannosaurus_Juvenile_SitToStandFast") == "Juvenile"
    assert sp.stage_of_path("/Game/X/AM_Tyrannosaurus_Subadult_Swallow") == "Subadult"
    assert sp.stage_of_path("/Game/X/MS_TyrannosaurusRex_Call_Broadcast") == ""
    assert sp.stage_of_path(None) == ""


def test_a_single_species_actor_is_named_by_its_own_batches_without_a_creation_record():
    tr = IdentityTracker()
    feed(tr, export_packet(5000, ANIM.format(s="Tyrannosaurus", stage="Adult")))
    assert tr.handle_species(5000) == "Tyrannosaurus" and tr.species_hint(5000) == "Tyrannosaurus"
    assert tr.species_source(5000) == "asset" and tr.handle_stage(5000) == "Adult"
    feed(tr, export_packet(5000, SOUND.format(s="Tyrannosaurus")))  # more of the same species: still it
    assert tr.handle_species(5000) == "Tyrannosaurus"


def test_an_actor_naming_several_species_is_a_relay_and_says_nothing():
    tr = IdentityTracker()
    feed(tr, export_packet(5001, ANIM.format(s="Tyrannosaurus", stage="Adult")))
    feed(tr, export_packet(5001, ANIM.format(s="Triceratops", stage="Adult")))
    assert tr.handle_species(5001) is None and tr.species_hint(5001) is None and tr.handle_stage(5001) == ""
    feed(tr, export_packet(5002, ANIM.format(s="Tyrannosaurus", stage="Adult"), ANIM.format(s="Deinosuchus", stage="Adult")))
    assert tr.handle_species(5002) is None  # two species in one export section


def test_a_destroyed_handle_forgets_its_assets_so_a_reused_handle_starts_afresh():
    tr = IdentityTracker()
    feed(tr, export_packet(5003, ANIM.format(s="Tyrannosaurus", stage="Adult")))
    feed(tr, IrisPacket(1, True, True, [], destroyed_handles=(5003,)))
    assert tr.handle_species(5003) is None


def test_a_known_class_path_is_never_overridden_by_assets():
    tr = IdentityTracker()
    tr.actors[5004] = ActorInfo(5004, 77, None, 0xABCD)
    tr.objects[77] = ObjectExport(handle=77, name="/Game/TheIsle/Core/Characters/Dinosaurs/Triceratops/BP_Triceratops.Default__BP_Triceratops_C")
    feed(tr, export_packet(5004, ANIM.format(s="Tyrannosaurus", stage="Adult")))
    assert tr.species_hint(5004) is None  # the path decides
    assert tr.species_source(5004) == "table"


def test_peer_source_shows_an_actor_without_a_creation_record_as_a_named_dinosaur():
    g = GamePeerSource()
    assert g._identity(6000) == (sp.KIND_UNKNOWN, "", "")
    feed(g.identity, export_packet(6000, ANIM.format(s="Tyrannosaurus", stage="Subadult")))
    assert g._identity(6000) == (sp.KIND_DINO, "Tyrannosaurus", "")


def test_the_species_of_a_creation_less_actor_is_learned_into_the_class_cache_once_it_has_a_record():
    cache = ClassCache(None, seed={})
    tr = IdentityTracker(cache)
    tr.actors[5005] = ActorInfo(5005, 88, None, 0x4F0078DE)  # archetype 88: its class path was never exported
    feed(tr, export_packet(5005, ANIM.format(s="Tyrannosaurus", stage="Adult")))
    assert cache.lookup(0x4F0078DE) == sp.conventional_class_path("Tyrannosaurus")
    assert sp.species_of_path(cache.lookup(0x4F0078DE)) == "Tyrannosaurus"


def test_a_derived_class_path_and_the_real_one_of_the_same_species_do_not_conflict():
    cache = ClassCache(None, seed={1: sp.conventional_class_path("Tyrannosaurus")})
    real = "/Game/TheIsle/Core/Characters/Dinosaurs/Tyrannosaurus/BP_Rex.Default__BP_Rex_C"
    cache.learn(1, real)  # the tables arrive with the real path
    assert cache.conflicts == 0 and cache.lookup(1) == real
    cache.learn(1, "/Game/TheIsle/Core/Characters/Dinosaurs/Triceratops/BP_Triceratops.Default__BP_Triceratops_C")
    assert cache.conflicts == 1 and cache.lookup(1) is None  # another species: nobody is trusted


def _near(species: str, name: str = "", kind: str = "dino", stage: str = "") -> Nearby:
    return Nearby(Entity("1", kind, name or "Actor #1", species, 0.0, 0.0, stage=stage), 0.0, 0.0, 724.0, 0.0)


def test_a_long_first_line_says_t_rex_and_a_short_one_keeps_the_full_name():
    head, _ = entity_label_lines(_near("Tyrannosaurus"))
    assert head == "Player ? - Tyrannosaurus - 724 m"  # 33 characters: fits, the full name
    head, _ = entity_label_lines(_near("Tyrannosaurus", name="P1" * 8, kind="player"))
    assert head == "P1P1P1P1P1P1P1P1 - T-Rex - 724 m"  # would not fit: the short form, not "Tyrannosaur…"
    head, _ = entity_label_lines(_near("Pachycephalosaurus"))
    assert head == "Player ? - Pachy - 724 m"  # a name over 14 characters used to be cut in the middle
    head, _ = entity_label_lines(_near("Zzzsaurusxxxxxxxxxx"))
    assert head.endswith("… - 724 m")  # no short form: cut as before


def test_the_tooltip_keeps_the_full_name_and_the_stage_an_asset_states():
    text = entity_summary(_near("Tyrannosaurus", stage="Subadult"))
    assert "Tyrannosaurus (Subadult)" in text
    assert "(Subadult)" not in entity_summary(_near("Tyrannosaurus"))
