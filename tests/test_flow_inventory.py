"""The UDP flow inventory: which flows the game talks on, which are decoded, and the warning for a second sizeable flow that is
not (a recording only holds what the pipeline was handed, so this is the one place a log can say something about the rest)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from isle_livemap.capture import (
    FLOW_SIZEABLE_BYTES, FLOW_SIZEABLE_PKTS, FLOWS_MAX, INBOUND, OUTBOUND, UNOWNED_MAX, Datagram, FlowTable, TelemetryPipeline,
    describe_flows,
)
from isle_livemap.sessionlog import FLOWS_INTERVAL_S, SessionLog

SERVER = "203.0.113.5:7777"
LOCAL = "192.168.0.10"


def dg(t, remote_ip="203.0.113.5", remote_port=7777, local_port=50000, size=100, direction=INBOUND):
    if direction == INBOUND:
        return Datagram(t, remote_ip, remote_port, LOCAL, local_port, b"\x00" * size, direction)
    return Datagram(t, LOCAL, local_port, remote_ip, remote_port, b"\x00" * size, direction)


def feed(pipe, n, **kw):
    for i in range(n):
        pipe.handle(dg(1.0 + i * 0.01, **kw))


def test_flow_table_counts_each_direction_per_remote_and_local_port():
    ft = FlowTable()
    for _ in range(3):
        ft.note(dg(1.0))
    ft.note(dg(2.0, direction=OUTBOUND, size=40))
    ft.note(dg(3.0, remote_ip="198.51.100.9", remote_port=27053, local_port=60000, direction=OUTBOUND, size=1300))
    rows = {(r["remote"], r["local_port"]): r for r in ft.rows()}
    main = rows[(SERVER, 50000)]
    assert (main["pkts_in"], main["pkts_out"], main["bytes_in"], main["bytes_out"]) == (3, 1, 300, 40)
    probe = rows[("198.51.100.9:27053", 60000)]
    assert (probe["pkts_in"], probe["pkts_out"], probe["bytes_out"]) == (0, 1, 1300)


def test_flow_table_is_bounded_and_ignores_datagrams_without_a_direction():
    ft = FlowTable()
    for i in range(FLOWS_MAX + 20):
        ft.note(dg(1.0, remote_port=10_000 + i))
    assert len(ft.rows()) == FLOWS_MAX and ft.overflow == 20
    ft.note(Datagram(1.0, "1.1.1.1", 1, "2.2.2.2", 2, b"x", ""))
    assert len(ft.rows()) == FLOWS_MAX
    for i in range(UNOWNED_MAX + 5):
        ft.note_unowned("1.1.1.1", 1000 + i, LOCAL, 2000, 10, 1.0)
    assert len(ft.unowned_rows()) == UNOWNED_MAX


def test_flow_table_is_thread_safe():
    ft = FlowTable()

    def work():
        for i in range(500):
            ft.note(dg(1.0 + i))
            ft.note_unowned("1.1.1.1", 5, LOCAL, 6, 20, 1.0)

    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert ft.rows()[0]["pkts_in"] == 2000 and ft.unowned_rows()[0]["pkts"] == 2000


def test_the_pipeline_counts_every_datagram_it_is_handed_in_the_flow_table():
    pipe = TelemetryPipeline()
    feed(pipe, 5)
    feed(pipe, 2, direction=OUTBOUND)
    (row,) = pipe.flows.rows()
    assert (row["pkts_in"], row["pkts_out"]) == (5, 2)


def test_learned_server_flow_is_decoded_and_latency_probes_are_ignored_without_a_warning():
    pipe = TelemetryPipeline()
    feed(pipe, 3, direction=OUTBOUND)  # the first outbound datagram teaches the server address
    feed(pipe, 400)
    pipe.handle(dg(9.0, remote_ip="198.51.100.9", remote_port=27053, local_port=60000, size=1300, direction=OUTBOUND))
    line = pipe.update_flow_report(30.0)
    assert f"decoded {SERVER}" in line and "other game flows (ignored): 198.51.100.9:27053" in line
    assert pipe.flow_hint == "" and "not decoded" not in pipe.status_text()


def test_a_second_sizeable_game_flow_that_is_not_the_server_raises_the_hint_and_the_status_text():
    pipe = TelemetryPipeline()
    feed(pipe, 3, direction=OUTBOUND)
    feed(pipe, FLOW_SIZEABLE_PKTS + 20, remote_ip="198.51.100.77", remote_port=7778, local_port=50001, size=200)
    line = pipe.update_flow_report(30.0)
    assert "198.51.100.77:7778" in line
    assert "second game UDP flow 198.51.100.77:7778" in pipe.flow_hint and "is not decoded" in pipe.flow_hint
    assert "second game UDP flow" in pipe.status_text()


def test_sizeable_by_bytes_alone_also_counts():
    pipe = TelemetryPipeline()
    feed(pipe, 3, direction=OUTBOUND)
    feed(pipe, 5, remote_ip="198.51.100.77", remote_port=7778, local_port=50001, size=FLOW_SIZEABLE_BYTES // 4)
    pipe.update_flow_report(30.0)
    assert pipe.flow_hint


def test_the_window_is_the_time_since_the_previous_report_not_the_whole_session():
    pipe = TelemetryPipeline()
    feed(pipe, 3, direction=OUTBOUND)
    feed(pipe, FLOW_SIZEABLE_PKTS + 5, remote_ip="198.51.100.77", remote_port=7778, local_port=50001)
    pipe.update_flow_report(30.0)
    assert pipe.flow_hint
    feed(pipe, 3, remote_ip="198.51.100.77", remote_port=7778, local_port=50001)  # quiet since the last report
    pipe.update_flow_report(60.0)
    assert pipe.flow_hint == ""


def test_server_traffic_on_a_port_the_game_does_not_own_is_warned_about():
    pipe = TelemetryPipeline()
    feed(pipe, 3, direction=OUTBOUND)
    for i in range(FLOW_SIZEABLE_PKTS + 10):
        pipe.note_unowned_udp("203.0.113.5", 7777, LOCAL, 50999, 300, 1.0 + i * 0.01)
    line = pipe.update_flow_report(30.0)
    assert "UDP on ports the game does not own: 1 flows" in line
    assert "port 50999" in pipe.flow_hint and "not decoded or recorded" in pipe.flow_hint


def test_other_applications_udp_is_counted_but_never_warned_about():
    pipe = TelemetryPipeline()
    feed(pipe, 3, direction=OUTBOUND)
    for i in range(5_000):
        pipe.note_unowned_udp("192.0.2.50", 50000, LOCAL, 3478, 1200, 1.0 + i * 0.001)  # e.g. a voice call
    line = pipe.update_flow_report(30.0)
    assert "5000 pkts" in line and pipe.flow_hint == ""


def test_report_with_no_flow_yet_is_one_short_line():
    line, hint, state = describe_flows([], [], None)
    assert line == "flows: no decoded flow yet" and hint == "" and state == {"rows": {}, "unowned": {}}


def test_session_log_writes_the_flows_line_every_interval_and_at_the_end(tmp_path: Path):
    pipe = TelemetryPipeline()
    clock = [100.0]
    log = SessionLog(tmp_path / "s.log", pipe, clock=lambda: clock[0])
    assert log.start()
    feed(pipe, 3, direction=OUTBOUND)
    feed(pipe, 10)
    log.tick()  # the first tick only starts the interval
    assert "flows:" not in (tmp_path / "s.log").read_text(encoding="utf-8")
    clock[0] += FLOWS_INTERVAL_S + 1
    log.tick()
    text = (tmp_path / "s.log").read_text(encoding="utf-8")
    assert text.count("flows: decoded ") == 1
    clock[0] += 5
    log.tick()
    assert (tmp_path / "s.log").read_text(encoding="utf-8").count("flows: decoded ") == 1  # not again before the interval
    log.close()
    assert (tmp_path / "s.log").read_text(encoding="utf-8").count("flows: decoded ") == 2  # and once more at the end


def test_session_log_reports_the_warning_once_when_it_appears_and_when_it_clears(tmp_path: Path):
    pipe = TelemetryPipeline()
    clock = [100.0]
    log = SessionLog(tmp_path / "s.log", pipe, clock=lambda: clock[0])
    assert log.start()
    feed(pipe, 3, direction=OUTBOUND)
    log.tick()
    feed(pipe, FLOW_SIZEABLE_PKTS + 5, remote_ip="198.51.100.77", remote_port=7778, local_port=50001)
    clock[0] += FLOWS_INTERVAL_S + 1
    log.tick()
    clock[0] += FLOWS_INTERVAL_S + 1
    log.tick()  # quiet window
    log.close()
    text = (tmp_path / "s.log").read_text(encoding="utf-8")
    assert text.count("flow warning: second game UDP flow 198.51.100.77:7778") == 1
    assert "flow warning cleared" in text


def test_a_pipeline_without_the_report_method_is_tolerated_by_the_session_log(tmp_path: Path):
    class Bare:
        server = None
        error_hook = None

        def add_observer(self, *a, **k): ...
        def remove_observer(self, *a, **k): ...
        def counts(self): return {}

    clock = [0.0]
    log = SessionLog(tmp_path / "b.log", Bare(), clock=lambda: clock[0])
    assert log.start()
    clock[0] += 100
    log.tick()
    log.close()


@pytest.mark.parametrize("n", [0, 1])
def test_status_text_has_no_hint_when_nothing_is_wrong(n):
    pipe = TelemetryPipeline()
    feed(pipe, n)
    pipe.update_flow_report(30.0)
    assert " · " not in pipe.status_text()


def test_live_capture_counts_packets_of_ports_the_game_does_not_own():
    pytest.importorskip("scapy")
    from scapy.layers.inet import IP, UDP
    from scapy.layers.l2 import Ether

    from isle_livemap.capture import LiveCapture

    pipe = TelemetryPipeline()
    live = LiveCapture(pipe)
    live._precap.set_ports((50000,), lambda at, pkt: None)
    owned = Ether() / IP(src="203.0.113.5", dst=LOCAL) / UDP(sport=7777, dport=50000) / (b"x" * 40)
    stray = Ether() / IP(src="203.0.113.5", dst=LOCAL) / UDP(sport=7777, dport=50999) / (b"x" * 300)
    live._on_packet(owned)
    live._on_packet(stray)
    (row,) = pipe.flows.unowned_rows()
    assert (row["src"], row["dst"], row["pkts"], row["bytes"]) == ("203.0.113.5:7777", f"{LOCAL}:50999", 1, 300)
    assert pipe.counts()["udp_total"] == 2  # both were seen; only the unowned one is in the inventory of ignored traffic
