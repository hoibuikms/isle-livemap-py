"""--record: passive pcap recorder, round trip through the real pipeline, failure and size-cap behaviour."""

from __future__ import annotations

import os
from dataclasses import replace
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("scapy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from isle_livemap.capture import INBOUND, OUTBOUND, Datagram, TelemetryPipeline  # noqa: E402
from isle_livemap.peers import GamePeerSource  # noqa: E402
from isle_livemap.recorder import PcapRecorder, build_record  # noqa: E402
from isle_livemap.replay import pcap_datagrams  # noqa: E402
from conftest import retimed  # noqa: E402
from test_e2e_peers import build_pcap, ready, run_main, settings_dir  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def feed(pipeline, path):
    n = 0
    for d in pcap_datagrams(path):
        # ReplayFeeder restamps onto the live clock at --speed 3 (what test_e2e_peers does); the own-position
        # tracker compares arrival time with the payload's own timestamps, so mimic that without sleeping
        pipeline.handle(replace(d, observed_at=d.observed_at / 3))
        n += 1
    return n


def peers_of(src):
    return {e.entity.id: (e.entity.x, e.entity.y, e.entity.z) for e in src.store.entities()}


def game_pipeline(recorder=None):
    pipe = TelemetryPipeline()
    peers = GamePeerSource(pipeline=pipe)
    peers.start()
    if recorder is not None:
        assert recorder.attach(pipe)
    return pipe, peers


def test_round_trip_record_then_replay_reproduces_the_decoded_peers(tmp_path):
    src = build_pcap(tmp_path / "src.pcap")
    rec = PcapRecorder(tmp_path / "rec.pcap")
    pipe, peers = game_pipeline(rec)
    n = feed(pipe, src)
    rec.close()
    assert peers.locations > 0 and peers_of(peers)
    assert rec.packets == n and not rec.error and not rec.capped

    pipe2, peers2 = game_pipeline()
    assert feed(pipe2, rec.path) == n  # same count, direction and server learning from the recording alone
    assert peers_of(peers2) == peers_of(peers)
    assert peers2.own_handle == peers.own_handle
    assert (peers2.creation_records, peers2.export_batches, peers2.destruction_records) == \
           (peers.creation_records, peers.export_batches, peers.destruction_records)
    assert pipe.latest.position is not None and pipe2.latest.position is not None
    assert (pipe2.latest.position.x, pipe2.latest.position.y) == (pipe.latest.position.x, pipe.latest.position.y)


def test_record_preserves_payload_addresses_and_order(tmp_path):
    ds = [
        Datagram(0.0, "192.168.0.10", 50000, "203.0.113.5", 7777, b"\x01\x02\x03", OUTBOUND),
        Datagram(0.1, "203.0.113.5", 7777, "192.168.0.10", 50000, b"\xff" * 40, INBOUND),
    ]
    rec = PcapRecorder(tmp_path / "r.pcap", clock=lambda: 1_700_000_000.25)
    assert rec.start()
    for d in ds:
        rec.on_datagram(d)
    rec.close()
    back = list(pcap_datagrams(rec.path))
    assert [(d.src_ip, d.src_port, d.dst_ip, d.dst_port, d.payload, d.direction) for d in back] == \
           [(d.src_ip, d.src_port, d.dst_ip, d.dst_port, d.payload, d.direction) for d in ds]
    assert back[1].observed_at == pytest.approx(0.1, abs=1e-5)


def test_file_is_a_standard_pcap_header(tmp_path):
    rec = PcapRecorder(tmp_path / "h.pcap")
    rec.start()
    rec.close()
    raw = rec.path.read_bytes()
    assert raw[:4] == bytes.fromhex("d4c3b2a1") and raw[20:24] == (101).to_bytes(4, "little") and len(raw) == 24


def test_unwritable_path_does_not_raise(tmp_path):
    rec = PcapRecorder(tmp_path / "missing_dir" / "x.pcap")
    pipe = TelemetryPipeline()
    assert rec.attach(pipe) is False
    assert "FAILED" in rec.status() and rec.error
    pipe.handle(Datagram(0.0, "a", 1, "b", 2, b"x", OUTBOUND))  # nothing attached, nothing breaks
    rec.on_datagram(Datagram(0.0, "a", 1, "b", 2, b"x", OUTBOUND))
    rec.close()


def test_write_error_midway_stops_recording_but_not_decoding(tmp_path, recorded):
    rec = PcapRecorder(tmp_path / "w.pcap")
    pipe = TelemetryPipeline()
    assert rec.attach(pipe)

    class Boom:
        def write(self, _):
            raise OSError("disk full")

        def flush(self): ...

        def close(self): ...

    rec._fh = Boom()  # noqa: SLF001
    move = bytes.fromhex(recorded["StationaryPayload"])
    for i in range(160):
        pipe.handle(Datagram(1.0 + i * 0.05 / 3, "192.168.0.10", 50000, "203.0.113.5", 7777,
                             retimed(move, i * 0.05 / 3), OUTBOUND))
    assert pipe.latest.position is not None  # own-player decoding unaffected
    assert not rec.active and "FAILED" in rec.status() and "disk full" in rec.error


def test_failing_observer_never_reaches_the_pipeline(tmp_path):
    pipe = TelemetryPipeline()
    pipe.add_datagram_observer(lambda d: 1 / 0)
    pipe.handle(Datagram(0.0, "a", 1, "b", 2, b"x", OUTBOUND))
    assert pipe.counts()["outbound"] == 1


def test_size_cap_stops_writing_with_a_clear_message(tmp_path):
    cap_mb = 2000 / 1048576
    rec = PcapRecorder(tmp_path / "cap.pcap", max_mb=cap_mb)
    assert rec.start()
    d = Datagram(0.0, "192.168.0.10", 50000, "203.0.113.5", 7777, b"\xaa" * 200, OUTBOUND)
    for _ in range(50):
        rec.on_datagram(d)
    assert rec.capped and not rec.error and not rec.active
    assert 0 < rec.packets < 50
    assert rec.path.stat().st_size <= rec.max_bytes
    assert "STOPPED" in rec.status() and "cap" in rec.status() and "--record-max-mb" in rec.status()
    assert len(list(pcap_datagrams(rec.path, 50000))) == rec.packets  # what was kept is still valid
    rec.close()


def test_status_text_reports_packets_size_and_creation_counts(tmp_path):
    src = build_pcap(tmp_path / "src.pcap")
    rec = PcapRecorder(tmp_path / "s.pcap")
    pipe, peers = game_pipeline(rec)
    feed(pipe, src)
    text = rec.status()
    assert text.startswith(f"recording s.pcap: {rec.packets} pkts, ") and " MB, creation records seen: 0" in text
    assert rec.destroyed == peers.destruction_records and rec.creation == peers.creation_records == 0
    rec.close()


def test_build_record_ip_checksum_is_valid():
    rec = build_record(Datagram(0.0, "10.1.2.3", 5, "10.4.5.6", 6, b"abc", OUTBOUND), 1.5)
    ip = rec[16:36]
    total = sum(int.from_bytes(ip[i:i + 2], "big") for i in range(0, 20, 2))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    assert total == 0xFFFF


def test_inspect_tool_runs_isolated_and_prints_the_summary(tmp_path):
    src = build_pcap(tmp_path / "src.pcap")
    out = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "inspect_capture.py"), str(src)],
                         capture_output=True, text=True, cwd=tmp_path, timeout=120)
    assert out.returncode == 0, out.stderr
    for needle in ("packets:", "duration", "batch kinds:", "creation batches: 0", "destruction records:",
                   "distinct handles:", "does not contain join data"):
        assert needle in out.stdout


def test_inspect_tool_dumps_raw_bits_of_creation_batches(tmp_path, monkeypatch):
    """Plumbing only: the fixtures have no creation record, so the classifier is forced to say "creation"."""
    sys.path.insert(0, str(ROOT / "tools"))
    import inspect_capture

    from isle_livemap import decode

    monkeypatch.setattr(decode, "batch_kind", lambda bits, batch: decode.KIND_CREATION)
    lines: list[str] = []
    res = inspect_capture.inspect(build_pcap(tmp_path / "src.pcap"), None, 2, 8, out=lines.append)
    text = "\n".join(lines)
    assert res["creation"] > 0 and "first 2 creation batches:" in text and "more bytes" in text
    payload = bytes.fromhex("e1c3a5") + b"\x00" * 5
    assert inspect_capture.batch_bytes(payload, 2, 20) == ((int.from_bytes(payload, "little") >> 2) & 0xFFFFF).to_bytes(3, "little")


def test_main_record_flag_records_during_a_replay_and_reports_status(tmp_path, monkeypatch):
    src = build_pcap(tmp_path / "src.pcap")
    out = tmp_path / "session.pcap"
    data = settings_dir(tmp_path)
    app, _img = run_main(monkeypatch, ["--replay", str(src), "--speed", "3", "--data-dir", str(data),
                                       "--record", str(out)], ready)
    assert app.recorder is not None and app.recorder.packets > 0
    app._update_status()  # noqa: SLF001
    assert app.view.record_status.startswith("recording session.pcap: ") and "creation records seen" in app.view.record_status
    assert "recording session.pcap:" in app.tray.toolTip()
    assert out.stat().st_size > 24  # closed (flushed) by aboutToQuit
    assert len(list(pcap_datagrams(out))) == app.recorder.packets
