"""Check what the minimap says about every creature against what you saw in the game.

    .venv/bin/python -I tools/label_report.py SESSION.pcap                 # one row per creature seen in the capture
    .venv/bin/python -I tools/label_report.py SESSION.pcap --csv out.csv   # the same, for a spreadsheet
    .venv/bin/python -I tools/label_report.py SESSION.pcap --at 14:10:30   # exactly the labels on the map at that moment
    .venv/bin/python -I tools/label_report.py SESSION.pcap --at +125       # ... 125 s into the capture

The capture is replayed through the same code as the live app (GamePeerSource, and with --at the real MapView drawn
offscreen), so a row is what the minimap showed, not a second opinion. Columns of the table:

  handle   the game's net handle (the minimap shows only "Dino" for a creature whose species is unknown)
  kind     player / dino / ai (server animal, fish) / unknown
  species  what the label says; "~" = remembered from an earlier session (saved class cache or shipped seed), "?" = unknown
  source   table  the server's export tables of this connection named it (confirmed)
           asset  the dinosaur assets of its own creation records name it (confirmed by this connection's data)
           cache  the saved class cache named it by protocol id (inferred, "~")
           seed   the class seed shipped with the app named it (inferred, "~")
  name     the player name (only creatures that reference their PlayerState have one)
  first/last  seconds into the capture (and wall-clock when it can be derived) of the first / last time it was on the map
  placed   whether it was ever drawn: "no" = replicated to us but its position was never received
  HP/ST    last health / stamina read, as the label prints them (current/max in points; ~ before a maximum that is only the
           creation record's), and how old that reading was when the creature left the map
           ("-" = never read: the label has no second line); growth, hunger and thirst of others are not replicated to us
  dist/brg distance and compass bearing from you when it first appeared

Player names are the game's, shown so you can find the player; --hide-names removes them before you share a report.
The session log written by the app has the same facts as "labels:" lines (every 30 s and when they change).
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")  # the label font is a Windows one: no warning about it here
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAMPLE_S = 0.5  # how often the report looks at the map while replaying
FRAME_HZ = 20.0  # simulated frame rate of the --at replay (the live app runs 60; smoothing does not need more here)
PAINT_HZ = 4.0
SETTLE_S = 0.4


class SimClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ------------------------------------------------------------------ capture times
def pcap_span(pcap: Path) -> tuple[float, float]:
    """Epoch seconds of the first and the last UDP datagram of a capture (the origin of ``replay.pcap_datagrams``)."""
    from scapy.layers.inet import IP, UDP
    from scapy.utils import PcapReader

    first = last = None
    with PcapReader(str(pcap)) as reader:
        for p in reader:
            if UDP in p and IP in p and len(bytes(p[UDP].payload)) > 0:
                last = float(p.time)
                if first is None:
                    first = last
    if first is None:
        raise ValueError(f"{pcap}: no UDP datagrams")
    return first, last


_LOG_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ ")


def log_utc_offset_s(pcap: Path, first_epoch: float, last_epoch: float) -> float | None:
    """Offset of the machine that recorded the capture from UTC, read from the session log beside it: its first
    and last lines are local wall-clock times, the pcap has epoch times. Rounded to a quarter hour."""
    log = pcap.with_suffix(".log")
    try:
        lines = [m.group(1) for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()
                 if (m := _LOG_TIME.match(ln))]
    except OSError:
        return None
    if not lines:
        return None
    offsets = []
    for stamp, epoch in ((lines[0], first_epoch), (lines[-1], last_epoch)):
        local = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        offsets.append(local - epoch)
    off = sum(offsets) / len(offsets)
    return round(off / 900.0) * 900.0


def wall_clock(rel_s: float, first_epoch: float, offset_s: float) -> str:
    return (datetime.fromtimestamp(first_epoch + rel_s, timezone.utc) + timedelta(seconds=offset_s)).strftime("%H:%M:%S")


def resolve_at(spec: str, first_epoch: float, last_epoch: float, offset_s: float) -> float:
    """Seconds into the capture for ``--at``: "+125" / "125s" (relative) or "HH:MM:SS" / "HH:MM" (the recording
    machine's wall clock). A time outside the capture is an error that says what the capture covers."""
    spec = spec.strip()
    m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)s?", spec)
    if m and not re.fullmatch(r"\d{1,2}:\d{2}", spec):
        rel = float(m.group(1))
    else:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", spec)
        if not m:
            raise ValueError(f"--at {spec!r}: use HH:MM:SS, HH:MM, +SECONDS or SECONDSs")
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if hh > 23 or mm > 59 or ss > 59:
            raise ValueError(f"--at {spec!r}: not a time of day; use HH:MM:SS, HH:MM, +SECONDS or SECONDSs")
        start = datetime.fromtimestamp(first_epoch, timezone.utc) + timedelta(seconds=offset_s)
        best = None
        for day in (-1, 0, 1):
            cand = start.replace(hour=hh, minute=mm, second=ss, microsecond=0) + timedelta(days=day)
            rel_c = cand.timestamp() - offset_s - first_epoch  # the wall clock shown as UTC, back to a real epoch
            if -1.0 <= rel_c <= last_epoch - first_epoch + 1.0:
                best = rel_c
        if best is None:
            end = wall_clock(last_epoch - first_epoch, first_epoch, offset_s)
            raise ValueError(f"--at {spec}: the capture covers {wall_clock(0, first_epoch, offset_s)} to {end} "
                             f"(recording machine time, UTC{offset_s / 3600.0:+g})")
        rel = max(best, 0.0)
    if rel > last_epoch - first_epoch + 1.0:
        raise ValueError(f"--at {spec}: the capture is only {last_epoch - first_epoch:.0f} s long")
    return rel


# ------------------------------------------------------------------ the replay
def make_cache(path: Path | None, use_seed: bool):
    from isle_livemap.classcache import ClassCache, load_seed

    seed = load_seed() if use_seed else {}
    return ClassCache.load(path, seed=seed) if path is not None else ClassCache(None, seed=seed)


def collect(pcap: Path, cache=None, local_port: int | None = None, stop_s: float | None = None) -> dict:
    """Replay ``pcap`` and return {"rows": [...], "peers": GamePeerSource, "duration_s": ..., "events": [...]}."""
    from isle_livemap import species as sp
    from isle_livemap.capture import Datagram, TelemetryPipeline
    from isle_livemap.peers import GamePeerSource
    from isle_livemap.replay import pcap_datagrams

    clock = SimClock()
    pipe = TelemetryPipeline()
    peers = GamePeerSource(pipeline=pipe, clock=clock, class_cache=cache)
    peers.start()
    seen: dict[int, dict] = {}
    events: list[tuple[float, str]] = []
    next_sample = 0.0
    last_rel = 0.0

    def sample(rel: float) -> None:
        for r in peers.label_rows():
            row = seen.get(r["handle"])
            if row is None:
                row = seen[r["handle"]] = {"handle": r["handle"], "first_s": rel, "dist_first": r["distance_m"],
                                           "bearing_first": r["bearing"], "samples": 0}
            row.update(last_s=rel, kind=r["kind"], species=r["species"], species_tier=r["species_tier"], source=r["source"],
                       hp=r["hp"], st=r["st"], hp_text=r["hp_text"], st_text=r["st_text"], max_src=r["max_src"],
                       vitals_age_s=r["vitals_age_s"], on_map=True)
            if r["name"]:
                row["name"] = r["name"]
            row["samples"] += 1
        on = {r["handle"] for r in peers.label_rows()}
        for handle, row in seen.items():
            if handle not in on:
                row["on_map"] = False

    for d in pcap_datagrams(pcap, local_port):
        rel = d.observed_at
        if stop_s is not None and rel > stop_s:
            break
        clock.t = 1000.0 + rel
        pipe.handle(Datagram(clock.t, d.src_ip, d.src_port, d.dst_ip, d.dst_port, d.payload, d.direction))
        last_rel = rel
        if rel >= next_sample:
            next_sample = rel + SAMPLE_S
            sample(rel)
        for line in peers.drain_events():
            events.append((rel, line))
    sample(last_rel)
    for line in peers.drain_events():
        events.append((last_rel, line))

    # creatures the server replicated (a creation record was seen) that the map never drew
    for handle, info in list(peers.identity.actors.items()):
        if handle in seen or handle == peers.own_handle:
            continue
        kind, species, name = peers._identity(handle)
        if kind in (sp.KIND_OTHER, sp.KIND_UNKNOWN) and not species:
            continue
        source = peers.identity.species_source(handle) or ""
        seen[handle] = {"handle": handle, "kind": kind, "species": species, "source": source, "name": name or "",
                        "species_tier": "unknown" if not species else ("inferred" if source in ("cache", "seed") else "confirmed"),
                        "first_s": None, "last_s": None, "hp": None, "st": None, "vitals_age_s": None,
                        "dist_first": None, "bearing_first": None, "on_map": False, "samples": 0}
    for row in seen.values():
        row["placed"] = row["samples"] > 0
        row.setdefault("name", "")
    return {"rows": sorted(seen.values(), key=lambda r: (r["first_s"] is None, r["first_s"] or 0.0, r["handle"])),
            "peers": peers, "duration_s": last_rel, "events": events}


# ------------------------------------------------------------------ text
def fmt_species(r: dict) -> str:
    if not r["species"]:
        return "?"
    return r["species"] + ("~" if r["species_tier"] == "inferred" else "")


def fmt_time(rel: float | None, first_epoch: float | None, offset_s: float) -> str:
    if rel is None:
        return "-"
    return f"{rel:6.1f}s" + (f" {wall_clock(rel, first_epoch, offset_s)}" if first_epoch is not None else "")


def fmt_vitals(r: dict) -> tuple[str, str]:
    return r.get("hp_text") or "-", r.get("st_text") or "-"


def render_table(result: dict, first_epoch: float | None, offset_s: float, include_ai: bool, hide_names: bool) -> list[str]:
    rows = [r for r in result["rows"] if include_ai or r["kind"] != "ai"]
    head = ("handle", "kind", "species", "source", "name", "first", "last", "placed", "HP", "ST", "reading age", "dist", "brg")
    table = [head]
    for r in rows:
        hp, st = fmt_vitals(r)
        age = "-" if r["vitals_age_s"] is None or (hp in ("-", "\u2013") and st in ("-", "\u2013")) else f"{r['vitals_age_s']:.0f}s"
        dist = "-" if r["dist_first"] is None else f"{r['dist_first']:.0f} m"
        from isle_livemap.geo import compass_label

        brg = "-" if r["bearing_first"] is None else compass_label(r["bearing_first"])
        name = "" if not r["name"] else ("<name hidden>" if hide_names else r["name"])
        table.append((str(r["handle"]), r["kind"], fmt_species(r), r["source"] or "-", name or "-",
                      fmt_time(r["first_s"], first_epoch, offset_s), fmt_time(r["last_s"], first_epoch, offset_s),
                      "yes" if r["placed"] else "no", hp, st, age, dist, brg))
    widths = [max(len(row[i]) for row in table) for i in range(len(head))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip() for row in table]
    lines.insert(1, "  ".join("-" * w for w in widths))
    fish = sum(1 for r in result["rows"] if r["kind"] == "ai")
    creatures = [r for r in result["rows"] if r["kind"] != "ai"]
    by_source: dict[str, int] = {}
    for r in creatures:
        by_source[r["source"] or "none"] = by_source.get(r["source"] or "none", 0) + 1
    lines.append("")
    lines.append(f"{len(creatures)} creatures ({sum(1 for r in creatures if r['placed'])} drawn, "
                 f"{sum(1 for r in creatures if not r['placed'])} replicated but never placed) and {fish} server animals"
                 + ("" if include_ai else " (not listed; --all lists them)") + " in " + f"{result['duration_s']:.0f} s")
    lines.append("species sources: " + (", ".join(f"{k}={v}" for k, v in sorted(by_source.items())) or "none"))
    named = sum(1 for r in creatures if r["name"])
    lines.append(f"names linked: {named}; health known for {sum(1 for r in creatures if r['hp'] is not None)}")
    for rel, text in result["events"]:
        lines.append(f"event at +{rel:.0f}s: {text}")
    return lines


def write_csv(path: Path, result: dict, first_epoch: float | None, offset_s: float, hide_names: bool) -> None:
    cols = ("handle", "kind", "species", "species_tier", "source", "name", "first_s", "last_s", "first_clock", "last_clock",
            "placed", "on_map_at_end", "hp", "st", "hp_pct", "st_pct", "reading_age_s", "dist_m_first", "bearing_first")
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in result["rows"]:
            w.writerow([r["handle"], r["kind"], r["species"], r["species_tier"], r["source"],
                        "" if hide_names and r["name"] else r["name"],
                        "" if r["first_s"] is None else f"{r['first_s']:.1f}", "" if r["last_s"] is None else f"{r['last_s']:.1f}",
                        "" if r["first_s"] is None or first_epoch is None else wall_clock(r["first_s"], first_epoch, offset_s),
                        "" if r["last_s"] is None or first_epoch is None else wall_clock(r["last_s"], first_epoch, offset_s),
                        r["placed"], r["on_map"], r.get("hp_text", ""), r.get("st_text", ""),
                        "" if r["hp"] is None else f"{r['hp']:.1f}",
                        "" if r["st"] is None else f"{r['st']:.1f}",
                        "" if r["vitals_age_s"] is None else f"{r['vitals_age_s']:.1f}",
                        "" if r["dist_first"] is None else f"{r['dist_first']:.0f}",
                        "" if r["bearing_first"] is None else f"{r['bearing_first']:.0f}"])


# ------------------------------------------------------------------ --at
def labels_at(pcap: Path, at_s: float, cache=None, local_port: int | None = None, png: Path | None = None) -> dict:
    """What the minimap showed ``at_s`` seconds into the capture. With PySide6 the real MapView is drawn offscreen (label
    wording, priority, room and limits are then exactly the app's); without it the labels are built from the same
    geo functions but not placed. Returns {"dots": [...], "status": str, "rendered": bool, ...}."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return _labels_at_without_qt(pcap, at_s, cache, local_port)

    from unittest import mock

    import isle_livemap.overlay as overlay
    from isle_livemap.capture import Datagram, TelemetryPipeline
    from isle_livemap.geo import MarkerStore, Settings
    from isle_livemap.overlay import MapView
    from isle_livemap.peers import GamePeerSource
    from isle_livemap.replay import pcap_datagrams

    QApplication.instance() or QApplication([])
    clock = SimClock()
    tmp = Path(tempfile.mkdtemp(prefix="labelrep_"))
    shown: dict[str, list[str]] = {}
    cur = {"id": None, "lines": []}
    recording = {"on": False}
    drawn: set[str] = set()  # peers whose dot is inside the minimap circle in the recorded frame (the rest are edge arrows)
    real_lines, real_place = overlay.peer_label_lines, overlay.place_label

    def label_lines(n, *a, **k):
        lines = real_lines(n, *a, **k)
        cur["id"], cur["lines"] = n.entity.id, lines
        return lines

    def place(ax, ay, w, h, placed, bounds, accept=None, *more, **kw):
        r = real_place(ax, ay, w, h, placed, bounds, accept, *more, **kw)
        if recording["on"] and r is not None and cur["id"] is not None:
            shown[cur["id"]] = list(cur["lines"])
        return r

    with mock.patch.object(time, "monotonic", clock), mock.patch.object(overlay, "peer_label_lines", label_lines), \
            mock.patch.object(overlay, "place_label", place):
        pipe = TelemetryPipeline()
        peers = GamePeerSource(pipeline=pipe, clock=clock, class_cache=cache)
        peers.start()
        view = MapView(pipe, MarkerStore(tmp / "markers.json"), Settings(), peers)
        view.timer.stop()
        view._last = clock.t
        real_marker = view._draw_entity_marker

        def marker(p, n, pt, *more):
            if recording["on"]:
                drawn.add(n.entity.id)
            return real_marker(p, n, pt, *more)

        view._draw_entity_marker = marker
        next_paint = 0.0
        dt = 1.0 / FRAME_HZ

        def advance(to: float) -> None:
            nonlocal next_paint
            while clock.t + dt <= to:
                clock.t += dt
                view.tick()
                if clock.t >= next_paint:
                    next_paint = clock.t + 1.0 / PAINT_HZ
                    view.grab()
            clock.t = max(clock.t, to)

        for d in pcap_datagrams(pcap, local_port):
            if d.observed_at > at_s:
                break
            advance(1000.0 + d.observed_at)
            pipe.handle(Datagram(clock.t, d.src_ip, d.src_port, d.dst_ip, d.dst_port, d.payload, d.direction))
        advance(clock.t + SETTLE_S)
        view.tick()
        recording["on"] = True
        image = view.grab()
        recording["on"] = False
        if png is not None:
            image.save(str(png))
        rows = {str(r["handle"]): r for r in peers.label_rows()}
        dots = []
        for n in view.nearby(None if view.full_map else view.settings.peer_radius_m):
            e = n.entity
            dots.append({"id": e.id, "kind": e.kind, "row": rows.get(e.id), "distance_m": n.distance_m, "bearing": n.bearing,
                         "lines": shown.get(e.id), "stale": n.stale, "inside": e.id in drawn})
        return {"dots": dots, "status": view.peer_status, "rendered": True, "own": pipe.latest.position,
                "radius_m": view.settings.peer_radius_m, "hidden": peers.hidden_creatures(), "errors": view.errors,
                "peers": peers}


def _labels_at_without_qt(pcap: Path, at_s: float, cache, local_port) -> dict:
    from isle_livemap.geo import Settings, label_limits, peer_label_lines, priority_key, visible_entities

    result = collect(pcap, cache, local_port, stop_s=at_s)
    peers = result["peers"]
    own = peers._own_position()
    settings = Settings()
    rows = {str(r["handle"]): r for r in peers.label_rows()}
    items = [(p.entity, p.entity.x, p.entity.y, 0.0, None if p.entity.vitals_t is None else max(peers._clock() - p.entity.vitals_t, 0.0))
             for p in peers.store.entities()]
    near = visible_entities(items, own[0], own[1], settings.peer_radius_m) if own else []
    caps, ai_cap = label_limits(len(near), sum(1 for n in near if n.entity.kind == "ai"), False)
    total = sum(caps[:3]) + ai_cap
    dots = []
    for i, n in enumerate(sorted(near, key=priority_key)):
        dots.append({"id": n.entity.id, "kind": n.entity.kind, "row": rows.get(n.entity.id), "distance_m": n.distance_m,
                     "bearing": n.bearing, "lines": peer_label_lines(n) if i < total else None, "stale": False,
                     "inside": None})
    return {"dots": dots, "status": peers.status(), "rendered": False, "own": own, "radius_m": settings.peer_radius_m,
            "hidden": peers.hidden_creatures(), "errors": 0, "peers": peers}


def render_at(result: dict, at_s: float, first_epoch: float | None, offset_s: float, include_ai: bool, hide_names: bool) -> list[str]:
    from isle_livemap.geo import compass_label

    clock = f" ({wall_clock(at_s, first_epoch, offset_s)})" if first_epoch is not None else ""
    lines = [f"Minimap at +{at_s:.1f} s{clock}: status line: {result['status'] or '(none)'}"]
    if not result["rendered"]:
        lines.append("PySide6 is not installed here: the labels below are built by the app's label functions but not placed "
                     "on the map, so room limits can hide some that the real minimap would not show.")
    if result["own"] is None:
        lines.append("No position fix of your own at this moment: the real minimap shows nothing around you.")
    dots = [d for d in result["dots"] if include_ai or d["kind"] != "ai"]
    lines.append(f"{len(result['dots'])} on the map within {result['radius_m']:.0f} m "
                 f"({sum(1 for d in result['dots'] if d['kind'] == 'ai')} server animals"
                 + ("" if include_ai else ", not listed; --all lists them") + f"); {result['hidden']} replicated but not placeable")
    for d in dots:
        row = d["row"] or {}
        sp = fmt_species({"species": row.get("species", ""), "species_tier": row.get("species_tier", "unknown")})
        name = row.get("name", "")
        name = "" if not name else ("<name hidden>" if hide_names else name)
        src = row.get("source") or "-"
        if d["lines"]:
            label = " / ".join(d["lines"])
            if hide_names and row.get("name"):
                label = label.replace(row["name"], "<name hidden>")
        elif d.get("inside") is False:
            label = "(outside the minimap circle: only an edge arrow, no label)"
        else:
            label = "(no label: no room, or over the label limit)"
        if hide_names and row.get("name") and d["lines"]:
            label = label.replace(row["name"][:16], "<name hidden>")
        lines.append(f"  #{d['id']:<7} {d['kind']:<7} {sp:<16} src={src:<6} {d['distance_m']:6.0f} m {compass_label(d['bearing']):<3}"
                     f"{' (dim)' if d['stale'] else ''}  {('name=' + name) if name else ''}")
        lines.append(f"            label: {label}")
    if result.get("errors"):
        lines.append(f"{result['errors']} frame error(s) while drawing")
    return lines


# ------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="What the minimap says about each creature in a recorded session.")
    ap.add_argument("pcap", type=Path, help="session_*.pcap (the session_*.log beside it gives the clock of the recording machine)")
    ap.add_argument("--at", metavar="TIME", help="the labels on the map at HH:MM:SS (recording machine clock), +SECONDS or SECONDSs")
    ap.add_argument("--csv", type=Path, metavar="FILE", help="write the per-creature table as CSV")
    ap.add_argument("--png", type=Path, metavar="FILE", help="with --at: save the drawn minimap as an image")
    ap.add_argument("--all", action="store_true", help="include server animals (fish)")
    ap.add_argument("--hide-names", action="store_true", help="replace player names (to share a report)")
    ap.add_argument("--cache", type=Path, metavar="class_cache.json", help="saved class cache to use (default: none; the seed is used)")
    ap.add_argument("--no-seed", action="store_true", help="do not use the class seed shipped with the app")
    ap.add_argument("--local-port", type=int, help="the game's local UDP port (default: inferred)")
    ap.add_argument("--tz-offset", type=float, metavar="HOURS", help="UTC offset of the recording machine (default: from the log)")
    return ap


def main(argv: list[str] | None = None, out=print) -> int:
    args = build_parser().parse_args(argv)
    if not args.pcap.is_file():
        out(f"{args.pcap}: no such file")
        return 2
    cache = make_cache(args.cache, not args.no_seed)
    try:
        first_epoch, last_epoch = pcap_span(args.pcap)
    except Exception as err:  # noqa: BLE001 - a bad file is a message, not a traceback
        out(f"{args.pcap}: {err}")
        return 2
    if args.tz_offset is not None:
        offset_s = args.tz_offset * 3600.0
    else:
        found = log_utc_offset_s(args.pcap, first_epoch, last_epoch)
        offset_s = found if found is not None else -time.altzone if time.localtime(first_epoch).tm_isdst > 0 else -time.timezone
    out(f"{args.pcap.name}: {datetime.fromtimestamp(first_epoch, timezone.utc) + timedelta(seconds=offset_s):%Y-%m-%d %H:%M:%S} "
        f"to {wall_clock(last_epoch - first_epoch, first_epoch, offset_s)} (UTC{offset_s / 3600.0:+g}), "
        f"{last_epoch - first_epoch:.0f} s")
    if args.at:
        try:
            at_s = resolve_at(args.at, first_epoch, last_epoch, offset_s)
        except ValueError as err:
            out(str(err))
            return 2
        for line in render_at(labels_at(args.pcap, at_s, cache, args.local_port, args.png), at_s, first_epoch, offset_s, args.all, args.hide_names):
            out(line)
        return 0
    result = collect(args.pcap, cache, args.local_port)
    for line in render_table(result, first_epoch, offset_s, args.all, args.hide_names):
        out(line)
    if args.csv:
        write_csv(args.csv, result, first_epoch, offset_s, args.hide_names)
        out(f"wrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
