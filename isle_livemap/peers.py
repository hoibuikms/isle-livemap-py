"""Other players for the minimap: a local, in-process ``PeerSource`` (no network client, no feed).

Backends (all produce ``geo.Entity`` so everything downstream is source-neutral):
  * ``LabPeerSource``  - dev/testing only (``--lab-peers``). Reads the assumed 18-byte lab record from loopback
                         127.0.0.1 through ``labsim.payload_sniffer.PayloadSniffer`` (socket).
  * ``SyntheticPeers`` - bare ``--replay`` demo (dev only): moving peers anchored to the replayed local player.
  * ``GamePeerSource`` - real-game traffic, the default source. Passive capture only: taps the same
                         inbound datagrams the local-player vitals decoder reads (class, species, name, position, health, stamina).

Units: ``geo`` is Unreal centimetres, X/Y horizontal, Z height, north = -Y. The lab record is
Y-up metres; it is converted exactly once, in ``from_record`` (through ``nearby._cm``).

Canonical lab record = ``labsim.payload_sniffer.parse_payload`` (``<IfffH`` + UTF-8 name).
``labsim.telemetry`` / ``tools/payload_probe.py`` use the signed twin (``<i3fh``). The bytes are
identical; ``from_record`` reinterprets the id and the 16-bit value as signed so both parsers give
the same entity (id >= 2**31 -> negative, status >= 0x8000 -> negative, then clamped by
``vital_pct``). The 16-bit value is treated as health 0..100 (TRD Q3 proposal).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Protocol

from .geo import (
    GAME_PEER_STALE_S, GAME_PEER_TTL_S, PEER_STALE_S, PEER_TTL_S, Entity, bearing_deg, canonical_id, compass_label,
    distance_m, hp_text, is_self, st_text, vital_pct, vitals_readings,
)
from .confidence import is_placeholder_name, species_tier, vitals_tier
from .nearby import _cm

__all__ = [
    "LAB_PLAYER_ID", "LAB_PORT", "MAX_ABS_COORD_M", "PEER_STALE_S", "PEER_TTL_S", "GamePeerSource", "LabPeerSource",
    "PeerEntity", "PeerSnapshot", "PeerSource", "PeerStore", "SyntheticPeers", "create_peer_source", "from_record",
    "status_is_warning",
]

log = logging.getLogger("isle_livemap.peers")

LAB_PLAYER_ID = 0  # == labsim.telemetry.PLAYER_ID: the lab's "me", never shown as a peer
LAB_PORT = 7777  # == labsim.telemetry.PORT
MAX_ABS_COORD_M = 100_000.0  # |coordinate| beyond 100 km means wrong layout/endianness: record is dropped


# ----------------------------------------------------------------- data model
@dataclass(frozen=True, slots=True)
class PeerEntity:
    entity: Entity
    last_seen: float  # monotonic seconds of the source clock
    born: int = 0  # serial of the store entry: a removed and re-added id (a reused game handle) gets a new one
    sample_at: float | None = None  # when the newest position was observed (packet time, like the own marker's); None: last_seen

    @property
    def id(self) -> str:
        return self.entity.id


@dataclass(frozen=True, slots=True)
class PeerSnapshot:
    """Immutable view of the peers. ``version`` changes when the set or any value changes."""

    entities: tuple[PeerEntity, ...] = ()
    version: int = 0
    status: str = ""
    enabled: bool = False
    unplaced: int = 0  # creatures the server replicates to us but that have no position yet (they may be close)
    late: bool = False  # the app started after the join: the creation records of the creatures already there were never seen


class PeerSource(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def snapshot(self) -> PeerSnapshot: ...


_WARN_WORDS = ("error", "unavailable", "failed", "not implemented", "waiting", "no ", "rejoin")


def status_is_warning(status: str) -> bool:
    s = status.lower()
    return any(w in s for w in _WARN_WORDS)


# ----------------------------------------------------------------- adapter
def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value >= (1 << (bits - 1)) else value


def from_record(rec) -> Entity | None:
    """``NodeRecord`` (id, x, y, z, status, name; lab Y-up metres) -> ``geo.Entity``.

    None when a coordinate is not finite or implausible. Unknown fields stay None; the empty
    name becomes ``Player #<id>``. Heading is never supplied by the record."""
    try:
        x, y, z = _cm({"x": rec.x, "y": rec.y, "z": rec.z})
        if max(abs(rec.x), abs(rec.y), abs(rec.z)) > MAX_ABS_COORD_M:
            return None
        eid = str(_signed(int(rec.id), 32))
        status = _signed(int(rec.status) & 0xFFFF, 16)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    name = " ".join(str(rec.name or "").replace("\x00", " ").split())
    return Entity(eid, "player", name or f"Player #{eid}", "", x, y, z, health_pct=vital_pct(float(status), 100.0))


# ----------------------------------------------------------------- store
class PeerStore:
    """Thread-safe latest-entity-per-id store with TTL and self-id dropping.

    Writers (backend threads) call ``update``; the overlay calls ``snapshot``. The snapshot object
    is cached, so an unchanged store returns the same object; ``version`` only moves when the
    set of ids or any entity value changes (a repeated identical packet just refreshes last_seen)."""

    def __init__(self, ttl: float = PEER_TTL_S, clock: Callable[[], float] = time.monotonic,
                 self_ids: Iterable[str] = (), ttl_for: Callable[[Entity], float] | None = None) -> None:
        self.ttl, self.clock = ttl, clock
        self.ttl_for = ttl_for  # per-entity silence limit (the game feed keeps creatures far longer than fish)
        self._lock = threading.Lock()
        self._seen: dict[str, PeerEntity] = {}
        self._config: set[str] = {canonical_id(i) for i in self_ids}  # user-configured (Settings.peer_self_ids)
        self._owned: set[str] = set()  # ids a backend owns (lab id 0, the learned game handle): survive set_self_ids
        self._self: set[str] = set(self._config)
        self._version = 0
        self._serial = 0
        self._cached: tuple[PeerEntity, ...] | None = ()
        self._snap: PeerSnapshot | None = None
        self.dropped_self = 0

    def update(self, entity: Entity, at: float | None = None) -> bool:
        """Insert/refresh one entity. False if it is a self id (dropped). ``at``: the packet time of the position it
        carries; an update without one (a state batch with no position) keeps the previous sample time."""
        now = self.clock()
        with self._lock:
            if is_self(entity.id, self_ids=self._self):
                self.dropped_self += 1
                return False
            old = self._seen.get(entity.id)
            if old is None:
                self._serial += 1
            sample_at = at if at is not None else (old.sample_at if old is not None else None)
            self._seen[entity.id] = PeerEntity(entity, now, old.born if old is not None else self._serial, sample_at)
            self._cached = self._snap = None
            if old is None or old.entity != entity:
                self._version += 1
            return True

    def set_self_ids(self, ids: Iterable[str]) -> None:
        """Replace the USER-configured self ids and purge any peer that turns out to be me. Ids a backend
        registered itself (``add_self_id`` / ``set_owned_self_ids``) are kept: editing the Peers settings
        must not re-show the lab/demo "me" or the learned game handle."""
        with self._lock:
            self._config = {canonical_id(i) for i in ids}
            self._self = self._config | self._owned
            self._purge_self()

    def add_self_id(self, raw_id) -> None:
        """E.g. the lab's id 0, or the real-game own handle once it locks: drop it now and from here on."""
        with self._lock:
            self._owned.add(canonical_id(raw_id))
            self._self = self._config | self._owned
            self._purge_self()

    def set_owned_self_ids(self, ids: Iterable[str]) -> None:
        """Replace the backend-owned self ids (e.g. a new own handle after respawn, or none after a reset)."""
        with self._lock:
            self._owned = {canonical_id(i) for i in ids}
            self._self = self._config | self._owned
            self._purge_self()

    def remove(self, raw_id) -> bool:
        """Drop one id now (explicit despawn). False if it was not tracked."""
        with self._lock:
            gone = self._seen.pop(canonical_id(raw_id), None)
            if gone is None:
                return False
            self._version += 1
            self._cached = self._snap = None
            return True

    def get(self, raw_id) -> Entity | None:
        """The entity stored under ``raw_id`` (None if absent or expired)."""
        with self._lock:
            self._expire(self.clock())
            p = self._seen.get(canonical_id(raw_id))
            return p.entity if p else None

    def clear(self) -> None:
        with self._lock:
            if self._seen:
                self._seen.clear()
                self._version += 1
                self._cached = self._snap = None

    def _purge_self(self) -> None:
        gone = [i for i in self._seen if is_self(i, self_ids=self._self)]
        for i in gone:
            del self._seen[i]
        if gone:
            self._version += 1
            self._cached = self._snap = None

    def _expire(self, now: float) -> None:
        ttl_for = self.ttl_for
        gone = [i for i, p in self._seen.items() if now - p.last_seen >= (ttl_for(p.entity) if ttl_for else self.ttl)]
        for i in gone:
            del self._seen[i]
        if gone:
            self._version += 1
            self._cached = self._snap = None

    def entities(self) -> tuple[PeerEntity, ...]:
        with self._lock:
            self._expire(self.clock())
            if self._cached is None:
                self._cached = tuple(sorted(self._seen.values(), key=lambda p: p.entity.id))
            return self._cached

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def snapshot(self, status: str = "", enabled: bool = True, unplaced: int = 0, late: bool = False) -> PeerSnapshot:
        with self._lock:
            self._expire(self.clock())
            if self._cached is None:
                self._cached = tuple(sorted(self._seen.values(), key=lambda p: p.entity.id))
            sn = self._snap
            if sn is None or sn.status != status or sn.enabled != enabled or sn.unplaced != unplaced or sn.late != late:
                sn = self._snap = PeerSnapshot(self._cached, self._version, status, enabled, unplaced, late)
            return sn


# ----------------------------------------------------------------- backend A: lab loopback
class LabPeerSource:
    """Lab record over ``PayloadSniffer`` (socket backend by default; loopback only, read-only)."""

    def __init__(self, port: int = LAB_PORT, backend: str = "socket", store: PeerStore | None = None,
                 self_ids: Iterable[str] = ()) -> None:
        self.port, self.backend = port, backend
        self.store = store or PeerStore(self_ids=self_ids)
        self.store.add_self_id(LAB_PLAYER_ID)
        self.me: Entity | None = None  # the lab's own node (id 0): kept apart, never a peer
        self.rejected = 0  # records with non-finite / implausible coordinates
        self._sniffer = None
        self._error: str | None = None

    def _on_record(self, rec) -> None:
        ent = from_record(rec)
        if ent is None:
            self.rejected += 1
        elif int(rec.id) == LAB_PLAYER_ID:
            self.me = ent
        else:
            self.store.update(ent)

    def start(self) -> None:
        try:
            from labsim.payload_sniffer import PayloadSniffer  # lab tooling, not part of the packaged app

            self._sniffer = PayloadSniffer(self._on_record, self.port, self.backend).start()
            self._error = None
        except Exception as exc:  # ImportError, bind failure, no scapy/privileges: never crash the app
            self._sniffer = None
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("lab peers unavailable: %s", self._error)

    def stop(self) -> None:
        if self._sniffer is not None:
            self._sniffer.stop()
            self._sniffer = None

    def status(self) -> str:
        if self._error:
            return f"lab: unavailable ({self._error})"
        if self._sniffer is None:
            return "lab: not started"
        sn = self._sniffer
        err = f", error {sn.error}" if sn.error else ""
        return (f"lab: listening 127.0.0.1:{self.port} ({len(self.store.entities())} peers, "
                f"{sn.captured} pkts, {sn.malformed} malformed, {self.rejected} rejected{err})")

    def snapshot(self) -> PeerSnapshot:
        return self.store.snapshot(self.status(), True)


# ----------------------------------------------------------------- backend S: synthetic (--replay)
class SyntheticPeers:
    """Moving peers around ``anchor()`` -> (x, y) | None, in geo centimetres.

    Includes an entry with the self id (``LAB_PLAYER_ID``) that the store must drop, one nameless
    peer (-> ``Player #id``), one with only a position, one that goes silent periodically (dims at
    3 s, removed at 5 s) and one that carries a heading (the arrow glyph). Every peer here is demo
    data; real sources supply no heading."""

    #            id,  name,           r_m, w rad/s, phase, hp,  heading?
    SPEC = (
        ("101", "Rex_Hunter", 180, 0.020, 0.0, 92, False),
        ("102", "Sprinter", 420, -0.030, 1.5, 55, False),
        ("103", "VeryLongPlayerNameThatWillBeTruncated", 700, 0.012, 3.0, 100, False),
        ("104", "FarAway", 3000, 0.010, 4.0, 70, False),
        ("105", "", 300, 0.025, 2.0, None, False),
        ("106", "Compass", 520, -0.015, 5.0, 80, True),
        ("107", "Ghost", 240, 0.035, 0.7, 60, False),
        (str(LAB_PLAYER_ID), "me", 5, 0.0, 0.0, 100, False),  # self id: dropped by the store
    )

    def __init__(self, anchor: Callable[[], tuple[float, float] | None], rate_hz: float = 2.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._anchor, self._period, self._clock = anchor, 1.0 / rate_hz, clock
        self._t0 = clock()
        self._step = -1
        self.store = PeerStore(clock=clock)
        self.store.add_self_id(LAB_PLAYER_ID)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def _emit(self, t: float, ax: float, ay: float) -> None:
        for eid, name, r_m, w, ph, hp, with_heading in self.SPEC:
            if eid == "107" and (t % 24.0) > 14.0:  # silent for 10 s of every 24 s
                continue
            a = ph + w * t
            heading = None
            if with_heading:  # tangent of the circle, as a clockwise-from-north map heading
                heading = math.degrees(math.atan2(-w * math.sin(a), -w * math.cos(a))) if w else None
            self.store.update(Entity(
                eid, "player", name, "", ax + math.cos(a) * r_m * 100.0, ay + math.sin(a) * r_m * 100.0, 30_000.0,
                health_pct=vital_pct(hp, 100.0) if hp is not None else None, heading=heading))

    def snapshot(self) -> PeerSnapshot:
        anchor = self._anchor()
        if anchor is None:
            return self.store.snapshot("synthetic peers: waiting for position", True)
        t = self._clock() - self._t0
        step = int(t / self._period)
        if step != self._step:  # one update per period
            self._step = step
            self._emit(step * self._period, *anchor)
        return self.store.snapshot(f"synthetic peers ({len(self.store.entities())})", True)


# ----------------------------------------------------------------- backend G: real game
class GamePeerSource:
    """Other replicated creatures from the game's own inbound traffic, through the SAME capture path as the
    local player. This is the default and only user-facing source (owner's personal tool; decision in TRD rev 6).
    Passive: it only reads datagrams that ``TelemetryPipeline`` already delivers (no memory access,
    no injection, nothing is sent).

    How it works (evidence in docs/trd/other-players-on-minimap.md, "Identity" and "Positions"):
      * ``decode.parse_iris_packet`` yields one batch per replicated object keyed by ``net_ref_handle``, plus the
        export sections and destruction records.
      * ``decode.IdentityTracker`` keeps the export tables of the connection and the creation record of every actor:
        handle -> archetype -> class path (``/Game/.../BP_X.Default__BP_X_C``), and player names from PlayerState
        records. ``species.describe`` turns a class path into ``kind`` (dino / player / ai / other) and a species
        name. Objects of kind "other" (plants, fruit, gore, items, level actors) are never drawn.
      * ``decode.ActorLocationTracker`` decodes positions: the two verified fixed layouts, plus layouts learned
        from creation anchors and continuity (pawns). ``decode.PawnVitalsTracker`` reads health and stamina (full blocks, then the pair that follows them).
      * A peer is keyed by its handle. The own handle is excluded as soon as the heartbeat lock gives it; a
        provisional peer with that handle is purged retroactively (``PeerStore.set_owned_self_ids``).
      * A destruction record removes the peer at once and forgets its identity (the server reuses handles). Iris
        replicates changes only, so a creature that stands still goes quiet: the fallback is generous (dim after
        ``STALE_S`` = 45 s, remove after ``TTL_S`` = 30 s without any report), not the 3 s / 5 s of the lab feed.
        Any state update of a tracked creature counts as a report, also one without a position: live captures show
        pawns that keep sending state for minutes after their position stopped changing.
      * Every decoded position is written to the store at once (no buffering); the overlay smooths and
        dead-reckons between samples (``geo.EntitySmoother``).

    Decoded: handle, x/y/z, class, species, kind, player name (when the creature references its PlayerState),
    health and stamina percentages (when the 9-slot attribute block is sent), heading (derived from movement, see
    ``_heading``). NOT decoded: growth, hunger, thirst."""

    STALE_S = GAME_PEER_STALE_S  # read by the overlay (dim)
    heading_from_movement = True  # read by the overlay: the heading is the direction of travel, so no arrow while at rest
    # Silence limit before a peer is dropped when no destruction record came. Creatures rest for minutes without
    # a single report (captured gaps of 100-300 s), so they stay until the server destroys them; the limit only
    # guards against a missed destruction record. Server animals (fish) are numerous and uninteresting: shorter.
    TTL_S = 600.0  # players and dinosaurs: the server's destruction record removes them; this only guards against a missed one
    TTL_AI_S = GAME_PEER_TTL_S
    stale_s, ttl_s = STALE_S, TTL_S
    # A creature the server destroyed leaves the map this long afterwards (not 30 s: a dot of something that is gone
    # is a stuck dot). Read by the overlay's smoother.
    GONE_GRACE_S = 1.5
    gone_grace_s = GONE_GRACE_S
    # The server destroys an actor that leaves its relevancy range and sends a new creation record when it comes back
    # (captured: the same handle returns 6-8 s later at about the same spot). Players and dinosaurs are therefore kept
    # as dimmed ghosts at their last position for this long, instead of vanishing and reappearing.
    # Measured (three recordings): a player-sized creature stays relevant for a median of 31-43 s before the server destroys
    # it, and of 131 handles that came back, 18 % did so within 12 s and 34 % within 30 s. A ghost is dimmed more with
    # every second and says on its label how long ago the server destroyed it ("gone 23s").
    GHOST_GRACE_S = 45.0
    ghost_grace_s = GHOST_GRACE_S
    ghost_kinds = frozenset(("player", "dino", "unknown"))

    # Actors whose creation record was never captured have no class. Their state batches still have a class-typical
    # size: every server-controlled animal of the recorded captures (33 resolved, 73 never created) repeats a 155 or
    # 158 bit state, and no dinosaur or world object does. The first batch of that size says "ai" (waiting for more
    # drew every new fish as an unknown creature for 2-3 s); the verdict is kept until the actor has clearly stopped
    # looking like an animal, so it cannot flap between two kinds.
    AI_LAYOUT_BITS = (155, 158)
    AI_LAYOUT_MIN_BATCHES = 1
    AI_LAYOUT_SHARE = 0.9
    AI_LAYOUT_KEEP_SHARE = 0.6  # a latched verdict is revoked only below this share ...
    AI_LAYOUT_REVOKE_BATCHES = 8  # ... after at least this many batches

    # Heading is not in the replicated data we can decode, so it is the direction of travel: the bearing from an anchor
    # position to the first later position at least this far away (a metre filters the position quantisation noise),
    # and not more than HEADING_MAX_STEP_CM (a larger jump is a respawn or a glitch, not walking).
    HEADING_MIN_MOVE_CM = 100.0
    HEADING_MAX_STEP_CM = 20_000.0

    LATE_JOIN_PACKETS = 100  # a fresh join sends the export tables within the first packets; this many without = missed

    # A creature whose creation record was missed and that never moved sends state batches but no position: the map
    # cannot place it. Its batches still look like a pawn's (mostly 60-400 bits, never the 155/158 bit animal state):
    # that profile counts it as "hidden" so the overlay can say so instead of showing nothing.
    CREATURE_BAND = (60, 400)  # recorded pawns: >= 89 % of their state batches; game state 59 %, sky actor 24 %
    HIDDEN_KNOWN_BATCHES = 10
    HIDDEN_CREATION_BATCHES = 3  # a creature whose creation record carried a maxHealth is certain: a few batches are enough
    CREATURE_BAND_SHARE = 0.8
    CREATURE_FISH_SHARE = 0.1
    HIDDEN_RECENT_S = 240.0  # a hidden creature must have sent state this recently (datagram time): a resting one is quiet for minutes
    PROFILE_MAX = 20_000

    # A dead creature stays in the world as a carcass: a pawn actor that sends a ~1 Hz state batch of 163 / 172 / 179 bits (a timer,
    # no position, no vitals) and sits exactly where a TICorpseSmell actor was created. Recorded (l): 7 of 7 bodies
    # created within 0.5 m of a smell actor except one 26 m away; 9 handles with the batch-size signature, none of the
    # 130 handles that had a decoded position had it. They were drawn as player-owned dinosaurs at their spawn point,
    # which showed up as "players" 650-850 m away that never moved. A carcass is not drawn.
    CORPSE_BITS = (163, 172, 179)
    CORPSE_MIN_BATCHES = 8
    CORPSE_SHARE = 0.7
    CORPSE_RUN_ALIVE = 12
    CORPSE_SMELL_CM = 50.0
    SMELL_MAX = 8192

    # A creature whose only position is its creation record's spawn point (drawn dimmed, "~") that keeps sending state batches
    # is active, not resting: the spawn point is where it entered the relevancy range, not where it is. Such a creature is not
    # drawn at the spawn point; it is counted as "position unknown" and listed in the Nearby table (distance "?").
    ACTIVE_WINDOW_S = 10.0
    ACTIVE_MIN_BATCHES = 8
    ACTIVE_MIN_SPAN_S = 10.0

    HEADLINE_HOLD_S = 12.0  # a new headline replaces the shown one only after this long (datagram time): no flapping

    STATUS_NO_PIPELINE = "game: unavailable (no capture pipeline attached)"

    def __init__(self, self_ids: Iterable[str] = (), pipeline=None, clock: Callable[[], float] = time.monotonic,
                 class_cache=None) -> None:
        from .decode import ActorLocationTracker, IdentityTracker, PawnVitalsTracker

        self._config_self = tuple(canonical_id(i) for i in self_ids)
        self.store = PeerStore(ttl=self.TTL_S, clock=clock, self_ids=self._config_self,
                               ttl_for=lambda e: self.TTL_AI_S if e.kind == "ai" else self.TTL_S)
        self.pipeline = pipeline
        self.own_handle: int | None = None
        self.class_cache = class_cache  # classcache.ClassCache | None: classes learned in earlier sessions
        self.identity = IdentityTracker(class_cache)
        self.locator = ActorLocationTracker()
        self.pawn_vitals = PawnVitalsTracker()
        self._clock = clock
        self._vitals: dict[int, object] = {}  # handle -> decode.PawnVitals (health / stamina percentages, see decode)
        self._vitals_t: dict[int, float] = {}  # handle -> source clock time of its newest health / stamina reading
        self._max_t: dict[int, float] = {}  # handle -> source clock time of its health maximum (creation record or full block)
        self._corrected: set[int] = set()  # handles whose label was already corrected once (logged once)
        self._events: deque[str] = deque(maxlen=64)  # label corrections for the session log (see drain_events)
        self._conflicts_seen = 0  # class cache entries dropped so far that were already reported
        self._layouts: dict[int, list] = {}  # unclassified handle -> [state batches of the animal size, all state batches, latched]
        self._anchors: dict[int, tuple[float, float]] = {}  # handle -> position the travel direction is measured from
        self._facing: dict[int, float] = {}  # handle -> map heading of the last rotation byte decoded with its position
        self._yaw_votes: dict[int, deque] = {}  # handle -> last comparisons of that facing with the direction of travel (True: disagree)
        self._yaw_distrust: set[int] = set()  # handles whose rotation byte keeps contradicting their movement: the travel direction is used
        self._profile: dict[int, list] = {}  # unplaced handle -> [state batches, in the pawn size band, animal sized, last t, first t]
        self._creatures: set[int] = set()  # handles whose creation record carried a maxHealth: a creature, whatever its class
        self._shape: dict[int, list] = {}  # handle -> [state batches, in the pawn size band, animal sized]: kept after it is placed
        self._past_own: set[int] = set()  # earlier own pawns (respawn): the old body is not "another creature"
        self._last_t = 0.0  # datagram time of the newest inbound packet
        # handle -> (kind, species, name, inferred, x, y, t) of a player / dinosaur the server destroyed. The same handle
        # often keeps sending state afterwards without a new creation record (relevancy flapping): if it shows up near
        # where it was, it is that creature, not an unknown one.
        self._zombies: dict[int, tuple] = {}
        self._resumed: set[int] = set()  # destroyed handles that went on sending state and were taken back (see ``_resume``)
        self._hidden_cache: tuple[float, list] = (-1e9, [])
        self._hidden_all: tuple | None = None  # (the rows of unplaced_creatures, len(_stale), hidden_players)
        self._banner_kind = ""  # what the minimap banner shows: "" | "late" | "unknown" (see _track_banner)
        self._banner_logged: tuple[str, float] = ("", -1e9)  # (kind last written to the session log, datagram time)
        self._headline: tuple[float, str] = (-1e9, "")  # (datagram time it was chosen, text)
        self._smells: dict[tuple[int, int], list[tuple[float, float]]] = {}  # 1 m grid cell -> TICorpseSmell positions (cm)
        self._corpses: set[int] = set()  # handles recognised as carcasses: never drawn
        self._timer: dict[int, list] = {}  # handle -> [last 8 batches (1 = carcass-sized), run of carcass-sized batches, ever alive]
        self._active: dict[int, list] = {}  # spawn-only handle -> [first state batch t, recent state batch times]
        self._stale: set[int] = set()  # creatures taken off the map because they were active but only had a spawn point
        self.filtered_corpses = 0  # state batches skipped because the actor is a carcass
        self.stale_spawns = 0  # creatures taken off the map because they were active but only had a spawn point
        self.packets = 0  # inbound datagrams seen
        self.decode_errors = 0  # datagrams whose decoding raised (the rest of the stream is still decoded)
        self._error_t = -1e9  # datagram time of the newest decode error
        self.locations = 0  # actor locations decoded (all sources)
        self.destroyed = 0  # destruction records that removed a tracked peer
        self.creation_records = 0  # batches whose prefix says "creation"
        self.export_batches = 0  # batches followed by an export section (object paths / net-token strings)
        self.destruction_records = 0  # destruction records seen in the envelope (tracked or not)
        self.filtered_world_objects = 0  # location/vitals batches skipped because the class is a world object
        self.vitals_blocks = 0  # health/stamina blocks decoded
        self._attached = False
        self.error = ""

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.pipeline is None:
            log.warning("game peers: no capture pipeline is attached; showing no peers")
            return
        try:
            self.pipeline.add_observer(self.on_inbound, self.on_reset)
        except Exception as err:  # noqa: BLE001 - never take the app down
            log.warning("game peers: could not attach to the capture pipeline: %s", err)
            self.error = str(err)
            return
        self._attached = True
        log.info("game peers: decoding other creatures from inbound replication (passive)")

    def stop(self) -> None:
        if self._attached and self.pipeline is not None:
            self.pipeline.remove_observer(self.on_inbound, self.on_reset)
        self._attached = False
        if self.class_cache is not None:
            self.class_cache.save(force=True)

    # -- pipeline callbacks (decode worker thread; must not raise) ---------
    def on_reset(self) -> None:
        """New server / game restart: handles are meaningless now."""
        self.own_handle = None
        self.store.set_owned_self_ids(())
        self.store.clear()
        if self.class_cache is not None:
            self.class_cache.save(force=True)
        self.identity.reset()
        self.locator.reset()
        self.pawn_vitals.reset()
        self._vitals.clear()
        self._vitals_t.clear()
        self._max_t.clear()
        self._corrected.clear()
        self._layouts.clear()
        self._anchors.clear()
        self._facing.clear()
        self._yaw_votes.clear()
        self._yaw_distrust.clear()
        self._profile.clear()
        self._creatures.clear()
        self._shape.clear()
        self._past_own.clear()
        self._zombies.clear()  # the handles of the old connection mean nothing now
        self._resumed.clear()
        self._smells.clear()
        self._corpses.clear()
        self._timer.clear()
        self._active.clear()
        self._stale.clear()
        self._hidden_cache = (-1e9, [])
        self._hidden_all = None
        self._headline = (-1e9, "")
        self._track_banner(force="")  # a new connection: the banner of the old one is gone

    def _sync_owned(self) -> None:
        """The own pawn and its earlier bodies are not other creatures: none of them is ever put on the map."""
        ids = [str(h) for h in self._past_own]
        if self.own_handle is not None:
            ids.append(str(self.own_handle))
        self.store.set_owned_self_ids(ids)

    YAW_DISAGREE_DEG = 120.0  # a rotation byte this far from the direction of travel is a disagreement
    YAW_VOTES = 8  # comparisons kept per creature
    YAW_DISTRUST_AT = 6  # this many disagreements among the last YAW_VOTES: the rotation byte is not trusted for that creature
    YAW_TRUST_BELOW = 2  # and trusted again at this many or fewer

    def _heading(self, handle: int, x: float, y: float, previous: float | None) -> float | None:
        """Where the arrow points, degrees clockwise from north: the way the creature faces (the rotation byte that comes
        with its position, held while it is silent), else the direction of travel once it has moved
        ``HEADING_MIN_MOVE_CM`` from its anchor, else ``previous``. A creature whose rotation byte persistently points
        the opposite way to how it moves (``YAW_DISTRUST_AT`` of the last ``YAW_VOTES`` comparisons more than
        ``YAW_DISAGREE_DEG`` apart) is treated as not carrying a usable rotation and falls back to the direction of travel."""
        travel = self._travel_bearing(handle, x, y)
        facing = self._facing.get(handle)
        if facing is not None and travel is not None:
            votes = self._yaw_votes.setdefault(handle, deque(maxlen=self.YAW_VOTES))
            votes.append(abs((facing - travel + 180.0) % 360.0 - 180.0) > self.YAW_DISAGREE_DEG)
            bad = sum(votes)
            if len(votes) >= self.YAW_VOTES and bad >= self.YAW_DISTRUST_AT:
                self._yaw_distrust.add(handle)
            elif bad <= self.YAW_TRUST_BELOW:
                self._yaw_distrust.discard(handle)
        if facing is not None and handle not in self._yaw_distrust:
            return facing
        return travel if travel is not None else previous

    def _travel_bearing(self, handle: int, x: float, y: float) -> float | None:
        """Direction of travel from the anchor once the creature has moved ``HEADING_MIN_MOVE_CM``; None otherwise."""
        anchor = self._anchors.get(handle)
        if anchor is None:
            self._anchors[handle] = (x, y)
            return None
        moved = math.hypot(x - anchor[0], y - anchor[1])
        if moved < self.HEADING_MIN_MOVE_CM:
            return None
        self._anchors[handle] = (x, y)
        return bearing_deg(*anchor, x, y) if moved <= self.HEADING_MAX_STEP_CM else None

    def _identity(self, handle: int) -> tuple[str, str, str]:
        """(kind, species, name) of an actor from what its creation record and the export tables say."""
        from . import species as sp

        info = self.identity.actors.get(handle)
        if info is None:
            hint = self.identity.species_hint(handle)  # no creation record: only the dinosaur assets of its own batches
            return (sp.KIND_DINO, sp.display_name(hint), "") if hint else (sp.KIND_UNKNOWN, "", "")
        path = self.identity.class_path(handle)
        if self.identity.is_object_record(handle):
            return sp.KIND_OTHER, "", ""
        kind, species = sp.describe(path, self.identity.species_hint(handle), info.player_state is not None)
        if kind == sp.KIND_DINO and not species and info.asset_species:
            species = sp.display_name(info.asset_species[0])
        return kind, species, self.identity.name_of(handle) or ""

    def _animal_layout(self, handle: int, bits: int) -> bool:
        """Count one state batch of an actor without class information; True while it looks like a server animal."""
        seen = self._layouts.setdefault(handle, [0, 0, False])
        seen[0] += bits in self.AI_LAYOUT_BITS
        seen[1] += 1
        share = seen[0] / seen[1]
        if seen[2]:
            if seen[1] >= self.AI_LAYOUT_REVOKE_BATCHES and share < self.AI_LAYOUT_KEEP_SHARE:
                seen[2] = False
        elif seen[1] >= self.AI_LAYOUT_MIN_BATCHES and share >= self.AI_LAYOUT_SHARE:
            seen[2] = True
        return seen[2]

    MIN_DYNAMIC_HANDLE = 1000  # lower handles are the game's static singletons (game state, world actors), never creatures
    SHAPE_MIN_BATCHES = 12
    SHAPE_MAX = 20_000

    def _note_shape(self, handle: int, nbits: int) -> None:
        row = self._shape.get(handle)
        if row is None:
            if len(self._shape) >= self.SHAPE_MAX:
                self._shape.clear()
            row = self._shape[handle] = [0, 0, 0]
        row[0] += 1
        row[1] += self.CREATURE_BAND[0] <= nbits <= self.CREATURE_BAND[1]
        row[2] += nbits in self.AI_LAYOUT_BITS

    def _creature_like(self, handle: int) -> bool:
        """An actor without a class that behaves like a creature: its creation record carried a maxHealth (plants, fruit and
        items have none), or its state batches have the size profile of a pawn (never the animal size)."""
        if handle < self.MIN_DYNAMIC_HANDLE:
            return False
        if handle in self._creatures:
            return True
        row = self._shape.get(handle)
        return row is not None and row[0] >= self.SHAPE_MIN_BATCHES and row[1] >= row[0] * self.CREATURE_BAND_SHARE \
            and row[2] < row[0] * self.CREATURE_FISH_SHARE

    # -- carcasses and spawn-only creatures ----------------------------------
    def _register_smell(self, location) -> None:
        """A TICorpseSmell actor sits exactly where a creature died (they are all created in the join burst, and a body that
        enters the relevancy range later is created on one)."""
        x, y = location[0], location[1]
        if sum(len(v) for v in self._smells.values()) >= self.SMELL_MAX:
            self._smells.clear()
        self._smells.setdefault((int(x // 100), int(y // 100)), []).append((x, y))

    def _near_smell(self, location) -> bool:
        x, y = location[0], location[1]
        cx, cy = int(x // 100), int(y // 100)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for sx, sy in self._smells.get((cx + dx, cy + dy), ()):
                    if math.hypot(sx - x, sy - y) <= self.CORPSE_SMELL_CM:
                        return True
        return False

    def _hide(self, handle: int) -> None:
        """Take a creature off the map on purpose (a carcass, or a spawn point it has left): not a server destruction."""
        self.store.remove(str(handle))

    def _mark_corpse(self, handle: int) -> None:
        if handle in self._corpses:
            return
        self._corpses.add(handle)
        self._hide(handle)
        self._profile.pop(handle, None)
        self._active.pop(handle, None)
        self._hidden_cache = (-1e9, [])
        self._hidden_all = None

    def _carcass_signature(self, handle: int, nbits: int) -> bool:
        """Count one state batch; True once the actor behaves like a carcass: for a creature that never had a decoded position
        or health block, 6 of its last 8 batches are the timer-sized one; for one that did (it may have died in front of us),
        its last ``CORPSE_RUN_ALIVE`` batches in a row are, none with a position. Recorded: no live creature of three captures
        had more than 6 in a row, six of a fourth had 9-27 (a pawn that died and lay there)."""
        row = self._timer.get(handle)
        if row is None:
            if len(self._timer) >= self.PROFILE_MAX:
                self._timer.clear()
            row = self._timer[handle] = [deque(maxlen=self.CORPSE_MIN_BATCHES), 0, False]
        sized = nbits in self.CORPSE_BITS
        row[0].append(1 if sized else 0)
        row[1] = row[1] + 1 if sized else 0
        if row[2]:
            return row[1] >= self.CORPSE_RUN_ALIVE
        return len(row[0]) == row[0].maxlen and sum(row[0]) >= self.CORPSE_SHARE * row[0].maxlen

    def _note_alive(self, handle: int) -> None:
        """This batch carried a decoded position or a health block: it is not a timer batch."""
        row = self._timer.get(handle)
        if row:
            if row[0]:
                row[0][-1] = 0
            row[1] = 0
            row[2] = True

    def _spawn_is_stale(self, handle: int, t: float) -> bool:
        """True when a creature drawn only at its spawn point has been sending state batches for ``ACTIVE_MIN_SPAN_S`` at
        ``ACTIVE_MIN_BATCHES`` per ``ACTIVE_WINDOW_S``: it is moving about, so the spawn point is where it entered the
        relevancy range, not where it is. A resting creature sends a batch now and then and keeps its dimmed spawn dot."""
        row = self._active.get(handle)
        if row is None:
            if len(self._active) >= self.PROFILE_MAX:
                self._active.clear()
            row = self._active[handle] = [t, deque()]
        times = row[1]
        times.append(t)
        while times and t - times[0] > self.ACTIVE_WINDOW_S:
            times.popleft()
        return len(times) >= self.ACTIVE_MIN_BATCHES and t - row[0] >= self.ACTIVE_MIN_SPAN_S

    def _note_unplaced(self, handle: int, nbits: int, t: float) -> None:
        """Profile of a creature-like actor that sent a state batch but has no position on the map."""
        row = self._profile.get(handle)
        if row is None:
            if len(self._profile) >= self.PROFILE_MAX:
                self._profile.clear()
            row = self._profile[handle] = [0, 0, 0, t, t]
        row[0] += 1
        row[1] += self.CREATURE_BAND[0] <= nbits <= self.CREATURE_BAND[1]
        row[2] += nbits in self.AI_LAYOUT_BITS
        row[3] = t

    def unplaced_creatures(self) -> list[dict]:
        """Creatures that are replicated to us (state batches until recently, no destruction record) but that the map cannot
        place: their creation record, and with it their position, was never seen and they have not moved since (or the
        position layout could not be learned). One dict per creature, most state batches first: handle, kind, species,
        name, batches, silent_s, and for the Nearby table rate (state batches per second), since_s (how long it has been
        known), hp / st (the health and stamina as far as they were read, "" when not). Recomputed once a second; safe to
        call from the UI thread."""
        if self._last_t - self._hidden_cache[0] < 1.0:
            return self._hidden_cache[1]
        from . import species as sp

        rows = []
        for handle, prof in list(self._profile.items()):  # decoder thread inserts: iterate a copy
            n, band, fish, last = prof[0], prof[1], prof[2], prof[3]
            first = prof[4] if len(prof) > 4 else last
            if (n < self.HIDDEN_CREATION_BATCHES or self._last_t - last > self.HIDDEN_RECENT_S or handle == self.own_handle
                    or handle < self.MIN_DYNAMIC_HANDLE
                    or handle in self._past_own or handle in self._corpses or self.store.get(str(handle)) is not None):
                continue
            kind, species, name = self._identity(handle)
            if handle in self._creatures:  # its creation record carried a maxHealth: a creature whatever the class says
                ok = kind != sp.KIND_AI and kind != sp.KIND_OTHER
            elif kind in (sp.KIND_DINO, sp.KIND_PLAYER):
                ok = n >= self.HIDDEN_KNOWN_BATCHES
            else:  # no class: it must behave like a creature (the size profile of a pawn's state batches, see ``_creature_like``)
                ok = kind == sp.KIND_UNKNOWN and self._creature_like(handle)
            if ok:
                rows.append(self._hidden_row(handle, kind, species, name, n, first, last))
        rows.sort(key=lambda r: (-r["batches"], r["handle"]))
        self._hidden_cache = (self._last_t, rows)
        self._hidden_all = None
        return rows

    def _hidden_row(self, handle: int, kind: str, species: str, name: str, n: int, first: float, last: float) -> dict:
        vit = self._vitals.get(handle)
        hp = st = ""
        if vit is not None:
            ent = Entity(str(handle), kind, "", species, 0.0, 0.0, **self._vitals_fields(handle, vit))
            hp = hp_text(ent) if (ent.health is not None or ent.health_pct is not None) else ""
            st = st_text(ent) if (ent.stamina is not None or ent.stamina_pct is not None) else ""
        span = max(last - first, 1.0)
        return {"handle": handle, "kind": kind, "species": species, "name": "" if is_placeholder_name(name) else name,
                "batches": n, "silent_s": max(self._last_t - last, 0.0), "rate": n / span,
                "since_s": max(self._last_t - first, 0.0), "hp": hp, "st": st}

    def hidden_players(self) -> list[dict]:
        """Everything the map cannot place although the server replicates it to us and it behaves like a creature: the
        creatures of ``unplaced_creatures`` plus those taken off the map because they were active with only a spawn point
        (``_stale``; they are normally in the first list too). One row per handle; the Nearby table, the banner on the
        minimap and the counter all use this one list."""
        rows = self.unplaced_creatures()
        cached = self._hidden_all
        if cached is not None and cached[0] is rows and cached[1] == len(self._stale):
            return cached[2]
        have = {r["handle"] for r in rows}
        extra = []
        for handle in list(self._stale):  # decoder thread discards: iterate a copy
            if handle in have or self.store.get(str(handle)) is not None:
                continue
            kind, species, name = self._identity(handle)
            prof = self._profile.get(handle)
            n, last = (prof[0], prof[3]) if prof else (0, self._last_t)
            first = prof[4] if prof and len(prof) > 4 else last
            extra.append(self._hidden_row(handle, kind, species, name, n, first, last))
        out = rows + sorted(extra, key=lambda r: r["handle"]) if extra else rows
        self._hidden_all = (rows, len(self._stale), out)
        return out

    def hidden_creatures(self) -> int:
        """How many creatures ``hidden_players`` lists."""
        return len(self.hidden_players())

    def late_start(self) -> bool:
        """The app attached after the join: this connection's class tables were (nearly) never seen. Whatever the server
        sent once, at the join, is missing: the creatures that were already there (and rest) have no position."""
        return self.pipeline is not None and not self.error and self.packets >= self.LATE_JOIN_PACKETS and self.identity.tables_missed()

    def headline(self) -> str:
        """The few words the overlay can show (its status line is elided to about 30 characters): what the user
        should do or know. Empty when there is nothing to add. A change is held back for ``HEADLINE_HOLD_S`` so a
        count that flips between two values (1 hidden / none) does not make the line flicker."""
        raw = self._raw_headline()
        since, shown = self._headline
        if raw != shown and (self._last_t - since >= self.HEADLINE_HOLD_S or not shown and not raw):
            self._headline = (self._last_t, raw)
            return raw
        return shown

    def _raw_headline(self) -> str:
        if self.decode_errors and self._last_t - self._error_t < self.HEADLINE_HOLD_S * 5:
            return "Decode errors: see log"
        if self.pipeline is None or self.error or self.packets < self.LATE_JOIN_PACKETS:
            return ""
        peers = self.store.entities()
        unknown = sum(1 for p in peers if p.entity.kind == "unknown")
        hidden = self.hidden_creatures()
        if self.identity.tables_missed():
            # One constant message for as long as this lasts. It used to say "Rejoin server: N hidden" / "to identify" /
            # "Started late" depending on a count that flipped between 0 and 1 every few seconds.
            return "Started late: rejoin to see all"
        if hidden:
            return f"{hidden} creature{'s' if hidden > 1 else ''}: no position yet"
        if not unknown and not any(p.entity.kind in ("dino", "player") for p in peers):
            return "No other players nearby"
        return ""

    def _own_position(self) -> tuple[float, float, float] | None:
        latest = getattr(self.pipeline, "latest", None)
        pos = getattr(latest, "position", None)
        return None if pos is None else (pos.x, pos.y, pos.z)

    def _emit(self, handle: int, x: float, y: float, z: float | None, kind: str, species: str, name: str,
              source: str = "") -> None:
        """``source``: "" for a decoded position, "spawn" for the creation record's spawn position (a guess about where the
        creature is now). A spawn position never replaces a position that was followed."""
        if handle in self._corpses:
            return
        eid = str(handle)
        old = self.store.get(eid)
        if source == "spawn" and old is not None and old.pos_source != "spawn":
            return
        vit = self._vitals.get(handle)
        inferred = bool(species) and self.identity.is_inferred(handle)
        back = self._zombies.get(handle) if kind == "unknown" else None
        if back is not None and math.hypot(x - back[4], y - back[5]) / 100.0 <= self.ZOMBIE_NEAR_M + self.ZOMBIE_SPEED_MS * max(self._last_t - back[6], 0.0):
            kind, species, name, inferred = back[0], back[1], back[2] or name, back[3]  # the destroyed creature is back, still no creation record
        if old is not None and not name and not is_placeholder_name(old.name):
            name = old.name  # a name that was known stays until the server creates the handle again (see _on_inbound)
            if old.kind == "player" and kind in ("dino", "unknown"):
                kind = "player"  # the PlayerState link may be missing from a batch: a player does not turn back into a dinosaur
        self._note_identity(handle, old, species, inferred)
        self.store.update(Entity(
            eid, kind, name or (f"Unknown #{eid}" if kind == "unknown" else f"Actor #{eid}"), species, x, y, z,
            **self._vitals_fields(handle, vit),
            heading=self._heading(handle, x, y, old.heading if old else None), inferred=inferred,
            vitals_t=self._vitals_t.get(handle), pos_source=source,
            stage=self.identity.handle_stage(handle) or (old.stage if old is not None else "")), at=self._last_t)

    def _vitals_fields(self, handle: int, vit) -> dict:
        """The vitals of ``decode.PawnVitals`` as Entity fields: percentages (bars), the raw points the game sent, and where
        the health maximum came from."""
        if vit is None:
            return {}
        src = "block" if vit.verified else "creation" if vit.max_from_creation else ""
        return dict(health_pct=vit.health_pct, stamina_pct=vit.stamina_pct, health=vit.health, max_health=vit.max_health,
                    stamina=vit.stamina, max_stamina=vit.max_stamina, max_src=src, max_t=self._max_t.get(handle))

    def _note_identity(self, handle: int, old: Entity | None, species: str, inferred: bool) -> None:
        """A species remembered from an earlier session that the server's tables later contradict (or that was dropped
        from the class cache) changes the label; that is logged once per creature, in the session log and here."""
        if old is None or handle in self._corrected or not (old.inferred and old.species):
            return
        if species and not inferred and species != old.species:
            text = f"{old.species}~ (remembered class) -> {species} (server tables)"
        elif not species:
            text = f"{old.species}~ (remembered class) -> unknown (the class cache entry was dropped)"
        else:
            return
        self._corrected.add(handle)
        self._events.append(f"label corrected: handle {handle}: {text}")
        log.info("label corrected: handle %s: %s", handle, text)

    def drain_events(self) -> list[str]:
        """Label corrections (and class cache drops) since the last call, for the session log."""
        out = []
        while self._events:
            try:
                out.append(self._events.popleft())
            except IndexError:
                break
        cache = self.class_cache
        dropped = getattr(cache, "conflicts", 0)
        if dropped > self._conflicts_seen:
            out.append(f"class cache: {dropped - self._conflicts_seen} remembered class(es) dropped after the server tables "
                       f"named something else ({dropped} in total)")
            self._conflicts_seen = dropped
        return out

    ZOMBIE_NEAR_M = 60.0  # a returning handle counts as the same creature within this + ZOMBIE_SPEED_MS * silence
    ZOMBIE_SPEED_MS = 25.0
    ZOMBIE_MAX = 2000

    def _drop(self, handle: int) -> None:
        gone = self.store.get(str(handle))
        if gone is not None and gone.kind in ("dino", "player") and handle != self.own_handle:
            if len(self._zombies) >= self.ZOMBIE_MAX:
                self._zombies.clear()
            self._zombies[handle] = (gone.kind, gone.species, "" if is_placeholder_name(gone.name) else gone.name, gone.inferred,
                                     gone.x, gone.y, self._last_t, gone.z or 0.0)
        if handle in self._past_own:  # the server removed the old body; the handle may be reused
            self._past_own.discard(handle)
            self._sync_owned()
        if self.store.remove(str(handle)):
            self.destroyed += 1
        self._resumed.discard(handle)
        self.locator.forget(handle)
        self.pawn_vitals.forget(handle)
        self._vitals.pop(handle, None)
        self._vitals_t.pop(handle, None)
        self._max_t.pop(handle, None)
        self._corrected.discard(handle)
        self._layouts.pop(handle, None)
        self._anchors.pop(handle, None)
        self._facing.pop(handle, None)
        self._yaw_votes.pop(handle, None)
        self._yaw_distrust.discard(handle)
        self._profile.pop(handle, None)
        self._creatures.discard(handle)
        self._shape.pop(handle, None)
        self._corpses.discard(handle)
        self._timer.pop(handle, None)
        self._active.pop(handle, None)
        self._stale.discard(handle)

    def on_inbound(self, d, own_handle: int | None = None) -> None:
        """Decode one inbound datagram. A failure costs that datagram only, but it is counted and shown (status line,
        log): a decoder that throws on every packet would otherwise look like "nobody is nearby"."""
        try:
            self._on_inbound(d, own_handle)
        except Exception:  # noqa: BLE001 - the capture worker must go on with the next datagram
            self.decode_errors += 1
            self._error_t = d.observed_at
            if self.decode_errors <= 3 or self.decode_errors % 1000 == 0:
                log.exception("game peers: decoding a datagram failed (%d so far)", self.decode_errors)

    def _on_inbound(self, d, own_handle: int | None = None) -> None:
        from . import species as sp
        from .decode import Bits, batch_kind, count_batches, creation_max_health, parse_iris_packet, KIND_CREATION, KIND_STATE

        self.packets += 1
        self._last_t = d.observed_at
        if own_handle is not None and own_handle != self.own_handle:
            if self.own_handle is not None:
                self._past_own.add(self.own_handle)  # died and respawned: the old pawn lingers as a body
            self.own_handle = own_handle  # replaces (not adds to) the previous own handle
            self._past_own.discard(own_handle)
            self._sync_owned()
            self._profile.pop(own_handle, None)
        packet = parse_iris_packet(d.payload)
        if packet is None:
            return
        counts = count_batches(d.payload, packet)
        self.creation_records += counts.creation
        self.export_batches += counts.exports
        self.destruction_records += counts.destroyed
        t = d.observed_at
        for handle in packet.destroyed_handles:
            self._drop(handle)
        created = self.identity.feed(d.payload, packet)
        if self.class_cache is not None:
            self.class_cache.save()  # rate limited; a failed write never reaches the decoder
        for creation in created:
            handle = creation.net_ref_handle
            if handle in self._past_own:  # reused handle: a new actor, not the old body
                self._past_own.discard(handle)
                self._sync_owned()
            self.store.remove(str(handle))  # a reused handle is a new actor
            back = self._zombies.pop(handle, None)
            self._resumed.discard(handle)
            self.pawn_vitals.forget(handle)
            self._vitals.pop(handle, None)
            self._vitals_t.pop(handle, None)
            self._max_t.pop(handle, None)
            self._corrected.discard(handle)
            self._layouts.pop(handle, None)
            self._anchors.pop(handle, None)
            self._facing.pop(handle, None)
            self._yaw_votes.pop(handle, None)
            self._yaw_distrust.discard(handle)
            self._profile.pop(handle, None)
            self._creatures.discard(handle)
            self._shape.pop(handle, None)
            self._corpses.discard(handle)
            self._timer.pop(handle, None)
            self._active.pop(handle, None)
            self._stale.discard(handle)
            self.locator.note_creation(handle, creation.location, t)
            path = self.identity.class_path(handle)
            if creation.location is not None and path and "CorpseSmell" in path:
                self._register_smell(creation.location)
            kind, species, name = self._identity(handle)
            kind, species, name = self._revive(back, kind, species, name, creation.location, t)
            if kind not in (sp.KIND_OTHER, sp.KIND_AI) and creation.location is not None and self._near_smell(creation.location):
                self._corpses.add(handle)  # created exactly where something died: the body
                continue
            if kind != sp.KIND_OTHER and kind != sp.KIND_UNKNOWN and creation.location is not None:
                self._emit(handle, *creation.location, kind, species, name, "spawn")  # a guess until a decoded fix confirms it
        bits = Bits(d.payload)
        spawn_at = {c.net_ref_handle: c.location for c in created if c.location is not None}
        for batch in packet.batches:
            handle = batch.net_ref_handle
            if handle == self.own_handle:
                continue
            if handle in self._corpses:
                self.filtered_corpses += 1
                continue
            kind, species, name = self._identity(handle)
            if kind == sp.KIND_UNKNOWN and handle in self._zombies:
                kind, species, name = self._resume(handle, t)
            if kind == sp.KIND_OTHER:
                self.filtered_world_objects += 1
                continue
            bkind = batch_kind(bits, batch)
            if bkind == KIND_CREATION:
                if kind != sp.KIND_AI:
                    max_health = creation_max_health(bits, batch)
                    self.pawn_vitals.note_creation(handle, max_health)
                    if max_health is not None:
                        self._max_t[handle] = self._clock()
                        self._creatures.add(handle)
                    # A creature whose class could not be named (the join tables were missed, the id is not in the cache)
                    # is still a creature when its creation record carries a maxHealth: plants, fruit and items have none.
                    # Its spawn position is drawn at once; waiting for a position update leaves a resting creature unseen.
                    if kind == sp.KIND_UNKNOWN and max_health is not None and handle in spawn_at \
                            and self.store.get(str(handle)) is None and not self._near_smell(spawn_at[handle]):
                        self._emit(handle, *spawn_at[handle], kind, species, name, "spawn")
                continue
            if bkind != KIND_STATE:
                continue
            if kind == sp.KIND_UNKNOWN and self._animal_layout(handle, batch.data_bit_count):
                kind = sp.KIND_AI
            self._note_shape(handle, batch.data_bit_count)
            if kind != sp.KIND_AI and handle != self.own_handle and self._carcass_signature(handle, batch.data_bit_count):
                self._mark_corpse(handle)
                self.filtered_corpses += 1
                continue
            vit = None
            if kind != sp.KIND_AI and batch.data_bit_count not in self.AI_LAYOUT_BITS:
                blocks = self.pawn_vitals.blocks
                vit = self.pawn_vitals.update(bits, batch)
                if vit is not None:
                    self._vitals[handle] = vit
                    self._vitals_t[handle] = self._clock()
                    if self.pawn_vitals.blocks != blocks:
                        self._max_t[handle] = self._vitals_t[handle]  # a full block carries the maxima as they are now
                    self.vitals_blocks = self.pawn_vitals.blocks
            creature = kind in (sp.KIND_DINO, sp.KIND_PLAYER) or (kind == sp.KIND_UNKNOWN and self._creature_like(handle))
            # An unclassified creature (join tables missed, id not remembered) learns its position layout like a classified one;
            # before, it was drawn once at its spawn point or first bootstrap and never followed.
            loc = self.locator.update(bits, batch, t, learn=creature or kind == sp.KIND_AI)
            if loc is None and kind != sp.KIND_AI and batch.data_bit_count not in self.AI_LAYOUT_BITS:
                # also for a creature that is on the map but whose anchor went stale while it rested: it is followed again
                # as soon as the same vector offset continues twice (ActorLocationTracker.bootstrap)
                if self.store.get(str(handle)) is None or creature:
                    loc = self.locator.bootstrap(bits, batch, t)
                    if loc is not None and kind == sp.KIND_UNKNOWN:
                        # It moved like a creature in consecutive batches: it is followed from now on. Without
                        # this an actor of the pawn size band (no creation record seen) was placed once and then ignored for
                        # the ~40 s its batches needed to count as creature-like (recorded: 161 m behind when it was next seen).
                        self._creatures.add(handle)
            if loc is not None or vit is not None:
                self._note_alive(handle)
            if loc is not None:
                self.locations += 1
                self._profile.pop(handle, None)
                self._active.pop(handle, None)
                self._stale.discard(handle)
                if loc.map_heading is not None and kind != sp.KIND_AI:
                    self._facing[handle] = loc.map_heading
                self._emit(handle, loc.x, loc.y, loc.z if loc.z_reliable else None, kind, species, name)
            elif (old := self.store.get(str(handle))) is None:
                if kind != sp.KIND_AI:
                    self._note_unplaced(handle, batch.data_bit_count, t)
            elif old.pos_source == "spawn" and kind != sp.KIND_AI and self._spawn_is_stale(handle, t):
                # Drawn only at its spawn point but active for a while: it is no longer there. Not drawn; listed as
                # "position unknown" (``unplaced_creatures``) until a decoded position puts it back on the map.
                self._hide(handle)
                self._stale.add(handle)
                row = self._shape.get(handle)
                if row is not None:
                    self._profile[handle] = [row[0], row[1], row[2], t, t]
                self.stale_spawns += 1
            else:
                # Still replicated (no destruction record) but this update carries no position: keep the creature
                # on the map at its last position instead of letting the TTL remove it.
                upd = old
                upd = self._upgrade_identity(upd, handle, kind, name)
                inferred = bool(species) and self.identity.is_inferred(handle)
                if species and (not old.species or (old.inferred and not inferred)) and (species, inferred) != (old.species, old.inferred):
                    self._note_identity(handle, old, species, inferred)  # tables arrived later: the label gets the better species
                    upd = replace(upd, species=species, inferred=inferred)
                if vit is not None:
                    upd = replace(upd, **self._vitals_fields(handle, vit), vitals_t=self._vitals_t.get(handle))
                self.store.update(upd)

    RESUME_MIN_S = 2.0  # a state batch this soon after the destruction record is a late packet, not a creature coming back
    RESUME_MAX_S = 600.0

    def _resume(self, handle: int, t: float) -> tuple[str, str, str]:
        """(kind, species, name) of a handle the server destroyed that goes on sending state batches with no creation record.

        Recorded: a named player's creature was destroyed (it left the relevancy range) and 38 s later the same handle sent a
        state batch every 0.3 s again, each with a clean position vector continuing the old track, but never a creation
        record. The class and the PlayerState link were gone with the destruction, so the handle read as an unknown actor
        that is not creature-like until about 70 of its batches had been counted: no position was followed for 70 s and the
        creature was missing from the map, then reappeared far from where it had been, without its name. The destroyed
        creature's identity is restored and its last position anchors the search again; a creation record that comes later
        replaces all of it (``_on_inbound``)."""
        from . import species as sp

        back = self._zombies.get(handle)
        unknown = (sp.KIND_UNKNOWN, "", "")
        if back is None or handle in self.identity.actors:
            return unknown
        age = t - back[6]
        if not self.RESUME_MIN_S <= age <= self.RESUME_MAX_S:
            return unknown
        self._creatures.add(handle)  # a creature-like handle: its position layout is learned like a classified one
        if handle not in self._resumed:
            self._resumed.add(handle)
            if age <= self.locator.ANCHOR_LIFETIME:
                self.locator.note_creation(handle, (back[4], back[5], back[7] if len(back) > 7 else 0.0), back[6])
        return back[0], back[1], back[2]

    def _revive(self, back, kind: str, species: str, name: str, location, t: float) -> tuple[str, str, str]:
        """The server destroyed a player's creature and created it again on the same handle (relevancy flapping), near where
        it was, and the new record's PlayerState reference does not resolve: it is the same player, with the same name.
        ``back``: the ``_zombies`` entry of the handle (kind, species, name, inferred, x, y, t) or None."""
        from . import species as sp

        if name or back is None or not back[2] or location is None or kind not in (sp.KIND_DINO, sp.KIND_UNKNOWN):
            return kind, species, name
        if math.hypot(location[0] - back[4], location[1] - back[5]) / 100.0 > self.ZOMBIE_NEAR_M + self.ZOMBIE_SPEED_MS * max(t - back[6], 0.0):
            return kind, species, name
        return (sp.KIND_PLAYER if back[0] == sp.KIND_PLAYER else kind), species or back[1], back[2]

    def _upgrade_identity(self, ent: Entity, handle: int, kind: str, name: str) -> Entity:
        """A creature that was drawn as ``unknown`` (its creation record came later, or it just linked to a PlayerState)
        becomes a dinosaur / player with its name even when this batch has no position; kinds never go back."""
        rank = {"unknown": 0, "dino": 1, "player": 2}
        up_kind = kind if rank.get(kind, -1) > rank.get(ent.kind, 99) else ent.kind
        label = name or (ent.name if ent.name and not ent.name.startswith(("Unknown #", "Actor #")) else "")
        new_name = label or (f"Unknown #{handle}" if up_kind == "unknown" else f"Actor #{handle}")
        if up_kind != ent.kind or new_name != ent.name:
            return replace(ent, kind=up_kind, name=new_name)
        return ent

    # -- PeerSource --------------------------------------------------------
    def status(self) -> str:
        """Short headline first (the overlay shows only its start), then the details for the log and the tool window."""
        detail = self._detail_status()
        head = self.headline()
        return f"{head} | {detail}" if head else detail

    def _detail_status(self) -> str:
        if self.pipeline is None:
            return self.STATUS_NO_PIPELINE
        if self.error:
            return f"game: unavailable ({self.error})"
        peers = self.store.entities()
        own = f"own handle {self.own_handle}" if self.own_handle is not None else "own handle not locked yet"
        if self.packets == 0:
            return "game: waiting for server traffic"
        if not peers:
            return f"game: waiting for actor locations ({self.packets} pkts, {own})"
        known = sum(1 for p in peers if p.entity.species)
        if self.late_join():
            unknown = sum(1 for p in peers if p.entity.kind == "unknown")
            cached = sum(1 for p in peers if p.entity.inferred)
            if unknown == len(peers):
                return (f"game: {len(peers)} actors, none identified: the app started after you joined, so the server's "
                        f"class and player tables were missed. Leave the server and join again (rejoin) to get species and names ({own})")
            via = f"; {cached} named from the saved class cache (marked ~)" if cached else ""
            return (f"game: {len(peers)} actors, {unknown} unidentified: the app started after you joined and missed the "
                    f"server's class tables{via}. Leave the server and join again (rejoin) to identify the rest ({own})")
        seen = (f"creation {self.creation_records}, exports {self.export_batches}, destroyed {self.destruction_records}")
        # A late start leaves the map incomplete whatever it shows: say so in the status instead of reading as complete.
        incomplete = ("INCOMPLETE (started after the join, players already there may be missing, rejoin): "
                      if self.late_start() else "")
        return (f"game: {incomplete}{len(peers)} actors ({known} with species; {seen}; "
                f"heading from rotation/movement; growth, hunger and thirst not available; {own})")

    def late_join(self) -> bool:
        """True when this connection's class tables were (nearly) never seen (many packets, no class path from them) and
        actors on the map are still unidentified. Classes recovered from the saved class cache do not clear it for
        the actors that stay unknown: the server will not resend the tables until a rejoin."""
        return (self.packets >= self.LATE_JOIN_PACKETS and self.identity.tables_missed()
                and any(p.entity.kind == "unknown" for p in self.store.entities()))

    def identity_resolved(self) -> int:
        return sum(1 for h in list(self.identity.actors) if self.identity.class_path(h) is not None)

    def summary(self) -> str:
        """One line for the session log: what is on the map, how it was decoded and what was filtered."""
        kinds: dict[str, int] = {}
        for p in self.store.entities():
            kinds[p.entity.kind] = kinds.get(p.entity.kind, 0) + 1
        shown = " ".join(f"{k}={v}" for k, v in sorted(kinds.items())) or "none"
        if self.late_join():
            shown += " (LATE JOIN: class/name tables missed, rejoin the server)"
        hidden = self.hidden_creatures()
        if hidden:
            shown += f" ({hidden} creature(s) replicated but never placed: no position received; rejoin to see them)"
        if self.decode_errors:
            shown += f" ({self.decode_errors} DECODE ERRORS)"
        inferred = sum(1 for p in self.store.entities() if p.entity.inferred)
        if inferred:
            shown += f" ({inferred} species from the saved class cache)"
        return (f"on map: {shown} | positions fixed={self.locator.fixed} learned={self.locator.learned} "
                f"(layouts {self.locator.trusted_layouts}) | vitals blocks={self.vitals_blocks} | "
                f"world-object batches skipped={self.filtered_world_objects} | actors with class path="
                f"{self.identity_resolved()}/{len(self.identity.actors)} player names={len(self.identity.player_names)}")

    # -- what the labels say (session log, tools/label_report.py) -------------------------------------------------
    LABELS_LISTED = 12  # creatures spelled out in one "labels:" line, nearest first; the rest is counted

    def label_rows(self) -> list[dict]:
        """One dict per creature on the map (fish included), nearest first: the facts a label is made of, with the
        confidence of each (``confidence``) and where the species came from. Safe to call from the log thread."""
        now = self._clock()
        own = self._own_position()
        rows = []
        for p in self.store.entities():
            e = p.entity
            try:
                handle = int(e.id)
            except ValueError:
                continue
            source = self.identity.species_source(handle) if e.species else None
            age = None if e.vitals_t is None else max(now - e.vitals_t, 0.0)
            dist = bearing = None
            if own is not None:
                dist, bearing = distance_m(own[0], own[1], e.x, e.y), bearing_deg(own[0], own[1], e.x, e.y)
            rows.append({
                "handle": handle, "kind": e.kind, "species": e.species, "species_tier": species_tier(e.species, e.inferred),
                "source": source or "", "name": "" if is_placeholder_name(e.name) else e.name,
                "stage": e.stage, "hp": e.health_pct, "st": e.stamina_pct, "vitals_age_s": age,
                "health": e.health, "max_health": e.max_health, "max_src": e.max_src,
                "stamina": e.stamina, "max_stamina": e.max_stamina, "hp_text": hp_text(e), "st_text": st_text(e),
                "vitals": vitals_tier(*vitals_readings(e), age), "distance_m": dist, "bearing": bearing,
            })
        rows.sort(key=lambda r: (r["distance_m"] is None, r["distance_m"] or 0.0, r["handle"]))
        return rows

    def labels_signature(self) -> frozenset:
        """What the labels claim, without the values that change all the time: the "labels:" log line is written when
        this changes (a species gets confirmed, a name is linked, the first health reading arrives)."""
        return frozenset((r["handle"], r["kind"], r["species"], r["species_tier"], bool(r["name"]), r["hp"] is not None or r["health"] is not None)
                         for r in self.label_rows() if r["kind"] != "ai")

    NEARBY_BANDS_M = (250.0, 500.0, 1000.0)

    def nearby_report(self) -> str:
        """One line on what the stream holds around the player, by the displayed own position: creatures placed within each
        band (a spawn-only position counted apart), the nearest one, the server animals within the first band, and the
        creatures that are replicated but have no position yet. It is what the session log needs to tell "nobody is near"
        from "somebody is near and the map cannot place them"."""
        own = self._own_position()
        ents = [p.entity for p in self.store.entities()]
        creatures = [e for e in ents if e.kind != "ai"]
        unplaced = self.unplaced_creatures()
        if own is None:
            return f"nearby: own position unknown | creatures placed={len(creatures)} unplaced={len(unplaced)}"
        rows = sorted(((math.hypot(e.x - own[0], e.y - own[1]) / 100.0, e) for e in creatures), key=lambda r: r[0])
        bands = []
        for limit in self.NEARBY_BANDS_M:
            inside = [e for d, e in rows if d <= limit]
            spawn = sum(1 for e in inside if e.pos_source == "spawn")
            bands.append(f"<={limit:.0f}m {len(inside)}" + (f" ({spawn} spawn-only)" if spawn else ""))
        fish = sum(1 for e in ents if e.kind == "ai" and math.hypot(e.x - own[0], e.y - own[1]) / 100.0 <= self.NEARBY_BANDS_M[0])
        if rows:
            d, e = rows[0]
            nearest = f"nearest {d:.0f}m {e.kind} {e.species or '?'}" + (" (spawn-only)" if e.pos_source == "spawn" else "")
        else:
            nearest = "no creature placed"
        return (f"nearby: own ({own[0] / 100:.0f}, {own[1] / 100:.0f}) m | creatures placed " + ", ".join(bands)
                + f" | {nearest} | server animals<={self.NEARBY_BANDS_M[0]:.0f}m {fish} | replicated without a position {len(unplaced)}"
                + f" | carcasses {len(self._corpses)} | active with only a spawn point {len(self._stale)}")

    def mark_moment(self) -> str:
        """The user says creatures are next to them right now: the same report goes to the session log, marked, so the
        moment can be found and checked against the capture. Returns the report."""
        report = self.nearby_report()
        self._events.append("user mark (creatures are next to the player now): " + report.removeprefix("nearby: "))
        return report

    def labels_line(self) -> str:
        """Compact "labels:" line: the creatures that are not fish, nearest first, each with the confidence of what the
        minimap says about it. Names are the game's player names (they are on the minimap too)."""
        rows = self.label_rows()
        fish = sum(1 for r in rows if r["kind"] == "ai")
        shown = [r for r in rows if r["kind"] != "ai"]
        parts = []
        for r in shown[: self.LABELS_LISTED]:
            sp = (r["species"] + ("~" if r["species_tier"] == "inferred" else "")) if r["species"] else "?"
            if r["source"]:
                sp += f"[{r['source']}]"
            bits = [str(r["handle"]), r["kind"], sp]
            if r["name"]:
                bits.append(f'"{r["name"]}"')
            if r["hp"] is None and r["health"] is None:
                bits.append("HP?")
            else:
                bits.append(f"HP{r['hp_text']}")
            if r["st"] is not None or r["stamina"] is not None:
                bits.append(f"ST{r['st_text']}")
            if r["vitals"] == "stale" and r["vitals_age_s"] is not None:
                bits.append(f"({r['vitals_age_s']:.0f}s old)")
            if r["distance_m"] is not None:
                bits.append(f"{r['distance_m']:.0f}m{compass_label(r['bearing'])}")
            parts.append(" ".join(bits))
        more = f" +{len(shown) - self.LABELS_LISTED} more" if len(shown) > self.LABELS_LISTED else ""
        return f"labels: creatures={len(shown)} fish={fish} | " + (" | ".join(parts) or "none") + more

    BANNER_LOG_GAP_S = 5.0  # a banner that appears and goes again within this is written to the log once, not twice

    def banner_kind(self) -> str:
        """What the banner on the minimap says (``geo.banner_lines``): "late" (the app started after the join), "unknown"
        (tables seen, but creatures with no position) or "" (nothing to say)."""
        if self.late_start():
            return "late"
        return "unknown" if self.hidden_creatures() else ""

    def _track_banner(self, force: str | None = None) -> None:
        """Write the banner to the session log when it appears and when it goes (datagram time, at most one change per
        ``BANNER_LOG_GAP_S``), so a log shows what the user was told and when."""
        kind = self.banner_kind() if force is None else force
        self._banner_kind = kind
        logged, at = self._banner_logged
        if kind == logged or self._last_t - at < self.BANNER_LOG_GAP_S and force is None:
            return
        self._banner_logged = (kind, self._last_t)
        n = self.hidden_creatures()
        if kind == "late":
            self._events.append(f"banner shown: LATE START: {n} player(s) hidden - leave and rejoin the server")
        elif kind == "unknown":
            self._events.append(f"banner shown: {n} creature(s): position unknown")
        elif logged:
            self._events.append("banner cleared")

    def snapshot(self) -> PeerSnapshot:
        self._track_banner()
        return self.store.snapshot(self.status(), True, self.hidden_creatures(), self.late_start())


# ----------------------------------------------------------------- factory
def create_peer_source(kind: str, anchor: Callable[[], tuple[float, float] | None] | None = None, *,
                       lab_port: int = LAB_PORT, self_ids: Iterable[str] = (), synthetic: bool = False, pipeline=None,
                       class_cache=None):
    """``kind``: "off" | "lab" | "game" -> unstarted source (None for off/unknown). ``pipeline`` is the
    ``TelemetryPipeline`` the "game" source taps; ``class_cache`` (``classcache.ClassCache``) is the class
    memory it falls back on when the join tables were missed.
    ``synthetic=True`` (the --replay default) returns ``SyntheticPeers`` instead (needs ``anchor``)."""
    if synthetic:
        return SyntheticPeers(anchor or (lambda: None))
    if kind == "lab":
        return LabPeerSource(lab_port, self_ids=self_ids)
    if kind == "game":
        return GamePeerSource(self_ids, pipeline, class_cache=class_cache)
    return None
