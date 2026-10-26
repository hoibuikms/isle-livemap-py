"""Replay a recorded session through the real app path and measure what the minimap would have drawn.

    .venv/bin/python -I tools/stability_replay.py SESSION.pcap [more.pcap ...] [--json out.json] [--seconds N]

Path: pcap -> TelemetryPipeline.handle -> GamePeerSource -> MapView.tick (60 fps, offscreen Qt) -> MapView.grab (5 Hz),
with ``time.monotonic`` replaced by a simulated clock that follows the datagram times, so a 6 minute capture replays
in about a minute and the result does not depend on the machine. Reported per capture:

  stuck   dots frozen while the server shows the creature moving; dots further from the newest server sample than
          dead reckoning allows; dots that outlive a destruction record; own marker standing still while it moves
  delay   dot lag behind the creature (position lag along its path, in ms), time from first record to first dot,
          decode time per datagram and the backlog a single decode worker would have built up
  steady  kind / species / name changes per actor, dots that vanish without a destruction record, label text
          reversals (HP 80 -> 79 -> 80), label slot hops, label on/off toggles, headline changes
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FPS = 60.0
SAMPLE_HZ = 20.0
PAINT_HZ = 5.0
MOVING_CM_S = 400.0


class SimClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def choppiness(frames: dict, samples: dict, kinds: dict) -> dict:
    """How fluid the dots move at the rendered frame rate, per kind ("player" = every dinosaur of another person, "ai").

    For every interval between two server positions over which the creature really moves (1.5 .. 40 m/s, samples at
    most 2 s apart) the displacement of the dot in each rendered frame is compared with the interval's mean:
      stall   frame moved less than 20 % of the mean (the dot stands still while the creature moves)
      spike   frame moved more than 3 x the mean (the dot jumps)
    ``frames``: id -> [(t, x, y)] per frame, ``samples``: id -> [(t, x, y)] distinct server positions."""
    acc: dict = {}
    for eid, d in frames.items():
        sm = samples.get(eid, [])
        kind = kinds.get(eid)
        if len(sm) < 3 or len(d) < 30 or kind is None:
            continue
        group = "ai" if kind == "ai" else "player"
        steps = [(b[0], math.hypot(b[1] - a[1], b[2] - a[2])) for a, b in zip(d, d[1:])]
        a_ = acc.setdefault(group, {"intervals": 0, "frames": 0, "stall": 0, "spike": 0, "max_run": 0})
        j = 0
        for a, b in zip(sm, sm[1:]):
            dt = b[0] - a[0]
            if not 0.05 < dt <= 2.0:
                continue
            speed = math.hypot(b[1] - a[1], b[2] - a[2]) / dt / 100.0
            if not 1.5 <= speed <= 40.0:
                continue
            while j < len(steps) and steps[j][0] < a[0] + 0.25:
                j += 1
            k = j
            win = []
            while k < len(steps) and steps[k][0] <= b[0] + 0.25:
                win.append(steps[k][1])
                k += 1
            if len(win) < 5:
                continue
            mean = sum(win) / len(win)
            run = 0
            a_["intervals"] += 1
            a_["frames"] += len(win)
            for x in win:
                if x < 0.2 * mean:
                    a_["stall"] += 1
                    run += 1
                    a_["max_run"] = max(a_["max_run"], run)
                else:
                    run = 0
                if x > 3.0 * mean:
                    a_["spike"] += 1
    out = {}
    for group, a_ in acc.items():
        ok = a_["intervals"] >= 30  # a handful of intervals says nothing
        out[group] = {"intervals": a_["intervals"],
                      "stall_frac": round(a_["stall"] / a_["frames"], 4) if ok else None,
                      "spike_frac": round(a_["spike"] / a_["frames"], 4) if ok else None,
                      "max_stall_run_s": round(a_["max_run"] / FPS, 2)}
    return out


def run(pcap: Path, cache=None, seconds: float | None = None, local_port: int | None = None,
        paint_hz: float = PAINT_HZ, debug: dict | None = None) -> dict:
    from unittest import mock

    from PySide6.QtWidgets import QApplication

    import isle_livemap.decode as decode
    import isle_livemap.overlay as overlay
    from isle_livemap.capture import Datagram, TelemetryPipeline
    from isle_livemap.geo import MarkerStore, Settings
    from isle_livemap.overlay import MapView
    from isle_livemap.peers import GamePeerSource
    from isle_livemap.replay import pcap_datagrams

    app = QApplication.instance() or QApplication([])
    clock = SimClock()
    tmp = Path(tempfile.mkdtemp(prefix="stab_"))
    first_record: dict[int, float] = {}
    pipe_errors: list[str] = []
    drops: dict[str, float] = {}  # handle -> sim time of its destruction record
    label_log: list[tuple[float, str, str, tuple, tuple]] = []  # (t, id, text, anchor, rect)
    cur = {"id": None, "text": ""}

    real_parse = decode.parse_iris_packet

    def parse(payload):
        packet = real_parse(payload)
        if packet is not None:
            for b in packet.batches:
                first_record.setdefault(b.net_ref_handle, clock.t)
        return packet

    real_label_lines = overlay.peer_label_lines
    real_place = overlay.place_label

    def label_lines(n, *a, **k):
        lines = real_label_lines(n, *a, **k)
        cur["id"], cur["text"] = n.entity.id, " / ".join(lines)
        return lines

    def place(ax, ay, w, h, placed, bounds, accept=None, *more, **kw):
        r = real_place(ax, ay, w, h, placed, bounds, accept, *more, **kw)
        if r is not None and cur["id"] is not None:
            label_log.append((clock.t, cur["id"], cur["text"], (ax, ay), r))
        return r

    out: dict = {"pcap": pcap.name}
    with mock.patch.object(time, "monotonic", clock), mock.patch.object(decode, "parse_iris_packet", parse), \
            mock.patch.object(overlay, "peer_label_lines", label_lines), mock.patch.object(overlay, "place_label", place):
        pipe = TelemetryPipeline()
        pipe.error_hook = pipe_errors.append
        peers = GamePeerSource(pipeline=pipe, clock=clock, class_cache=cache)
        real_drop = peers._drop

        def drop(handle):
            drops[str(handle)] = clock.t
            real_drop(handle)

        peers._drop = drop
        peers.start()
        view = MapView(pipe, MarkerStore(tmp / "m.json"), Settings(), peers)
        view.timer.stop()
        view._last = clock.t
        out["_view"] = view

        t_tick, t_paint, t_snapshot, t_status = [], [], [], []
        decode_ms: list[tuple[float, float, int]] = []  # (arrival, ms, bytes)
        samples: dict[str, list] = defaultdict(list)  # server-side position samples (t, x, y) per id
        series: dict[str, list] = defaultdict(list)  # displayed (t, x, y)
        timeline: dict[str, dict] = defaultdict(lambda: {"kind": [], "species": [], "name": []})
        displayed_prev: set[str] = set()
        first_dot: dict[str, float] = {}
        gone_at: dict[str, float] = {}
        vanished_without_destroy: list[tuple[str, float]] = []
        reappeared: list[tuple[str, float]] = []
        ghost_s = 0.0
        ghost_after_drop: dict[str, float] = {}
        frozen_s = drift_s = 0.0
        drift_max_cm = 0.0
        hist: dict[str, list] = defaultdict(list)
        drift_run: dict[str, float] = defaultdict(float)
        drift_run_max = 0.0
        own_hist: list = []
        own_samples: list = []
        own_series: list = []
        frame_pos: dict[str, list] = defaultdict(list)  # per rendered frame (60 Hz): (t, x, y) of every dot, for choppiness()
        own_frozen_s = 0.0
        headlines: list[tuple[float, str]] = []
        last_store_version = -1
        next_sample = next_paint = 0.0
        t0_dg = None
        last_pos_at = 0.0
        exceptions = 0
        sample_dt = 1.0 / SAMPLE_HZ

        def frame() -> None:
            nonlocal next_sample, next_paint, ghost_s, frozen_s, drift_s, drift_max_cm, own_frozen_s, exceptions
            nonlocal displayed_prev
            clock.t += 1.0 / FPS
            s = time.perf_counter()
            try:
                view.tick()
            except Exception:  # noqa: BLE001 - counted: a tick that raises is a frozen frame in the live app
                exceptions += 1
            t_tick.append(time.perf_counter() - s)
            now = clock.t
            for eid_, p_ in view.ents.pos.items():
                frame_pos[eid_].append((now, p_[0], p_[1]))
            if now >= next_sample:
                next_sample = now + sample_dt
                sample(now)
            if paint_hz and now >= next_paint:
                next_paint = now + 1.0 / paint_hz
                s = time.perf_counter()
                view.grab()
                t_paint.append(time.perf_counter() - s)

        def sample(now: float) -> None:
            nonlocal displayed_prev, ghost_s, frozen_s, drift_s, drift_max_cm, own_frozen_s, drift_run_max
            near = view.nearby(None)
            ids = {n.entity.id for n in near}
            in_store = {p.id: p for p in peers.store.entities()}
            hl = view.peer_status.split(" | ")[0] if " | " in view.peer_status else ""
            if not headlines or headlines[-1][1] != hl:
                headlines.append((now, hl))
            for n in near:
                e = n.entity
                first_dot.setdefault(e.id, now)
                series[e.id].append((now, n.x, n.y))
                tl = timeline[e.id]
                for key, val in (("kind", e.kind), ("species", e.species), ("name", e.name)):
                    if not tl[key] or tl[key][-1][1] != val:
                        tl[key].append((now, val))
                p = in_store.get(e.id)
                if p is None:
                    ghost_s += sample_dt
                    ghost_after_drop.setdefault(e.id, now)
                    continue
                srv = (p.entity.x, p.entity.y)
                v = view.ents._vel.get(e.id)
                allowed = (math.hypot(v[0], v[1]) * v[2] if v else 0.0) + 300.0
                d = dist((n.x, n.y), srv)
                if d > allowed:
                    drift_s += sample_dt
                    drift_max_cm = max(drift_max_cm, d - allowed)
                    drift_run[e.id] += sample_dt
                    drift_run_max = max(drift_run_max, drift_run[e.id])
                else:
                    drift_run[e.id] = 0.0
                h = hist[e.id]
                h.append((now, srv, (n.x, n.y)))
                while h and now - h[0][0] > 3.0:
                    h.pop(0)
                if len(h) > 5 and h[-1][0] - h[0][0] >= 2.9 and dist(h[0][1], h[-1][1]) > 500 and dist(h[0][2], h[-1][2]) < 50:
                    frozen_s += sample_dt
            for back in ids - displayed_prev:
                if back in gone_at and back in dict(vanished_without_destroy):
                    reappeared.append((back, now - gone_at[back]))
            for gone in displayed_prev - ids:
                gone_at[gone] = now
                if gone not in drops or drops[gone] < first_dot.get(gone, 0):
                    vanished_without_destroy.append((gone, now))
            displayed_prev = ids
            pos = pipe.latest.position
            if pos is not None:
                own_series.append((now, view.interp.x, view.interp.y))
                own_hist.append((now, (pos.x, pos.y), (view.interp.x, view.interp.y)))
                while own_hist and now - own_hist[0][0] > 2.0:
                    own_hist.pop(0)
                if len(own_hist) > 5 and own_hist[-1][0] - own_hist[0][0] >= 1.9 and \
                        dist(own_hist[0][1], own_hist[-1][1]) > 300 and dist(own_hist[0][2], own_hist[-1][2]) < 30:
                    own_frozen_s += sample_dt

        def record_store(now: float) -> None:
            nonlocal last_store_version
            if peers.store.version == last_store_version:
                return
            last_store_version = peers.store.version
            for p in peers.store.entities():
                s = samples[p.id]
                if not s or (s[-1][1], s[-1][2]) != (p.entity.x, p.entity.y):
                    s.append((now, p.entity.x, p.entity.y))

        arrivals = 0
        base = None
        for d in pcap_datagrams(pcap, local_port):
            if base is None:
                base = d.observed_at
            rel = d.observed_at - base
            if seconds is not None and rel > seconds:
                break
            target = 1000.0 + rel
            while clock.t + 1.0 / FPS <= target:
                frame()
            clock.t = max(clock.t, target)
            s = time.perf_counter()
            pipe.handle(Datagram(clock.t, d.src_ip, d.src_port, d.dst_ip, d.dst_port, d.payload, d.direction))
            decode_ms.append((rel, (time.perf_counter() - s) * 1000.0, len(d.payload)))
            record_store(clock.t)
            pos = pipe.latest.position
            if d.direction == "outbound" and pos is not None and (not own_samples or own_samples[-1][1:] != (pos.x, pos.y)):
                own_samples.append((clock.t, pos.x, pos.y))
            arrivals += 1
        end = clock.t
        for _ in range(int(FPS * 2)):  # let the last updates settle
            frame()
        s = time.perf_counter()
        for _ in range(20):
            peers.snapshot()
        t_snapshot.append((time.perf_counter() - s) / 20)
        s = time.perf_counter()
        for _ in range(20):
            peers.status()
        t_status.append((time.perf_counter() - s) / 20)
        out["summary_line"] = peers.summary()[:300]
        peers_errors = [peers.decode_errors]
        view_errors = [view.errors]
        peers.stop()

    view = out.pop("_view", None)
    if view is not None:  # a widget left to the garbage collector is destroyed in the middle of some later Qt call
        try:
            import shiboken6

            view.timer.stop()
            shiboken6.delete(view)
        except Exception:  # noqa: BLE001
            pass
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    if debug is not None:
        debug.update(own_samples=own_samples, own_series=own_series, samples=samples, series=series, timeline=timeline, first_record=first_record, first_dot=first_dot,
                     drops=drops, label_log=label_log, vanished=vanished_without_destroy, headlines=headlines)
    duration = max(end - 1000.0, 1.0)
    minutes = duration / 60.0
    out["duration_s"] = round(duration, 1)
    out["datagrams"] = arrivals
    out["tick_exceptions"] = exceptions

    # ---- delay: time from first record to first dot
    fix_at = own_series[0][0] if own_series else 0.0  # before the own position is known no dot can be placed
    ttd = [first_dot[i] - max(first_record[int(i)], fix_at) for i in first_dot if i.isdigit() and int(i) in first_record]
    out["time_to_first_dot_s"] = {"n": len(ttd), "p50": pct(ttd, .5), "p95": pct(ttd, .95), "max": max(ttd, default=None)}
    placed_handles = {int(i) for i in first_dot if i.isdigit()}
    out["records_without_dot"] = len([h for h in first_record if h not in placed_handles])

    # ---- delay: lag of the dot behind the creature's own path (positive = behind), moving creatures only.
    # "hold" is the lag of a dot that just stays on the newest sample: the floor the server's send interval imposes.
    def path_lag(ser, sm, max_span):
        lag, hold, err, herr = [], [], [], []
        j = 0
        for t, x, y in ser:
            while j + 1 < len(sm) and sm[j + 1][0] <= t:
                j += 1
            if j + 1 >= len(sm):
                break
            (ta, xa, ya), (tb, xb, yb) = sm[j], sm[j + 1]
            span = tb - ta
            if not (0.05 <= span <= max_span) or not ta <= t <= tb:
                continue
            vx, vy = (xb - xa) / span, (yb - ya) / span
            speed = math.hypot(vx, vy)
            if speed < MOVING_CM_S or speed > 6000:
                continue
            f = (t - ta) / span
            hx, hy = xa + (xb - xa) * f, ya + (yb - ya) * f
            lag.append(((hx - x) * vx + (hy - y) * vy) / (speed * speed) * 1000.0)  # ms
            hold.append((t - ta) * 1000.0)
            err.append(math.hypot(hx - x, hy - y))
            herr.append(math.hypot(hx - xa, hy - ya))
        return lag, hold, err, herr

    def jerk(ser):
        """Frames (20 Hz) whose displacement is far above both neighbours' average: a visible jump."""
        n = 0
        steps = [dist(a[1:], b[1:]) for a, b in zip(ser, ser[1:]) if b[0] - a[0] < 0.2]
        for i in range(2, len(steps) - 2):
            around = (steps[i - 2] + steps[i - 1] + steps[i + 1] + steps[i + 2]) / 4.0
            if steps[i] > max(2.5 * around, 150.0):
                n += 1
        return n

    lags, holds, jumps, errs, herrs = [], [], 0, [], []
    for eid, ser in series.items():
        sm = samples.get(eid, [])
        if len(sm) < 3:
            continue
        a, b, e1, e2 = path_lag(ser, sm, 1.0)
        lags += a
        holds += b
        errs += e1
        herrs += e2
        jumps += jerk(ser)
    out["lag_ms"] = {"n": len(lags), "p50": pct(lags, .5), "p95": pct(lags, .95), "max": max(lags, default=None),
                     "hold_p50": pct(holds, .5), "hold_p95": pct(holds, .95)}
    out["err_cm"] = {"p50": pct(errs, .5), "p95": pct(errs, .95), "hold_p50": pct(herrs, .5), "hold_p95": pct(herrs, .95)}
    out["jumps_per_min"] = round(jumps / minutes, 2)
    out["choppy"] = choppiness(frame_pos, samples, {i: tl["kind"][0][1] for i, tl in timeline.items() if tl["kind"]})
    own_lag, own_hold, own_err, own_herr = path_lag(own_series, own_samples, 0.6) if own_samples else ([], [], [], [])
    out["own_lag_ms"] = {"n": len(own_lag), "p50": pct(own_lag, .5), "p95": pct(own_lag, .95), "max": max(own_lag, default=None),
                         "hold_p50": pct(own_hold, .5), "hold_p95": pct(own_hold, .95)}
    out["own_err_cm"] = {"p50": pct(own_err, .5), "p95": pct(own_err, .95), "hold_p50": pct(own_herr, .5), "hold_p95": pct(own_herr, .95)}
    out["own_jumps_per_min"] = round(jerk(own_series) / minutes, 2)

    # ---- stuck
    out["stuck"] = {
        "frozen_dot_s": round(frozen_s, 2), "drift_beyond_dr_s": round(drift_s, 2), "drift_max_cm": round(drift_max_cm),
        "ghost_dot_s": round(ghost_s, 2), "own_marker_frozen_s": round(own_frozen_s, 2),
        "drift_longest_run_s": round(drift_run_max, 2),
    }
    out["errors"] = {"decode": peers_errors[0], "pipeline": len(pipe_errors), "view": view_errors[0]}
    after, ghosts = [], []  # server animals leave at once; players and dinosaurs stay as dimmed ghosts (peers.GHOST_GRACE_S)
    for eid, t_drop in drops.items():
        g = gone_at.get(eid)
        if eid in first_dot and g is not None and g >= t_drop:
            kinds = timeline.get(eid, {}).get("kind") or []
            (ghosts if kinds and kinds[-1][1] in ("player", "dino", "unknown") else after).append(g - t_drop)
    out["dot_after_destroy_s"] = {"n": len(after), "p50": pct(after, .5), "p95": pct(after, .95), "max": max(after, default=None)}
    out["ghost_after_destroy_s"] = {"n": len(ghosts), "p50": pct(ghosts, .5), "p95": pct(ghosts, .95), "max": max(ghosts, default=None)}
    out["vanished_without_destroy"] = len(vanished_without_destroy)
    out["vanished_then_back"] = len(reappeared)

    # ---- steadiness
    changes = {k: 0 for k in ("kind", "species", "name")}
    worst = []
    for eid, tl in timeline.items():
        for k in changes:
            n = max(len(tl[k]) - 1, 0)
            changes[k] += n
            if n >= 2:
                worst.append((eid, k, [v for _, v in tl[k]][:6]))
    out["changes_per_minute"] = {k: round(v / minutes, 2) for k, v in changes.items()}
    out["changes_total"] = changes
    out["multi_change_examples"] = worst[:6]
    out["headline_changes"] = max(len(headlines) - 1, 0)
    out["headline_values"] = [h for _, h in headlines][:12]

    by_id: dict[str, list] = defaultdict(list)
    for t, eid, text, anchor, rect in label_log:
        by_id[eid].append((t, text, anchor, rect))
    hops = reversals = toggles = n_labels = flipflops = 0
    paint_ts = sorted({t for t, *_ in label_log})
    painted_by = defaultdict(set)
    for t, eid, *_ in label_log:
        painted_by[eid].add(t)
    for eid, rows in by_id.items():
        last = {}
        vals: list[tuple] = []
        recent: list[tuple] = []  # (time, slot) of the last three paints
        for t, text, (ax, ay), (rx, ry, rw, rh) in rows:
            n_labels += 1
            slot = "below" if ry >= ay else "above" if ry + rh <= ay else ("right" if rx >= ax else "left")
            if "slot" in last and last["slot"] != slot and t - last["t"] < 1.0:
                hops += 1
            last = {"slot": slot, "t": t}
            recent = (recent + [(t, slot)])[-3:]
            if len(recent) == 3 and recent[0][1] == recent[2][1] != recent[1][1] and recent[2][0] - recent[0][0] < 2.0:
                flipflops += 1  # A -> B -> A within two seconds: the label flickered between two sides
            nums = tuple(int(x) for x in __import__("re").findall(r"(\d+)%", text))  # the percentages of the vitals line
            vals.append(nums)
        for a, b, c in zip(vals, vals[1:], vals[2:]):
            if a == c and a != b:
                reversals += 1
        ts = sorted(painted_by[eid])
        idx = {t: i for i, t in enumerate(paint_ts)}
        on = [idx[t] for t in ts]
        for a, b in zip(on, on[1:]):
            if b - a == 2:
                toggles += 1  # labelled, skipped exactly one paint, labelled again
    out["label"] = {"placed": n_labels, "slot_hops_per_min": round(hops / minutes, 2),
                    "slot_flipflops_per_min": round(flipflops / minutes, 2),
                    "text_reversals_per_min": round(reversals / minutes, 2), "on_off_toggles_per_min": round(toggles / minutes, 2)}

    # ---- decode time and the backlog a single worker would have
    ms = [m for _, m, _ in decode_ms]
    out["decode_ms"] = {"p50": pct(ms, .5), "p95": pct(ms, .95), "p99": pct(ms, .99), "max": max(ms, default=None)}
    free, backlog_max, wait_max, queue_max = 0.0, 0.0, 0.0, 0
    pending: list[float] = []  # completion times of queued items
    for arrival, cost, _n in decode_ms:
        start = max(arrival, free)
        wait_max = max(wait_max, start - arrival)
        free = start + cost / 1000.0
        pending = [c for c in pending if c > arrival]
        pending.append(free)
        queue_max = max(queue_max, len(pending))
    out["worker"] = {"max_wait_ms": round(wait_max * 1000.0, 1), "max_queue": queue_max,
                     "busy_share": round(sum(ms) / 1000.0 / duration, 4)}
    out["tick_ms"] = {"p50": pct([x * 1000 for x in t_tick], .5), "p95": pct([x * 1000 for x in t_tick], .95),
                      "max": max(t_tick) * 1000 if t_tick else None}
    out["paint_ms"] = {"p50": pct([x * 1000 for x in t_paint], .5), "p95": pct([x * 1000 for x in t_paint], .95),
                       "max": max(t_paint) * 1000 if t_paint else None}
    out["snapshot_ms"] = round(t_snapshot[0] * 1000.0, 3) if t_snapshot else None
    out["status_ms"] = round(t_status[0] * 1000.0, 3) if t_status else None
    return out


def _get(r: dict, path: str):
    for key in path.split("."):
        r = r.get(key) if isinstance(r, dict) else None
    return r


# (path, limit, "max" = must not exceed the limit). Limits are relative to the dot that stays on the newest sample
# ("hold") where the server's send interval sets the floor.
BUDGETS = (
    ("tick_exceptions", 0), ("errors.decode", 0), ("errors.pipeline", 0), ("errors.view", 0),
    ("stuck.frozen_dot_s", 1.0), ("stuck.drift_longest_run_s", 3.0), ("stuck.own_marker_frozen_s", 0.5),
    ("dot_after_destroy_s.max", 2.0), ("ghost_after_destroy_s.max", 13.0), ("vanished_then_back", 0),
    ("jumps_per_min", 10.0), ("own_jumps_per_min", 5.0),
    # regression guards from the recorded sessions (worst: a late-join session with sparse updates, ai .14 / .04, players .06 / .03)
    ("choppy.player.stall_frac", 0.09), ("choppy.player.spike_frac", 0.035),
    ("choppy.ai.stall_frac", 0.16), ("choppy.ai.spike_frac", 0.05),
    ("label.slot_hops_per_min", 3.0), ("label.slot_flipflops_per_min", 0.0), ("label.text_reversals_per_min", 1.0), ("label.on_off_toggles_per_min", 1.0),
    ("lag_ms.p50", 150.0), ("time_to_first_dot_s.p50", 0.5), ("time_to_first_dot_s.p95", 5.0),
    ("worker.max_wait_ms", 250.0), ("worker.max_queue", 200), ("worker.busy_share", 0.05),
    ("decode_ms.p99", 5.0), ("decode_ms.max", 50.0), ("tick_ms.p95", 2.0), ("paint_ms.p95", 16.0),
)


def check_budgets(r: dict) -> list[str]:
    """Violations of the stability budgets for one ``run`` result (empty: all met)."""
    bad = []
    for path, limit in BUDGETS:
        v = _get(r, path)
        if v is not None and v > limit:
            bad.append(f"{path} = {v:.3g} > {limit}")
    if r.get("multi_change_examples"):
        bad.append(f"an actor changed kind/species/name more than once: {r['multi_change_examples'][0]}")
    if (r.get("headline_changes") or 0) > max(1.2 * r["duration_s"] / 60.0, 1.0):
        bad.append(f"headline changed {r['headline_changes']} times in {r['duration_s']} s")
    # p95 may be 10 % over the dot that stays on the newest sample: the velocity blend trades a little error at stops for fluid motion
    for name, prefix, factor, count in (("dot error p50", "err_cm", 0.8, "lag_ms.n"), ("dot error p95", "err_cm", 1.1, "lag_ms.n"),
                                        ("own dot error p95", "own_err_cm", 0.8, "own_lag_ms.n")):
        key = "p50" if "p50" in name else "p95"
        mine, hold = _get(r, f"{prefix}.{key}"), _get(r, f"{prefix}.hold_{key}")
        if (_get(r, count) or 0) >= 200 and mine is not None and hold and mine > hold * factor:
            bad.append(f"{name} {mine:.0f} cm is not below {factor:.0%} of the hold dot's {hold:.0f} cm")
    return bad


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("pcap", nargs="+", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--seconds", type=float)
    ap.add_argument("--cache", type=Path, help="class_cache.json to start from (default: the shipped seed only)")
    ap.add_argument("--no-seed", action="store_true")
    ap.add_argument("--check", action="store_true", help="exit 1 when a stability budget (BUDGETS) is exceeded")
    args = ap.parse_args(argv)
    from isle_livemap.classcache import ClassCache

    results = []
    for path in args.pcap:
        cache = ClassCache.load(args.cache, seed={} if args.no_seed else None)
        cache.path = None  # replays never write the user's cache
        r = run(path, cache, args.seconds)
        r["budget_violations"] = check_budgets(r)
        results.append(r)
        print(json.dumps(r, indent=1, default=str))
    if args.json:
        args.json.write_text(json.dumps(results, indent=1, default=str))
    failed = [(r["pcap"], r["budget_violations"]) for r in results if r["budget_violations"]]
    for name, bad in failed:
        print(f"BUDGET VIOLATIONS in {name}:", *bad, sep="\n  ", file=sys.stderr)
    return 1 if (args.check and failed) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
