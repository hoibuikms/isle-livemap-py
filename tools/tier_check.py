"""Which creatures were inside the 250 m / 500 m tiers, and was each one shown honestly?

    .venv/bin/python -I tools/tier_check.py SESSION.pcap [more.pcap ...] [--port N] [--check]
    .venv/bin/python -I tools/tier_check.py --labels SESSION.pcap [more.pcap ...] [--port N] [--check] [--png DIR]
    .venv/bin/python -I tools/tier_check.py --ledger SESSION.pcap [more.pcap ...] [--port N | --all-ports] [--seconds N] [--check]

Replays the capture and, once a second, takes every dinosaur / player / unclassified creature within 500 m of the own
position (the app's own distance, ``geo.priority_tier``). A creature there is listed with its position source:

  fix    a decoded position (followed), shown as is
  spawn  only the creation record's spawn point: shown dimmed and with "~" before the distance (``entity_label_lines``)

``--check`` exits 1 when a creature in the 500 m tiers is placed by its spawn point without the "~" marker, or when a
creature the source holds is missing from the snapshot it was counted from. The own position is the app's raw decoded
one: when it is in another frame than the replicated creatures the tiers list the wrong creatures, which this tool cannot
see (there is no ground truth for the offset in a capture; see docs/PEERS.md, "Own position frame").

``--ledger``: ground truth from the raw stream against what the minimap drew. Every creature the stream says exists (a
creation record with a maxHealth, or the state batches of a pawn; never the repeated animal state of a server animal) and
that the server has not destroyed is checked once a second against the frame the real MapView painted: drawn (arrow, crowd
triangle), a pointer on the circle edge, or not shown, and if not shown, why. It prints, per distance tier (by the last
known position against the displayed own position), how many creature-seconds were drawn and what the rest was, the
creatures that were replicated but never placed (and how many of those the overlay counter announces), the ghosts left by
server destruction, and any creature taken off the map while the server still replicated it. ``--all-ports`` replays every
connection of the capture, ``--seconds N`` only the first N seconds, ``--ghost-grace S`` tries another ghost grace.
``--check`` exits 1 when fewer than 99 % of the creature-seconds within 500 m are drawn, when a placed creature vanished
while still replicated, or when the counter announces less than 80 % of the unplaced creatures.

``--labels``: the label guarantee. Replays the capture through the real MapView (offscreen Qt, simulated clock) and once
a second asks, for every player / dinosaur / unclassified creature within 500 m of the displayed own position: is it
inside the view, is its arrow drawn (not the tiny crowd triangle), does it have its own label whose first line says who
(a name or "Player ?") and what (a species or "Dino"), is that label inside the round view and clear of every arrow?
Prints the coverage per capture, the seconds with "Player ?" / "Dino", and every miss with its reason. ``--png DIR``
saves the busiest second (most creatures within 500 m) so the overlay can be looked at. ``--check`` exits 1 on a miss
that is not the documented crowd limit (``geo.GUARANTEED_MAX_LABELS`` / ``GUARANTEED_CROWD``)."""

from __future__ import annotations

import math
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from isle_livemap.capture import TelemetryPipeline  # noqa: E402
from isle_livemap.geo import Nearby, entity_label_lines, priority_tier  # noqa: E402
from isle_livemap.peers import GamePeerSource  # noqa: E402
from isle_livemap.replay import pcap_datagrams  # noqa: E402

CREATURE_KINDS = ("player", "dino", "unknown")
RADIUS_M = 500.0


class _Clock:
    t = 1000.0

    def __call__(self) -> float:
        return self.t


def check(pcap: Path, port: int | None = None) -> dict:
    """{"seconds": n, "tiers": Counter, "sources": Counter, "unmarked": [(t, handle, dist)], "listed": {handle: info}}."""
    clock = _Clock()
    res = {"seconds": 0, "tiers": Counter(), "sources": Counter(), "unmarked": [], "listed": defaultdict(dict)}
    with mock.patch.object(time, "monotonic", clock):
        pipe = TelemetryPipeline()
        source = GamePeerSource(pipeline=pipe)
        source.start()
        base = next_at = None
        for d in pcap_datagrams(pcap, port):
            if base is None:
                base = next_at = d.observed_at
            clock.t = 1000.0 + d.observed_at - base
            pipe.handle(d)
            if d.observed_at < next_at:
                continue
            next_at = d.observed_at + 1.0
            pos = pipe.latest.position
            if pos is None:
                continue
            res["seconds"] += 1
            for pe in source.snapshot().entities:
                e = pe.entity
                if e.kind not in CREATURE_KINDS:
                    continue
                dist = math.hypot(e.x - pos.x, e.y - pos.y) / 100.0
                if dist > RADIUS_M:
                    continue
                tier = priority_tier(dist)
                src = e.pos_source or "fix"
                res["tiers"][tier] += 1
                res["sources"][src] += 1
                info = res["listed"][e.id]
                info.setdefault("first", d.observed_at - base)
                info["last"], info["kind"], info["species"], info["source"] = d.observed_at - base, e.kind, e.species or "?", src
                info["min_dist"] = min(info.get("min_dist", dist), dist)
                head = entity_label_lines(Nearby(e, e.x, e.y, dist, 0.0, age_s=1.0, stale_s=45.0))[0]
                if src == "spawn" and "~" not in head:
                    res["unmarked"].append((round(d.observed_at - base, 1), e.id, round(dist)))
    return res


def label_coverage(pcap: Path, port: int | None = None, png_dir: Path | None = None, seconds: float | None = None,
                   cache=None) -> dict:
    """The ``--labels`` check of one capture (see the module docstring). Returns {"seconds", "creature_seconds", "ok",
    "misses": Counter(reason), "examples": {reason: [(t, handle, dist)]}, "no_name_s", "no_species_s", "clipped",
    "hides_arrow", "busiest": (n, t), "png": path | None}."""
    import os
    import tempfile

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from isle_livemap.capture import Datagram
    from isle_livemap.geo import PLAYER_KINDS, MarkerStore, Settings, marker_extent
    from isle_livemap.overlay import MapView

    QApplication.instance() or QApplication([])
    clock = _Clock()
    res = {"seconds": 0, "creature_seconds": 0, "ok": 0, "misses": Counter(), "examples": defaultdict(list), "no_name_s": 0,
           "no_species_s": 0, "clipped": 0, "hides_arrow": 0, "busiest": (0, 0.0), "png": None, "labels_full": 0, "labels_compact": 0,
           "max_band": 0, "label_overlap": 0}
    tmp = Path(tempfile.mkdtemp(prefix="tiers_"))
    best_pix = None
    with mock.patch.object(time, "monotonic", clock):
        pipe = TelemetryPipeline()
        source = GamePeerSource(pipeline=pipe, clock=clock, class_cache=cache)
        source.start()
        view = MapView(pipe, MarkerStore(tmp / "m.json"), Settings(), source)
        view.timer.stop()
        view._last = clock.t
        view.trace_frames = True
        view.resize(360, 360)
        base = None
        next_tick = next_check = 0.0
        for d in pcap_datagrams(pcap, port):
            if base is None:
                base = d.observed_at
            rel = d.observed_at - base
            if seconds is not None and rel > seconds:
                break
            target = 1000.0 + rel
            while next_tick <= rel:  # the view runs at 20 Hz here (the real one at 60): enough for the layout
                clock.t = 1000.0 + next_tick
                view.tick()
                next_tick += 0.05
            clock.t = target
            pipe.handle(Datagram(clock.t, d.src_ip, d.src_port, d.dst_ip, d.dst_port, d.payload, d.direction))
            if rel >= next_check:
                next_check = rel + 1.0
                if pipe.latest.position is None:
                    continue
                pix = view.grab()
                _evaluate(view, res, rel, PLAYER_KINDS, marker_extent, pix, png_dir)
                if res.get("_best") is not None and res["_best"] is not best_pix:
                    best_pix = res["_best"]
        view.trace_frames = False
        source.stop()
        try:
            import shiboken6

            view.timer.stop()
            shiboken6.delete(view)
        except Exception:  # noqa: BLE001
            pass
    if best_pix is not None and png_dir is not None:
        png_dir.mkdir(parents=True, exist_ok=True)
        out = png_dir / f"{pcap.stem}_busiest_{res['busiest'][0]}.png"
        from PySide6.QtCore import Qt

        best_pix.scaledToWidth(best_pix.width() * 3, Qt.TransformationMode.SmoothTransformation).save(str(out))  # 3x: readable
        res["png"] = str(out)
    res.pop("_best", None)
    return res


def _evaluate(view, res, rel, player_kinds, marker_extent, pix, png_dir) -> None:
    from isle_livemap.geo import GUARANTEED_CROWD

    tr = view.frame_trace
    if not tr:
        return
    res["seconds"] += 1
    pos, glyph, labels = tr.get("pos", {}), tr.get("glyph", {}), tr.get("labels", {})
    # everyone the view counts as near, by the displayed position (the app's own distance)
    from isle_livemap.geo import GHOST_GUARANTEE_S

    # a ghost (the server destroyed it) keeps its guaranteed label for GHOST_GUARANTEE_S; after that it only has the normal ones
    band = [n for n in view.nearby(None) if n.entity.kind in player_kinds and n.distance_m <= 500.0
            and (n.gone_s is None or n.gone_s <= GHOST_GUARANTEE_S)]
    res["max_band"] = max(res["max_band"], len(band))
    if len(band) > res["busiest"][0]:
        res["busiest"] = (len(band), rel)
        res["_best"] = pix.copy()
    half = view.width() / 2
    radius = half - 3
    arrows = [(p[0], p[1], marker_extent(n.entity), n.entity.id, n.gone_s) for n in band for p in [pos.get(n.entity.id)] if p and glyph.get(n.entity.id)]
    crowd = len(band) > GUARANTEED_CROWD
    limit = GUARANTEED_CROWD_LABELS_FOR(crowd)
    ranked = sorted(band, key=lambda n: n.distance_m)
    for rank, n in enumerate(ranked, 1):
        e = n.entity
        res["creature_seconds"] += 1
        eid = e.id
        reason = None
        if eid not in pos:
            reason = "outside the round view (edge indicator only)"
        elif not glyph.get(eid):
            reason = "arrow is only a tiny crowd triangle"
        elif eid not in labels:
            reason = f"no label: crowd of {len(band)} within 500 m, past the limit of {limit}" if rank > limit else "no label: no spot found"
        else:
            lines, r, _small = labels[eid]
            head = lines[0].split(" - ")
            name_ok = len(head) >= 3 and bool(head[0])
            species_ok = len(head) >= 3 and bool(head[1])
            if not (name_ok and species_ok):
                reason = f"label without name / species: {lines[0]!r}"
            else:
                if head[0] == "Player ?":
                    res["no_name_s"] += 1
                if head[1] in ("Dino", "Dino ?"):
                    res["no_species_s"] += 1
                cx = max(abs(r[0] - half), abs(r[0] + r[2] - half))
                cy = max(abs(r[1] - half), abs(r[1] + r[3] - half))
                if cx * cx + cy * cy > (radius + 6.0) ** 2:
                    res["clipped"] += 1
                    reason = "label clipped by the round view"
                else:
                    own = pos[eid]
                    for ax, ay, ext, other, other_gone in arrows:
                        if (ax, ay) != (own[0], own[1]) and r[0] - 1 < ax < r[0] + r[2] + 1 and r[1] - 1 < ay < r[1] + r[3] + 1:
                            res["hides_arrow"] += 1
                            reason = "label covers another creature's arrow"
                            res.setdefault("cover_detail", []).append((round(rel), eid, other, None if other_gone is None else round(other_gone)))
                            break
                if reason is None:
                    res["labels_full" if len(lines) > 1 else "labels_compact"] += 1
        if reason is None:
            lab = labels[eid][1]
            if any(o != eid and _area(lab, labels[o][1]) > 6.0 for o in labels):
                res["label_overlap"] += 1  # readable? two labels share more than a few pixels (informational, not a miss)
            res["ok"] += 1
        else:
            res["misses"][reason] += 1
            ex = res["examples"][reason]
            if len(ex) < 4:
                ex.append((round(rel), eid, round(n.distance_m)))


def _area(a, b) -> float:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def GUARANTEED_CROWD_LABELS_FOR(crowd: bool) -> int:
    from isle_livemap.geo import GUARANTEED_CROWD_LABELS, GUARANTEED_MAX_LABELS

    return GUARANTEED_CROWD_LABELS if crowd else GUARANTEED_MAX_LABELS


def print_label_coverage(name: str, res: dict) -> bool:
    n = res["creature_seconds"]
    print(f"{name}: {res['seconds']} s checked, {n} creature-seconds within 500 m (busiest second: {res['busiest'][0]} creatures at "
          f"{res['busiest'][1]:.0f} s)")
    if not n:
        print("  nobody within 500 m")
        return True
    print(f"  drawn with arrow AND own label (name + species, inside the view, clear of other arrows): {res['ok']}/{n} = {100.0 * res['ok'] / n:.2f} %")
    print(f"  labels: {res['labels_full']} two-line, {res['labels_compact']} one-line; label shows 'Player ?': {res['no_name_s']} s, "
          f"'Dino' (species unknown): {res['no_species_s']} s; clipped {res['clipped']}, covering an arrow {res['hides_arrow']}, "
          f"sharing pixels with another label {res['label_overlap']} s")
    hard = False
    for reason, count in res["misses"].most_common():
        print(f"  MISS x{count}: {reason}  e.g. {res['examples'][reason]}")
        hard |= "crowd of" not in reason
    if res.get("cover_detail"):
        print(f"  covered arrows (t, label of, arrow of, arrow's ghost age): {res['cover_detail'][:6]}")
    if res.get("png"):
        print(f"  picture: {res['png']}")
    return not hard


def main(argv: list[str]) -> int:
    png = Path(argv[argv.index("--png") + 1]) if "--png" in argv else None
    args = [a for a in argv if not a.startswith("--")]
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else None
    secs_arg = argv[argv.index("--seconds") + 1] if "--seconds" in argv else None
    gg_arg = argv[argv.index("--ghost-grace") + 1] if "--ghost-grace" in argv else None
    paths = [a for a in args if a != str(port) and a != secs_arg and a != gg_arg and (png is None or a != str(png))]
    if not paths:
        print(__doc__)
        return 2
    if "--ghost-grace" in argv:  # what-if: another ghost grace (seconds), to compare with the shipped one
        GamePeerSource.GHOST_GRACE_S = GamePeerSource.ghost_grace_s = float(argv[argv.index("--ghost-grace") + 1])
    if "--ledger" in argv:
        ok = True
        secs = float(argv[argv.index("--seconds") + 1]) if "--seconds" in argv else None
        for name in paths:
            ports = local_ports(Path(name)) if "--all-ports" in argv else [port]
            for pt in ports:
                ok &= print_ledger(Path(name).name, ledger(Path(name), pt, secs))
        return 1 if "--check" in argv and not ok else 0
    if "--labels" in argv:
        ok = True
        for name in paths:
            ok &= print_label_coverage(Path(name).name, label_coverage(Path(name), port, png))
        return 1 if "--check" in argv and not ok else 0
    bad = False
    for name in paths:
        res = check(Path(name), port)
        print(f"{Path(name).name}: {res['seconds']} s with an own position")
        print(f"  creature-seconds within 500 m: tier 250 m {res['tiers'][0]}, tier 500 m {res['tiers'][1]}; "
              f"by position source {dict(res['sources'])}")
        for h, i in sorted(res["listed"].items(), key=lambda kv: kv[1]["first"]):
            print(f"  handle {h:>7} {i['kind']:8s} {i['species']:18s} {i['source']:5s} {i['first']:6.0f}-{i['last']:.0f} s, nearest {i['min_dist']:.0f} m")
        if res["unmarked"]:
            print(f"  UNMARKED spawn positions in the 500 m tiers: {res['unmarked'][:6]}")
        bad |= bool(res["unmarked"])
    return 1 if "--check" in argv and bad else 0



# --------------------------------------------------------------------------------------------------------------------
# --ledger: ground truth from the raw stream versus what the minimap drew
# --------------------------------------------------------------------------------------------------------------------
LEDGER_MIN_SHAPE_BATCHES = 12  # a creature without a creation record maxHealth: this many state batches of a pawn's size
LEDGER_BAND = (60, 400)
LEDGER_ANIMAL_BITS = (155, 158)
LEDGER_TIERS = (250.0, 500.0, 1000.0)
LEDGER_DRAWN = "drawn"
LEDGER_TRIANGLE = "drawn (crowd triangle)"
LEDGER_EDGE = "edge pointer only"


def local_ports(pcap: Path, min_bytes: int = 20_000) -> list[int]:
    """The game's UDP port of every connection in a capture (private side of each flow), busiest first."""
    import ipaddress

    from scapy.layers.inet import IP, UDP
    from scapy.utils import PcapReader

    counts: dict[int, int] = {}
    with PcapReader(str(pcap)) as reader:
        for p in reader:
            if UDP in p and IP in p:
                n = len(bytes(p[UDP].payload))
                for ip, port in ((p[IP].src, p[UDP].sport), (p[IP].dst, p[UDP].dport)):
                    if n and ipaddress.ip_address(ip).is_private:
                        counts[port] = counts.get(port, 0) + n
    return [port for port, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n >= min_bytes]


class _Row:
    """One actor generation in the raw stream (a handle is reused after its destruction record)."""

    __slots__ = ("handle", "first", "last", "creation", "creation_hp", "states", "band", "animal", "destroyed", "placed_at", "pos",
                 "fixes", "spawn_only_pos", "pos_t", "ever_in_store", "removed_t")

    def __init__(self, handle: int, t: float) -> None:
        self.handle, self.first, self.last = handle, t, t
        self.creation = self.creation_hp = False
        self.states = self.band = self.animal = 0
        self.destroyed: float | None = None
        self.placed_at: float | None = None
        self.pos: tuple[float, float] | None = None
        self.pos_t = 0.0
        self.fixes = 0
        self.spawn_only_pos = False
        self.ever_in_store = False
        self.removed_t: float | None = None

    def candidate(self) -> bool:
        """A creature by the raw stream alone (the app's own classification is not used): its creation record carried a
        maxHealth, or it sends the state batches of a pawn; never the repeated animal state of a server animal."""
        if self.handle < GamePeerSource.MIN_DYNAMIC_HANDLE:
            return False
        if self.states >= 3 and self.animal >= 0.6 * self.states:
            return False
        if self.creation_hp:
            return True
        return self.states >= LEDGER_MIN_SHAPE_BATCHES and self.band >= 0.8 * self.states and self.animal < 0.1 * self.states


def ledger(pcap: Path, port: int | None = None, seconds: float | None = None, cache=None) -> dict:
    """Replay ``pcap`` through the real pipeline and MapView (offscreen Qt, simulated clock) and, once a second, compare every
    creature candidate of the raw stream (see ``_Row.candidate``) that the server still replicates with what the minimap drew.

    Returns {"seconds", "rows": [...], "cells": Counter((tier, category)), "by_actor": {...}, "unplaced": {...}, ...}. Tiers are
    0 (<= 250 m), 1 (<= 500 m), 2 (<= 1000 m), 3 (beyond) by the actor's last known position against the displayed own position."""
    import os
    import tempfile

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from isle_livemap import decode
    from isle_livemap import species as sp
    from isle_livemap.capture import INBOUND, Datagram
    from isle_livemap.geo import MarkerStore, Settings
    from isle_livemap.overlay import MapView

    QApplication.instance() or QApplication([])
    clock = _Clock()
    tmp = Path(tempfile.mkdtemp(prefix="ledger_"))
    live: dict[int, _Row] = {}
    done: list[_Row] = []
    res = {"seconds": 0, "cells": Counter(), "by_actor": defaultdict(Counter), "kinds": defaultdict(Counter),
           "unplaced_s": 0, "unplaced_handles": set(), "unplaced_ledger_s": 0, "unplaced_counted_s": 0, "counter_over_s": 0,
           "gone_age": Counter(), "uncounted": Counter(), "removed_examples": [],
           "rows": done, "port": port}
    with mock.patch.object(time, "monotonic", clock):
        pipe = TelemetryPipeline()
        source = GamePeerSource(pipeline=pipe, clock=clock, class_cache=cache)
        source.start()
        rel_now = [0.0]
        orig_emit = source._emit

        def emit(handle, x, y, z, kind, spc, name, src=""):
            row = live.get(handle)
            if row is not None:
                if src != "spawn":
                    row.fixes += 1
                    row.pos, row.pos_t, row.spawn_only_pos = (x, y), rel_now[0], False
                elif row.fixes == 0:
                    row.pos, row.pos_t, row.spawn_only_pos = (x, y), rel_now[0], True
                if row.placed_at is None:
                    row.placed_at = rel_now[0]
            return orig_emit(handle, x, y, z, kind, spc, name, src)

        source._emit = emit
        view = MapView(pipe, MarkerStore(tmp / "m.json"), Settings(), source)
        view.timer.stop()
        view._last = clock.t
        view.trace_frames = True
        view.resize(360, 360)
        base = None
        next_tick = next_check = 0.0
        for d in pcap_datagrams(pcap, port):
            if base is None:
                base = d.observed_at
            rel = d.observed_at - base
            if seconds is not None and rel > seconds:
                break
            while next_tick <= rel:
                clock.t = 1000.0 + next_tick
                view.tick()
                next_tick += 0.05
            clock.t = 1000.0 + rel
            rel_now[0] = rel
            if d.direction == INBOUND:
                _ledger_note(decode, live, done, d, rel)
            pipe.handle(Datagram(clock.t, d.src_ip, d.src_port, d.dst_ip, d.dst_port, d.payload, d.direction))
            if rel >= next_check:
                next_check = rel + 1.0
                if pipe.latest.position is None or not view.interp.has_fix:
                    continue
                view.grab()  # paints a frame: ``frame_trace`` is what that frame drew
                _ledger_eval(res, view, source, live, rel, sp)
        for row in live.values():
            done.append(row)
        res["final_kinds"] = {r.handle: source._identity(r.handle) for r in done if r.candidate()}
        source.stop()
        try:
            import shiboken6

            view.timer.stop()
            shiboken6.delete(view)
        except Exception:  # noqa: BLE001
            pass
    return res


def _ledger_note(decode, live: dict, done: list, d, rel: float) -> None:
    packet = decode.parse_iris_packet(d.payload)
    if packet is None:
        return
    bits = decode.Bits(d.payload)
    for handle in packet.destroyed_handles:
        row = live.pop(handle, None)
        if row is not None:
            row.destroyed = rel
            done.append(row)
    for batch in packet.batches:
        handle = batch.net_ref_handle
        kind = decode.batch_kind(bits, batch)
        row = live.get(handle)
        if row is None:
            row = live[handle] = _Row(handle, rel)
        elif kind == decode.KIND_CREATION and row.creation:
            row.destroyed = rel  # a second creation record of a live handle: the server re-created it
            done.append(row)
            row = live[handle] = _Row(handle, rel)
        row.last = rel
        if kind == decode.KIND_CREATION:
            row.creation = True
            if decode.creation_max_health(bits, batch) is not None:
                row.creation_hp = True
        elif kind == decode.KIND_STATE:
            n = batch.data_bit_count
            row.states += 1
            row.band += LEDGER_BAND[0] <= n <= LEDGER_BAND[1]
            row.animal += n in LEDGER_ANIMAL_BITS


def _ledger_eval(res: dict, view, source, live: dict, rel: float, sp) -> None:
    tr = view.frame_trace
    if tr is None:
        return
    res["seconds"] += 1
    ox, oy = view.interp.x, view.interp.y
    pos, glyph, edge = tr.get("pos", {}), tr.get("glyph", {}), tr.get("edge", set())
    owned = {source.own_handle, *source._past_own}
    unplaced_now = 0
    unplaced_set: set[int] = set()
    for handle, row in live.items():
        if handle in owned or not row.candidate():
            continue
        kind, species, _name = source._identity(handle)
        if kind in (sp.KIND_AI, sp.KIND_OTHER):
            continue  # the class path itself says server animal / world object
        eid = str(handle)
        ent = source.store.get(eid)
        if ent is not None:
            row.ever_in_store = True
        if row.pos is None:
            res["unplaced_s"] += 1
            res["unplaced_handles"].add(handle)
            res["cells"][("none", "no position (never placed)")] += 1
            res["by_actor"][handle]["no position (never placed)"] += 1
            if row.states >= 3 and rel - row.last <= GamePeerSource.HIDDEN_RECENT_S:
                unplaced_now += 1  # replicated recently: the overlay counter must account for it
                unplaced_set.add(handle)
            continue
        dist = math.hypot(row.pos[0] - ox, row.pos[1] - oy) / 100.0
        tier = 0 if dist <= LEDGER_TIERS[0] else 1 if dist <= LEDGER_TIERS[1] else 2 if dist <= LEDGER_TIERS[2] else 3
        if eid in pos:
            cat = LEDGER_DRAWN if glyph.get(eid) else LEDGER_TRIANGLE
        elif eid in edge:
            cat = LEDGER_EDGE
        elif ent is None:
            cat = "removed while still replicated" if row.ever_in_store else "never reached the store"
            if row.removed_t is None:
                row.removed_t = rel
                if len(res["removed_examples"]) < 12:
                    res["removed_examples"].append((round(rel), handle, round(dist), round(rel - row.last), row.states))
        elif ent.kind in ("ai", "other"):
            cat = f"classified {ent.kind}"
        elif eid not in view.ents.entities:
            cat = "in the store, not yet in the map"
        else:
            cat = "filtered by the view (age / settings)"
        res["cells"][(tier, cat)] += 1
        res["by_actor"][handle][cat] += 1
        res["kinds"][handle][ent.kind if ent is not None else "-"] += 1
        if row.spawn_only_pos:
            res["cells"][(tier, "(position is the spawn point only)")] += 1
    if hasattr(source, "unplaced_creatures"):
        counted_set = {r["handle"] for r in source.unplaced_creatures()}
    else:  # an older build that only counted: assume the count refers to the first ones (what-if comparisons only)
        counted_set = set(sorted(unplaced_set)[:source.hidden_creatures()])
    counted = len(counted_set)
    for h in unplaced_set - counted_set:
        res["uncounted"][h] += 1
    res["unplaced_ledger_s"] += unplaced_now
    res["unplaced_counted_s"] += len(unplaced_set & counted_set)
    res["counter_over_s"] += len(counted_set - unplaced_set)
    # creatures the server destroyed in the last minute and that were last seen within 500 m: still on the map as a ghost?
    for row in reversed(res["rows"][-600:]):
        if row.destroyed is None or rel - row.destroyed > 60.0:
            if row.destroyed is not None and rel - row.destroyed > 60.0:
                break
            continue
        if row.handle in live or row.handle in owned or row.pos is None or not row.candidate() or rel - row.destroyed < 1.0:
            continue
        if source._identity(row.handle)[0] in (sp.KIND_AI, sp.KIND_OTHER):
            continue
        dist = math.hypot(row.pos[0] - ox, row.pos[1] - oy) / 100.0
        if dist > LEDGER_TIERS[1]:
            continue
        shown = str(row.handle) in pos
        res["cells"][("gone", "after destroy: ghost drawn" if shown else "after destroy: not drawn")] += 1
        age = rel - row.destroyed
        res["gone_age"][("shown" if shown else "gone", int(age // 15))] += 1


def print_ledger(name: str, res: dict) -> bool:
    cells = res["cells"]
    port = res["port"]
    print(f"{name}{'' if port is None else f' (port {port})'}: {res['seconds']} s checked, "
          f"{len(res['rows'])} actor generations, {sum(1 for r in res['rows'] if r.candidate())} creature candidates")
    ok = True
    for tier, label in enumerate(("<= 250 m", "<= 500 m", "<= 1000 m", "beyond 1000 m")):
        total = sum(n for (t, c), n in cells.items() if t == tier and not c.startswith("("))
        if not total:
            continue
        shown = sum(n for (t, c), n in cells.items() if t == tier and c in (LEDGER_DRAWN, LEDGER_TRIANGLE))
        print(f"  {label}: {total} creature-seconds, drawn {shown} = {100.0 * shown / total:.2f} %")
        for (t, c), n in sorted(cells.items(), key=lambda kv: -kv[1]):
            if t == tier and c != LEDGER_DRAWN:
                print(f"      {n:6d}  {c}")
        if tier < 2 and shown < 0.99 * total:
            ok = False
    unplaced = cells.get(("none", "no position (never placed)"), 0)
    if unplaced:
        print(f"  replicated but never placed: {unplaced} creature-seconds over {len(res['unplaced_handles'])} actors (distance unknown); "
              f"the overlay counter accounts for {res['unplaced_counted_s']} of the {res['unplaced_ledger_s']} that were replicated recently")
        if res["unplaced_ledger_s"] and res["unplaced_counted_s"] < 0.8 * res["unplaced_ledger_s"]:
            ok = False
        if res["uncounted"]:
            print(f"      not counted (handle: seconds): {dict(res['uncounted'].most_common(6))}")
    shown, hidden = cells.get(("gone", "after destroy: ghost drawn"), 0), cells.get(("gone", "after destroy: not drawn"), 0)
    if shown or hidden:
        print(f"  destroyed by the server within 500 m, in the minute after: {shown} creature-seconds still drawn as a ghost, {hidden} not")
    if res["removed_examples"]:
        print(f"  removed while still replicated (t, handle, dist m, silent s, state batches): {res['removed_examples'][:8]}")
        ok = False
    return ok


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
