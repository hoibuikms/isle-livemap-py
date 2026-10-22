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
from dataclasses import asdict, dataclass, field
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
    # server feed (the ONLY source of other entities; needs a server you control)
    feed_url: str = ""
    feed_token: str = ""  # stored in plain text; prefer ISLE_FEED_TOKEN
    feed_radius_m: float = 1000.0
    feed_interval_s: float = 0.5
    show_players: bool = True
    show_ai: bool = True

    def normalise(self) -> "Settings":
        self.size_percent = clamp_size_percent(self.size_percent)
        self.zoom = min(max(float(self.zoom), 0.5), 16.0)
        radii = [r for r in self.ring_radii_m if isinstance(r, (int, float)) and r > 0]
        self.ring_radii_m = radii[:6] or list(DEFAULT_RING_RADII_M)
        self.feed_url = str(self.feed_url or "").strip()
        self.feed_token = str(self.feed_token or "").strip()
        self.feed_radius_m = _clamp_num(self.feed_radius_m, 50.0, 20_000.0, 1000.0)
        self.feed_interval_s = _clamp_num(self.feed_interval_s, 0.25, 30.0, 0.5)
        self.show_players, self.show_ai = bool(self.show_players), bool(self.show_ai)
        return self

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or data_dir() / "settings.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
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


# ----------------------------------------------------------------- server-feed entities
# World units are the same as the local telemetry: Unreal centimetres, same axes
# (north = -Y). Entities come ONLY from the user's own server feed (see feed.py).


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


@dataclass(frozen=True, slots=True)
class Nearby:
    entity: Entity
    x: float  # (possibly smoothed) display position, world units
    y: float
    distance_m: float
    bearing: float


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


def entity_label_lines(n: Nearby, name_max: int = 16) -> tuple[str, str]:
    e = n.entity
    head = truncate(e.name or e.id, name_max) + (f" ({truncate(e.species, 14)})" if e.species else "")
    return head, f"HP {_pct(e.health_pct)} G {_pct(e.growth_pct)} {format_distance(n.distance_m)}"


def entity_summary(n: Nearby) -> str:
    e = n.entity
    sp = f" {e.species}" if e.species else ""
    return (f"[{e.kind}] {truncate(e.name or e.id, 24)}{sp}  HP {_pct(e.health_pct)}  G {_pct(e.growth_pct)}  "
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
    """Per-entity exponential smoothing between ~2 Hz feed updates (like Interpolator)."""

    def __init__(self, tau_s: float = 0.15, snap_distance_units: float = 200_000.0) -> None:
        self.tau, self.snap = tau_s, snap_distance_units
        self.entities: dict[str, Entity] = {}
        self.pos: dict[str, list[float]] = {}

    def update(self, entities) -> None:
        new = {e.id: e for e in entities}
        for eid, e in new.items():
            cur = self.pos.get(eid)
            if cur is None or math.hypot(e.x - cur[0], e.y - cur[1]) > self.snap:
                self.pos[eid] = [e.x, e.y]
        for eid in set(self.pos) - set(new):
            del self.pos[eid]
        self.entities = new

    def step(self, dt: float) -> None:
        a = 1.0 - math.exp(-max(dt, 0.0) / self.tau) if self.tau > 0 else 1.0
        for eid, e in self.entities.items():
            p = self.pos[eid]
            p[0] += (e.x - p[0]) * a
            p[1] += (e.y - p[1]) * a

    def items(self):
        return [(e, *self.pos[eid]) for eid, e in self.entities.items()]


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
