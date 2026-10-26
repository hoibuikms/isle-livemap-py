"""Which creatures were inside the 250 m / 500 m tiers, and was each one shown honestly?

    .venv/bin/python -I tools/tier_check.py SESSION.pcap [more.pcap ...] [--port N] [--check]
    .venv/bin/python -I tools/tier_check.py --labels SESSION.pcap [more.pcap ...] [--port N] [--check] [--png DIR]

Replays the capture and, once a second, takes every dinosaur / player / unclassified creature within 500 m of the own
position (the app's own distance, ``geo.priority_tier``). A creature there is listed with its position source:

  fix    a decoded position (followed), shown as is
  spawn  only the creation record's spawn point: shown dimmed and with "~" before the distance (``entity_label_lines``)

``--check`` exits 1 when a creature in the 500 m tiers is placed by its spawn point without the "~" marker, or when a
creature the source holds is missing from the snapshot it was counted from. The own position is the app's raw decoded
one: when it is in another frame than the replicated creatures the tiers list the wrong creatures, which this tool cannot
see (there is no ground truth for the offset in a capture; see docs/PEERS.md, "Own position frame").

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
    band = [n for n in view.nearby(None) if n.entity.kind in player_kinds and n.distance_m <= 500.0]
    res["max_band"] = max(res["max_band"], len(band))
    if len(band) > res["busiest"][0]:
        res["busiest"] = (len(band), rel)
        res["_best"] = pix.copy()
    half = view.width() / 2
    radius = half - 3
    arrows = [(p[0], p[1], marker_extent(n.entity)) for n in band for p in [pos.get(n.entity.id)] if p and glyph.get(n.entity.id)]
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
                    for ax, ay, ext in arrows:
                        if (ax, ay) != (own[0], own[1]) and r[0] - 1 < ax < r[0] + r[2] + 1 and r[1] - 1 < ay < r[1] + r[3] + 1:
                            res["hides_arrow"] += 1
                            reason = "label covers another creature's arrow"
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
    if res.get("png"):
        print(f"  picture: {res['png']}")
    return not hard


def main(argv: list[str]) -> int:
    png = Path(argv[argv.index("--png") + 1]) if "--png" in argv else None
    args = [a for a in argv if not a.startswith("--")]
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else None
    paths = [a for a in args if a != str(port) and (png is None or a != str(png))]
    if not paths:
        print(__doc__)
        return 2
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
