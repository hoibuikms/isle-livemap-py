"""Summarise a recording (``--record FILE`` or any pcap of the game's UDP traffic) through our decoders.

    python -I tools/inspect_capture.py FILE.pcap [--local-port N] [--first 5] [--max-bytes 96]
    python -I tools/inspect_capture.py --latest          # newest session pcap in the default sessions directory
    python -I tools/inspect_capture.py DIRECTORY         # newest session_*.pcap inside DIRECTORY

Offline only (no network, no Qt, no sleeping). The datagrams go through the real ``TelemetryPipeline``, so the
server and own handle are learned exactly as in the app. Prints packet counts, duration, batch kinds,
creation / export / destruction counts, distinct handles, the class histogram of the creation records (class
path -> count, with the kind and species ``species.py`` derives from it), the creatures with their positions
decoded, and a hex dump of the first N creation batches and N export-bearing batches. Player names are counted;
``--names`` prints them (they are other people's in-game names: keep the output to yourself).

Bit order: the decoders read LSB-first. A batch is dumped as its payload bits packed LSB-first into bytes, i.e.
byte i holds bits 8*i .. 8*i+7 of the batch, bit 0 being the least significant bit of byte 0 ("bit offset" is
where that batch starts inside the datagram, counted the same way). For export-bearing batches the export
section sits in the datagram after the batch; its position is not exposed by the envelope walk, so the whole
datagram is dumped as well. Nothing here interprets those bytes.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # python -I drops the script dir from sys.path


def hexdump(data: bytes, limit: int) -> str:
    shown = data[:limit]
    lines = [f"      {i:04x}  {shown[i:i + 16].hex(' ')}" for i in range(0, len(shown), 16)]
    if len(data) > limit:
        lines.append(f"      ... {len(data) - limit} more bytes")
    return "\n".join(lines) or "      (empty)"


def batch_bytes(payload: bytes, offset: int, count: int) -> bytes:
    from isle_livemap.decode import Bits

    return Bits(payload).read(offset, count).to_bytes((count + 7) // 8, "little")


def inspect(path: Path, local_port: int | None, first: int, max_bytes: int, out=print, names: bool = False,
            top: int = 25) -> dict:
    from isle_livemap.capture import INBOUND, OUTBOUND, TelemetryPipeline
    from isle_livemap.decode import (
        KIND_CREATION, KIND_STATE, KIND_UNKNOWN, Bits, batch_kind, parse_iris_packet,
    )
    from isle_livemap.replay import pcap_datagrams

    from isle_livemap.peers import GamePeerSource

    pipe = TelemetryPipeline()
    game = GamePeerSource()  # the same decoding the app does; fed below through the pipeline's observer tap
    ident = game.identity
    emit = game._emit

    def counting_emit(handle, *a):  # noqa: ANN001 - wraps a private method of the object we own
        located[handle] += 1
        emit(handle, *a)

    game._emit = counting_emit
    feed = ident.feed

    def recording_feed(payload, packet=None):
        out_ = feed(payload, packet)
        for cr in out_:
            created_at.append((cr.net_ref_handle, cr.archetype_handle, ident.actors[cr.net_ref_handle].player_state is not None))
        return out_

    ident.feed = recording_feed
    created_at: list[tuple[int, int | None, bool]] = []
    located: Counter = Counter()
    kinds: Counter = Counter()
    handles: set[int] = set()
    creation_handles: set[int] = set()
    destroyed_handles: set[int] = set()
    export_batches = destruction_records = unparsed = incomplete = 0
    n_in = n_out = 0
    first_t = last_t = None
    creation_dumps: list[tuple] = []
    export_dumps: list[tuple] = []
    reasons: Counter = Counter()
    last_own = None

    def on_inbound(d, own):  # only inbound datagrams from the learned server reach this tap
        nonlocal export_batches, destruction_records, unparsed, incomplete, last_own
        if own is not None:
            last_own = own
        packet = parse_iris_packet(d.payload)
        if packet is None:
            unparsed += 1
            return
        if not packet.is_complete:
            incomplete += 1
            reasons[packet.incomplete_reason or "?"] += 1
        bits = Bits(d.payload)
        game.on_inbound(d, own)
        destruction_records += len(packet.destroyed_handles)
        destroyed_handles.update(packet.destroyed_handles)
        for b in packet.batches:
            kind = batch_kind(bits, b)
            kinds[kind] += 1
            handles.add(b.net_ref_handle)
            if kind == KIND_CREATION:
                creation_handles.add(b.net_ref_handle)
                if len(creation_dumps) < first:
                    creation_dumps.append((d.observed_at, b, d.payload))
            if b.has_exports:
                export_batches += 1
                if len(export_dumps) < first:
                    export_dumps.append((d.observed_at, b, d.payload))

    pipe.add_observer(on_inbound)
    for d in pcap_datagrams(path, local_port):
        first_t = d.observed_at if first_t is None else first_t
        last_t = d.observed_at
        n_in += d.direction == INBOUND
        n_out += d.direction == OUTBOUND
        pipe.handle(d)

    # the handle the pipeline locked onto (survival heartbeat); not exposed after the fact, so re-ask the tracker
    own = pipe._vitals.self_handle or last_own  # noqa: SLF001 - read-only diagnostic; the lock times out after the last heartbeat
    duration = (last_t - first_t) if first_t is not None else 0.0
    out(f"file: {path}")
    out(f"packets: {n_in + n_out} (inbound {n_in}, outbound {n_out}); duration {duration:.1f} s")
    out(f"server learned: {pipe._server or 'no'}; own handle: {own if own is not None else 'not locked'}")  # noqa: SLF001
    out(f"inbound packets not parsed as Iris: {unparsed}; incomplete envelopes: {incomplete}"
        + (f" ({dict(reasons)})" if reasons else ""))
    out(f"batch kinds: state {kinds[KIND_STATE]}, creation {kinds[KIND_CREATION]}, unknown {kinds[KIND_UNKNOWN]}")
    out(f"creation batches: {kinds[KIND_CREATION]}; export-bearing batches: {export_batches}; "
        f"destruction records: {destruction_records}")
    out(f"distinct handles: {len(handles)} (with a creation batch: {len(creation_handles)}; destroyed: "
        f"{len(destroyed_handles)})")
    if not kinds[KIND_CREATION] and not export_batches:
        out("NOTE: no creation records and no exports: this capture does not contain join data "
            "(it must start before joining the server).")
    _report_identity(ident, located, created_at, out, names, top)
    for title, dumps in (("creation batches", creation_dumps), ("export-bearing batches", export_dumps)):
        out(f"\nfirst {len(dumps)} {title}:")
        for t, b, payload in dumps:
            out(f"  t={t:.3f}s handle={b.net_ref_handle} bit offset {b.data_bit_offset}, {b.data_bit_count} bits, "
                f"owner={int(b.has_owner_data)} exports={int(b.has_exports)}")
            out(hexdump(batch_bytes(payload, b.data_bit_offset, b.data_bit_count), max_bytes))
            if title.startswith("export"):
                out(f"    whole datagram ({len(payload)} bytes):")
                out(hexdump(payload, max_bytes * 4))
    return {"packets": n_in + n_out, "creation": kinds[KIND_CREATION], "exports": export_batches,
            "destroyed": destruction_records, "handles": len(handles)}


def _report_identity(ident, located, created, out, names: bool, top: int) -> None:
    from isle_livemap import species as sp

    classes: Counter = Counter()
    by_kind: Counter = Counter()
    for handle, archetype, has_ps in created:
        path = ident.object_path(archetype if archetype is not None else handle)  # resolved with ALL exports seen
        hint = None if path else ident.archetype_species(archetype)
        kind, species = sp.describe(path, hint, has_ps)
        label = path or (f"<archetype {archetype} never exported>" if archetype is not None else "<unnamed level actor>")
        classes[(label, kind, species)] += 1
        by_kind[kind] += 1
    out(f"\nclass histogram of {len(created)} creation records (kind: {dict(by_kind)}):")
    for (path, kind, species), n in classes.most_common(top):
        out(f"  {n:5d}  {kind:8s} {species or '-':16s} {path}")
    if len(classes) > top:
        out(f"  ... {len(classes) - top} more classes")
    creatures: Counter = Counter()
    for (_path, kind, species), n in classes.items():
        if kind in (sp.KIND_DINO, sp.KIND_PLAYER, sp.KIND_AI):
            creatures[(kind, species)] += n
    out("creatures by species: " + (", ".join(f"{s_ or '?'} [{k}] x{n}" for (k, s_), n in creatures.most_common()) or "none"))
    out(f"player names decoded from PlayerState records: {len(ident.player_names)} still alive"
        + (": " + ", ".join(sorted(ident.player_names.values())) if names else " (use --names to list them)"))
    out(f"creatures with a position (creation or state): {len(located)} handles, {sum(located.values())} samples; "
        f"destruction records matching a creation seen in the capture: {ident.destroyed_known} of "
        f"{ident.destroyed_known + ident.destroyed_unknown}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file", type=Path, nargs="?", help="a pcap, or a directory (its newest session_*.pcap is used)")
    ap.add_argument("--latest", action="store_true",
                    help="use the newest session pcap of FILE (a directory) or of the default sessions directory")
    ap.add_argument("--local-port", type=int, help="game's local UDP port (auto-detected if omitted)")
    ap.add_argument("--first", type=int, default=5, help="how many creation / export records to dump (default 5)")
    ap.add_argument("--max-bytes", type=int, default=96, help="bytes shown per batch dump (default 96)")
    ap.add_argument("--names", action="store_true", help="also list the decoded player names")
    ap.add_argument("--top", type=int, default=25, help="classes shown in the histogram (default 25)")
    args = ap.parse_args(argv)
    if args.file is None and not args.latest:
        ap.error("give a pcap file or directory, or use --latest")
    if args.file is None or args.file.is_dir():
        from isle_livemap.sessionlog import default_session_dir, newest_session_pcap

        directory = args.file or default_session_dir()
        newest = newest_session_pcap(directory)
        if newest is None:
            print(f"error: no session_*.pcap in {directory}", file=sys.stderr)
            return 2
        args.file = newest
        print(f"using newest session pcap: {newest}")
        log = newest.with_name(newest.name.split("_part")[0].removesuffix(".pcap") + ".log")
        if log.is_file():
            print(f"session log: {log}")
    if not args.file.is_file():
        print(f"error: {args.file} not found", file=sys.stderr)
        return 2
    try:
        inspect(args.file, args.local_port, args.first, args.max_bytes, names=args.names, top=args.top)
    except ImportError as err:
        print(f"error: {err} (scapy is required to read pcap files: pip install scapy)", file=sys.stderr)
        return 2
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
