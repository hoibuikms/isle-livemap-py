"""The live-capture path cannot run on this OS (Windows + Npcap), so its failure handling is tested with fakes:
a supervisor that survives errors, a decode worker that survives bad datagrams and is restarted if it dies, an interface
that goes quiet, and a pre-capture buffer that never blocks the sniffer."""

from __future__ import annotations

import queue
import threading
import time

import pytest

pytest.importorskip("scapy")

from scapy.layers.inet import IP, UDP  # noqa: E402
from scapy.layers.l2 import Ether  # noqa: E402

import isle_livemap.capture as cap  # noqa: E402
from isle_livemap.capture import INBOUND, Datagram, LiveCapture, PreCaptureBuffer, TelemetryPipeline  # noqa: E402


def datagram(t: float = 1.0) -> Datagram:
    return Datagram(t, "203.0.113.5", 7777, "192.168.0.10", 50000, b"\x00" * 8, INBOUND)


def pkt(sport=7777, dport=50000, payload=b"x" * 40):
    return Ether() / IP(src="203.0.113.5", dst="192.168.0.10") / UDP(sport=sport, dport=dport) / payload


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class FakeSniffer:
    def __init__(self, alive=True):
        self.running, self.stopped = alive, 0
        self.thread = threading.Thread(target=lambda: time.sleep(5), daemon=True)
        if alive:
            self.thread.start()

    def stop(self):
        self.stopped += 1


# ------------------------------------------------------------------ supervisor
def test_supervisor_survives_steps_that_raise_and_carries_on(monkeypatch):
    pipe = TelemetryPipeline()
    live = LiveCapture(pipe)
    live.RETRY_STEP_S = 0.01
    calls = []

    def find_pid():
        calls.append(1)
        if len(calls) <= 3:
            raise OSError("toolhelp failed")
        return None

    monkeypatch.setattr(cap, "find_game_pid", find_pid)
    monkeypatch.setattr(live, "_ensure_early", lambda: None)
    t = threading.Thread(target=live._supervise, daemon=True)
    t.start()
    try:
        assert wait_for(lambda: pipe.latest.status == "waiting for game")  # the loop is alive after three failures
        assert pipe.counts()["capture_errors"] == 3 and t.is_alive()
    finally:
        live._stop.set()
        t.join(2.0)
    assert not t.is_alive()


def test_supervisor_reports_the_error_in_the_status_while_it_retries(monkeypatch):
    pipe = TelemetryPipeline()
    live = LiveCapture(pipe)
    live.RETRY_STEP_S = 0.2
    monkeypatch.setattr(cap, "find_game_pid", lambda: (_ for _ in ()).throw(OSError("denied")))
    t = threading.Thread(target=live._supervise, daemon=True)
    t.start()
    try:
        assert wait_for(lambda: "capture error, retrying: OSError: denied" in pipe.latest.status)
    finally:
        live._stop.set()
        t.join(2.0)


def test_step_resets_the_pipeline_once_when_the_game_exits(monkeypatch):
    pipe = TelemetryPipeline()
    live = LiveCapture(pipe)
    resets = []
    monkeypatch.setattr(pipe, "reset", lambda: resets.append(1))
    monkeypatch.setattr(live, "_ensure_early", lambda: None)
    live._tracked_pid = 4242
    monkeypatch.setattr(cap, "find_game_pid", lambda: None)
    assert live._step() == 1.0 and live._step() == 1.0
    assert resets == [1] and live._tracked_pid is None and pipe.latest.status == "waiting for game"


def test_step_waits_for_the_udp_socket(monkeypatch):
    pipe = TelemetryPipeline()
    live = LiveCapture(pipe)
    monkeypatch.setattr(live, "_ensure_early", lambda: None)
    monkeypatch.setattr(cap, "find_game_pid", lambda: 7)
    monkeypatch.setattr(cap, "owned_udp_ports", lambda pid: set())
    assert live._step() == 0.5 and pipe.latest.status == "game running, no UDP socket yet"


# ------------------------------------------------------------------ decode worker
def test_worker_goes_on_after_a_datagram_that_raises_and_counts_slow_ones():
    class Pipe(TelemetryPipeline):
        def __init__(self):
            super().__init__()
            self.seen = []

        def handle(self, d):
            self.seen.append(d.observed_at)
            if d.observed_at == 1.0:
                raise ValueError("bad datagram")
            if d.observed_at == 2.0:
                time.sleep(0.08)

    pipe = Pipe()
    errors = []
    pipe.error_hook = errors.append
    live = LiveCapture(pipe)
    for t in (1.0, 2.0, 3.0):
        live._q.put(datagram(t))
    th = threading.Thread(target=live._work, daemon=True)
    th.start()
    try:
        assert wait_for(lambda: pipe.seen == [1.0, 2.0, 3.0])
        assert wait_for(lambda: pipe.counts()["slow_decodes"] == 1)
    finally:
        live._stop.set()
        th.join(1.0)
    assert errors == ["pipeline.handle"] and pipe.counts()["queue_max"] >= 2


def test_a_dead_worker_is_started_again(monkeypatch):
    pipe = TelemetryPipeline()
    live = LiveCapture(pipe)
    started = []
    monkeypatch.setattr(live, "_start_worker", lambda: started.append(1))
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    live._work_thread = dead
    live._ensure_worker()
    assert started == [1] and pipe.counts()["worker_restarts"] == 1
    live._work_thread = threading.Thread(target=lambda: time.sleep(0.3), daemon=True)
    live._work_thread.start()
    live._ensure_worker()
    assert started == [1]  # a living worker is left alone
    live._stop.set()
    live._work_thread = dead
    live._ensure_worker()
    assert started == [1]  # and nothing is restarted while stopping


def test_sniffer_callback_counts_errors_and_dropped_packets_without_raising():
    pipe = TelemetryPipeline()
    live = LiveCapture(pipe)
    live._on_packet(object())  # not a packet at all
    assert pipe.counts()["capture_errors"] == 1
    live._precap.set_ports({50000}, lambda *a: None)
    live._q = queue.Queue(maxsize=1)
    live._on_packet(pkt())
    live._on_packet(pkt())  # the queue is full
    assert pipe.counts()["dropped"] == 1 and live._q.qsize() == 1


# ------------------------------------------------------------------ an interface that goes quiet
def test_traffic_stall_reopens_on_all_interfaces_once_per_cooldown():
    pipe = TelemetryPipeline()
    live = LiveCapture(pipe)
    sniffer = FakeSniffer()
    live._sniffers = [("eth0", sniffer)]
    pipe.note_udp(5)
    live._check_stall(100.0)  # the counter moved since "-1": the clock starts
    live._check_stall(105.0)
    assert sniffer.stopped == 0 and not live._force_all
    live._check_stall(111.0)  # 11 s without a single UDP packet
    assert sniffer.stopped == 1 and live._force_all and live._sniffers == []
    live._sniffers = [("eth1", sniffer)]
    live._check_stall(125.0)  # quiet again, but inside the cooldown
    assert sniffer.stopped == 1
    live._check_stall(145.0)
    assert sniffer.stopped == 2


def test_no_stall_while_udp_keeps_flowing():
    pipe = TelemetryPipeline()
    live = LiveCapture(pipe)
    sniffer = FakeSniffer()
    live._sniffers = [("eth0", sniffer)]
    for second in range(0, 60, 2):
        pipe.note_udp()
        live._check_stall(100.0 + second)
    assert sniffer.stopped == 0 and not live._force_all


# ------------------------------------------------------------------ pre-capture buffer
def test_hand_over_keeps_order_when_packets_arrive_during_it():
    buf = PreCaptureBuffer()
    for i in range(3):
        buf.offer(float(i), f"old{i}", 7777, 50000)
    out = []

    def emit(t, p):
        out.append(p)
        if p == "old0":
            assert buf.offer(9.0, "late0", 7777, 50000) is True  # still held: the port is not owned yet
            assert 50000 in buf.ports and 50000 not in buf.owned
            assert buf.offer(9.1, "other", 1111, 2222) is True  # an unrelated port is held as always

    handed = buf.set_ports({50000}, emit)
    assert out == ["old0", "old1", "old2", "late0"] and handed == 4
    assert buf.offer(10.0, "live", 7777, 50000) is False  # owned now: handled by the caller directly
    assert 50000 in buf.owned and len(buf) == 1  # "other" is still waiting


def test_hand_over_does_not_block_the_sniffer():
    buf = PreCaptureBuffer()
    for i in range(3):
        buf.offer(float(i), i, 7777, 50000)
    gate = threading.Event()
    waited = []

    def slow_emit(t, p):
        gate.wait(0.4)  # a slow consumer

    th = threading.Thread(target=lambda: buf.set_ports({50000}, slow_emit))
    th.start()
    time.sleep(0.05)
    began = time.perf_counter()
    buf.offer(5.0, "x", 3333, 4444)  # the sniffer thread offering while the hand-over runs
    waited.append(time.perf_counter() - began)
    gate.set()
    th.join(2.0)
    assert waited[0] < 0.05  # the old code held the lock for the whole hand-over


def test_a_closed_port_is_unowned_again_and_a_bad_emit_does_not_wedge_the_hand_over():
    buf = PreCaptureBuffer()
    buf.offer(1.0, "a", 7777, 50000)

    def bad(t, p):
        raise RuntimeError("boom")

    assert buf.set_ports({50000}, bad) == 1 and 50000 in buf.owned and buf.ports == frozenset({50000})
    buf.set_ports((), lambda *a: None)  # the game exited
    assert buf.owned == frozenset() and buf.offer(2.0, "b", 7777, 50000) is True


def test_emit_held_turns_packets_of_the_port_being_handed_over_into_datagrams():
    live = LiveCapture(TelemetryPipeline())
    for i in range(3):
        live._precap.offer(float(i), pkt(), 7777, 50000)
    assert live._precap.set_ports({50000}, live._emit_held) == 3
    got = [live._q.get_nowait() for _ in range(3)]
    assert [d.observed_at for d in got] == [0.0, 1.0, 2.0] and all(d.direction == INBOUND for d in got)


def test_session_log_counter_line_shows_capture_health(tmp_path):
    from isle_livemap.sessionlog import SessionLog

    pipe = TelemetryPipeline()
    pipe.note_gauge("queue_max", 12)
    pipe.note_count("slow_decodes", 2)
    lines = []
    log = SessionLog.__new__(SessionLog)
    log.pipeline, log.peers, log.recorder = pipe, None, None
    log.creation = log.exports = log.destroyed = 0
    log.handles = set()
    log.event = lambda fmt, *a: lines.append(fmt % a)
    log.snapshot()
    assert "worker queue max=12 slow decodes=2 capture errors=0 worker restarts=0" in lines[0]
