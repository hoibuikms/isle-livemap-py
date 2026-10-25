"""Which attributes does the server replicate to us, and for whom? Own pawn versus every other creature, per capture.

    .venv/bin/python -I tools/attribute_census.py SESSION.pcap [more.pcap ...]

The own pawn is the ground truth: its growth, hunger and thirst are known (the app decodes them), so we know what their
slots look like on the wire and how often they appear. The same signatures are then counted in the batches of all other
creatures. Signatures (GAS attribute slot = float, set flag bit, the same float again, 66 bits long):

  full block   maxHealth, with maxHealth again 198 bits later and a health slot (<= maxHealth) 66 bits before it
  growth       a slot in (0, 1] 132 bits before maxHealth, i.e. 66 bits before health (the own full frame has it)
  hunger chain two equal slots 132 or 198 bits apart with a slot between them (the own survival heartbeat: hunger,
               thirst, [one more], hunger repeated; it reaches the own pawn about once a second)

A field that the server replicates to everybody shows up for other pawns at about the rate it does for the own pawn. One
that is owner-only never does. The creation-record column counts creation records of at least 1500 bits that carry
a maxHealth (``decode.creation_max_health``).

Recorded sessions (5 captures, 3 game sessions): the own pawn's full blocks 74; of the 60 that belong to a growing (juvenile)
own pawn 57 carry a growth slot, the adult own pawn of one session none (growth 1.0 is probably the default and is not
sent). Other pawns' full blocks 16, none with a growth slot, among them 5 growth ticks of growing pawns (their maxHealth
rises by 0.5 to 1.4 %, the same event that always carries the growth slot to the owner). Hunger chains: the own pawn in
95-100 % of its attribute batches (about one per second), others in 5 batches, all of them maxHealth blocks. A plain
float32 copy of the own growth appears nowhere in the own pawn's batches either, so a differently encoded growth for
others cannot be excluded, but nothing in the data points to one."""


from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from isle_livemap.capture import TelemetryPipeline  # noqa: E402
from isle_livemap.decode import (  # noqa: E402
    KIND_CREATION, Bits, attribute_slots, batch_kind, creation_max_health, parse_iris_packet,
)
from isle_livemap.peers import GamePeerSource  # noqa: E402
from isle_livemap.replay import pcap_datagrams  # noqa: E402

SLOT = 66


def full_blocks(slots: dict[int, float]):
    """(offset of maxHealth, health, maxHealth, growth slot value or None) for every full block of one batch."""
    for m, top in slots.items():
        if top < 0.5 or slots.get(m + 3 * SLOT) is None or abs(slots[m + 3 * SLOT] - top) > 1e-4 * top:
            continue
        health = slots.get(m - SLOT)
        if health is None or not 0 < health <= top * 1.0001 or slots.get(m + SLOT) is None:
            continue
        growth = slots.get(m - 2 * SLOT)
        yield m, health, top, growth if growth is not None and 0 < growth <= 1.0001 else None


def has_hunger_chain(slots: dict[int, float]) -> bool:
    for a, v in slots.items():
        if v < 1e-3 or v >= 1500.0:
            continue
        mid = slots.get(a + SLOT)
        if mid is None or not 1e-3 < mid < 2000.0:
            continue
        for gap in (2 * SLOT, 3 * SLOT):
            w = slots.get(a + gap)
            if w is not None and abs(w - v) <= 1e-6 * max(1.0, v):
                return True
    return False


def census(pcap: Path) -> dict[str, Counter]:
    pipe = TelemetryPipeline()
    source = GamePeerSource(pipeline=pipe)
    source.start()
    own_seen: set[int] = set()
    per_handle: dict[int, Counter] = {}
    for d in pcap_datagrams(pcap):
        pipe.handle(d)
        if source.own_handle is not None:
            own_seen.add(source.own_handle)
        packet = parse_iris_packet(d.payload)
        if packet is None:
            continue
        bits = Bits(d.payload)
        for batch in packet.batches:
            who = per_handle.setdefault(batch.net_ref_handle, Counter())
            who["batches"] += 1
            if batch_kind(bits, batch) == KIND_CREATION:
                if batch.data_bit_count >= 1500:
                    who["creation records >= 1500 bits"] += 1
                    who["  ... with a maxHealth"] += creation_max_health(bits, batch) is not None
                continue
            slots = dict(attribute_slots(bits, batch))
            if not slots:
                continue
            who["batches with attribute slots"] += 1
            who["hunger chains"] += has_hunger_chain(slots)
            for _m, _health, _top, growth in full_blocks(slots):
                who["full blocks"] += 1
                who["  ... with a growth slot"] += growth is not None
    out = {"own": Counter(), "other": Counter()}  # a handle that was ever the own pawn is the own pawn, also before the lock
    for handle, counts in per_handle.items():
        out["own" if handle in own_seen else "other"].update(counts)
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for name in argv:
        res = census(Path(name))
        print(Path(name).name)
        keys = [k for k in ("batches", "batches with attribute slots", "hunger chains", "full blocks", "  ... with a growth slot",
                            "creation records >= 1500 bits", "  ... with a maxHealth")]
        print(f"  {'':34s}{'own pawn':>10s}{'others':>10s}")
        for key in keys:
            print(f"  {key:34s}{res['own'][key]:10d}{res['other'][key]:10d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
