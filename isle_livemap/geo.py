"""Pure-Python map logic: projection, rings, markers, settings, smoothing.

No Qt and no Windows imports here, so everything is unit-testable anywhere.
World units are Unreal units (centimetres): 100 units = 1 metre.
"""

from __future__ import annotations

import json
import math
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .decode import normalize_degrees

# ----------------------------------------------------------------- projection
# Gateway map projection, identical to GatewayMapProjection.cs.
WORLD_SCALE = 1000.0
X_OFFSET = 505.0
Y_OFFSET = 607.0
MAP_WIDTH = 1112.0
MAP_HEIGHT = 1116.0
UNITS_PER_METER = 100.0


def project_unclamped(x: float, y: float) -> tuple[float, float]:
    """World (x, y) -> normalised map (left, top), not clamped to [0, 1]."""
    return ((x / WORLD_SCALE + X_OFFSET) / MAP_WIDTH, (y / WORLD_SCALE + Y_OFFSET) / MAP_HEIGHT)


def project(x: float, y: float) -> tuple[float, float]:
    left, top = project_unclamped(x, y)
    return (min(max(left, 0.0), 1.0), min(max(top, 0.0), 1.0))


def unproject(left: float, top: float) -> tuple[float, float]:
    left = min(max(left, 0.0), 1.0) if math.isfinite(left) else 0.5
    top = min(max(top, 0.0), 1.0) if math.isfinite(top) else 0.5
    return ((left * MAP_WIDTH - X_OFFSET) * WORLD_SCALE, (top * MAP_HEIGHT - Y_OFFSET) * WORLD_SCALE)


# ----------------------------------------------------------------- distance / bearing


def distance_m(ax: float, ay: float, bx: float, by: float) -> float:
    """Horizontal distance in metres between two world points."""
    return math.hypot(bx - ax, by - ay) / UNITS_PER_METER


def bearing_deg(ax: float, ay: float, bx: float, by: float) -> float:
    """Compass bearing from A to B, clockwise from map-north (north = -Y).

    Same convention as MovementHeading.cs: atan2(dX, -dY)."""
    return normalize_degrees(math.degrees(math.atan2(bx - ax, -(by - ay))))


_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def compass_label(bearing: float) -> str:
    return _COMPASS[int(((bearing % 360.0) + 22.5) // 45) % 8]


def format_distance(meters: float) -> str:
    if meters < 1000.0:
        return f"{round(meters):d} m"
    km = meters / 1000.0
    text = f"{km:.1f}".rstrip("0").rstrip(".") if km < 10 else f"{km:.0f}"
    return f"{text} km"


# ----------------------------------------------------------------- range rings
DEFAULT_RING_RADII_M = (250.0, 500.0, 1000.0)


@dataclass(frozen=True, slots=True)
class Ring:
    radius_m: float
    rx: float  # radius as a fraction of map width
    ry: float  # radius as a fraction of map height
    label: str


def build_rings(radii_m: tuple[float, ...] | list[float] = DEFAULT_RING_RADII_M) -> list[Ring]:
    """Concentric rings centred on the player. Radii are real-world metres and
    are converted with the same scale as the projection (so they stay correct
    at every zoom level)."""
    rings = []
    for r in sorted({float(v) for v in radii_m if math.isfinite(v) and v > 0}):
        units = r * UNITS_PER_METER
        rings.append(Ring(r, units / (MAP_WIDTH * WORLD_SCALE), units / (MAP_HEIGHT * WORLD_SCALE), format_distance(r)))
    return rings


def parse_radii(text: str) -> list[float]:
    """'250, 500, 1km' -> [250.0, 500.0, 1000.0]. Invalid parts are ignored."""
    out: list[float] = []
    for part in text.replace(";", ",").split(","):
        p = part.strip().lower().replace(" ", "")
        if not p:
            continue
        mult = 1.0
        if p.endswith("km"):
            p, mult = p[:-2], 1000.0
        elif p.endswith("m"):
            p = p[:-1]
        try:
            v = float(p) * mult
        except ValueError:
            continue
        if math.isfinite(v) and 1.0 <= v <= 20_000.0:
            out.append(v)
    return out[:6]


# ----------------------------------------------------------------- markers (manual only)
MARKER_KINDS = ("waypoint", "nest", "water", "dinosaur", "other")


@dataclass(slots=True)
class Marker:
    """A marker the user placed by hand. Never derived from network data."""

    name: str
    kind: str
    x: float
    y: float
    notes: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def relative_to(self, px: float, py: float) -> tuple[float, float]:
        """(distance in metres, bearing in degrees) from the player."""
        return distance_m(px, py, self.x, self.y), bearing_deg(px, py, self.x, self.y)

    def describe(self, px: float, py: float) -> str:
        d, b = self.relative_to(px, py)
        return f"{self.name}  {format_distance(d)} {compass_label(b)} ({b:.0f}°)"


class MarkerStore:
    """JSON-file backed list of markers (local file, no network)."""

    MAX_MARKERS = 200

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "markers.json"
        self.markers: list[Marker] = []
        self.load()

    def load(self) -> None:
        self.markers = []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in raw.get("markers", []) if isinstance(raw, dict) else []:
            try:
                kind = str(item.get("kind", "other"))
                m = Marker(
                    name=str(item["name"])[:60],
                    kind=kind if kind in MARKER_KINDS else "other",
                    x=float(item["x"]),
                    y=float(item["y"]),
                    notes=str(item.get("notes", ""))[:300],
                    id=str(item.get("id") or uuid.uuid4().hex[:12]),
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if math.isfinite(m.x) and math.isfinite(m.y):
                self.markers.append(m)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "markers": [asdict(m) for m in self.markers]}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, name: str, kind: str, x: float, y: float, notes: str = "") -> Marker:
        if len(self.markers) >= self.MAX_MARKERS:
            raise ValueError(f"marker limit ({self.MAX_MARKERS}) reached")
        kind = kind if kind in MARKER_KINDS else "other"
        m = Marker((name.strip() or kind.capitalize())[:60], kind, float(x), float(y), notes[:300])
        self.markers.append(m)
        self.save()
        return m

    def remove(self, marker_id: str) -> bool:
        before = len(self.markers)
        self.markers = [m for m in self.markers if m.id != marker_id]
        changed = len(self.markers) != before
        if changed:
            self.save()
        return changed

    def clear(self) -> None:
        self.markers = []
        self.save()


# ----------------------------------------------------------------- settings
SIZE_MIN, SIZE_MAX = 65, 175


def clamp_size_percent(value: float) -> int:
    return int(min(max(round(value), SIZE_MIN), SIZE_MAX))


def _clamp_num(value, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi) if math.isfinite(v) else default


PEER_SOURCES = ("off", "lab", "game")


@dataclass(slots=True)
class Settings:
    size_percent: int = 100
    zoom: float = 1.0
    ring_radii_m: list[float] = field(default_factory=lambda: list(DEFAULT_RING_RADII_M))
    species: str = ""  # own species label is user-chosen: the capture does not expose it
    show_zones: bool = True
    show_food: bool = False
    show_rings: bool = True
    window_x: int | None = None
    window_y: int | None = None
    iface: str = ""  # last --iface used for live capture
    # other players (packet-sourced, see peers.py)
    peer_source: str = "off"  # "off" | "lab" | "game"; never "game" by default (owner-only opt-in)
    peer_radius_m: float = 1000.0
    peer_self_ids: list[str] = field(default_factory=list)  # extra ids that are "me" (e.g. a second client)
    show_vitals_detail: bool = False
    show_players: bool = True
    show_ai: bool = True  # inert for packet sources (they never report an AI kind)

    def normalise(self) -> "Settings":
        self.size_percent = clamp_size_percent(self.size_percent)
        self.zoom = min(max(float(self.zoom), 0.5), 16.0)
        radii = [r for r in self.ring_radii_m if isinstance(r, (int, float)) and r > 0]
        self.ring_radii_m = radii[:6] or list(DEFAULT_RING_RADII_M)
        self.peer_source = self.peer_source if self.peer_source in PEER_SOURCES else "off"
        self.peer_radius_m = _clamp_num(self.peer_radius_m, 50.0, 20_000.0, 1000.0)
        ids = self.peer_self_ids if isinstance(self.peer_self_ids, (list, tuple, set)) else []
        self.peer_self_ids = sorted({c for c in (canonical_id(i) for i in ids) if c}, key=str)[:32]
        self.show_vitals_detail = bool(self.show_vitals_detail)
        self.show_players, self.show_ai = bool(self.show_players), bool(self.show_ai)
        return self

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or data_dir() / "settings.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
            if "peer_radius_m" not in known and "feed_radius_m" in raw:  # legacy key from before peers existed (migration only)
                known["peer_radius_m"] = raw["feed_radius_m"]
            return cls(**known).normalise()
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or data_dir() / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self.normalise()), indent=1), encoding="utf-8")


def data_dir() -> Path:
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "isle-livemap"
    return Path.home() / ".isle-livemap"


# ----------------------------------------------------------------- smoothing


def shortest_arc(a: float, b: float) -> float:
    """Signed shortest rotation from angle a to b, in (-180, 180]."""
    d = (b - a + 540.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


class Interpolator:
    """Eases the displayed position/heading toward the latest telemetry so a
    ~20-60 Hz stream is drawn smoothly at 60 fps. Exponential smoothing with a
    time constant, so the result is independent of the frame rate."""

    def __init__(self, tau_s: float = 0.09, snap_distance_units: float = 200_000.0) -> None:
        self.tau = tau_s
        self.snap = snap_distance_units
        self.x = self.y = self.heading = 0.0
        self.target_x = self.target_y = self.target_heading = 0.0
        self.has_fix = False

    def set_target(self, x: float, y: float, heading: float | None) -> None:
        self.target_x, self.target_y = x, y
        if heading is not None:
            self.target_heading = heading % 360.0
        if not self.has_fix:
            self.x, self.y, self.heading = x, y, self.target_heading
            self.has_fix = True

    def step(self, dt: float) -> tuple[float, float, float]:
        if not self.has_fix:
            return self.x, self.y, self.heading
        if math.hypot(self.target_x - self.x, self.target_y - self.y) > self.snap:
            self.x, self.y = self.target_x, self.target_y  # respawn / teleport
        a = 1.0 - math.exp(-max(dt, 0.0) / self.tau) if self.tau > 0 else 1.0
        self.x += (self.target_x - self.x) * a
        self.y += (self.target_y - self.y) * a
        self.heading = (self.heading + shortest_arc(self.heading, self.target_heading) * a) % 360.0
        return self.x, self.y, self.heading


# ----------------------------------------------------------------- other entities
# World units are the same as the local telemetry: Unreal centimetres, same axes
# (north = -Y). Other players come from a local PeerSource (see peers.py).

PEER_STALE_S = 3.0  # dimmed after this long without an update
PEER_TTL_S = 5.0  # removed after this long (the only despawn rule; matches labsim NODE_TTL)
LOCAL_ID = "local"  # id of geo.local_entity(); packet ids never equal it


@dataclass(frozen=True, slots=True)
class Entity:
    id: str
    kind: str  # "player" | "ai"
    name: str
    species: str
    x: float
    y: float
    z: float | None = None
    growth_pct: float | None = None
    health_pct: float | None = None
    stamina_pct: float | None = None
    hunger_pct: float | None = None
    thirst_pct: float | None = None
    heading: float | None = None  # map heading, degrees clockwise from north (like the local arrow); None = unknown

    def __post_init__(self) -> None:
        h = self.heading
        object.__setattr__(self, "heading", normalize_degrees(float(h)) if h is not None and math.isfinite(h) else None)


@dataclass(frozen=True, slots=True)
class Nearby:
    entity: Entity
    x: float  # (possibly smoothed) display position, world units
    y: float
    distance_m: float
    bearing: float
    age_s: float | None = None  # seconds since the source last reported it (None = not tracked)

    @property
    def stale(self) -> bool:
        return self.age_s is not None and self.age_s >= PEER_STALE_S


# ---- marker styles: the one place colours / sizes / glyphs of map entities live
@dataclass(frozen=True, slots=True)
class EntityStyle:
    color: str
    outline: str
    radius: float  # dot radius / arrow half-length scale, px
    glyph: str  # "arrow" (self), "dot" (+ arrow when heading is known), "diamond"


ENTITY_STYLE = {
    "self": EntityStyle("#ff4040", "#000000", 10.0, "arrow"),  # local player: red, biggest, drawn last
    "player": EntityStyle("#4dd0ff", "#00141c", 4.0, "dot"),  # other players: cyan, smaller, outlined
    "ai": EntityStyle("#ff8a3d", "#1c0e00", 5.0, "diamond"),
}
STALE_ALPHA = 105  # 0-255 opacity of a stale (> PEER_STALE_S) peer
PEER_ARROW_SCALE = 0.6  # peer arrow size relative to the local arrow


def canonical_id(raw) -> str:
    """Normalise an id for comparison: stripped text; integer ids are wrapped to signed
    32 bit so the unsigned sniffer parse and the signed telemetry parse agree."""
    text = str(raw).strip()
    try:
        n = int(text, 10)
    except ValueError:
        return text
    if 2**31 <= n < 2**32:
        n -= 2**32
    return str(n)


def is_self(entity_id: str, me_id: str = LOCAL_ID, self_ids=()) -> bool:
    """True for the local player: the literal "local", ``me_id`` or any configured self id."""
    cid = canonical_id(entity_id)
    return cid in (LOCAL_ID, canonical_id(me_id)) or cid in {canonical_id(i) for i in self_ids}


def edge_anchor(cx: float, cy: float, tx: float, ty: float, radius: float) -> tuple[float, float] | None:
    """Point on the circle (centre cx,cy) in the direction of (tx,ty); None for a zero/invalid vector."""
    dx, dy = tx - cx, ty - cy
    n = math.hypot(dx, dy)
    if not math.isfinite(n) or n == 0.0:
        return None
    return cx + dx / n * radius, cy + dy / n * radius


def nearby_entities(items, px: float, py: float, radius_m: float | None = None,
                    show_players: bool = True, show_ai: bool = True) -> list[Nearby]:
    """``items``: iterable of (Entity, x, y). Distance and bearing from (px, py),
    filtered by kind toggles and radius (None = unlimited), nearest first."""
    out = []
    for e, x, y in items:
        if (e.kind == "player" and not show_players) or (e.kind == "ai" and not show_ai):
            continue
        d = distance_m(px, py, x, y)
        if radius_m is not None and d > radius_m:
            continue
        out.append(Nearby(e, x, y, d, bearing_deg(px, py, x, y)))
    out.sort(key=lambda n: (n.distance_m, n.entity.id))
    return out


def truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: max(limit - 1, 1)] + "…"


def _pct(v: float | None) -> str:
    return "?" if v is None else f"{v:.0f}%"


VITAL_FIELDS = (("Growth", "growth_pct"), ("Health", "health_pct"), ("Stamina", "stamina_pct"),
                ("Hunger", "hunger_pct"), ("Water", "thirst_pct"))


def vital_pct(cur: float | None, mx: float | None) -> float | None:
    """A vital as a 0-100 percentage (None when unknown); one decoding for every entity."""
    from .tracking import Vitals

    f = Vitals.fraction(cur, mx)
    return None if f is None else f * 100.0


def local_entity(pos, vitals, species: str = "") -> Entity:
    """The local player as an Entity (``pos``: MovementCandidate | None, ``vitals``:
    Vitals | None) so it shares the formatting path of other players: same cm units
    and axes, vitals as 0-100 percentages."""
    from .tracking import Vitals

    pct = vital_pct
    v = vitals or Vitals()
    return Entity(
        "local", "player", "You", species,
        pos.x if pos is not None else 0.0, pos.y if pos is not None else 0.0, pos.z if pos is not None else None,
        growth_pct=pct(v.growth, 1.0), health_pct=pct(v.health, v.max_health),
        stamina_pct=pct(v.stamina, v.max_stamina), hunger_pct=pct(v.hunger, v.max_hunger),
        thirst_pct=pct(v.thirst, v.max_thirst),
    )


def vital_lines(e: Entity) -> list[str]:
    """"Label NN%" for every vital known for the entity (local player and peers alike)."""
    return [f"{label} {v:.0f}%" for label, attr in VITAL_FIELDS if (v := getattr(e, attr)) is not None]


def height_delta_m(n: Nearby, my_z: float | None) -> float | None:
    """Entity height minus player height in metres (> 0: above you); None if either is unknown."""
    if n.entity.z is None or my_z is None:
        return None
    return (n.entity.z - my_z) / 100.0


def distance_3d_m(n: Nearby, my_z: float | None) -> float:
    """sqrt(horizontal² + dZ²); falls back to the horizontal distance without heights."""
    dz = height_delta_m(n, my_z)
    return n.distance_m if dz is None else math.hypot(n.distance_m, dz)


def visible_entities(items, px: float, py: float, radius_m: float | None = None, show_players: bool = True,
                     show_ai: bool = True, self_ids=(), me_id: str = LOCAL_ID, my_z: float | None = None,
                     use_3d: bool = False, ttl_s: float | None = PEER_TTL_S) -> list[Nearby]:
    """The one selection used by the minimap, the Nearby panel and nearby.py.

    ``items``: iterable of (Entity, x, y) or (Entity, x, y, age_s) from (px, py). Drops the
    local player (``is_self``: "local", ``me_id``, ``self_ids``), kinds switched off, entries
    older than ``ttl_s`` and anything beyond ``radius_m`` (None = unlimited). Horizontal mode
    keeps the radius inclusive (``nearby_entities`` contract); ``use_3d`` measures sqrt(h²+dZ²)
    from ``my_z`` and keeps it strict (``nearby_3d`` contract). Nearest first, ties by id."""
    ids = {canonical_id(i) for i in self_ids} | {LOCAL_ID, canonical_id(me_id)}
    out = []
    for item in items:
        e, x, y = item[0], item[1], item[2]
        age = item[3] if len(item) > 3 else None
        if canonical_id(e.id) in ids:
            continue
        if (e.kind == "player" and not show_players) or (e.kind == "ai" and not show_ai):
            continue
        if age is not None and ttl_s is not None and age >= ttl_s:
            continue
        n = Nearby(e, x, y, distance_m(px, py, x, y), bearing_deg(px, py, x, y), age)
        d = distance_3d_m(n, my_z) if use_3d else n.distance_m
        if radius_m is not None and (d >= radius_m if use_3d else d > radius_m):
            continue
        out.append((d, n))
    out.sort(key=lambda t: (t[0], t[1].entity.id))
    return [n for _, n in out]


def nearby_3d(me: Entity, entities, max_distance_m: float, self_ids=()) -> list[Nearby]:
    """Entities with 3D distance < ``max_distance_m`` from ``me`` (e.g. ``local_entity``),
    nearest first, skipping ``me`` itself and ``self_ids`` (see ``visible_entities``)."""
    return visible_entities([(e, e.x, e.y) for e in entities], me.x, me.y, max_distance_m,
                            self_ids=self_ids, me_id=me.id, my_z=me.z, use_3d=True)


def entity_label_lines(n: Nearby, name_max: int = 16, my_z: float | None = None) -> tuple[str, str]:
    e, dz = n.entity, height_delta_m(n, my_z)
    head = truncate(e.name or e.id, name_max) + (f" ({truncate(e.species, 14)})" if e.species else "")
    tail = f"HP {_pct(e.health_pct)} G {_pct(e.growth_pct)} {format_distance(n.distance_m)}"
    return head, tail if dz is None else f"{tail} {dz:+.0f}m"


def peer_label_lines(n: Nearby, my_z: float | None = None, detail: bool = False) -> list[str]:
    """Minimap label: ``entity_label_lines`` plus, with ``detail``, the known vitals (``vital_lines``)."""
    lines = list(entity_label_lines(n, my_z=my_z))
    if detail and (v := vital_lines(n.entity)):
        lines.append(truncate("  ".join(v), 44))
    return lines


def entity_row(n: Nearby, my_z: float | None = None) -> tuple[str, ...]:
    """Table cells: name, species, health, growth, stamina, hunger, water, distance, 3D distance, height delta."""
    e, dz = n.entity, height_delta_m(n, my_z)
    return (truncate(e.name or e.id, 24), e.species or "-", _pct(e.health_pct), _pct(e.growth_pct),
            _pct(e.stamina_pct), _pct(e.hunger_pct), _pct(e.thirst_pct),
            format_distance(n.distance_m), f"{distance_3d_m(n, my_z):.0f} m",
            "?" if dz is None else f"{dz:+.0f} m {'higher' if dz > 0 else 'lower' if dz < 0 else 'level'}")


def entity_summary(n: Nearby, my_z: float | None = None) -> str:
    e = n.entity
    sp = f" {e.species}" if e.species else ""
    extra = ""
    for tag, v in (("ST", e.stamina_pct), ("HU", e.hunger_pct), ("TH", e.thirst_pct)):
        if v is not None:
            extra += f" {tag} {v:.0f}%"
    if e.z is not None and my_z is not None:
        extra += f" dZ {(e.z - my_z) / 100.0:+.0f}m"
    return (f"[{e.kind}] {truncate(e.name or e.id, 24)}{sp}  HP {_pct(e.health_pct)}  G {_pct(e.growth_pct)}{extra}  "
            f"{format_distance(n.distance_m)} {compass_label(n.bearing)} ({n.bearing:.0f}°)")


Rect = tuple[float, float, float, float]  # x, y, w, h


def _overlaps(a: Rect, b: Rect) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def place_label(ax: float, ay: float, w: float, h: float, placed: list[Rect], bounds: Rect) -> Rect | None:
    """Pick a label rect next to anchor (ax, ay) (right, left, above, below) that
    stays inside ``bounds`` and overlaps no already placed label; None if none fits."""
    for r in ((ax + 8, ay - h / 2, w, h), (ax - 8 - w, ay - h / 2, w, h),
              (ax - w / 2, ay - 8 - h, w, h), (ax - w / 2, ay + 8, w, h)):
        inside = (r[0] >= bounds[0] and r[1] >= bounds[1]
                  and r[0] + r[2] <= bounds[0] + bounds[2] and r[1] + r[3] <= bounds[1] + bounds[3])
        if inside and not any(_overlaps(r, o) for o in placed):
            placed.append(r)
            return r
    return None


class EntitySmoother:
    """Per-entity exponential smoothing between source updates (like Interpolator).

    ``update(entities)`` (no ``now``) keeps the old contract: ids missing from an update vanish
    at once. ``update(entities, now)`` tracks ``last_seen`` and keeps a missing id until
    ``ttl_s`` has passed, so one missed packet does not flicker. Entries may be ``Entity`` or
    anything with ``.entity`` / ``.last_seen`` (``peers.PeerEntity``). Heading is smoothed along
    the shortest arc when the source supplies one."""

    def __init__(self, tau_s: float = 0.15, snap_distance_units: float = 200_000.0, ttl_s: float = PEER_TTL_S) -> None:
        self.tau, self.snap, self.ttl = tau_s, snap_distance_units, ttl_s
        self.entities: dict[str, Entity] = {}
        self.pos: dict[str, list[float]] = {}
        self.heading: dict[str, float] = {}
        self.last_seen: dict[str, float] = {}

    def update(self, entities, now: float | None = None) -> None:
        new: dict[str, Entity] = {}
        seen: dict[str, float] = {}
        for item in entities:
            e = getattr(item, "entity", item)
            new[e.id] = e
            ls = getattr(item, "last_seen", None)
            seen[e.id] = ls if ls is not None else (now if now is not None else 0.0)
        for eid, e in new.items():
            cur = self.pos.get(eid)
            if cur is None or math.hypot(e.x - cur[0], e.y - cur[1]) > self.snap:
                self.pos[eid] = [e.x, e.y]
                if e.heading is not None:
                    self.heading[eid] = e.heading
            if e.heading is None:
                self.heading.pop(eid, None)
            elif eid not in self.heading:
                self.heading[eid] = e.heading
        self.last_seen.update(seen)
        if now is None:
            gone = set(self.pos) - set(new)
        else:  # keep missing ids until their TTL runs out
            gone = {i for i in self.pos if i not in new and now - self.last_seen.get(i, now) >= self.ttl}
        for eid in gone:
            self.pos.pop(eid, None)
            self.heading.pop(eid, None)
            self.last_seen.pop(eid, None)
            self.entities.pop(eid, None)
        self.entities = {**{i: e for i, e in self.entities.items() if i in self.pos}, **new}

    def step(self, dt: float) -> None:
        a = 1.0 - math.exp(-max(dt, 0.0) / self.tau) if self.tau > 0 else 1.0
        for eid, e in self.entities.items():
            p = self.pos[eid]
            p[0] += (e.x - p[0]) * a
            p[1] += (e.y - p[1]) * a
            h = self.heading.get(eid)
            if h is not None and e.heading is not None:
                self.heading[eid] = (h + shortest_arc(h, e.heading) * a) % 360.0

    def _shown(self, eid: str, e: Entity) -> Entity:
        h = self.heading.get(eid)
        return e if h is None or e.heading is None else replace(e, heading=h)

    def items(self):
        return [(e, *self.pos[eid]) for eid, e in self.entities.items()]

    def items_aged(self, now: float):
        """[(Entity with smoothed heading, x, y, age_s)] for ids younger than the TTL."""
        out = []
        for eid, e in self.entities.items():
            age = max(now - self.last_seen.get(eid, now), 0.0)
            if age < self.ttl:
                out.append((self._shown(eid, e), *self.pos[eid], age))
        return out


# ----------------------------------------------------------------- bundled data
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):  # PyInstaller bundle
    ASSET_DIR = Path(sys._MEIPASS) / "isle_livemap" / "assets"
else:
    ASSET_DIR = Path(__file__).parent / "assets"


def load_zones(path: Path | None = None) -> dict:
    """Bundled migration / patrol polygons and the 13 food-source layers."""
    path = path or ASSET_DIR / "zones.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("migration", "patrol", "food"):
        data.setdefault(key, [])
    return data
