"""Build ``isle_livemap/assets/class_seed.json`` from captures that saw the server's join tables.

    python tools/build_class_seed.py session_a.pcap session_b.pcap [-o isle_livemap/assets/class_seed.json]

Replays every pcap through the same decoder the app uses and keeps each protocol id (the state-layout hash of a
creation record) whose archetype path the export tables gave, and only when every capture agrees on one path.
A dinosaur id that no capture had the tables for is kept too when the dinosaur assets of its own creation records or
of its own batches (the same evidence the app uses live, source "asset") name one species folder in every capture: its
path is then the conventional /Characters/Dinosaurs/<Folder>/BP_<Folder> one. The seed holds asset paths only: no player names,
account ids or addresses.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from isle_livemap.capture import TelemetryPipeline  # noqa: E402
from isle_livemap.classcache import SCHEMA, SEED_PATH, ClassCache  # noqa: E402
from isle_livemap.peers import GamePeerSource  # noqa: E402
from isle_livemap.replay import pcap_datagrams  # noqa: E402


def paths_of(pcap: Path) -> tuple[dict[int, set[str]], dict[int, set[str]]]:
    """(protocol id -> paths the export tables gave, protocol id -> dinosaur paths derived from the creation assets)."""
    pipe = TelemetryPipeline()
    cache = ClassCache(None, seed={})  # learns, from the actors' own dinosaur assets, ids whose class no table gave
    source = GamePeerSource(pipeline=pipe, class_cache=cache)
    source.start()
    for d in pcap_datagrams(pcap):
        pipe.handle(d)
    found: dict[int, set[str]] = {}
    derived: dict[int, set[str]] = {}
    for info in source.identity.actors.values():
        if info.archetype_handle is None:
            continue
        path = source.identity.object_path(info.archetype_handle)  # the tables only: never the cache
        if path and path.startswith("/"):
            found.setdefault(info.protocol_id, set()).add(path)
            continue
        folder = source.identity.archetype_species(info.archetype_handle)
        if folder and folder.isalnum():
            derived.setdefault(info.protocol_id, set()).add(
                f"/Game/TheIsle/Core/Characters/Dinosaurs/{folder}/BP_{folder}.Default__BP_{folder}_C")
    # Ids the app learned while replaying, from the assets exported in the actor's own batches (handle_species): the actor may
    # be destroyed again by now, so the evidence is read from the cache, not from the tracker's final state.
    for protocol, path in cache.items():
        if protocol not in found:
            derived.setdefault(protocol, set()).add(path)
    return found, derived


def build(pcaps: list[Path]) -> tuple[dict[int, str], int]:
    merged: dict[int, set[str]] = {}
    derived: dict[int, set[str]] = {}
    for pcap in pcaps:
        found, guessed = paths_of(pcap)
        for protocol, paths in found.items():
            merged.setdefault(protocol, set()).update(paths)
        for protocol, paths in guessed.items():
            derived.setdefault(protocol, set()).update(paths)
    for protocol, paths in derived.items():
        if protocol not in merged:  # a path the tables gave always wins over a derived one
            merged[protocol] = paths
    seed = {p: next(iter(v)) for p, v in merged.items() if len(v) == 1}
    return seed, len(merged) - len(seed)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pcaps", nargs="+", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=SEED_PATH)
    args = ap.parse_args()
    seed, ambiguous = build(args.pcaps)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "entries": {f"{k:08x}": v for k, v in sorted(seed.items())}}
    args.output.write_text(json.dumps(payload, indent=0, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(seed)} protocol ids written to {args.output} ({ambiguous} skipped: more than one path)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
