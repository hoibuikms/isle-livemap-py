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
from functools import lru_cache
from typing import Callable

from .confidence import (
    SPECIES_INFERRED, VITALS_STALE, age_tag, is_placeholder_name, species_tier, vitals_tier,
)
from .decode import normalize_degrees
from .species import diet_of, size_class

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
    # other players (packet-sourced, see peers.py)
    peer_radius_m: float = 1000.0
    peer_self_ids: list[str] = field(default_factory=list)  # extra ids that are "me" (e.g. a second client); settings-file only
    show_vitals_detail: bool = False
    show_vitals_bars: bool = True  # a thin health / stamina bar under the dot of a creature whose vitals are known
    show_players: bool = True
    show_ai: bool = True  # server-controlled animals (fish) from the game source; settings-file only, no tray item
    # automatic session recording (live capture only; see sessionlog.py). Settings-file only.
    record_sessions: bool = True
    record_dir: str = ""  # "" = <data dir>/logs/sessions
    record_keep_sessions: int = 10  # newest sessions kept (pcap parts + log count as one session)
    record_total_cap_mb: float = 1024.0  # total size cap of the sessions directory

    def normalise(self) -> "Settings":
        self.size_percent = clamp_size_percent(self.size_percent)
        self.zoom = min(max(float(self.zoom), 0.5), 16.0)
        radii = [r for r in self.ring_radii_m if isinstance(r, (int, float)) and r > 0]
        self.ring_radii_m = radii[:6] or list(DEFAULT_RING_RADII_M)
        self.peer_radius_m = _clamp_num(self.peer_radius_m, 50.0, 20_000.0, 1000.0)
        ids = self.peer_self_ids if isinstance(self.peer_self_ids, (list, tuple, set)) else []
        self.peer_self_ids = sorted({c for c in (canonical_id(i) for i in ids) if c}, key=str)[:32]
        self.show_vitals_detail = bool(self.show_vitals_detail)
        self.show_vitals_bars = bool(self.show_vitals_bars)
        self.show_players, self.show_ai = bool(self.show_players), bool(self.show_ai)
        self.record_sessions = bool(self.record_sessions)
        self.record_dir = self.record_dir.strip() if isinstance(self.record_dir, str) else ""
        self.record_keep_sessions = int(_clamp_num(self.record_keep_sessions, 1, 1000, 10))
        self.record_total_cap_mb = _clamp_num(self.record_total_cap_mb, 10.0, 1_000_000.0, 1024.0)
        return self

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or data_dir() / "settings.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
            # obsolete keys such as "peer_source" are dropped by the filter above
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
        self._sample_at: float | None = None
        self._vel: tuple[float, float, float] | None = None  # (vx, vy in cm/s, extrapolation horizon s)

    def set_target(self, x: float, y: float, heading: float | None, at: float | None = None) -> None:
        """New telemetry sample. With ``at`` (the sample's monotonic time) the velocity between the last two
        samples is kept, and ``step(dt, now)`` advances the target along it (same rules as ``EntitySmoother``)."""
        prev_x, prev_y, prev_at = self.target_x, self.target_y, self._sample_at
        self.target_x, self.target_y = x, y
        if heading is not None:
            self.target_heading = heading % 360.0
        if at is None or not self.has_fix or prev_at is None:
            self._vel = None
        elif math.hypot(x - prev_x, y - prev_y) > self.snap:
            self._vel = None
        elif (x, y) == (prev_x, prev_y):
            if at - prev_at >= DR_MIN_DT_S:
                self._vel = None  # re-sent at the same spot: stopped
        else:
            dt = at - prev_at
            if dt >= DR_MIN_DT_S:
                vx, vy = (x - prev_x) / dt, (y - prev_y) / dt
                ok = dt <= DR_MAX_INTERVAL_S and math.hypot(vx, vy) <= DR_MAX_SPEED
                self._vel = (vx, vy, min(dt, DR_MAX_EXTRAP_S)) if ok else None
        self._sample_at = at
        if not self.has_fix:
            self.x, self.y, self.heading = x, y, self.target_heading
            self.has_fix = True

    def _aim(self, now: float | None) -> tuple[float, float]:
        """The newest sample advanced along its velocity, plus ``tau`` of lead so the filter's steady lag cancels."""
        v, at = self._vel, self._sample_at
        if now is None or v is None or at is None:
            return self.target_x, self.target_y
        ahead = min(max(now - at, 0.0) + self.tau, v[2])
        return self.target_x + v[0] * ahead, self.target_y + v[1] * ahead

    def step(self, dt: float, now: float | None = None) -> tuple[float, float, float]:
        if not self.has_fix:
            return self.x, self.y, self.heading
        if math.hypot(self.target_x - self.x, self.target_y - self.y) > self.snap:
            self.x, self.y = self.target_x, self.target_y  # respawn / teleport
        tx, ty = self._aim(now)
        a = 1.0 - math.exp(-max(dt, 0.0) / self.tau) if self.tau > 0 else 1.0
        self.x += (tx - self.x) * a
        self.y += (ty - self.y) * a
        self.heading = (self.heading + shortest_arc(self.heading, self.target_heading) * a) % 360.0
        return self.x, self.y, self.heading


# ----------------------------------------------------------------- other entities
# World units are the same as the local telemetry: Unreal centimetres, same axes
# (north = -Y). Other players come from a local PeerSource (see peers.py).

PEER_STALE_S = 3.0  # dimmed after this long without an update
PEER_TTL_S = 5.0  # removed after this long (the only despawn rule; matches labsim NODE_TTL)
# Real-game peers: Iris replicates DELTAS, so an actor that stands still sends nothing; silence is not absence.
# Destruction records remove an actor at once; these are only the fallback (judgement call, see docs/PEERS.md).
GAME_PEER_STALE_S = 8.0
GAME_PEER_TTL_S = 30.0
LOCAL_ID = "local"  # id of geo.local_entity(); packet ids never equal it


@dataclass(frozen=True, slots=True)
class Entity:
    id: str
    kind: str  # "player" | "dino" | "unknown" (drawn as players) | "ai" (see PLAYER_KINDS)
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
    inferred: bool = False  # the species comes from the saved class cache (the join tables were missed), not the server
    vitals_t: float | None = None  # source clock time of the newest health / stamina reading (None: no timestamp)

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
    stale_s: float = PEER_STALE_S  # dimmed from this age on (the source decides, see GAME_PEER_STALE_S)
    vitals_age_s: float | None = None  # seconds since its health / stamina were last read (None = no timestamp)

    @property
    def stale(self) -> bool:
        return self.age_s is not None and self.age_s >= self.stale_s


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
    "ai": EntityStyle("#8fa6b3", "#0b1318", 5.0, "diamond"),  # server animals (fish): muted, they are background
}
# Entity kinds from the game source (species.py): "player" (a creature that references a PlayerState), "dino" (a
# dinosaur class, controller unknown), "unknown" (no class information) are all "other players" for the toggles and
# the style; "ai" is a server controlled animal. World objects ("other") never become entities.
PLAYER_KINDS = ("player", "dino", "unknown")
ENTITY_STYLE.update({k: ENTITY_STYLE["player"] for k in PLAYER_KINDS if k not in ENTITY_STYLE})
# Dinosaurs (no PlayerState) are coloured by diet when the species is known; players stay cyan with a white ring, so
# "a person" reads at a glance; a dinosaur whose species is not known is a neutral grey-blue.
DIET_STYLE = {
    "carnivore": EntityStyle("#ff7eb6", "#2a0a1a", 4.0, "dot"),
    "herbivore": EntityStyle("#7bd88f", "#08200f", 4.0, "dot"),
    "omnivore": EntityStyle("#ffd166", "#2a1c00", 4.0, "dot"),
}
NEUTRAL_DINO_STYLE = EntityStyle("#b8c4d6", "#10141a", 4.0, "dot")
LEGEND_ENTRIES = (
    ("You", ENTITY_STYLE["self"].color, "red arrow"),
    ("Player", ENTITY_STYLE["player"].color, "cyan dot with a white ring"),
    ("Carnivore", DIET_STYLE["carnivore"].color, "pink dot"),
    ("Herbivore", DIET_STYLE["herbivore"].color, "green dot"),
    ("Omnivore", DIET_STYLE["omnivore"].color, "yellow dot"),
    ("Dinosaur, species not known", NEUTRAL_DINO_STYLE.color, "grey dot"),
    ("Animal / fish", ENTITY_STYLE["ai"].color, "small grey diamond"),
)


@lru_cache(maxsize=256)
def _peer_style(kind: str, species: str) -> EntityStyle:
    if kind == "player" or kind == "ai":
        return ENTITY_STYLE[kind]
    return DIET_STYLE.get(diet_of(species)) or NEUTRAL_DINO_STYLE


def peer_style(e: "Entity") -> EntityStyle:
    """Colours of a peer: players cyan, other dinosaurs by diet (neutral without a species), server animals muted."""
    return _peer_style(e.kind, e.species)


STALE_ALPHA = 105  # 0-255 opacity of a stale (> PEER_STALE_S) peer
PEER_ARROW_SCALE = 0.6  # peer arrow size relative to the local arrow

# Dot radius (px) by species size class (species.size_class). Every radius is clamped to [DOT_MIN_PX, DOT_MAX_PX] so a
# dot stays readable and never hides its neighbours. Server animals (fish) stay small whatever they are.
SIZE_RADIUS_PX = {"tiny": 3.0, "small": 3.8, "medium": 4.6, "large": 5.6, "huge": 6.8}
AI_RADIUS_PX = 3.5
DOT_MIN_PX, DOT_MAX_PX = 2.5, 8.0
JUVENILE_SCALE = 0.6  # a growth fraction (when known) scales the radius from this up to 1.0
_BASE_ARROW_RADIUS_PX = 4.0  # the radius the PEER_ARROW_SCALE arrow was designed for


@lru_cache(maxsize=512)
def _radius(kind: str, species: str, growth_q: int | None) -> float:
    if kind == "ai":
        return AI_RADIUS_PX
    r = SIZE_RADIUS_PX[size_class(species)]
    if growth_q is not None:
        r *= JUVENILE_SCALE + (1.0 - JUVENILE_SCALE) * growth_q / 20
    return min(DOT_MAX_PX, max(DOT_MIN_PX, r))


def marker_radius(e: Entity) -> float:
    """Dot radius in px of a peer: from its species size class (fish: fixed small), scaled by growth when known."""
    g = e.growth_pct
    return _radius(e.kind, e.species, None if g is None or not math.isfinite(g) else round(min(100.0, max(0.0, g)) / 5))


def marker_extent(e: Entity) -> float:
    """How far from its centre a peer glyph reaches, px (an arrow is longer than its dot radius)."""
    r = marker_radius(e)
    if e.heading is not None:
        return 10 * PEER_ARROW_SCALE * r / _BASE_ARROW_RADIUS_PX
    return r + 1 if e.kind == "ai" else r


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
        if (e.kind in PLAYER_KINDS and not show_players) or (e.kind == "ai" and not show_ai):
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
                     use_3d: bool = False, ttl_s: float | None = PEER_TTL_S,
                     stale_s: float = PEER_STALE_S) -> list[Nearby]:
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
        vitals_age = item[4] if len(item) > 4 else None
        if canonical_id(e.id) in ids:
            continue
        if (e.kind in PLAYER_KINDS and not show_players) or (e.kind == "ai" and not show_ai):
            continue
        if age is not None and ttl_s is not None and age >= ttl_s:
            continue
        n = Nearby(e, x, y, distance_m(px, py, x, y), bearing_deg(px, py, x, y), age, stale_s, vitals_age)
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


def entity_label_lines(n: Nearby, name_max: int = 16, my_z: float | None = None,
                       vitals: str | None = None) -> tuple[str, str]:
    """(head, tail) of the minimap label, worded by how sure each field is (``confidence``): a confirmed species is
    plain, one remembered from an earlier session carries "~", an unknown one is "?". A player name is only printed
    when the creature referenced its PlayerState. The tail holds health and stamina ("HP 86% ST 33%", "HP ?" when no
    reading was ever seen, a coarse age such as "20s+" when the reading is old), then distance, compass bearing and height
    difference. Growth, hunger and thirst of other creatures are not replicated to us: never printed, not even as "?".
    ``vitals``: the vitals tier to print (the map view passes the one it settled); None works it out from the age."""
    e, dz = n.entity, height_delta_m(n, my_z)
    mark = "~" if species_tier(e.species, e.inferred) == SPECIES_INFERRED else ""
    name = e.name or e.id
    if not is_placeholder_name(name):
        head = truncate(name, name_max) + (f" ({truncate(e.species, 14)}{mark})" if e.species else " (?)")
    elif e.species:  # no player name known: the species is the best name there is
        head = truncate(e.species, name_max) + mark
    else:
        word = "Animal" if e.kind == "ai" else "Dino" if e.kind in ("dino", "player") else "Creature"
        head = f"{word} ? #{e.id}"
    tier = vitals or vitals_tier(e.health_pct, e.stamina_pct, n.vitals_age_s)
    parts = []
    if e.health_pct is not None:
        parts.append(f"HP {e.health_pct:.0f}%")
    elif e.kind in PLAYER_KINDS:
        parts.append("HP ?")
    if e.stamina_pct is not None:
        parts.append(f"ST {e.stamina_pct:.0f}%")
    if tier == VITALS_STALE and (e.health_pct is not None or e.stamina_pct is not None):
        parts.append(f"[{age_tag(n.vitals_age_s)}]")
    if e.growth_pct is not None:
        parts.append(f"G {e.growth_pct:.0f}%")
    parts += [format_distance(n.distance_m), compass_label(n.bearing)]
    if dz is not None:
        parts.append(f"{dz:+.0f}m")
    return head, " ".join(parts)


LABEL_RANK = {"player": 0, "dino": 1, "unknown": 2, "ai": 3}  # who gets a label first when the minimap is crowded
LABELS_MINIMAP, LABELS_FULL = 8, 24
AI_LABELS_MINIMAP, AI_LABELS_FULL = 3, 8  # fish and other server animals must never use up the labels of players
CROWDED_ON_SCREEN = 20  # above this many peers of a group that group gets only a few labels
CROWDED_LABELS = 4


def label_rank(n: Nearby) -> int:
    return LABEL_RANK.get(n.entity.kind, len(LABEL_RANK))


def label_sort_key(n: Nearby) -> tuple[int, float, str]:
    """Order in which peers compete for a label: players, then dinosaurs, unidentified, server animals; nearest first."""
    return label_rank(n), n.distance_m, n.entity.id


def label_limits(creatures_on_screen: int, ai_on_screen: int, full_map: bool) -> tuple[int, int]:
    """(max labels in total, max labels for server animals) for the peers currently drawn on screen."""
    total = LABELS_FULL if full_map else LABELS_MINIMAP
    ai = AI_LABELS_FULL if full_map else AI_LABELS_MINIMAP
    if creatures_on_screen > CROWDED_ON_SCREEN:
        total = min(total, CROWDED_LABELS)
    if ai_on_screen > CROWDED_ON_SCREEN:
        ai = 0
    return total, ai


def peer_label_lines(n: Nearby, my_z: float | None = None, detail: bool = False, vitals: str | None = None) -> list[str]:
    """Minimap label: ``entity_label_lines`` plus, with ``detail``, the known vitals (``vital_lines``)."""
    lines = list(entity_label_lines(n, my_z=my_z, vitals=vitals))
    if detail and (v := vital_lines(n.entity)):
        lines.append(truncate("  ".join(v), 44))
    return lines


def entity_row(n: Nearby, my_z: float | None = None) -> tuple[str, ...]:
    """Table cells: name, species, health, growth, stamina, hunger, water, distance, 3D distance, height delta."""
    e, dz = n.entity, height_delta_m(n, my_z)
    return (truncate(e.name or e.id, 24), (e.species + "~" if e.inferred and e.species else e.species) or "-", _pct(e.health_pct), _pct(e.growth_pct),
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
    known = "".join(f"  {tag} {v:.0f}%" for tag, v in (("HP", e.health_pct), ("G", e.growth_pct)) if v is not None)
    return (f"[{e.kind}] {truncate(e.name or e.id, 24)}{sp}{known}{extra}  "
            f"{format_distance(n.distance_m)} {compass_label(n.bearing)} ({n.bearing:.0f}°)")


Rect = tuple[float, float, float, float]  # x, y, w, h


def _overlaps(a: Rect, b: Rect, tol: float = 0.0) -> bool:
    return a[0] < b[0] + b[2] - tol and b[0] < a[0] + a[2] - tol and a[1] < b[1] + b[3] - tol and b[1] < a[1] + a[3] - tol


def label_slots(ax: float, ay: float, w: float, h: float, bounds: Rect) -> list[Rect]:
    """Candidate rects for a label at anchor (ax, ay), best first: centred under the marker, then above (centred),
    right, left, then hung to one side of the marker near the edge of a round view, then one row further out."""
    bx, _by, bw, _bh = bounds
    cx = min(max(ax - w / 2, bx), bx + bw - w)  # centred under/above, clamped into the view
    return [(cx, ay + 8, w, h), (cx, ay - 8 - h, w, h),
            (ax + 8, ay - h / 2, w, h), (ax - 8 - w, ay - h / 2, w, h),
            (ax - 8, ay + 8, w, h), (ax + 8 - w, ay + 8, w, h), (ax - 8, ay - 8 - h, w, h), (ax + 8 - w, ay - 8 - h, w, h),
            (cx, ay + 9 + h, w, h), (cx, ay - 9 - 2 * h, w, h)]


SLOT_SLACK_PX = 3.0  # the slot a label already has may be this much worse than a new one would be allowed to be


def place_label(ax: float, ay: float, w: float, h: float, placed: list[Rect], bounds: Rect,
                accept: Callable[[Rect], bool] | None = None, prefer: int | None = None,
                slot: list[int] | None = None, prefer_accept: Callable[[Rect], bool] | None = None) -> Rect | None:
    """Pick a label rect for anchor (ax, ay): the first candidate of ``label_slots`` that lies inside ``bounds``,
    overlaps no already placed label and satisfies ``accept`` (e.g. lie inside the round minimap). ``prefer`` is the
    index chosen for this marker last time: it is tried first, and with ``SLOT_SLACK_PX`` of tolerance on the bounds,
    on overlaps and (through ``prefer_accept``) on ``accept``, so a label keeps its side while it still more or less
    fits. Without that a dot moving along the edge of the round view made its label jump between two sides every few
    frames. ``slot`` (a list) receives the chosen index. Returns None if none fits."""
    bx, by, bw, bh = bounds
    if w > bw:
        return None
    slots = label_slots(ax, ay, w, h, bounds)
    order = range(len(slots)) if prefer is None or not 0 <= prefer < len(slots) else \
        [prefer, *[i for i in range(len(slots)) if i != prefer]]
    for i in order:
        r = slots[i]
        tol = SLOT_SLACK_PX if i == prefer else 0.0
        inside = (r[0] >= bx - tol and r[1] >= by - tol and r[0] + r[2] <= bx + bw + tol and r[1] + r[3] <= by + bh + tol)
        test = (prefer_accept or accept) if tol else accept
        if inside and (test is None or test(r)) and not any(_overlaps(r, o, tol) for o in placed):
            placed.append(r)
            if slot is not None:
                slot[:] = [i]
            return r
    return None


# Dead reckoning for peers. The server only sends a position now and then (Iris replicates changes), so a dot that
# waits for the next sample trails reality by up to one send interval. Between samples the target is advanced along
# the velocity of the last two distinct samples, never further than the interval it was measured over (the next
# sample is due then) and never beyond DR_MAX_EXTRAP_S, so a stop is overshot by less than one interval of motion.
MOVING_MIN_SPEED_CM_S = 30.0  # below this a creature counts as standing (a drifting fix, not a step)
MOVING_HOLD_S = 0.7  # the arrow outlives the dead-reckoning horizon a little, so it does not blink between 3 Hz fixes
DR_MIN_DT_S = 0.05  # samples closer than this give a noisy velocity; the old velocity is kept
DR_MAX_INTERVAL_S = 3.0  # samples further apart are not one continuous movement: no velocity
DR_MAX_EXTRAP_S = 1.0
DR_MAX_SPEED = 6_000.0  # cm/s (60 m/s); faster is a glitch or a respawn, not movement
DR_CORR_TAU_S = 0.2  # a new sample is blended in over this long, so a late or off-course sample bends the path instead of jumping
DR_FF_GRACE_S = 0.3  # the dot keeps its velocity this long past the horizon: a sample that is a little late must not stop it


class EntitySmoother:
    """Per-entity exponential smoothing between source updates (like Interpolator), plus dead reckoning.

    ``update(entities)`` (no ``now``) keeps the old contract: ids missing from an update vanish
    at once. ``update(entities, now)`` tracks ``last_seen`` and keeps a missing id until
    ``ttl_s`` has passed, so one missed packet does not flicker. Entries may be ``Entity`` or
    anything with ``.entity`` / ``.last_seen`` (``peers.PeerEntity``). Heading is smoothed along
    the shortest arc when the source supplies one. ``step(dt, now)`` with ``now`` also advances each
    target along its measured velocity (see ``DR_*``); ``step(dt)`` alone follows the last sample."""

    def __init__(self, tau_s: float = 0.15, snap_distance_units: float = 200_000.0, ttl_s: float = PEER_TTL_S) -> None:
        self.tau, self.snap, self.ttl = tau_s, snap_distance_units, ttl_s
        # None: an id the source stopped listing stays until ``ttl_s`` after its last report (a lab feed that drops one
        # packet). A number: the source is authoritative (the game feed removes a creature when the server destroys it),
        # so a missing id leaves after this many seconds.
        self.missing_grace_s: float | None = None
        self.heading_only_when_moving = False  # the source's heading is the direction of travel: no arrow for a creature at rest
        self._missing: dict[str, float] = {}  # id -> time it was first absent from an update
        self._born: dict[str, int] = {}  # id -> serial of the source's entry: a new one is a different creature (reused id)
        self.entities: dict[str, Entity] = {}
        self.pos: dict[str, list[float]] = {}
        self.heading: dict[str, float] = {}
        self.last_seen: dict[str, float] = {}
        self._sample: dict[str, tuple[float, float, float]] = {}  # newest distinct (x, y, time)
        self._vel: dict[str, tuple[float, float, float]] = {}  # (vx, vy in cm/s, extrapolation horizon s)

    def _note_sample(self, eid: str, e: Entity, t: float, refreshed: bool) -> None:
        s = self._sample.get(eid)
        if s is None or math.hypot(e.x - s[0], e.y - s[1]) > self.snap:
            self._sample[eid] = (e.x, e.y, t)  # first sight or teleport: nothing to measure against
            self._vel.pop(eid, None)
        elif (e.x, e.y) == (s[0], s[1]):
            if refreshed and t - s[2] >= DR_MIN_DT_S:
                self._vel.pop(eid, None)  # re-sent at the same spot: it has stopped
        else:
            dt = t - s[2]
            if dt >= DR_MIN_DT_S:
                vx, vy = (e.x - s[0]) / dt, (e.y - s[1]) / dt
                if dt <= DR_MAX_INTERVAL_S and math.hypot(vx, vy) <= DR_MAX_SPEED:
                    self._vel[eid] = (vx, vy, min(dt, DR_MAX_EXTRAP_S))
                else:
                    self._vel.pop(eid, None)
            self._sample[eid] = (e.x, e.y, t)

    def update(self, entities, now: float | None = None) -> None:
        new: dict[str, Entity] = {}
        seen: dict[str, float] = {}
        born_of: dict[str, int] = {}
        for item in entities:
            e = getattr(item, "entity", item)
            new[e.id] = e
            born_of[e.id] = getattr(item, "born", 0)
            ls = getattr(item, "last_seen", None)
            seen[e.id] = ls if ls is not None else (now if now is not None else 0.0)
        for eid, e in new.items():
            born = born_of.get(eid, 0)
            if born and self._born.get(eid, born) != born:  # the id was reused: not the creature the dot was following
                self.pos.pop(eid, None)
                self._sample.pop(eid, None)
                self._vel.pop(eid, None)
                self.heading.pop(eid, None)
            self._born[eid] = born
            cur = self.pos.get(eid)
            if cur is None or math.hypot(e.x - cur[0], e.y - cur[1]) > self.snap:
                self.pos[eid] = [e.x, e.y]
                if e.heading is not None:
                    self.heading[eid] = e.heading
            self._note_sample(eid, e, seen[eid], seen[eid] > self.last_seen.get(eid, -math.inf))
            if e.heading is None:
                self.heading.pop(eid, None)
            elif eid not in self.heading:
                self.heading[eid] = e.heading
        self.last_seen.update(seen)
        for eid in new:
            self._missing.pop(eid, None)
        if now is None:
            gone = set(self.pos) - set(new)
        else:
            for eid in self.pos:
                if eid not in new:
                    self._missing.setdefault(eid, now)
            gone = self._expired(now)
        self._forget(gone)
        self.entities = {**{i: e for i, e in self.entities.items() if i in self.pos}, **new}

    def _expired(self, now: float) -> set[str]:
        grace = self.missing_grace_s
        if grace is None:
            return {i for i in self._missing if now - self.last_seen.get(i, now) >= self.ttl}
        return {i for i, since in self._missing.items() if now - since >= grace}

    def _forget(self, gone) -> None:
        for eid in gone:
            self.pos.pop(eid, None)
            self.heading.pop(eid, None)
            self.last_seen.pop(eid, None)
            self.entities.pop(eid, None)
            self._sample.pop(eid, None)
            self._vel.pop(eid, None)
            self._missing.pop(eid, None)
            self._born.pop(eid, None)

    def prune(self, now: float) -> None:
        """Drop ids that the source no longer lists once their grace is over. ``update`` only runs when the source
        changed, but time passes every frame: without this a destroyed creature would stay until the next change."""
        if self._missing:
            gone = self._expired(now)
            if gone:
                self._forget(gone)

    def target(self, eid: str, now: float | None = None, lead: float = 0.0) -> tuple[float, float]:
        """Where the entity is expected to be at ``now``: its last sample, advanced along its velocity
        (``lead`` seconds further, still within the horizon)."""
        e = self.entities[eid]
        v = self._vel.get(eid)
        s = self._sample.get(eid)
        if now is None or v is None or s is None:
            return e.x, e.y
        ahead = min(max(now - s[2], 0.0) + lead, v[2])
        return e.x + v[0] * ahead, e.y + v[1] * ahead

    def step(self, dt: float, now: float | None = None) -> None:
        dt = max(dt, 0.0)
        a = 1.0 - math.exp(-dt / self.tau) if self.tau > 0 else 1.0
        c = 1.0 - math.exp(-dt / max(self.tau, DR_CORR_TAU_S))
        for eid, e in self.entities.items():
            p = self.pos[eid]
            v = self._vel.get(eid)
            s = self._sample.get(eid)
            if now is None or v is None or s is None:
                p[0] += (e.x - p[0]) * a
                p[1] += (e.y - p[1]) * a
            else:
                # Feed-forward: the dot moves at the measured velocity all the time and the error to the newest sample
                # (advanced along that velocity, within its horizon) is blended in. It never waits at the end of a
                # horizon for a late sample and never jumps when the sample arrives: no stop-and-go, no lag at a steady pace.
                age = max(now - s[2], 0.0)
                f = min(age, v[2])
                go = dt if age <= v[2] + DR_FF_GRACE_S else 0.0
                p[0] += v[0] * go + (e.x + v[0] * f - p[0]) * c
                p[1] += v[1] * go + (e.y + v[1] * f - p[1]) * c
            h = self.heading.get(eid)
            if h is not None and e.heading is not None:
                self.heading[eid] = (h + shortest_arc(h, e.heading) * a) % 360.0

    def is_moving(self, eid: str, now: float) -> bool:
        """The creature was measured moving and the dot is still being carried along that movement. A creature that
        stands still is not re-sent by the server, so this is about time since its last position, not about a flag."""
        v, s = self._vel.get(eid), self._sample.get(eid)
        if v is None or s is None or math.hypot(v[0], v[1]) < MOVING_MIN_SPEED_CM_S:
            return False
        return max(now - s[2], 0.0) <= v[2] + DR_FF_GRACE_S + MOVING_HOLD_S

    def _shown(self, eid: str, e: Entity, now: float | None = None) -> Entity:
        h = self.heading.get(eid)
        if e.heading is not None and self.heading_only_when_moving and now is not None and not self.is_moving(eid, now):
            return replace(e, heading=None)  # a heading measured from movement says nothing once the creature has stopped
        return e if h is None or e.heading is None else replace(e, heading=h)

    def items(self):
        return [(e, *self.pos[eid]) for eid, e in self.entities.items()]

    def items_aged(self, now: float):
        """[(Entity with smoothed heading, x, y, age_s, vitals_age_s)] for ids younger than the TTL."""
        out = []
        for eid, e in self.entities.items():
            age = max(now - self.last_seen.get(eid, now), 0.0)
            if age < self.ttl:
                vitals_age = None if e.vitals_t is None else max(now - e.vitals_t, 0.0)
                out.append((self._shown(eid, e, now), *self.pos[eid], age, vitals_age))
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
