"""How sure the minimap is about what a label says: tiers per field, the label wording for each, and the hysteresis
that keeps a label from changing wording every frame.

Species   confirmed  this connection named it: the server's export tables, or the dinosaur assets that its own creation
                     records reference (the report says which: species source "table" / "asset")
          inferred   a class remembered from earlier sessions (saved class cache / shipped seed): shown with "~" and
                     drawn softer
          unknown    nothing is known: shown as "Dino" ("Animal" for a server animal), drawn softer
Name      a player name is only ever shown when the creature references its PlayerState; otherwise there is none
Vitals    known      a full attribute block was seen (so the maxima are real) and the value is younger than VITALS_FRESH_S
          stale      the same value, older: shown with how old it is
          none       never seen for this creature: the second line is left out; a part of it that is missing shows an en dash
Growth, hunger and thirst of other creatures are not replicated to us: they are never shown, not even as "?".

Pure functions only (no Qt, no geo import), so the minimap, the Nearby table, the session log and tools/label_report.py
all say the same thing."""

from __future__ import annotations

from typing import Callable, Hashable

SPECIES_CONFIRMED, SPECIES_INFERRED, SPECIES_UNKNOWN = "confirmed", "inferred", "unknown"
VITALS_KNOWN, VITALS_STALE, VITALS_NONE = "known", "stale", "none"

SPECIES_RANK = {SPECIES_UNKNOWN: 0, SPECIES_INFERRED: 1, SPECIES_CONFIRMED: 2}
VITALS_RANK = {VITALS_NONE: 0, VITALS_STALE: 1, VITALS_KNOWN: 2}

VITALS_FRESH_S = 120.0  # an unchanged value is not re-sent (deltas only), so a reading stays true for a long time; older is shown as old
DWELL_S = 3.0  # a label gets less sure only after the weaker evidence has lasted this long (more sure: at once)

# Names the game source gives a creature whose player name is not known: not a name.
PLACEHOLDER_PREFIXES = ("Actor #", "Unknown #")
SPECIES_SOURCES = ("table", "asset", "cache", "seed")  # where a species came from (IdentityTracker.species_source)
INFERRED_SOURCES = ("cache", "seed")


def is_placeholder_name(name: str | None) -> bool:
    return not name or name.startswith(PLACEHOLDER_PREFIXES)


def species_tier(species: str | None, inferred: bool) -> str:
    if not species:
        return SPECIES_UNKNOWN
    return SPECIES_INFERRED if inferred else SPECIES_CONFIRMED


def vitals_tier(health_pct: float | None, stamina_pct: float | None, age_s: float | None,
                fresh_s: float = VITALS_FRESH_S) -> str:
    """none: no reading; stale: the reading is older than ``fresh_s``; known: a fresh one (or one with no age:
    the local player and the lab feed carry no timestamp)."""
    if health_pct is None and stamina_pct is None:
        return VITALS_NONE
    return VITALS_STALE if age_s is not None and age_s > fresh_s else VITALS_KNOWN


def age_tag(age_s: float | None) -> str:
    """Coarse age of a stale reading ("20s+", "1m+", "2m+", "5m+", "10m+"): a handful of values over a creature's life, not a
    counter that changes the label text every second."""
    if age_s is None:
        return ""
    for limit, text in ((600.0, "10m+"), (300.0, "5m+"), (120.0, "2m+"), (60.0, "1m+")):
        if age_s >= limit:
            return text
    return "20s+"


class TierTracker:
    """Holds the last shown value of one field per creature. A value of equal or higher rank replaces it at once; a lower
    one (the label would say less) only after it has lasted ``dwell_s``, so a creature whose identity or vitals flicker
    for a moment does not make its label flicker with it."""

    def __init__(self, rank: dict[str, int], dwell_s: float = DWELL_S) -> None:
        self.rank, self.dwell_s = rank, dwell_s
        self._shown: dict[Hashable, tuple[str, object]] = {}
        self._pending: dict[Hashable, tuple[str, object, float]] = {}  # key -> (tier, payload, since)

    def settle(self, key: Hashable, tier: str, payload: object, now: float) -> tuple[str, object]:
        shown = self._shown.get(key)
        if shown is None or self.rank[tier] >= self.rank[shown[0]]:
            self._shown[key] = (tier, payload)
            self._pending.pop(key, None)
            return tier, payload
        pend = self._pending.get(key)
        if pend is None or pend[0] != tier:
            self._pending[key] = (tier, payload, now)
        elif now - pend[2] >= self.dwell_s:
            self._shown[key] = (tier, payload)
            self._pending.pop(key, None)
            return tier, payload
        return shown

    def keep_only(self, keys) -> None:
        keys = set(keys)
        for table in (self._shown, self._pending):
            for k in [k for k in table if k not in keys]:
                del table[k]


class LabelConfidence:
    """What the minimap label of one creature may claim right now (one instance per map view)."""

    def __init__(self, dwell_s: float = DWELL_S, fresh_s: float = VITALS_FRESH_S) -> None:
        self._species = TierTracker(SPECIES_RANK, dwell_s)
        self._vitals = TierTracker(VITALS_RANK, dwell_s)
        self.fresh_s = fresh_s

    def settle(self, eid: str, species: str, inferred: bool, health: float | None, stamina: float | None,
               vitals_age_s: float | None, now: float) -> tuple[str, bool, float | None, float | None, str]:
        """(species, inferred, health, stamina, vitals tier) to print."""
        _, (sp, inf) = self._species.settle(eid, species_tier(species, inferred), (species, inferred), now)
        raw = vitals_tier(health, stamina, vitals_age_s, self.fresh_s)
        tier, (hp, st) = self._vitals.settle(eid, raw, (health, stamina), now)
        return sp, inf, hp, st, tier

    def keep_only(self, ids) -> None:
        self._species.keep_only(ids)
        self._vitals.keep_only(ids)
