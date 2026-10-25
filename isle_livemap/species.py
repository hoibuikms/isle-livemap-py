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


# Relative adult body size per species, used only to scale the minimap dot. These are rough estimates (real-world
# length / mass of the animal, not game data): the game does not send a size, and other creatures' growth is not
# decodable. Species names are normalised by ``_size_key`` (case, spaces and a trailing "rex" ignored).
SIZE_TINY, SIZE_SMALL, SIZE_MEDIUM, SIZE_LARGE, SIZE_HUGE = "tiny", "small", "medium", "large", "huge"
SIZE_ORDER = (SIZE_TINY, SIZE_SMALL, SIZE_MEDIUM, SIZE_LARGE, SIZE_HUGE)
SIZE_CLASSES: dict[str, tuple[str, ...]] = {
    SIZE_TINY: ("Hypsilophodon", "Troodon", "Beipiaosaurus", "Dryosaurus", "Pteranodon", "Compsognathus", "Microraptor"),
    SIZE_SMALL: ("Dilophosaurus", "Herrerasaurus", "Omniraptor", "Utahraptor", "Gallimimus", "Pachycephalosaurus",
                 "Austroraptor", "Dromaeosaurus"),
    SIZE_MEDIUM: ("Carnotaurus", "Ceratosaurus", "Allosaurus", "Maiasaura", "Tenontosaurus", "Diabloceratops",
                  "Kentrosaurus", "Parasaurolophus", "Camptosaurus", "Oxalaia"),
    SIZE_LARGE: ("Stegosaurus", "Deinosuchus", "Acrocanthosaurus", "Therizinosaurus", "Sarcosuchus"),
    SIZE_HUGE: ("Tyrannosaurus", "Triceratops", "Giganotosaurus", "Spinosaurus", "Brachiosaurus"),
}
DEFAULT_SIZE = SIZE_MEDIUM  # a dinosaur whose species is not in the table (or not known at all)


def _size_key(name: str) -> str:
    key = "".join(c for c in name.lower() if c.isalnum())
    return key[:-3] if key.endswith("rex") else key


_SIZE_OF = {_size_key(sp): size for size, names in SIZE_CLASSES.items() for sp in names}


def size_class(species: str | None) -> str:
    """Size class of a species name (``Deinosuchus`` / ``deinosuchus~`` / ``TyrannosaurusRex``); unknown -> DEFAULT_SIZE."""
    return _SIZE_OF.get(_size_key((species or "").rstrip("~")), DEFAULT_SIZE)


# Diet per species, used only to colour the minimap dot (the game does not send it). Rough, from the animals' in-game
# diet; a species that is not listed has no diet and gets the neutral dinosaur colour.
DIET_CARNIVORE, DIET_HERBIVORE, DIET_OMNIVORE = "carnivore", "herbivore", "omnivore"
DIET_SPECIES: dict[str, tuple[str, ...]] = {
    DIET_CARNIVORE: ("Allosaurus", "Acrocanthosaurus", "Austroraptor", "Carnotaurus", "Ceratosaurus", "Compsognathus",
                     "Deinosuchus", "Dilophosaurus", "Dromaeosaurus", "Giganotosaurus", "Herrerasaurus", "Microraptor",
                     "Oxalaia", "Pteranodon", "Sarcosuchus", "Spinosaurus", "Troodon", "Tyrannosaurus", "Utahraptor"),
    DIET_HERBIVORE: ("Beipiaosaurus", "Brachiosaurus", "Camptosaurus", "Diabloceratops", "Dryosaurus", "Hypsilophodon",
                     "Kentrosaurus", "Maiasaura", "Parasaurolophus", "Stegosaurus", "Tenontosaurus", "Therizinosaurus",
                     "Triceratops"),
    DIET_OMNIVORE: ("Gallimimus", "Omniraptor", "Pachycephalosaurus"),
}
_DIET_OF = {_size_key(sp): diet for diet, names in DIET_SPECIES.items() for sp in names}


def diet_of(species: str | None) -> str:
    """"carnivore" / "herbivore" / "omnivore" for a species name (``Deinosuchus~`` and ``TyrannosaurusRex`` work), else ""."""
    return _DIET_OF.get(_size_key((species or "").rstrip("~")), "")


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
