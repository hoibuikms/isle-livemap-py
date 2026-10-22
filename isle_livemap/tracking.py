"""Stateful trackers that turn raw decoder candidates into one stable signal.

Ports of LocalMovementTracker.cs and UnrealDinosaurVitalsTracker.cs. All times
are float seconds (any monotonic-or-epoch clock, as long as it is consistent).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .decode import Bits, IrisBatch, MovementCandidate, decode_movement, f32, parse_iris_packet, to_f32

# ================================================================ movement

_HYPOTHESIS_LIFETIME = 1.0
_EPS = 1e-6  # float-seconds stand-in for C# tick resolution
_MIN_BOOTSTRAP = 0.6
_MAX_READY_AGE = 0.25
_REQUIRED_HITS = 8
_TS_REGRESSION_TOL = to_f32(0.002)
_TS_ADVANCE_ALLOWANCE = 2.0
_TS_WALL_MULT = 4.0
_MAX_BASE_DELTA = 5_000.0
_MAX_UNITS_PER_SEC = 100_000.0
_MAX_SAME_TS_DELTA = 1.0
_MAX_RECOVERY_DELTA = 100_000.0
_MIN_RECOVERY_BITS = 23
_MAX_CONTINUITY_ELAPSED = 1.0
_RECOVERY_LIFETIME = 1.0
_MIN_RECOVERY_DURATION = 0.6
_REQUIRED_RECOVERY_HITS = 8
_DISTANT_DELAY = 2.0
_DISTANT_LIFETIME = 1.0
_MIN_DISTANT_DURATION = 1.5
_MAX_DISTANT_READY_AGE = 0.25
_REQUIRED_DISTANT_HITS = 24


@dataclass(frozen=True, slots=True)
class _Hyp:
    candidate: MovementCandidate
    first_seen: float
    last_seen: float
    hits: int


def distance(a: MovementCandidate, b: MovementCandidate) -> float:
    return math.sqrt((b.x - a.x) ** 2 + (b.y - a.y) ** 2 + (b.z - a.z) ** 2)


def _is_continuous(prev: MovementCandidate, prev_at: float, cur: MovementCandidate, cur_at: float) -> bool:
    elapsed = max(0.0, min(cur_at - prev_at, _MAX_CONTINUITY_ELAPSED))
    return distance(prev, cur) <= _MAX_BASE_DELTA + _MAX_UNITS_PER_SEC * elapsed


def _plausible_forward_ts(cur: MovementCandidate, cand: MovementCandidate, elapsed: float) -> bool:
    delta = cand.client_timestamp - cur.client_timestamp
    max_advance = _TS_ADVANCE_ALLOWANCE + to_f32(elapsed) * _TS_WALL_MULT
    return -_TS_REGRESSION_TOL <= delta <= max_advance


def _strictly_forward_ts(cur: MovementCandidate, cand: MovementCandidate, elapsed: float) -> bool:
    delta = cand.client_timestamp - cur.client_timestamp
    return delta > _TS_REGRESSION_TOL and _plausible_forward_ts(cur, cand, elapsed)


def _same_ts_movement(cur: MovementCandidate, cand: MovementCandidate) -> bool:
    return (
        abs(cand.client_timestamp - cur.client_timestamp) <= _TS_REGRESSION_TOL
        and distance(cur, cand) > _MAX_SAME_TS_DELTA
    )


class MovementTracker:
    """Locks onto the real own-player movement among overlapping bit candidates."""

    def __init__(self) -> None:
        self._hyps: dict[tuple, _Hyp] = {}
        self._distant: dict[tuple, _Hyp] = {}
        self._current: MovementCandidate | None = None
        self._recovery: _Hyp | None = None
        self._last_lock_update = 0.0

    def reset(self) -> None:
        self._hyps.clear()
        self._distant.clear()
        self._current = None
        self._recovery = None
        self._last_lock_update = 0.0

    def track(self, payload: bytes, observed_at: float) -> MovementCandidate | None:
        candidates = decode_movement(payload)
        return self.track_candidates(candidates, observed_at)

    def track_candidates(
        self, candidates: list[MovementCandidate], observed_at: float
    ) -> MovementCandidate | None:
        current = self._current
        if current is not None:
            sample = self.recover_distant(candidates, current, self._last_lock_update, observed_at)
            if sample is not None:
                self._lock(sample, observed_at)
                self._distant.clear()
                return sample
            sample = self.continue_track(candidates, current, self._last_lock_update, observed_at)
            if sample is None:
                sample = self.recover_track(candidates, current, observed_at)
            if sample is not None:
                self._lock(sample, observed_at)
                return sample
            return None

        for lay in [k for k, h in self._hyps.items() if observed_at - h.last_seen > _HYPOTHESIS_LIFETIME]:
            del self._hyps[lay]
        for cand in candidates:
            hyp = self._hyps.get(cand.layout)
            if (
                hyp is None
                or observed_at - hyp.last_seen > _HYPOTHESIS_LIFETIME
                or not _is_continuous(hyp.candidate, hyp.last_seen, cand, observed_at)
            ):
                self._hyps[cand.layout] = _Hyp(cand, observed_at, observed_at, 1)
            else:
                self._hyps[cand.layout] = replace(
                    hyp, candidate=cand, last_seen=observed_at, hits=hyp.hits + 1
                )
        ready = [
            h
            for h in self._hyps.values()
            if h.hits >= _REQUIRED_HITS
            and observed_at - h.first_seen >= _MIN_BOOTSTRAP - _EPS
            and observed_at - h.last_seen <= _MAX_READY_AGE
        ]
        if not ready:
            return None
        ready.sort(
            key=lambda h: (-h.hits, -h.candidate.component_bit_count, -(abs(h.candidate.x) + abs(h.candidate.y)))
        )
        chosen = ready[0].candidate
        self._lock(chosen, observed_at)
        self._hyps.clear()
        return chosen

    def _lock(self, sample: MovementCandidate, at: float) -> None:
        self._current = sample
        self._last_lock_update = at
        self._recovery = None

    # -- recovery of a stale lock after a short gap -------------------------
    def recover_track(
        self, candidates: list[MovementCandidate], current: MovementCandidate, observed_at: float
    ) -> MovementCandidate | None:
        eligible = [
            c
            for c in candidates
            if c.component_bit_count >= _MIN_RECOVERY_BITS
            and c.client_timestamp > 0.0
            and distance(current, c) <= _MAX_RECOVERY_DELTA
        ]
        if not eligible:
            if self._recovery is not None and observed_at - self._recovery.last_seen > _RECOVERY_LIFETIME:
                self._recovery = None
            return None
        rec = self._recovery
        if rec is not None and observed_at - rec.last_seen <= _RECOVERY_LIFETIME:
            elapsed = max(0.0, observed_at - rec.last_seen)
            continuing = [
                c
                for c in eligible
                if _is_continuous(rec.candidate, rec.last_seen, c, observed_at)
                and _plausible_forward_ts(rec.candidate, c, elapsed)
                and not _same_ts_movement(rec.candidate, c)
            ]
            continuing.sort(
                key=lambda c: (-c.client_timestamp, distance(rec.candidate, c), -c.component_bit_count)
            )
            if continuing:
                selected = continuing[0]
                rec = replace(rec, candidate=selected, last_seen=observed_at, hits=rec.hits + 1)
                self._recovery = rec
                if rec.hits >= _REQUIRED_RECOVERY_HITS and observed_at - rec.first_seen >= _MIN_RECOVERY_DURATION - _EPS:
                    return selected
                return None
        eligible.sort(
            key=lambda c: (-c.component_bit_count, distance(current, c), -c.client_timestamp)
        )
        self._recovery = _Hyp(eligible[0], observed_at, observed_at, 1)
        return None

    def recover_distant(
        self,
        candidates: list[MovementCandidate],
        current: MovementCandidate,
        last_lock_update: float,
        observed_at: float,
    ) -> MovementCandidate | None:
        """Respawn recovery: the old lock must have gone quiet, then a new
        canonical layout must prove a sustained forward-timestamp chain."""
        if observed_at < last_lock_update or observed_at - last_lock_update < _DISTANT_DELAY - _EPS:
            self._distant.clear()
            return None
        for lay in [
            k
            for k, h in self._distant.items()
            if observed_at < h.last_seen or observed_at - h.last_seen > _DISTANT_LIFETIME
        ]:
            del self._distant[lay]
        for cand in candidates:
            if not (
                cand.component_bit_count >= _MIN_RECOVERY_BITS
                and cand.client_timestamp > 0.0
                and distance(current, cand) > _MAX_RECOVERY_DELTA
            ):
                continue
            hyp = self._distant.get(cand.layout)
            if (
                hyp is None
                or observed_at - hyp.last_seen > _DISTANT_LIFETIME
                or not _is_continuous(hyp.candidate, hyp.last_seen, cand, observed_at)
                or not _strictly_forward_ts(hyp.candidate, cand, max(0.0, observed_at - hyp.last_seen))
            ):
                self._distant[cand.layout] = _Hyp(cand, observed_at, observed_at, 1)
            else:
                self._distant[cand.layout] = replace(
                    hyp, candidate=cand, last_seen=observed_at, hits=hyp.hits + 1
                )
        ready = [
            h
            for h in self._distant.values()
            if h.hits >= _REQUIRED_DISTANT_HITS
            and observed_at - h.first_seen >= _MIN_DISTANT_DURATION - _EPS
            and observed_at - h.last_seen <= _MAX_DISTANT_READY_AGE
        ]
        if not ready:
            return None
        ready.sort(
            key=lambda h: (
                -h.hits,
                -(abs(h.candidate.x) + abs(h.candidate.y)),
                -h.candidate.client_timestamp,
            )
        )
        return ready[0].candidate

    @staticmethod
    def continue_track(
        candidates: list[MovementCandidate],
        current: MovementCandidate,
        last_update: float,
        observed_at: float,
    ) -> MovementCandidate | None:
        elapsed = max(0.0, observed_at - last_update)
        ok = [
            c
            for c in candidates
            if _is_continuous(current, last_update, c, observed_at)
            and _plausible_forward_ts(current, c, elapsed)
            and not _same_ts_movement(current, c)
        ]
        if not ok:
            return None
        ok.sort(key=lambda c: (-c.client_timestamp, distance(current, c), c.location_bit_offset))
        return ok[0]


# ================================================================ vitals


@dataclass(frozen=True, slots=True)
class Vitals:
    growth: float | None = None
    health: float | None = None
    max_health: float | None = None
    stamina: float | None = None
    max_stamina: float | None = None
    hunger: float | None = None
    max_hunger: float | None = None
    thirst: float | None = None
    max_thirst: float | None = None

    @staticmethod
    def fraction(value: float | None, maximum: float | None) -> float | None:
        if value is None or maximum is None or maximum <= 0:
            return None
        return max(0.0, min(1.0, value / maximum))


@dataclass(frozen=True, slots=True)
class VitalsObservation:
    observed_at: float
    vitals: Vitals
    net_ref_handle: int


_CANDIDATE_LIFETIME = 2.5
_SELF_TIMEOUT = 12.0
_REQUIRED_HEARTBEAT_HITS = 2
_ATTR_BITS = 66
_MAX_HEARTBEAT_BATCH_BITS = 1200
_MAX_TRAILING_BITS = _ATTR_BITS * 4
_DEFAULT_MAX_THIRST = 1000.0
_MIN_PLAUSIBLE_MAX = 0.01
_MIN_UNVERIFIED_HEALTH = 0.1
_UNSET = object()


@dataclass(frozen=True, slots=True)
class _Heartbeat:
    hunger: float
    thirst: float
    health: float | None
    stamina: float | None


@dataclass(frozen=True, slots=True)
class _HbCandidate:
    heartbeat: _Heartbeat
    first_seen: float
    last_seen: float
    hits: int


@dataclass(frozen=True, slots=True)
class _Verified:
    growth: float | None
    health: float | None
    max_health: float
    stamina: float | None
    max_stamina: float
    max_hunger: float


def _nearly_equal(left: float | None, right: float) -> bool:
    if left is None:
        return False
    return abs(left - right) <= max(0.000001, abs(right) * 0.000001)


def _nearly_equal_d(left: float, right: float) -> bool:
    return abs(left - right) <= max(0.0001, abs(right) * 0.000001)


def _read_attr(bits: Bits, absolute: int) -> float | None:
    """A GAS FGameplayAttributeData slot: float, flag bit, repeated float."""
    if absolute < 0 or absolute + _ATTR_BITS > bits.nbits or not bits.is_set(absolute + 32):
        return None
    first = f32(bits.read(absolute, 32))
    second = f32(bits.read(absolute + 33, 32))
    if (
        not math.isfinite(first)
        or not math.isfinite(second)
        or first < 0.0
        or first > 100_000.0
        or abs(first - second) > max(to_f32(0.0001), abs(first) * to_f32(0.000001))
    ):
        return None
    return first


def _read_batch_attr(bits: Bits, batch: IrisBatch, rel: int) -> float | None:
    if rel < 0 or rel + _ATTR_BITS > batch.data_bit_count:
        return None
    return _read_attr(bits, batch.data_bit_offset + rel)


def _plausible_optional_health(expected: Vitals | None, health: float) -> bool:
    if expected is None or not (expected.max_health or 0) > 0 or health <= 0 or health > expected.max_health * 1.01:
        return False
    return expected.health is None or abs(health - expected.health) <= expected.max_health * 0.5


def _plausible_optional_stamina(expected: Vitals | None, stamina: float) -> bool:
    return (
        expected is not None
        and (expected.max_stamina or 0) > 0
        and stamina <= expected.max_stamina * 1.01
        and (expected.stamina is None or abs(stamina - expected.stamina) <= expected.max_stamina * 0.5)
    )


def _can_bootstrap_stamina(expected: Vitals | None, stamina: float) -> bool:
    no_max = expected is None or not (expected.max_stamina or 0) > 0
    return no_max and _MIN_PLAUSIBLE_MAX <= stamina <= 100_000.0


def _can_bootstrap_vitals(expected: Vitals | None, health: float, stamina: float) -> bool:
    no_hp = expected is None or not (expected.max_health or 0) > 0
    no_st = expected is None or not (expected.max_stamina or 0) > 0
    return no_hp and no_st and _MIN_UNVERIFIED_HEALTH <= health <= 100_000.0 and 0.0 <= stamina <= 100_000.0


def _find_heartbeat(bits: Bits, batch: IrisBatch, expected: Vitals | None) -> _Heartbeat | None:
    if (
        not batch.has_owner_data
        or batch.data_bit_count < _ATTR_BITS * 3
        or batch.data_bit_count > _MAX_HEARTBEAT_BATCH_BITS
    ):
        return None
    matches: list[_Heartbeat] = []
    for rel in range(0, batch.data_bit_count - _ATTR_BITS * 3 + 1):
        trailing = batch.data_bit_count - (rel + _ATTR_BITS * 3)
        if trailing < 0 or trailing > _MAX_TRAILING_BITS or trailing % _ATTR_BITS != 0:
            continue
        hunger = _read_batch_attr(bits, batch, rel)
        if hunger is None:
            continue
        thirst = _read_batch_attr(bits, batch, rel + _ATTR_BITS)
        dup = _read_batch_attr(bits, batch, rel + _ATTR_BITS * 2)
        if thirst is None or dup is None:
            continue
        if (
            abs(hunger - dup) > max(0.0001, hunger * 0.00001)
            or hunger > 10_000.0
            or thirst > 2_000.0
            or (expected is None and max(hunger, thirst) < 0.05)
            or ((expected is not None and (expected.max_thirst or 0) > 0) and thirst > expected.max_thirst * 1.05)
        ):
            continue
        if expected is not None and (expected.max_hunger or 0) > 0 and hunger > expected.max_hunger * 1.05:
            continue
        health = stamina = None
        if trailing >= _ATTR_BITS:
            cur_st = _read_batch_attr(bits, batch, rel + _ATTR_BITS * 3 + trailing - _ATTR_BITS)
            if cur_st is not None and (
                _plausible_optional_stamina(expected, cur_st) or _can_bootstrap_stamina(expected, cur_st)
            ):
                stamina = cur_st
        if stamina is not None and trailing >= _ATTR_BITS * 2:
            cur_hp = _read_batch_attr(bits, batch, rel + _ATTR_BITS * 3 + trailing - _ATTR_BITS * 2)
            if cur_hp is not None and (
                _plausible_optional_health(expected, cur_hp)
                or _can_bootstrap_vitals(expected, cur_hp, stamina)
            ):
                health = cur_hp
        matches.append(_Heartbeat(hunger, thirst, health, stamina))
    if not matches:
        return None
    if expected is not None and expected.hunger is not None and expected.thirst is not None:
        eh, et = expected.hunger, expected.thirst

        def cost(c: _Heartbeat) -> float:
            return abs(c.hunger - eh) / max(1.0, expected.max_hunger if expected.max_hunger is not None else abs(eh)) + abs(
                c.thirst - et
            ) / max(100.0, expected.max_thirst if expected.max_thirst is not None else abs(et))

        selected = min(matches, key=cost)
        if eh > 0.05 and et > 0.05 and selected.hunger < eh * 0.1 and selected.thirst < et * 0.1:
            return None
        return selected
    return max(matches, key=lambda c: c.thirst + c.hunger * 0.1)


def decode_detached_attribute_frame(
    payload: bytes, current_hunger: float | None, bits: Bits | None = None
) -> Vitals | None:
    """Periodic GAS frame that is not tied to a parsed Iris batch."""
    bits = bits or Bits(payload)
    total = bits.nbits
    for shift in (0, -100):
        last_required = 1425 + shift + _ATTR_BITS
        for start in range(0, total - last_required + 1):
            growth = _read_attr(bits, start + 831 + shift)
            if growth is None:
                continue
            vals = [_read_attr(bits, start + off + shift) for off in (897, 963, 1029, 1095, 237, 1359, 1392, 1425)]
            if any(v is None for v in vals):
                continue
            health, max_health, stamina, max_stamina, max_hunger, r1, r2, r3 = vals  # type: ignore[misc]
            if (
                not _nearly_equal_d(max_health, r1)
                or not _nearly_equal_d(max_health, r2)
                or not _nearly_equal_d(max_health, r3)
                or max_health < _MIN_PLAUSIBLE_MAX
                or max_stamina < _MIN_PLAUSIBLE_MAX
                or max_hunger < _MIN_PLAUSIBLE_MAX
                or growth > 1.001
                or health > max_health * 1.01
                or stamina > max_stamina * 1.01
                or (current_hunger is not None and current_hunger > max_hunger * 1.1)
            ):
                continue
            return Vitals(
                growth=growth,
                health=health,
                max_health=max_health,
                stamina=stamina,
                max_stamina=max_stamina,
                max_hunger=max_hunger,
                max_thirst=_DEFAULT_MAX_THIRST,
            )
    return None


class VitalsTracker:
    """Learns which replicated actor is the player's own dinosaur from its
    survival heartbeat, then reads growth/health/stamina/hunger/thirst."""

    def __init__(self) -> None:
        self._candidates: dict[int, _HbCandidate] = {}
        self._verified: dict[int, _Verified] = {}
        self._self_handle: int | None = None
        self._last_self_hb = 0.0
        self._vitals = Vitals(max_thirst=_DEFAULT_MAX_THIRST)

    # -- lifecycle -----------------------------------------------------------
    def reset(self) -> None:
        self._candidates.clear()
        self._verified.clear()
        self._reset_self()

    def _reset_self(self) -> None:
        self._self_handle = None
        self._last_self_hb = 0.0
        self._candidates.clear()
        self._vitals = Vitals(max_thirst=_DEFAULT_MAX_THIRST)

    # -- main entry ------------------------------------------------------------
    def track(self, payload: bytes, observed_at: float) -> VitalsObservation | None:
        packet = parse_iris_packet(payload)
        if packet is None:
            return None
        bits = Bits(payload)
        if self._self_handle is not None and (
            observed_at < self._last_self_hb or observed_at - self._last_self_hb > _SELF_TIMEOUT
        ):
            self._reset_self()
        changed = False
        if self._self_handle is None:
            heartbeats: dict[int, _Heartbeat] = {}
            for batch in packet.batches:
                hb = _find_heartbeat(bits, batch, None)
                if hb is None:
                    continue
                self._observe_candidate(batch.net_ref_handle, hb, observed_at)
                heartbeats[batch.net_ref_handle] = hb
            self._self_handle = self._select_ready(observed_at)
            handle = self._self_handle
            if handle is not None and handle in heartbeats:
                self._restore_verified(handle)
                self._last_self_hb = observed_at
                changed |= self._set_heartbeat(heartbeats[handle])
                for batch in packet.batches:
                    if batch.net_ref_handle == handle:
                        changed |= self._read_periodic(bits, batch)
        else:
            handle = self._self_handle
            for batch in packet.batches:
                if batch.net_ref_handle != handle:
                    continue
                hb = _find_heartbeat(bits, batch, self._vitals)
                if hb is not None:
                    self._last_self_hb = observed_at
                    changed |= self._set_heartbeat(hb)
                changed |= self._read_periodic(bits, batch)
        if not packet.is_complete and self._self_handle is not None:
            decoded = decode_detached_attribute_frame(payload, self._vitals.hunger, bits)
            if decoded is not None:
                self._vitals = replace(
                    self._vitals,
                    growth=decoded.growth,
                    health=decoded.health,
                    max_health=decoded.max_health,
                    stamina=decoded.stamina,
                    max_stamina=decoded.max_stamina,
                    max_hunger=decoded.max_hunger,
                    max_thirst=_DEFAULT_MAX_THIRST,
                )
                self._remember_verified()
                changed = True
        for h in [
            h
            for h, c in self._candidates.items()
            if observed_at < c.last_seen or observed_at - c.last_seen > _CANDIDATE_LIFETIME
        ]:
            del self._candidates[h]
        if not changed or self._self_handle is None:
            return None
        return VitalsObservation(observed_at, self._vitals, self._self_handle)

    # -- candidate selection ---------------------------------------------------
    @staticmethod
    def _continuous(prev: float, cur: float, allowance: float) -> bool:
        return abs(prev - cur) <= max(allowance, abs(prev) * 0.75)

    def _observe_candidate(self, handle: int, hb: _Heartbeat, at: float) -> None:
        c = self._candidates.get(handle)
        if (
            c is None
            or at < c.last_seen
            or at - c.last_seen > _CANDIDATE_LIFETIME
            or not self._continuous(c.heartbeat.hunger, hb.hunger, 100.0)
            or not self._continuous(c.heartbeat.thirst, hb.thirst, 250.0)
        ):
            self._candidates[handle] = _HbCandidate(hb, at, at, 1)
        elif at == c.last_seen:
            self._candidates[handle] = replace(c, heartbeat=hb)
        else:
            self._candidates[handle] = replace(c, heartbeat=hb, last_seen=at, hits=c.hits + 1)

    def _select_ready(self, at: float) -> int | None:
        ready = [
            (h, c)
            for h, c in self._candidates.items()
            if c.hits >= _REQUIRED_HEARTBEAT_HITS and at - c.last_seen <= _CANDIDATE_LIFETIME
        ]
        if not ready:
            return None
        ready.sort(key=lambda p: (-p[1].hits, -min(p[1].heartbeat.hunger, 100.0), p[1].first_seen))
        return ready[0][0]

    # -- state updates ---------------------------------------------------------
    def _set_heartbeat(self, hb: _Heartbeat) -> bool:
        v = self._vitals
        if (
            _nearly_equal(v.hunger, hb.hunger)
            and _nearly_equal(v.thirst, hb.thirst)
            and (hb.health is None or _nearly_equal(v.health, hb.health))
            and (hb.stamina is None or _nearly_equal(v.stamina, hb.stamina))
        ):
            return False
        self._vitals = replace(
            v,
            hunger=hb.hunger,
            thirst=hb.thirst,
            health=hb.health if hb.health is not None else v.health,
            stamina=hb.stamina if hb.stamina is not None else v.stamina,
        )
        self._remember_verified()
        return True

    def _apply_full_frame(self, growth, health, max_health, stamina, max_stamina, max_hunger) -> bool:
        v = self._vitals
        if (
            _nearly_equal(v.growth, growth)
            and _nearly_equal(v.health, health)
            and _nearly_equal(v.max_health, max_health)
            and _nearly_equal(v.stamina, stamina)
            and _nearly_equal(v.max_stamina, max_stamina)
            and _nearly_equal(v.max_hunger, max_hunger)
        ):
            return False
        self._vitals = replace(
            v,
            growth=growth,
            health=health,
            max_health=max_health,
            stamina=stamina,
            max_stamina=max_stamina,
            max_hunger=max_hunger,
            max_thirst=_DEFAULT_MAX_THIRST,
        )
        self._remember_verified()
        return True

    def _read_periodic(self, bits: Bits, batch: IrisBatch) -> bool:
        if self._read_sparse(bits, batch):
            return True
        n = batch.data_bit_count
        if 1480 <= n <= 1510:
            shift = 0
        elif 1380 <= n <= 1410:
            shift = -100
        else:
            return False
        vals = [_read_batch_attr(bits, batch, off + shift) for off in (831, 897, 963, 1029, 1095, 237)]
        if any(x is None for x in vals):
            return False
        growth, health, max_health, stamina, max_stamina, max_hunger = vals  # type: ignore[misc]
        if (
            max_health < _MIN_PLAUSIBLE_MAX
            or max_stamina < _MIN_PLAUSIBLE_MAX
            or max_hunger < _MIN_PLAUSIBLE_MAX
            or growth > 1.001
            or health > max_health * 1.01
            or stamina > max_stamina * 1.01
            or (self._vitals.hunger is not None and self._vitals.hunger > max_hunger * 1.1)
        ):
            return False
        return self._apply_full_frame(growth, health, max_health, stamina, max_stamina, max_hunger)

    def _read_sparse(self, bits: Bits, batch: IrisBatch) -> bool:
        n = batch.data_bit_count
        if 1400 <= n <= 1435:
            prefix = 0
        elif 1500 <= n <= 1535:
            prefix = 100
        else:
            return False
        offs = (229, 361, 823, 889, 955, 1021, 1285, 1318, 1351)
        vals = [_read_batch_attr(bits, batch, o + prefix) for o in offs]
        if any(x is None for x in vals):
            return False
        max_hunger, max_stamina, growth, health, max_health, stamina, r1, r2, r3 = vals  # type: ignore[misc]
        if (
            not _nearly_equal_d(max_health, r1)
            or not _nearly_equal_d(max_health, r2)
            or not _nearly_equal_d(max_health, r3)
            or max_health < _MIN_PLAUSIBLE_MAX
            or max_stamina < _MIN_PLAUSIBLE_MAX
            or max_hunger < _MIN_PLAUSIBLE_MAX
            or growth > 1.001
            or health > max_health * 1.01
            or stamina > max_stamina * 1.01
            or (self._vitals.hunger is not None and self._vitals.hunger > max_hunger * 1.1)
        ):
            return False
        return self._apply_full_frame(growth, health, max_health, stamina, max_stamina, max_hunger)

    def _remember_verified(self) -> None:
        v, h = self._vitals, self._self_handle
        if h is None or not (v.max_health or 0) > 0 or not (v.max_stamina or 0) > 0 or not (v.max_hunger or 0) > 0:
            return
        self._verified[h] = _Verified(v.growth, v.health, v.max_health, v.stamina, v.max_stamina, v.max_hunger)

    def _restore_verified(self, handle: int) -> None:
        ver = self._verified.get(handle)
        if ver is None:
            return
        self._vitals = replace(
            self._vitals,
            growth=ver.growth,
            health=ver.health,
            max_health=ver.max_health,
            stamina=ver.stamina,
            max_stamina=ver.max_stamina,
            max_hunger=ver.max_hunger,
            max_thirst=_DEFAULT_MAX_THIRST,
        )
