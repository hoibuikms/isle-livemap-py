"""python -m isle_livemap [--replay [FILE.pcap]] ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="isle_livemap", description="Passive own-player minimap overlay for The Isle: Evrima.")
    ap.add_argument("--replay", nargs="?", const="synthetic", metavar="PCAP",
                    help="demo without the game: synthetic walker (default) or a pcap of your own session")
    ap.add_argument("--local-port", type=int, help="game's local UDP port inside the pcap (auto-detected if omitted)")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier")
    ap.add_argument("--iface", help="capture interface (default: scapy's default route)")
    ap.add_argument("--diagnose", action="store_true", help="print capture diagnostics (no UI) and exit")
    ap.add_argument("--seconds", type=float, default=10.0, help="--diagnose: sniff duration per run (default 10)")
    ap.add_argument("--lab-peers", nargs="?", const=7777, type=int, metavar="PORT",
                    help="dev/testing: show other players from lab records on loopback 127.0.0.1:PORT (default 7777)")
    ap.add_argument("--game-peers", action="store_true",
                    help="owner-only, opt-in: other players from real game traffic (passive capture of the same inbound stream; position only)")
    ap.add_argument("--data-dir", type=Path, help="where markers.json/settings.json live")
    args = ap.parse_args(argv)
    if args.lab_peers is not None and args.game_peers:
        ap.error("--lab-peers and --game-peers are mutually exclusive")

    if args.diagnose:
        from .diagnose import run_diagnose

        return run_diagnose(args.iface, args.seconds)

    from PySide6.QtWidgets import QApplication

    from .app import App
    from .capture import CaptureUnavailable, LiveCapture, TelemetryPipeline
    from .geo import MarkerStore, Settings

    settings_path = args.data_dir / "settings.json" if args.data_dir else None
    settings = Settings.load(settings_path)
    store = MarkerStore(args.data_dir / "markers.json" if args.data_dir else None)
    if args.iface and not args.replay and settings.iface != args.iface:
        settings.iface = args.iface
        settings.save(settings_path)
    iface = args.iface or settings.iface or None
    pipeline = TelemetryPipeline()
    qapp = QApplication(sys.argv[:1])
    qapp.setQuitOnLastWindowClosed(False)

    source = None
    if args.replay:
        from .replay import ReplayFeeder, SyntheticSource, pcap_datagrams

        if args.replay == "synthetic":
            synth = SyntheticSource()
            source = ReplayFeeder(pipeline, synth.datagrams(3600.0), args.speed, loop=lambda: synth.datagrams(3600.0))
        else:
            path = Path(args.replay)
            source = ReplayFeeder(pipeline, pcap_datagrams(path, args.local_port), args.speed)
        source.start()
    else:
        source = LiveCapture(pipeline, iface)
        try:
            source.start()
        except CaptureUnavailable as err:
            print(f"error: {err}", file=sys.stderr)
            return 2

    from .peers import create_peer_source

    def anchor():
        pos = pipeline.latest.position
        return (pos.x, pos.y) if pos is not None else None

    lab_port = args.lab_peers if args.lab_peers is not None else 7777

    def make_peers(kind: str):
        return create_peer_source(kind, anchor, lab_port=lab_port, self_ids=settings.peer_self_ids, pipeline=pipeline)

    # Flags win over settings. --replay alone shows the synthetic demo peers. "game" is only ever
    # chosen explicitly (--game-peers or peer_source = "game"); it is never the default.
    if args.lab_peers is not None:
        kind = "lab"
    elif args.game_peers:
        kind = "game"
    elif args.replay:
        kind = "synthetic"
    else:
        kind = settings.peer_source
    peers = create_peer_source("off", anchor, synthetic=True) if kind == "synthetic" else make_peers(kind)
    if peers is not None:
        peers.start()

    app = App(qapp, pipeline, store, settings, peers, make_peers)  # noqa: F841 (kept alive)
    qapp.aboutToQuit.connect(source.stop)
    qapp.aboutToQuit.connect(lambda: app.peers.stop() if app.peers is not None else None)
    return qapp.exec()


if __name__ == "__main__":
    raise SystemExit(main())
