"""Two class-path mistakes that put the wrong things on the minimap (recorded on a server whose name is not kept here):

* a burrow actor (``BP_PsittacosaurusBurrow``, ``BP_DryoBurrow``) lives under the dinosaur's folder but is a world object. It
  was drawn as a player-owned dinosaur: 36 of the 44 "creatures" of that session were burrows 1.5-4 km away.
* a pawn of the Giganotosaurus / Spinosaurus pack lives under ``/Game/GigaSpino/<Species>/``, not under
  ``/Characters/Dinosaurs/``. It was classed as a world object and never drawn, however close it was.

Synthetic strings only; the real ``_on_inbound`` loop is driven."""

from __future__ import annotations

import pytest

from isle_livemap import species as sp

from test_carcass_and_stale_spawn import creation_packet
from test_resume_follow import ME, source  # noqa: F401 - the fixture

PSITTA_BURROW = "/Game/TheIsle/Core/Characters/Dinosaurs/Psittacosaurus/BP_PsittacosaurusBurrow.Default__BP_PsittacosaurusBurrow_C"
DRYO_BURROW = "/Game/TheIsle/Core/Characters/Dinosaurs/Dryosaurus/BP_DryoBurrow.Default__BP_DryoBurrow_C"
GIGA = "/Game/GigaSpino/Giganotosaurus/BP_Giganotosaurus.Default__BP_Giganotosaurus_C"
SPINO = "/Game/GigaSpino/Spinosaurus/BP_Spinosaurus.Default__BP_Spinosaurus_C"
DRYO = "/Game/TheIsle/Core/Characters/Dinosaurs/Dryosaurus/BP_Dryosaurus.Default__BP_Dryosaurus_C"


@pytest.mark.parametrize("path", [PSITTA_BURROW, DRYO_BURROW])
def test_a_burrow_is_a_world_object_not_a_creature(path):
    assert sp.kind_of_path(path) == sp.KIND_OTHER
    assert sp.describe(path) == (sp.KIND_OTHER, "")
    assert sp.describe(path, asset_species="Psittacosaurus") == (sp.KIND_OTHER, "")  # its own assets name a species
    assert sp.folder_species(path) is None  # a burrow names no species by its folder


def test_the_real_pawn_next_to_a_burrow_class_is_still_a_dinosaur():
    assert sp.describe(DRYO) == (sp.KIND_DINO, "Dryosaurus")


@pytest.mark.parametrize("path, species", [(GIGA, "Giganotosaurus"), (SPINO, "Spinosaurus")])
def test_a_pawn_of_the_giga_spino_pack_is_a_dinosaur(path, species):
    assert sp.kind_of_path(path) == sp.KIND_DINO
    assert sp.describe(path) == (sp.KIND_DINO, species)
    assert sp.folder_species(path) == species
    assert sp.describe(path, has_player_state=True) == (sp.KIND_PLAYER, species)


def test_an_animation_asset_of_the_pack_names_its_species():
    assert sp.asset_species("/Game/GigaSpino/Giganotosaurus/Animations/AM_Giganotosaurus_Adult_Bite") == "Giganotosaurus"


def test_a_burrow_is_not_drawn_and_a_giganotosaurus_is(source, monkeypatch):
    g = source
    far = (ME[0] + 150_000.0, ME[1], ME[2])
    near = (ME[0] + 60_000.0, ME[1] + 10_000.0, ME[2])
    creation_packet(g, monkeypatch, [(9301, PSITTA_BURROW, far), (9302, DRYO_BURROW, far), (9303, GIGA, near)], 20.0)
    assert g.store.get("9301") is None and g.store.get("9302") is None
    giga = g.store.get("9303")
    assert giga is not None and giga.kind == "dino" and giga.species == "Giganotosaurus"


# ---------------------------------------------------------------- a late start: no class path to tell a burrow
def test_a_small_creation_record_that_names_dinosaur_assets_without_a_class_path_is_not_a_creature(source):
    from isle_livemap.decode import ActorInfo

    g = source
    g.identity.actors[9401] = ActorInfo(9401, None, (ME[0] + 150_000.0, ME[1], ME[2]), 7, ("Psittacosaurus",), creation_bits=262)
    g.identity.actors[9402] = ActorInfo(9402, None, (ME[0] + 60_000.0, ME[1], ME[2]), 8, ("Psittacosaurus",), creation_bits=3100)
    assert g.identity.is_object_record(9401) and not g.identity.is_object_record(9402)
    assert g._identity(9401)[0] == sp.KIND_OTHER  # noqa: SLF001
    assert g._identity(9402)[0] != sp.KIND_OTHER  # noqa: SLF001  (a pawn-sized record is never hidden as an object)


def test_asset_species_of_a_burrow_is_not_offered_to_the_class_cache(source):
    from isle_livemap.classcache import ClassCache
    from isle_livemap.decode import ActorInfo

    g = source
    g.identity.class_cache = ClassCache(None, seed={})
    g.identity.actors[9401] = ActorInfo(9401, 55, None, 7, ("Psittacosaurus",), creation_bits=262)
    g.identity._handle_assets[9401] = __import__("collections").Counter({"Psittacosaurus": 3})  # noqa: SLF001
    g.identity._learn_asset_class(9401)  # noqa: SLF001
    assert g.identity.class_cache.lookup(7) is None
