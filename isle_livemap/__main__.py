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
    ap.add_argument("--data-dir", type=Path, help="where markers.json/settings.json live")
    args = ap.parse_args(argv)

    from PySide6.QtWidgets import QApplication

    from .app import App
    from .capture import CaptureUnavailable, LiveCapture, TelemetryPipeline
    from .geo import MarkerStore, Settings

    settings_path = args.data_dir / "settings.json" if args.data_dir else None
    settings = Settings.load(settings_path)
    store = MarkerStore(args.data_dir / "markers.json" if args.data_dir else None)
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
        source = LiveCapture(pipeline, args.iface)
        try:
            source.start()
        except CaptureUnavailable as err:
            print(f"error: {err}", file=sys.stderr)
            return 2

    app = App(qapp, pipeline, store, settings)  # noqa: F841 (kept alive)
    qapp.aboutToQuit.connect(source.stop)
    return qapp.exec()


if __name__ == "__main__":
    raise SystemExit(main())
