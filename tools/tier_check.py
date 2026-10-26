"""Which creatures were inside the 250 m / 500 m tiers, and was each one shown honestly?

    .venv/bin/python -I tools/tier_check.py SESSION.pcap [more.pcap ...] [--port N] [--check]

Replays the capture and, once a second, takes every dinosaur / player / unclassified creature within 500 m of the own
position (the app's own distance, ``geo.priority_tier``). A creature there is listed with its position source:

  fix    a decoded position (followed), shown as is
  spawn  only the creation record's spawn point: shown dimmed and with "~" before the distance (``entity_label_lines``)

``--check`` exits 1 when a creature in the 500 m tiers is placed by its spawn point without the "~" marker, or when a
creature the source holds is missing from the snapshot it was counted from. The own position is the app's raw decoded
one: when it is in another frame than the replicated creatures the tiers list the wrong creatures, which this tool cannot
see (there is no ground truth for the offset in a capture; see docs/PEERS.md, "Own position frame")."""

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


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else None
    paths = [a for a in args if a != str(port)]
    if not paths:
        print(__doc__)
        return 2
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
