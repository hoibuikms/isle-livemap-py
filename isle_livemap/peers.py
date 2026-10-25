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
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Protocol

from .geo import (
    GAME_PEER_STALE_S, GAME_PEER_TTL_S, PEER_STALE_S, PEER_TTL_S, Entity, bearing_deg, canonical_id, is_self, vital_pct,
)
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
                 self_ids: Iterable[str] = ()) -> None:
        self.ttl, self.clock = ttl, clock
        self._lock = threading.Lock()
        self._seen: dict[str, PeerEntity] = {}
        self._config: set[str] = {canonical_id(i) for i in self_ids}  # user-configured (Settings.peer_self_ids)
        self._owned: set[str] = set()  # ids a backend owns (lab id 0, the learned game handle): survive set_self_ids
        self._self: set[str] = set(self._config)
        self._version = 0
        self._cached: tuple[PeerEntity, ...] | None = ()
        self._snap: PeerSnapshot | None = None
        self.dropped_self = 0

    def update(self, entity: Entity) -> bool:
        """Insert/refresh one entity. False if it is a self id (dropped)."""
        now = self.clock()
        with self._lock:
            if is_self(entity.id, self_ids=self._self):
                self.dropped_self += 1
                return False
            old = self._seen.get(entity.id)
            self._seen[entity.id] = PeerEntity(entity, now)
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
        gone = [i for i, p in self._seen.items() if now - p.last_seen >= self.ttl]
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

    def snapshot(self, status: str = "", enabled: bool = True) -> PeerSnapshot:
        with self._lock:
            self._expire(self.clock())
            if self._cached is None:
                self._cached = tuple(sorted(self._seen.values(), key=lambda p: p.entity.id))
            sn = self._snap
            if sn is None or sn.status != status or sn.enabled != enabled:
                sn = self._snap = PeerSnapshot(self._cached, self._version, status, enabled)
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
        from creation anchors and continuity (pawns). ``decode.decode_vitals_block`` reads health and stamina.
      * A peer is keyed by its handle. The own handle is excluded as soon as the heartbeat lock gives it; a
        provisional peer with that handle is purged retroactively (``PeerStore.set_owned_self_ids``).
      * A destruction record removes the peer at once and forgets its identity (the server reuses handles). Iris
        replicates changes only, so a creature that stands still goes quiet: the fallback is generous (dim after
        ``STALE_S`` = 8 s, remove after ``TTL_S`` = 30 s without any report), not the 3 s / 5 s of the lab feed.
        Any state update of a tracked creature counts as a report, also one without a position: live captures show
        pawns that keep sending state for minutes after their position stopped changing.
      * Every decoded position is written to the store at once (no buffering); the overlay smooths and
        dead-reckons between samples (``geo.EntitySmoother``).

    Decoded: handle, x/y/z, class, species, kind, player name (when the creature references its PlayerState),
    health and stamina percentages (when the 9-slot attribute block is sent), heading (derived from movement, see
    ``_heading``). NOT decoded: growth, hunger, thirst."""

    STALE_S = GAME_PEER_STALE_S  # read by the overlay (dim) and the smoother (TTL)
    TTL_S = GAME_PEER_TTL_S
    stale_s, ttl_s = STALE_S, TTL_S

    # Actors whose creation record was never captured have no class. Their state batches still have a class-typical
    # size: every server-controlled animal of the recorded captures (33 resolved, 73 never created) repeats a 155 or
    # 158 bit state, and no dinosaur or world object does. Eight state batches, 90 % of them that size: "ai".
    AI_LAYOUT_BITS = (155, 158)
    AI_LAYOUT_MIN_BATCHES = 8
    AI_LAYOUT_SHARE = 0.9

    # Heading is not in the replicated data we can decode, so it is the direction of travel: the bearing from an anchor
    # position to the first later position at least this far away (a metre filters the position quantisation noise),
    # and not more than HEADING_MAX_STEP_CM (a larger jump is a respawn or a glitch, not walking).
    HEADING_MIN_MOVE_CM = 100.0
    HEADING_MAX_STEP_CM = 20_000.0

    LATE_JOIN_PACKETS = 100  # a fresh join sends the export tables within the first packets; this many without = missed

    STATUS_NO_PIPELINE = "game: unavailable (no capture pipeline attached)"

    def __init__(self, self_ids: Iterable[str] = (), pipeline=None, clock: Callable[[], float] = time.monotonic,
                 class_cache=None) -> None:
        from .decode import ActorLocationTracker, IdentityTracker

        self._config_self = tuple(canonical_id(i) for i in self_ids)
        self.store = PeerStore(ttl=self.TTL_S, clock=clock, self_ids=self._config_self)
        self.pipeline = pipeline
        self.own_handle: int | None = None
        self.class_cache = class_cache  # classcache.ClassCache | None: classes learned in earlier sessions
        self.identity = IdentityTracker(class_cache)
        self.locator = ActorLocationTracker()
        self._vitals: dict[int, object] = {}  # handle -> decode.ActorVitals
        self._layouts: dict[int, list[int]] = {}  # unclassified handle -> [state batches of the animal size, all state batches]
        self._anchors: dict[int, tuple[float, float]] = {}  # handle -> position the travel direction is measured from
        self.packets = 0  # inbound datagrams seen
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
        self._vitals.clear()
        self._layouts.clear()
        self._anchors.clear()

    def _heading(self, handle: int, x: float, y: float, previous: float | None) -> float | None:
        """Direction of travel (degrees clockwise from north) once the creature has moved ``HEADING_MIN_MOVE_CM``
        from its anchor; ``previous`` while it stands still."""
        anchor = self._anchors.get(handle)
        if anchor is None:
            self._anchors[handle] = (x, y)
            return previous
        moved = math.hypot(x - anchor[0], y - anchor[1])
        if moved < self.HEADING_MIN_MOVE_CM:
            return previous
        self._anchors[handle] = (x, y)
        return bearing_deg(*anchor, x, y) if moved <= self.HEADING_MAX_STEP_CM else previous

    def _identity(self, handle: int) -> tuple[str, str, str]:
        """(kind, species, name) of an actor from what its creation record and the export tables say."""
        from . import species as sp

        info = self.identity.actors.get(handle)
        if info is None:
            return sp.KIND_UNKNOWN, "", ""
        path = self.identity.class_path(handle)
        kind, species = sp.describe(path, self.identity.species_hint(handle), info.player_state is not None)
        if kind == sp.KIND_DINO and not species and info.asset_species:
            species = sp.display_name(info.asset_species[0])
        return kind, species, self.identity.name_of(handle) or ""

    def _animal_layout(self, handle: int, bits: int) -> bool:
        """Count one state batch of an actor without class information; True once it looks like a server animal."""
        seen = self._layouts.setdefault(handle, [0, 0])
        seen[0] += bits in self.AI_LAYOUT_BITS
        seen[1] += 1
        return seen[1] >= self.AI_LAYOUT_MIN_BATCHES and seen[0] >= seen[1] * self.AI_LAYOUT_SHARE

    def _emit(self, handle: int, x: float, y: float, z: float | None, kind: str, species: str, name: str) -> None:
        eid = str(handle)
        old = self.store.get(eid)
        vit = self._vitals.get(handle)
        self.store.update(Entity(
            eid, kind, name or (f"Unknown #{eid}" if kind == "unknown" else f"Actor #{eid}"), species, x, y, z,
            health_pct=vit.health_pct if vit else None, stamina_pct=vit.stamina_pct if vit else None,
            heading=self._heading(handle, x, y, old.heading if old else None), inferred=bool(species) and self.identity.is_inferred(handle)))

    def _drop(self, handle: int) -> None:
        if self.store.remove(str(handle)):
            self.destroyed += 1
        self.locator.forget(handle)
        self._vitals.pop(handle, None)
        self._layouts.pop(handle, None)
        self._anchors.pop(handle, None)

    def on_inbound(self, d, own_handle: int | None = None) -> None:
        from . import species as sp
        from .decode import Bits, batch_kind, count_batches, decode_vitals_block, parse_iris_packet, KIND_STATE

        self.packets += 1
        if own_handle is not None and own_handle != self.own_handle:
            self.own_handle = own_handle  # replaces (not adds to) the previous own handle
            self.store.set_owned_self_ids((str(own_handle),))
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
            self.store.remove(str(handle))  # a reused handle is a new actor
            self._vitals.pop(handle, None)
            self._layouts.pop(handle, None)
            self._anchors.pop(handle, None)
            self.locator.note_creation(handle, creation.location, t)
            kind, species, name = self._identity(handle)
            if kind != sp.KIND_OTHER and kind != sp.KIND_UNKNOWN and creation.location is not None:
                self._emit(handle, *creation.location, kind, species, name)  # the spawn position is a position
        bits = Bits(d.payload)
        for batch in packet.batches:
            handle = batch.net_ref_handle
            if handle == self.own_handle:
                continue
            kind, species, name = self._identity(handle)
            if kind == sp.KIND_OTHER:
                self.filtered_world_objects += 1
                continue
            if batch_kind(bits, batch) != KIND_STATE:
                continue
            if kind == sp.KIND_UNKNOWN and self._animal_layout(handle, batch.data_bit_count):
                kind = sp.KIND_AI
            vit = decode_vitals_block(bits, batch) if batch.data_bit_count >= 593 else None
            if vit is not None:
                self._vitals[handle] = vit
                self.vitals_blocks += 1
            loc = self.locator.update(bits, batch, t, learn=kind in (sp.KIND_DINO, sp.KIND_PLAYER, sp.KIND_AI))
            if loc is not None:
                self.locations += 1
                self._emit(handle, loc.x, loc.y, loc.z if loc.z_reliable else None, kind, species, name)
            elif (old := self.store.get(str(handle))) is not None:
                # Still replicated (no destruction record) but this update carries no position: keep the creature
                # on the map at its last position instead of letting the TTL remove it.
                self.store.update(replace(old, health_pct=vit.health_pct, stamina_pct=vit.stamina_pct) if vit is not None else old)

    # -- PeerSource --------------------------------------------------------
    def status(self) -> str:
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
                        f"class and player tables were missed. Rejoin the server to get species and names ({own})")
            via = f"; {cached} named from the saved class cache (marked ~)" if cached else ""
            return (f"game: {len(peers)} actors, {unknown} unidentified: the app started after you joined and missed the "
                    f"server's class tables{via}. Rejoin the server to identify the rest ({own})")
        seen = (f"creation {self.creation_records}, exports {self.export_batches}, destroyed {self.destruction_records}")
        return (f"game: {len(peers)} actors ({known} with species; {seen}; "
                f"heading from movement; growth, hunger and thirst not available; {own})")

    def late_join(self) -> bool:
        """True when this connection's class tables were (nearly) never seen (many packets, no class path from them) and
        actors on the map are still unidentified. Classes recovered from the saved class cache do not clear it for
        the actors that stay unknown: the server will not resend the tables until a rejoin."""
        return (self.packets >= self.LATE_JOIN_PACKETS and self.identity.tables_missed()
                and any(p.entity.kind == "unknown" for p in self.store.entities()))

    def identity_resolved(self) -> int:
        return sum(1 for h in self.identity.actors if self.identity.class_path(h) is not None)

    def summary(self) -> str:
        """One line for the session log: what is on the map, how it was decoded and what was filtered."""
        kinds: dict[str, int] = {}
        for p in self.store.entities():
            kinds[p.entity.kind] = kinds.get(p.entity.kind, 0) + 1
        shown = " ".join(f"{k}={v}" for k, v in sorted(kinds.items())) or "none"
        if self.late_join():
            shown += " (LATE JOIN: class/name tables missed, rejoin the server)"
        inferred = sum(1 for p in self.store.entities() if p.entity.inferred)
        if inferred:
            shown += f" ({inferred} species from the saved class cache)"
        return (f"on map: {shown} | positions fixed={self.locator.fixed} learned={self.locator.learned} "
                f"(layouts {self.locator.trusted_layouts}) | vitals blocks={self.vitals_blocks} | "
                f"world-object batches skipped={self.filtered_world_objects} | actors with class path="
                f"{self.identity_resolved()}/{len(self.identity.actors)} player names={len(self.identity.player_names)}")

    def snapshot(self) -> PeerSnapshot:
        return self.store.snapshot(self.status(), True)


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
