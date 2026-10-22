"""Nearby-entities radar: a thin adapter over the shared ``isle_livemap.geo`` path.

The local player comes from ``geo.local_entity`` and every nearby entry is a
``geo.Entity``, so positions, vitals (0-100 %), names, ids, the self filter, 3D
distance and height delta all use the same code as the map overlay and Nearby panel.
Nothing is parsed or computed separately here.

UNITS / AXES WARNING: ``geo`` works in Unreal centimetres with X/Y horizontal and Z as
height. The lab radar input (``from_lab``) is **Y-up metres** (X and Z horizontal, Y
height); it is converted once, at the boundary, into the geo convention.

``tabulate`` is an optional dependency: used when installed, otherwise a plain
aligned table is printed.
"""
from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from .geo import (
    UNITS_PER_METER, Entity, Nearby, _pct, distance_3d_m, height_delta_m, local_entity, nearby_3d, truncate,
    vital_pct,
)

try:  # optional; not added to requirements
    from tabulate import tabulate as _tabulate
except ImportError:  # pragma: no cover - depends on environment
    _tabulate = None

HEADERS = ["Name", "Species", "Health", "Growth", "Distance (m)", "Delta height (m)"]


def _num(value: Any) -> float:
    """float(value), rejecting bools, NaN and infinities (ValueError/TypeError)."""
    if isinstance(value, bool):
        raise TypeError("bool is not a coordinate")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError("non-finite number")
    return f


def _cm(pos: Mapping[str, Any]) -> tuple[float, float, float]:
    """Lab Y-up metres {"x","y","z"} -> geo (x, y, height) in centimetres."""
    return (_num(pos["x"]) * UNITS_PER_METER, _num(pos["z"]) * UNITS_PER_METER,
            _num(pos["y"]) * UNITS_PER_METER)


def _opt(value: Any) -> float | None:
    try:
        return _num(value)
    except (TypeError, ValueError):
        return None


def my_entity(my_pos: Mapping[str, Any]) -> Entity:
    """The local player via ``geo.local_entity``. Raises ValueError/KeyError if ``my_pos`` is invalid."""
    x, y, z = _cm(my_pos)
    return local_entity(SimpleNamespace(x=x, y=y, z=z), None)


def from_lab(raw: Mapping[str, Any]) -> Entity | None:
    """One lab dict -> ``geo.Entity`` (None if ``pos`` is missing or non-numeric).

    ``health`` is a 0-100 value, ``growth`` a 0-1 fraction; both go through ``vital_pct``
    like the local player's vitals. Missing name/species/vitals stay empty / None."""
    try:
        x, y, z = _cm(raw["pos"])
    except (KeyError, TypeError, ValueError):
        return None
    name, species = raw.get("name"), raw.get("species")
    return Entity(
        str(raw.get("id", "")), "player", name if isinstance(name, str) else "",
        species if isinstance(species, str) else "", x, y, z,
        growth_pct=vital_pct(_opt(raw.get("growth")), 1.0), health_pct=vital_pct(_opt(raw.get("health")), 100.0),
    )


def nearby_entities(my_pos: Mapping[str, Any], entities: Iterable[Mapping[str, Any]],
                    max_distance: float = 100.0) -> list[Nearby]:
    """Entities within ``max_distance`` metres (3D, strictly less) of ``my_pos``, nearest first.

    Y is vertical: distance = sqrt(dx^2 + dy^2 + dz^2) and the height delta is
    entity.y - me.y (positive = higher), computed by ``geo.distance_3d_m`` /
    ``geo.height_delta_m``. Entities with a missing or non-numeric ``pos`` are skipped.
    Raises ValueError/KeyError only if ``my_pos`` itself is invalid.
    """
    me = my_entity(my_pos)
    parsed = [e for e in map(from_lab, entities) if e is not None]
    return nearby_3d(me, parsed, max_distance)


def table_rows(rows: Iterable[Nearby], my_y: float | None) -> list[list[str]]:
    """Cells for ``HEADERS``; ``my_y`` is the local height in geo units (``my_entity(...).z``)."""
    out = []
    for n in rows:
        e, dy = n.entity, height_delta_m(n, my_y)
        out.append([truncate(e.name or e.id or "?", 24), e.species or "-", _pct(e.health_pct), _pct(e.growth_pct),
                    f"{distance_3d_m(n, my_y):.1f}",
                    "?" if dy is None else f"{dy:+.1f} ({'higher' if dy > 0 else 'lower' if dy < 0 else 'level'})"])
    return out


def format_table(rows: Iterable[Nearby], my_y: float | None) -> str:
    body = table_rows(rows, my_y)
    if _tabulate is not None:
        return _tabulate(body, headers=HEADERS, tablefmt="simple")
    widths = [max(len(r[i]) for r in [HEADERS, *body]) for i in range(len(HEADERS))]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(HEADERS), "  ".join("-" * w for w in widths), *map(line, body)])


def print_nearby(my_pos: Mapping[str, Any], entities: Iterable[Mapping[str, Any]],
                 max_distance: float = 100.0) -> list[Nearby]:
    """Compute, print the table, and return the list (see ``nearby_entities``)."""
    rows = nearby_entities(my_pos, entities, max_distance)
    print(format_table(rows, my_entity(my_pos).z))
    return rows


if __name__ == "__main__":  # pragma: no cover
    print_nearby({"x": 100.0, "y": 50.0, "z": 200.0}, [
        {"id": 1, "name": "Alpha", "species": "Rex", "health": 100, "growth": 0.8,
         "pos": {"x": 105.0, "y": 52.0, "z": 202.0}},
        {"id": 2, "name": "Beta", "species": "Raptor", "health": 60, "growth": 0.5,
         "pos": {"x": 100.0, "y": 10.0, "z": 200.0}},
    ])
