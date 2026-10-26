"""Class path -> what the actor is: ``kind`` (dino / ai / other) and a friendly ``species`` name.

Everything here is a pure function of the strings the server sends (asset paths from the Iris export sections,
see ``decode.IdentityTracker``). Two small tables decide, both explicit and covered by tests:

* ``CLASS_RULES``: which part of a class path means "creature". Anything with a known path that matches no rule
  is a world object (plants, fruit, gore, items, level actors) and is kept off the minimap. A path that is not
  known at all is "unknown", never "other".
* ``DISPLAY_NAMES``: the few places where the asset name is not the name people use. Every other species is the
  asset name itself, so a species that is not in any table still shows up under its real class name.

Examples (taken from a live capture)::

    /Game/TheIsle/Core/Characters/Dinosaurs/Gallimimus/BP_Gallimimus.Default__BP_Gallimimus_C -> dino, "Gallimimus"
    /Game/TheIsle/Core/AI/Characters/Fish/BP_Longear.Default__BP_Longear_C                     -> ai,   "Longear"
    /Game/TheIsle/Core/Spawnables/Fruits/BP_FruitMangoStatic.Default__BP_FruitMangoStatic_C   -> other
"""

from __future__ import annotations

import re

KIND_DINO = "dino"  # a dinosaur pawn: player controlled or not, the path cannot say
KIND_PLAYER = "player"  # a dinosaur pawn whose creation record references a PlayerState (structural proof)
KIND_AI = "ai"  # server controlled animal (fish)
KIND_OTHER = "other"  # world object: never drawn
KIND_UNKNOWN = "unknown"  # no class information at all (the creation record was not captured)

# First match wins. (substring of the class path, kind).
CLASS_RULES: tuple[tuple[str, str], ...] = (
    ("/Characters/Dinosaurs/", KIND_DINO),
    ("/AI/Characters/", KIND_AI),
    ("/Characters/Fishes/", KIND_AI),
)

# Class-name (after cleaning, see ``clean_class_name``) -> display name, only where the asset name is not what
# people say. "TyrannosaurusRex" is the name used by the sound assets of the same species.
DISPLAY_NAMES: dict[str, str] = {
    "TyrannosaurusRex": "Tyrannosaurus",
    "Fish_RainbowFish": "Rainbow Fish",
}

# Folder names under /Characters/Dinosaurs/ that are not a species.
_NOT_SPECIES_FOLDERS = frozenset({"Abilities"})
DINOSAUR_FOLDER = "/Characters/Dinosaurs/"


def clean_class_name(name: str) -> str:
    """``Default__BP_Triceratops_C`` / ``BP_Triceratops_C`` / ``/Game/.../BP_Triceratops`` -> ``Triceratops``."""
    name = name.rsplit(".", 1)[-1].rsplit("/", 1)[-1]
    if name.startswith("Default__"):
        name = name[len("Default__"):]
    if name.startswith("BP_"):
        name = name[3:]
    if name.endswith("_C"):
        name = name[:-2]
    return name


def display_name(raw: str) -> str:
    """Friendly form of an already cleaned class / folder name; unknown names are returned as they are."""
    return DISPLAY_NAMES.get(raw, raw)


def kind_of_path(path: str | None) -> str:
    """``dino`` / ``ai`` / ``other`` for a class path, ``unknown`` when there is no path."""
    if not path:
        return KIND_UNKNOWN
    for marker, kind in CLASS_RULES:
        if marker in path:
            return kind
    return KIND_OTHER


def folder_species(path: str | None) -> str | None:
    """Species folder of a ``/Characters/Dinosaurs/<Species>/...`` path (animations, sounds, the class itself)."""
    if not path or DINOSAUR_FOLDER not in path:
        return None
    folder = path.split(DINOSAUR_FOLDER, 1)[1].split("/", 1)[0]
    return folder if folder and folder not in _NOT_SPECIES_FOLDERS else None


def species_of_path(path: str | None) -> str:
    """Friendly species of a class path: the dinosaur folder, else the cleaned class name. "" when no path."""
    if not path:
        return ""
    folder = folder_species(path)
    return display_name(folder if folder else clean_class_name(path))


def describe(path: str | None, asset_species: str | None = None, has_player_state: bool = False) -> tuple[str, str]:
    """(kind, species) of an actor from its class path.

    ``asset_species``: a dinosaur folder learned from the assets that accompany this class's creation records
    (used only when the class path itself was never exported). ``has_player_state``: the creation record references
    a PlayerState, which makes a dinosaur a player (a PlayerState exists only for connected players)."""
    kind = kind_of_path(path)
    if kind == KIND_UNKNOWN and asset_species:
        kind, species = KIND_DINO, display_name(asset_species)
    elif kind == KIND_OTHER:
        return KIND_OTHER, ""
    else:
        species = species_of_path(path)
    if has_player_state and kind in (KIND_DINO, KIND_UNKNOWN):
        kind = KIND_PLAYER
    return kind, species


# -- dinosaur species named by an asset, whichever asset it is --------------------------------------------------
# A pawn's animation montages, sounds and mesh live under /Characters/Dinosaurs/<Species>/ (the folder), and the asset
# names repeat the species: AM_Tyrannosaurus_Adult_Swallow, MS_TyrannosaurusRex_Call_Broadcast. Both name the species
# even when the class itself was never exported (the join tables were missed).
GROWTH_STAGES = ("Hatchling", "Juvenile", "Subadult", "Adult")
_ASSET_NAME = re.compile(r"(?:^|/)(?:AM|MS|SK|SM|ABP|BP)_([A-Z][A-Za-z]+?)(?:Rex)?_(?:" + "|".join(GROWTH_STAGES) + r"|Call|Anim|Sound)")
_STAGE = re.compile(r"_(" + "|".join(GROWTH_STAGES) + r")_")

# Names that are species folders of the game; an asset NAME (no folder in its path) only counts when it starts with one of them,
# so that "AM_Fish_Swim" or "MS_Ambient_Wind" are never read as a dinosaur.
KNOWN_SPECIES = frozenset({
    "Allosaurus", "Austroraptor", "Beipiaosaurus", "Brachiosaurus", "Camptosaurus", "Carnotaurus", "Ceratosaurus",
    "Compsognathus", "Deinosuchus", "Diabloceratops", "Dilophosaurus", "Dromaeosaurus", "Dryosaurus", "Gallimimus",
    "Giganotosaurus", "Herrerasaurus", "Hypsilophodon", "Kentrosaurus", "Maiasaura", "Microraptor", "Omniraptor",
    "Oxalaia", "Pachycephalosaurus", "Parasaurolophus", "Pteranodon", "Sarcosuchus", "Spinosaurus", "Stegosaurus",
    "Tenontosaurus", "Therizinosaurus", "Triceratops", "Troodon", "Tyrannosaurus", "Utahraptor", "Acrocanthosaurus",
})


def asset_species(path: str | None) -> str | None:
    """Species a dinosaur asset belongs to: its ``/Characters/Dinosaurs/<Species>/`` folder, else the species in an
    animation / sound asset name (``AM_Tyrannosaurus_Adult_Swallow``, ``MS_TyrannosaurusRex_Call_Danger``), when that name
    is a species of the game. None for anything else."""
    folder = folder_species(path)
    if folder:
        return folder
    if not path:
        return None
    m = _ASSET_NAME.search(path.rsplit(".", 1)[0])
    if m is None:
        return None
    name = m.group(1)
    return name if name in KNOWN_SPECIES else None


def stage_of_path(path: str | None) -> str:
    """Growth stage an animation asset states (``AM_Tyrannosaurus_Subadult_Swallow`` -> "Subadult"); "" when it says none."""
    if not path:
        return ""
    m = _STAGE.search(path.rsplit("/", 1)[-1])
    return m.group(1) if m else ""


def conventional_class_path(folder: str) -> str:
    """The class path the game uses for every species seen so far (``.../Dinosaurs/<Folder>/BP_<Folder>``)."""
    return f"/Game/TheIsle/Core/Characters/Dinosaurs/{folder}/BP_{folder}.Default__BP_{folder}_C"


# -- short names for crowded labels -------------------------------------------------------------------------------
# The full name stays in the tables and tooltips. A minimap label whose first line would be long uses these instead of
# cutting a name in the middle ("Pachycephalosa…"): the name people say, not an ellipsis.
SHORT_NAMES: dict[str, str] = {
    "Tyrannosaurus": "T-Rex",
    "Pachycephalosaurus": "Pachy",
    "Parasaurolophus": "Parasaur",
    "Therizinosaurus": "Therizino",
    "Acrocanthosaurus": "Acro",
    "Giganotosaurus": "Giga",
    "Brachiosaurus": "Brachio",
    "Beipiaosaurus": "Beipiao",
    "Compsognathus": "Compy",
    "Diabloceratops": "Diablo",
    "Herrerasaurus": "Herrera",
    "Hypsilophodon": "Hypsi",
    "Tenontosaurus": "Tenonto",
}


def short_species(name: str) -> str:
    """Short form of a species name for a crowded label; the name itself when it has none."""
    return SHORT_NAMES.get(name, name)
