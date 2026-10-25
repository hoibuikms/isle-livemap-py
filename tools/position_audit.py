"""Are the decoded positions of other creatures plausible? There is no ground truth for another player's position, so the
audit checks what must hold for any real creature, per capture:

    .venv/bin/python -I tools/position_audit.py SESSION.pcap [more.pcap ...] [--check]

  speed      implied speed between two consecutive fixes of a creature (dinosaurs and players): nobody walks 40 m/s
  rest       displacement after a silence of 2 s or more. The server sends a position only when it changed, so a creature
             that stands still is silent, and after the silence it must be where it was
  distance   distance of every fix from the own player: replicated creatures are within the relevancy range (6.4 km seen)
  layouts    fixes by source: fixed layout (fish), learned layout (anchored vector search), bootstrap (two batches)

``--check`` exits 1 when a dinosaur / player step exceeds MAX_STEP_MS or a creature rests-then-jumps more than
MAX_REST_JUMP_M. Recorded sessions: no dinosaur / player step above 16 m/s (136 steps), six silences of 4-146 s ended
0.0-3.8 m from where the creature had been.

A search for an anchor older than ANCHOR_LIFETIME (a creature that rested for minutes) was tried and dropped: it found
vectors 2.5-4.9 km from the old anchor, speeds of 60-97 m/s between consecutive fixes, i.e. false locks."""

from __future__ import annotations

import bisect
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import isle_livemap.decode as D  # noqa: E402
from isle_livemap.capture import TelemetryPipeline  # noqa: E402
from isle_livemap.peers import GamePeerSource  # noqa: E402
from isle_livemap.replay import pcap_datagrams  # noqa: E402

MAX_STEP_MS = 40.0
MAX_REST_JUMP_M = 30.0
REST_GAP_S = 2.0


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * (len(s) - 1)))], 1)


def steps(rows):
    """Implied speeds (m/s) between consecutive fixes (t, x, y, ...) in cm; pairs closer than 50 ms are noise."""
    out = []
    for a, b in zip(rows, rows[1:]):
        dt = b[0] - a[0]
        if dt > 0.05:
            out.append(math.dist(a[1:3], b[1:3]) / dt / 100.0)
    return out


def rest_jumps(rows, gap_s: float = REST_GAP_S):
    """(gap s, displacement m) for every silence of at least ``gap_s`` between two fixes."""
    return [(b[0] - a[0], math.dist(a[1:3], b[1:3]) / 100.0) for a, b in zip(rows, rows[1:]) if b[0] - a[0] >= gap_s]


def audit(pcap: Path) -> dict:
    log: dict[int, list] = defaultdict(list)
    tracker = D.ActorLocationTracker
    update, bootstrap = tracker.update, tracker.bootstrap

    def traced_update(self, bits, batch, t, learn=False):
        before = (self.fixed, self.learned)
        loc = update(self, bits, batch, t, learn)
        if loc is not None:
            log[batch.net_ref_handle].append((t, loc.x, loc.y, loc.z, "fixed" if self.fixed > before[0] else "learned"))
        return loc

    def traced_bootstrap(self, bits, batch, t, origin):
        loc = bootstrap(self, bits, batch, t, origin)
        if loc is not None:
            log[batch.net_ref_handle].append((t, loc.x, loc.y, loc.z, "boot"))
        return loc

    tracker.update, tracker.bootstrap = traced_update, traced_bootstrap
    try:
        pipe = TelemetryPipeline()
        source = GamePeerSource(pipeline=pipe)
        source.start()
        own: list[tuple[float, float, float]] = []
        for d in pcap_datagrams(pcap):
            pipe.handle(d)
            pos = pipe.latest.position
            if pos is not None:
                own.append((d.observed_at, pos.x, pos.y))
        kinds = {h: source._identity(h)[0] for h in log}
    finally:
        tracker.update, tracker.bootstrap = update, bootstrap
    times = [o[0] for o in own]
    res = {"fixes": Counter(r[4] for rows in log.values() for r in rows), "speed": defaultdict(list),
           "distance": defaultdict(list), "rest": [], "fast": []}
    for h, rows in log.items():
        kind = kinds[h]
        res["speed"][kind] += steps(rows)
        if kind in ("dino", "player"):
            res["rest"] += [(h, *r) for r in rest_jumps(rows)]
            res["fast"] += [(h, round(v, 1)) for v in steps(rows) if v > MAX_STEP_MS]
        for r in rows[::5]:
            i = min(bisect.bisect_left(times, r[0]), len(own) - 1) if own else None
            if i is not None:
                res["distance"][kind].append(math.hypot(r[1] - own[i][1], r[2] - own[i][2]) / 100.0)
    return res


def main(argv: list[str]) -> int:
    check = "--check" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 2
    bad = False
    for name in paths:
        res = audit(Path(name))
        print(Path(name).name, dict(res["fixes"]))
        for kind, v in res["speed"].items():
            print(f"  {kind:8s} steps={len(v):6d} speed m/s p50={pct(v, .5)} p95={pct(v, .95)} p99={pct(v, .99)} max={pct(v, 1)}")
        for kind, v in res["distance"].items():
            print(f"  {kind:8s} distance to own m p50={pct(v, .5)} p95={pct(v, .95)} max={pct(v, 1)}")
        for h, gap, jump in res["rest"]:
            print(f"  rest: handle {h} silent {gap:.1f} s, then {jump:.1f} m from where it was")
        if res["fast"]:
            print(f"  FAST dinosaur / player steps (> {MAX_STEP_MS:.0f} m/s): {res['fast'][:6]}")
        bad |= bool(res["fast"]) or any(j > MAX_REST_JUMP_M for _h, _g, j in res["rest"])
    return 1 if check and bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
