"""Automatic session files: default-on only for live capture, retention, session log content, fail-safety."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("scapy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from isle_livemap.capture import OUTBOUND, Datagram, TelemetryPipeline  # noqa: E402
from isle_livemap.geo import Settings  # noqa: E402
from isle_livemap.peers import GamePeerSource  # noqa: E402
from isle_livemap.replay import pcap_datagrams  # noqa: E402
from isle_livemap.sessionlog import (  # noqa: E402
    SessionFiles, default_session_dir, newest_session_pcap, prune_sessions,
)
from test_e2e_peers import build_pcap, run_main, settings_dir  # noqa: E402
from test_recorder import feed  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 10, 22, 10, 15, 0)


def touch(d: Path, name: str, size: int = 10) -> Path:
    p = d / name
    p.write_bytes(b"x" * size)
    return p


def names(d: Path) -> set[str]:
    return {p.name for p in d.iterdir()}


# ---------------------------------------------------------------- default on / off through __main__
class FakeLive:
    def __init__(self, pipeline, iface=None):
        self.pipeline = pipeline

    def start(self): ...

    def stop(self): ...


def run_live(monkeypatch, tmp_path, extra=(), **settings):
    import isle_livemap.capture as cap

    monkeypatch.setattr(cap, "LiveCapture", FakeLive)
    data = settings_dir(tmp_path, **settings)
    app, _ = run_main(monkeypatch, ["--data-dir", str(data), *extra], lambda a: True)
    return app, data


def test_live_launch_records_a_session_by_default(tmp_path, monkeypatch):
    app, data = run_live(monkeypatch, tmp_path)
    sess = data / "logs" / "sessions"
    pcaps, logs = sorted(sess.glob("session_*.pcap")), sorted(sess.glob("session_*.log"))
    assert len(pcaps) == 1 and len(logs) == 1 and pcaps[0].stem == logs[0].stem
    assert app.recorder is not None
    app._update_status()  # noqa: SLF001
    assert app.view.record_status.startswith(f"recording {pcaps[0].name}: ")
    assert "creation records seen: 0" in app.view.record_status
    text = logs[0].read_text()
    assert "app start" in text and "recorder started" in text and "app stop" in text


@pytest.mark.parametrize("argv,settings", [
    (["--no-record"], {}),
    ([], {"record_sessions": False}),
])
def test_live_launch_can_be_switched_off(tmp_path, monkeypatch, argv, settings):
    app, data = run_live(monkeypatch, tmp_path, argv, **settings)
    assert app.recorder is None and not (data / "logs").exists()


def test_record_dir_setting_is_used(tmp_path, monkeypatch):
    custom = tmp_path / "mine"
    _app, data = run_live(monkeypatch, tmp_path, record_dir=str(custom))
    assert list(custom.glob("session_*.pcap")) and not (data / "logs").exists()


@pytest.mark.parametrize("argv", [["--replay", "FILE", "--speed", "3"], ["--replay"], ["--lab-peers", "47999", "--replay"]])
def test_replay_never_creates_session_files(tmp_path, monkeypatch, argv):
    src = build_pcap(tmp_path / "src.pcap")
    data = settings_dir(tmp_path)
    argv = [str(src) if a == "FILE" else a for a in argv]
    app, _ = run_main(monkeypatch, [*argv, "--data-dir", str(data)], lambda a: True)
    assert app.recorder is None and not (data / "logs").exists()


def test_lab_peers_alone_never_creates_session_files(tmp_path, monkeypatch):
    app, data = run_live(monkeypatch, tmp_path, ["--lab-peers", "47998"])
    assert app.recorder is None and not (data / "logs").exists()


def test_unwritable_session_dir_does_not_crash_the_app(tmp_path, monkeypatch):
    blocker = touch(tmp_path, "file_not_dir")
    app, _ = run_live(monkeypatch, tmp_path, record_dir=str(blocker / "sub"))
    assert app.recorder is None  # the app still started


def test_settings_defaults_and_clamping():
    s = Settings()
    assert (s.record_sessions, s.record_dir, s.record_keep_sessions, s.record_total_cap_mb) == (True, "", 10, 1024.0)
    s = Settings(record_keep_sessions=0, record_total_cap_mb="junk", record_dir=5).normalise()
    assert (s.record_keep_sessions, s.record_total_cap_mb, s.record_dir) == (1, 1024.0, "")


def test_default_directory_is_under_the_data_dir(tmp_path):
    assert default_session_dir(tmp_path) == tmp_path / "logs" / "sessions"


# ---------------------------------------------------------------- retention
def test_retention_keeps_newest_sessions_and_deletes_only_matching_files(tmp_path):
    for i in range(1, 6):
        touch(tmp_path, f"session_20260101_00000{i}.pcap")
        touch(tmp_path, f"session_20260101_00000{i}.log")
    stray = [touch(tmp_path, n) for n in ("notes.txt", "session_old.pcap", "session_20260101_000001.pcap.bak",
                                          "other_20260101_000001.pcap", "session_20260101_000001.txt")]
    (tmp_path / "session_20260101_000002_dir.pcap").mkdir()
    gone = prune_sessions(tmp_path, keep_sessions=2, total_cap_bytes=10**9)
    assert len(gone) == 6
    assert names(tmp_path) >= {p.name for p in stray}
    assert {n for n in names(tmp_path) if n.startswith("session_2026") and n.endswith((".pcap", ".log"))
            and "dir" not in n} == {f"session_20260101_00000{i}.{e}" for i in (4, 5) for e in ("pcap", "log")}


def test_retention_respects_total_cap_oldest_first_and_protects_the_running_session(tmp_path):
    for i in range(1, 5):
        touch(tmp_path, f"session_20260101_00000{i}.pcap", 100)
    live = tmp_path / "session_20260101_000001.pcap"  # oldest, but it is the running session
    prune_sessions(tmp_path, keep_sessions=10, total_cap_bytes=250, protect=(live,))
    assert names(tmp_path) == {"session_20260101_000001.pcap", "session_20260101_000004.pcap"}


def test_retention_survives_missing_directory(tmp_path):
    assert prune_sessions(tmp_path / "nope") == []


def test_part_files_belong_to_their_session(tmp_path):
    for n in ("session_20260101_000001.pcap", "session_20260101_000001_part2.pcap", "session_20260101_000001.log",
              "session_20260101_000002.pcap"):
        touch(tmp_path, n)
    prune_sessions(tmp_path, keep_sessions=1, total_cap_bytes=10**9)
    assert names(tmp_path) == {"session_20260101_000002.pcap"}


# ---------------------------------------------------------------- session files and log content
def session(tmp_path, pipe, peers=None, **kw):
    sf = SessionFiles(tmp_path / "s", pipe, peers=peers, now=lambda: STAMP, **kw)
    assert sf.start()
    return sf


def test_session_names_and_start_prunes(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    for i in range(1, 4):
        touch(d, f"session_20250101_00000{i}.pcap")
    sf = session(tmp_path, TelemetryPipeline(), keep_sessions=2)
    assert sf.pcap_path.name == "session_20261022_101500.pcap" and sf.log_path.name == "session_20261022_101500.log"
    assert names(d) == {"session_20250101_000003.pcap", "session_20261022_101500.pcap", "session_20261022_101500.log"}
    sf.close()
    assert "retention removed 2 old file(s)" in sf.log_path.read_text()


def test_same_second_launches_do_not_overwrite(tmp_path):
    a = session(tmp_path, TelemetryPipeline())
    b = SessionFiles(tmp_path / "s", TelemetryPipeline(), now=lambda: STAMP)
    assert b.start() and b.pcap_path != a.pcap_path
    a.close()
    b.close()


def test_log_of_a_replayed_recording(tmp_path):
    src = build_pcap(tmp_path / "src.pcap", inbound=("FirstHeartbeat", "SecondHeartbeat", "DetachedDamagedAttributes"))
    pipe = TelemetryPipeline()
    peers = GamePeerSource(pipeline=pipe)
    peers.start()
    sf = session(tmp_path, pipe, peers)
    n = feed(pipe, src)
    sf.log.tick(force_snapshot=True)
    sf.close()
    text = sf.log_path.read_text()
    for needle in ("app start", "recorder started", "server learned: 203.0.113.5:7777", "own handle locked: 103940",
                   "peer source: game:", "counters: in=", "destruction record: handle=", "app stop",
                   "recorder stopped"):
        assert needle in text, needle
    assert "first creation record" not in text  # the fixtures hold none
    assert len(list(pcap_datagrams(sf.pcap_path))) == n
    # concise: no per-packet lines
    assert len(text.splitlines()) < 40 < n


def test_log_reports_first_creation_record_and_decoder_exceptions(tmp_path, monkeypatch):
    from isle_livemap import decode

    src = build_pcap(tmp_path / "src.pcap")
    pipe = TelemetryPipeline()
    sf = session(tmp_path, pipe)
    monkeypatch.setattr(decode, "batch_kind", lambda bits, batch: decode.KIND_CREATION)
    pipe.add_observer(lambda d, own: 1 / 0)
    feed(pipe, src)
    for _ in range(7):
        try:
            1 / 0
        except ZeroDivisionError:
            sf.log.on_error("unit test")
    sf.close()
    text = sf.log_path.read_text()
    assert text.count("first creation record: handle=") == 1
    assert "decoder exception in inbound observer (#1)" in text and "ZeroDivisionError" in text
    assert "further ones are not logged" in text  # rate limited


def test_cap_rolls_to_a_new_part_instead_of_stopping(tmp_path):
    pipe = TelemetryPipeline()
    sf = session(tmp_path, pipe, file_cap_mb=2000 / 1048576, keep_sessions=5)
    d = Datagram(0.0, "192.168.0.10", 50000, "203.0.113.5", 7777, b"\xaa" * 200, OUTBOUND)
    for _ in range(30):
        pipe.handle(d)
    rec = sf.recorder
    assert rec.active and not rec.capped and not rec.error and rec.parts > 1
    sf.close()
    parts = sorted((tmp_path / "s").glob("session_*.pcap"))
    assert len(parts) == rec.parts and parts[-1].name.endswith(f"_part{rec.parts}.pcap")
    assert sum(len(list(pcap_datagrams(p, 50000))) for p in parts) == rec.packets == 30
    assert "continuing in session_20261022_101500_part2.pcap" in sf.log_path.read_text()


def test_rolling_respects_the_total_cap_keeping_the_newest_parts(tmp_path):
    pipe = TelemetryPipeline()
    sf = session(tmp_path, pipe, file_cap_mb=2000 / 1048576, total_cap_mb=10 / 1024)  # 10 KB total
    d = Datagram(0.0, "192.168.0.10", 50000, "203.0.113.5", 7777, b"\xaa" * 200, OUTBOUND)
    for _ in range(300):
        pipe.handle(d)
    last = sf.recorder.path
    sf.close()
    left = list((tmp_path / "s").glob("session_*.pcap"))
    assert last in left and 1 < len(left) < sf.recorder.parts
    assert sum(p.stat().st_size for p in left) <= 10 * 1024 + 2000
    assert sf.log_path.exists()  # the log is never the first to go


def test_unwritable_directory_never_raises(tmp_path):
    blocker = touch(tmp_path, "blocker")
    pipe = TelemetryPipeline()
    sf = SessionFiles(blocker / "sub", pipe)
    assert sf.start() is False and sf.error
    sf.close()
    pipe.handle(Datagram(0.0, "a", 1, "b", 2, b"x", OUTBOUND))


def test_log_write_failure_is_swallowed(tmp_path):
    pipe = TelemetryPipeline()
    sf = session(tmp_path, pipe)
    sf.log._handler.stream.close()  # noqa: SLF001 - simulate a dead log file
    sf.log.event("still fine")
    sf.log.tick(force_snapshot=True)
    sf.log.on_inbound(Datagram(0.0, "a", 1, "b", 2, b"x", OUTBOUND), 5)
    sf.close()


def test_logger_handlers_do_not_leak_between_sessions(tmp_path):
    import logging

    before = sum(1 for n in logging.root.manager.loggerDict if n.startswith("isle_livemap.session."))
    sf = session(tmp_path, TelemetryPipeline())
    handlers = sf.log._logger.handlers  # noqa: SLF001
    sf.close()
    assert not handlers
    assert before <= sum(1 for n in logging.root.manager.loggerDict if n.startswith("isle_livemap.session.")) <= before + 1


# ---------------------------------------------------------------- inspect tool, gitignore
def run_tool(*args, cwd):
    return subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "inspect_capture.py"), *map(str, args)],
                          capture_output=True, text=True, cwd=cwd, timeout=120)


def test_newest_session_pcap_picks_by_stamp_and_part(tmp_path):
    for n in ("session_20260101_000001.pcap", "session_20260102_000000.pcap", "session_20260102_000000_part2.pcap",
              "session_20260102_000000_part10.pcap", "zzz.pcap"):
        touch(tmp_path, n)
    assert newest_session_pcap(tmp_path).name == "session_20260102_000000_part10.pcap"
    assert newest_session_pcap(tmp_path / "nope") is None


def test_inspect_accepts_a_directory_and_picks_the_newest(tmp_path):
    d = tmp_path / "sess"
    d.mkdir()
    build_pcap(d / "session_20260101_000000.pcap")
    (d / "session_20260102_000000.pcap").write_bytes(b"not a pcap")  # newest; would fail if it were read as old
    build_pcap(d / "session_20260103_000000.pcap")
    (d / "session_20260103_000000.log").write_text("x")
    out = run_tool(d, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert "using newest session pcap" in out.stdout and "session_20260103_000000.pcap" in out.stdout
    assert "session log:" in out.stdout and "packets:" in out.stdout
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run_tool(empty, cwd=tmp_path).returncode == 2


def test_inspect_latest_uses_the_default_sessions_dir(tmp_path):
    home = tmp_path / "home"
    d = home / ".isle-livemap" / "logs" / "sessions"
    d.mkdir(parents=True)
    build_pcap(d / "session_20260103_000000.pcap")
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home / "AppData")}
    out = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "inspect_capture.py"), "--latest"],
                         capture_output=True, text=True, cwd=tmp_path, timeout=120, env=env)
    if sys.platform == "win32":
        pytest.skip("default dir is under APPDATA on Windows")
    assert out.returncode == 0, out.stderr
    assert "session_20260103_000000.pcap" in out.stdout


def test_logs_dir_is_gitignored():
    assert "logs/" in (ROOT / ".gitignore").read_text().splitlines()
