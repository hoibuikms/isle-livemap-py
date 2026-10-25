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
                    help="dev/testing only: show other players from lab records on loopback 127.0.0.1:PORT (default 7777) "
                         "instead of the game. Without this flag, nearby players always come from the game capture.")
    ap.add_argument("--record", metavar="FILE.pcap",
                    help="passively record the game's UDP datagrams (both directions) to a pcap while the app runs; "
                         "start the app with this BEFORE joining a server so actor creation data is captured. "
                         "Read it back with --replay FILE or tools/inspect_capture.py FILE")
    ap.add_argument("--record-max-mb", type=float, default=200.0, metavar="MB",
                    help="--record: stop writing at this file size; automatic sessions roll to a new file at this "
                         "size instead (default 200)")
    ap.add_argument("--no-record", action="store_true",
                    help="do not write the automatic session files (live capture records session_*.pcap + .log "
                         "under <data dir>/logs/sessions by default; setting record_sessions=false does the same)")
    ap.add_argument("--data-dir", type=Path, help="where markers.json/settings.json live")
    args = ap.parse_args(argv)

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

    # Started before any source so nothing sent while joining a server is missed. A failure only disables recording.
    recorder = None
    if args.record:
        from .recorder import PcapRecorder

        recorder = PcapRecorder(args.record, args.record_max_mb)
        if recorder.attach(pipeline):
            print(f"recording game datagrams to {args.record} (cap {args.record_max_mb:g} MB); "
                  "join the server now, quit the app when the neighbour has left", file=sys.stderr)
        else:
            print(f"warning: not recording: {recorder.error}", file=sys.stderr)

    # Automatic session files: live capture only (not --replay / --lab-peers), unless disabled or --record FILE
    # was given. Created before any source starts so the join is captured. Failures only disable them.
    session = None
    if not args.replay and args.lab_peers is None and not args.record and not args.no_record \
            and settings.record_sessions:
        from .sessionlog import SessionFiles, default_session_dir

        directory = Path(settings.record_dir) if settings.record_dir else default_session_dir(args.data_dir)
        session = SessionFiles(directory, pipeline, keep_sessions=settings.record_keep_sessions,
                               total_cap_mb=settings.record_total_cap_mb, file_cap_mb=args.record_max_mb)
        if session.start():
            print(f"session files: {session.pcap_path} (+ .log)", file=sys.stderr)
        recorder = session.recorder  # a failed recorder still shows "recording FAILED (...)" in the status line
        if session.error:
            print(f"warning: session recording problem: {session.error}", file=sys.stderr)

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
            if session is not None:
                session.close()
            return 2

    from .peers import create_peer_source

    def anchor():
        pos = pipeline.latest.position
        return (pos.x, pos.y) if pos is not None else None

    lab_port = args.lab_peers if args.lab_peers is not None else 7777

    # Nearby players come from the game capture (the same pipeline as the own position) by default; there is
    # no user-facing source choice. Dev/test tooling only: --lab-peers (loopback records) and a bare --replay
    # (synthetic demo peers). --replay FILE.pcap uses the game path.
    peers = None
    try:
        if args.lab_peers is not None:
            peers = create_peer_source("lab", anchor, lab_port=lab_port, self_ids=settings.peer_self_ids)
        elif args.replay == "synthetic":
            peers = create_peer_source("off", anchor, synthetic=True)
        else:
            from .classcache import ClassCache
            from .geo import data_dir

            cache = ClassCache.load((args.data_dir or data_dir()) / "class_cache.json")
            peers = create_peer_source("game", anchor, self_ids=settings.peer_self_ids, pipeline=pipeline,
                                       class_cache=cache)
        peers.start()
        if session is not None and session.log is not None:
            session.log.peers = peers
    except Exception as err:  # noqa: BLE001 - a broken peer source must never take the minimap down
        print(f"warning: nearby players unavailable: {err}", file=sys.stderr)
        peers = None

    app = App(qapp, pipeline, store, settings, peers, recorder=recorder)  # noqa: F841 (kept alive)
    qapp.aboutToQuit.connect(source.stop)
    if session is not None:
        qapp.aboutToQuit.connect(session.close)
    elif recorder is not None:
        qapp.aboutToQuit.connect(recorder.close)
    qapp.aboutToQuit.connect(lambda: app.peers.stop() if app.peers is not None else None)
    return qapp.exec()


if __name__ == "__main__":
    raise SystemExit(main())
