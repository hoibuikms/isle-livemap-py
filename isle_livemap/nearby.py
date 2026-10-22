"""Standalone nearby-entities radar: pure function plus a small table printer.

UNITS / AXES WARNING: this module uses **Y-up metres** (X and Z are the horizontal
plane, Y is height), exactly as specified for the lab radar. The existing
``isle_livemap.geo`` works in Unreal centimetres with X/Y horizontal and Z as
height. Do not feed values between the two without converting. Only the
unit-free ``truncate`` helper is reused from geo.

``tabulate`` is an optional dependency: used when installed, otherwise a plain
aligned table is printed.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from .geo import truncate

try:  # optional; not added to requirements
    from tabulate import tabulate as _tabulate
except ImportError:  # pragma: no cover - depends on environment
    _tabulate = None

HEADERS = ["Name", "Species", "Health", "Growth", "Distance (m)", "Height diff (m)"]


def _num(value: Any) -> float:
    """float(value), rejecting bools, NaN and infinities (ValueError/TypeError)."""
    if isinstance(value, bool):
        raise TypeError("bool is not a coordinate")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError("non-finite number")
    return f


def _point(pos: Mapping[str, Any]) -> tuple[float, float, float]:
    return _num(pos["x"]), _num(pos["y"]), _num(pos["z"])


def nearby_entities(my_pos: Mapping[str, Any], entities: Iterable[Mapping[str, Any]],
                    max_distance: float = 100.0) -> list[dict]:
    """Entities within ``max_distance`` metres of ``my_pos``, nearest first.

    Math (Y-up, metres):
        dx = e.x - me.x,  dy = e.y - me.y,  dz = e.z - me.z
        distance = sqrt(dx^2 + dy^2 + dz^2)      (3D Euclidean distance)
        height_diff = dy = e.y - me.y
    Sign convention for ``height_diff``: positive = entity is higher than you,
    negative = lower, zero = level.

    Only entities with ``distance < max_distance`` (strictly) are kept. Entities
    whose ``pos`` is missing or non-numeric are skipped, never raised on; missing
    name/species/health/growth are kept and shown as None ("?" in the table).

    Returns copies of the input dicts with ``distance`` and ``height_diff`` added.
    Raises ValueError/KeyError only if ``my_pos`` itself is invalid.
    """
    mx, my, mz = _point(my_pos)
    rows: list[dict] = []
    for ent in entities:
        try:
            ex, ey, ez = _point(ent["pos"])
        except (KeyError, TypeError, ValueError):
            continue  # malformed entity: skip
        dx, dy, dz = ex - mx, ey - my, ez - mz
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist < max_distance:
            rows.append({**ent, "distance": dist, "height_diff": dy})
    rows.sort(key=lambda r: r["distance"])
    return rows


def _health(v: Any) -> str:
    return "?" if v is None else str(v)


def _growth(v: Any) -> str:
    try:
        return f"{_num(v):.0%}"  # 0.8 -> "80%"
    except (TypeError, ValueError):
        return "?"


def _height(dy: float) -> str:
    return f"{dy:+.1f} ({'higher' if dy > 0 else 'lower' if dy < 0 else 'level'})"


def table_rows(rows: Iterable[Mapping[str, Any]]) -> list[list[str]]:
    return [[truncate(str(r.get("name") or "?"), 24), str(r.get("species") or "-"),
             _health(r.get("health")), _growth(r.get("growth")),
             f"{r['distance']:.1f}", _height(r["height_diff"])] for r in rows]


def format_table(rows: Iterable[Mapping[str, Any]]) -> str:
    body = table_rows(rows)
    if _tabulate is not None:
        return _tabulate(body, headers=HEADERS, tablefmt="simple")
    widths = [max(len(r[i]) for r in [HEADERS, *body]) for i in range(len(HEADERS))]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(HEADERS), "  ".join("-" * w for w in widths), *map(line, body)])


def print_nearby(my_pos: Mapping[str, Any], entities: Iterable[Mapping[str, Any]],
                 max_distance: float = 100.0) -> list[dict]:
    """Compute, print the table, and return the list (see ``nearby_entities``)."""
    rows = nearby_entities(my_pos, entities, max_distance)
    print(format_table(rows))
    return rows


if __name__ == "__main__":  # pragma: no cover
    print_nearby({"x": 100.0, "y": 50.0, "z": 200.0}, [
        {"id": 1, "name": "Alpha", "species": "Rex", "health": 100, "growth": 0.8,
         "pos": {"x": 105.0, "y": 52.0, "z": 202.0}},
        {"id": 2, "name": "Beta", "species": "Raptor", "health": 60, "growth": 0.5,
         "pos": {"x": 100.0, "y": 10.0, "z": 200.0}},
    ])
