"""Capture loss: the game acknowledges every server packet it received in the header of its own packets, so a sequence it
acknowledged and the sniffer never delivered is a packet the capture lost (``capture.LossMeter``)."""

from __future__ import annotations

import pytest

from isle_livemap import decode as D
from isle_livemap.capture import (
    INBOUND, LOSS_GRACE_S, LOSS_HINT_MIN_ACKED, LOSS_HINT_RATE, OUTBOUND, Datagram, LossMeter, TelemetryPipeline,
)
from isle_livemap.replay import BitWriter

SERVER_IP, SERVER_PORT, LOCAL_IP, LOCAL_PORT = "203.0.113.5", 7777, "192.168.0.10", 50000


def notify_payload(seq: int, acked: int, history: tuple[int, ...] = (0,)) -> bytes:
    """A datagram that holds only the packet-notify header (the layout parse_iris_packet / parse_packet_notify read)."""
    w = BitWriter()
    w.write(0, 2)   # serialized int, maximum 4
    w.write(0, 3)   # serialized int, maximum 8
    w.write(0, 1)   # no handshake
    w.write(0, 8)
    w.write((seq << 18) | (acked << 4) | (len(history) - 1), 32)
    for word in history:
        w.write(word, 32)
    w.write(0, 1)
    w.write(1, 1)   # end of the bunches
    w.write(1, 1)   # end of the packet
    return w.to_bytes()


def inbound(t: float, seq: int) -> Datagram:
    return Datagram(t, SERVER_IP, SERVER_PORT, LOCAL_IP, LOCAL_PORT, notify_payload(seq, 0), INBOUND)


def outbound(t: float, acked: int, history: tuple[int, ...] = (0,)) -> Datagram:
    return Datagram(t, LOCAL_IP, LOCAL_PORT, SERVER_IP, SERVER_PORT, notify_payload(0, acked, history), OUTBOUND)


@pytest.mark.parametrize("seq,acked,history", [(0, 0, (0,)), (1, 16383, (0xFFFFFFFF,)), (12345, 321, (0x80000001, 5)), (16383, 7, (1, 2, 3))])
def test_packet_notify_header_roundtrip(seq, acked, history):
    assert D.parse_packet_notify(notify_payload(seq, acked, history)) == (seq, acked, history)


def test_packet_notify_header_rejects_what_has_no_header():
    assert D.parse_packet_notify(b"") is None
    assert D.parse_packet_notify(b"\x00\x00") is None


def test_packet_notify_sequence_agrees_with_the_replication_parser_on_recorded_packets(join_packets):
    checked = 0
    for rec in join_packets:
        pk = D.parse_iris_packet(rec["payload"])
        if pk is None:
            continue
        header = D.parse_packet_notify(rec["payload"])
        assert header is not None and header[0] == pk.packet_sequence
        checked += 1
    assert checked > 10


def history_for(*acked_before: int, newest: int) -> tuple[int, ...]:
    """History words in which exactly the given sequences (older than ``newest``) are marked as received."""
    word = 0
    for s in acked_before:
        word |= 1 << ((newest - 1 - s) & 0x3FFF)
    return (word,)


def test_an_acknowledged_sequence_the_capture_never_saw_is_lost():
    m = LossMeter()
    for seq, t in ((100, 1.0), (101, 1.1), (103, 1.3)):  # 102 never arrives
        m.note_inbound(seq, t)
    m.note_ack(103, history_for(101, 102, 100, newest=103), 1.4)
    line, rate, acked = m.report(1.4 + LOSS_GRACE_S + 0.1)
    assert (m.acked, m.lost) == (4, 1) and acked == 4 and rate == pytest.approx(0.25)
    assert "1 of 4" in line


def test_a_sequence_the_game_did_not_receive_is_not_a_capture_loss():
    m = LossMeter()
    for seq in (100, 101, 103):
        m.note_inbound(seq, 1.0)
    m.note_ack(103, history_for(101, 100, newest=103), 1.4)  # 102 is not marked: the network lost it, the capture did not
    m.report(1.4 + LOSS_GRACE_S + 0.1)
    assert (m.acked, m.lost) == (3, 0)


def test_the_grace_lets_a_late_capture_order_pass():
    m = LossMeter()
    m.note_inbound(100, 1.0)
    m.note_ack(101, history_for(100, newest=101), 1.05)  # the ack is captured before packet 101 itself
    m.note_inbound(101, 1.1)
    m.report(1.05 + LOSS_GRACE_S + 0.1)
    assert (m.acked, m.lost) == (2, 0)


def test_acknowledgements_of_sequences_older_than_the_capture_are_ignored():
    m = LossMeter()
    m.note_inbound(500, 1.0)  # the capture began here
    m.note_ack(501, history_for(500, 499, 498, 300, newest=501), 1.2)
    m.report(1.2 + LOSS_GRACE_S + 0.1)
    assert m.acked == 2 and m.lost == 1  # 500 seen, 501 missing; 499, 498, 300 predate the first captured packet


def test_a_sequence_is_judged_once_however_often_it_is_acknowledged():
    m = LossMeter()
    m.note_inbound(10, 1.0)
    for t in (1.1, 1.2, 1.3):
        m.note_ack(11, history_for(10, newest=11), t)
    m.report(5.0)
    assert (m.acked, m.lost) == (2, 1)


def test_the_sequence_wraps_at_14_bits():
    m = LossMeter()
    for seq, t in ((16382, 1.0), (16383, 1.1), (1, 1.3)):  # 0 is missing
        m.note_inbound(seq, t)
    m.note_ack(1, history_for(0, 16383, 16382, newest=1), 1.4)
    m.report(5.0)
    assert (m.acked, m.lost) == (4, 1)


def test_hint_rises_at_the_rate_and_clears_when_it_falls():
    m = LossMeter()
    n = LOSS_HINT_MIN_ACKED
    lost_every = 20  # 5% of the packets, above the warning rate
    t = 0.0
    for seq in range(n + 20):
        if seq % lost_every:
            m.note_inbound(seq, t)
        t += 0.01
    for seq in range(n + 20):
        m.note_ack(seq, (0,), t)
    m.report(t + LOSS_GRACE_S + 1)
    assert m.hint.startswith("capture missed")
    for seq in range(n + 20, 3 * n):  # a clean window
        m.note_inbound(seq, t); m.note_ack(seq, (0,), t)
    m.report(t + 3 * LOSS_GRACE_S + 2)
    assert m.hint == ""


def test_a_short_window_never_raises_the_hint():
    m = LossMeter()
    m.note_inbound(0, 0.0)
    for seq in range(1, 11):
        m.note_ack(seq, (0,), 0.1)
    m.report(10.0)
    assert m.lost == 10 and m.hint == ""  # 10 acknowledged packets are too few to warn on


def test_the_pipeline_counts_loss_from_both_directions_and_resets_with_the_server():
    pipe = TelemetryPipeline()
    pipe.handle(outbound(0.9, 0))                                  # learns the server
    for seq, t in ((1, 1.0), (2, 1.1), (4, 1.3)):                  # 3 is lost by the capture
        pipe.handle(inbound(t, seq))
    pipe.handle(outbound(1.4, 4, history_for(3, 2, 1, newest=4)))
    line = pipe.update_loss_report(now=1.4 + LOSS_GRACE_S + 0.1)
    assert "1 of" in line and pipe.loss.lost == 1
    pipe.reset()
    assert (pipe.loss.acked, pipe.loss.lost) == (0, 0) and pipe.loss_hint == ""


def test_the_status_text_carries_the_hint():
    pipe = TelemetryPipeline()
    pipe.loss.hint = "capture missed 9% of the game's packets"
    assert "capture missed 9%" in pipe.status_text()


def feed_window(pipe, first_seq, count, t0, lost_every=0):
    """``count`` server packets acknowledged one by one; every ``lost_every``-th is never delivered to the capture."""
    for i in range(count):
        seq, t = first_seq + i, t0 + 0.01 * i
        if not (lost_every and seq % lost_every == 0):
            pipe.handle(inbound(t, seq))
        pipe.handle(outbound(t + 0.005, seq))
    pipe.handle(outbound(t0 + 0.01 * count + LOSS_GRACE_S + 1.0, first_seq + count - 1))  # moves the datagram clock on


def test_the_session_log_writes_the_loss_line_and_the_warning_once(tmp_path):
    from isle_livemap.sessionlog import FLOWS_INTERVAL_S, SessionLog

    pipe = TelemetryPipeline()
    pipe.handle(outbound(0.5, 0))
    clock = [100.0]
    log = SessionLog(tmp_path / "s.log", pipe, clock=lambda: clock[0])
    assert log.start()
    log.tick()  # the first tick only starts the interval
    feed_window(pipe, 1, 200, 1.0, lost_every=10)  # 10% missing
    clock[0] += FLOWS_INTERVAL_S + 1
    log.tick()
    feed_window(pipe, 400, 200, 20.0)  # a clean window
    clock[0] += FLOWS_INTERVAL_S + 1
    log.tick()
    log.close()
    text = (tmp_path / "s.log").read_text(encoding="utf-8")
    assert text.count("capture loss: ") >= 2
    assert text.count("capture loss warning: capture missed 10%") == 1
    assert "capture loss warning cleared" in text


def test_a_pipeline_without_a_loss_meter_does_not_break_the_log(tmp_path):
    from isle_livemap.sessionlog import FLOWS_INTERVAL_S, SessionLog

    class Bare:
        latest = None
        def add_observer(self, *a, **k): pass
        def remove_observer(self, *a, **k): pass
        def status_text(self): return ""
        def counts(self): return {}

    clock = [0.0]
    log = SessionLog(tmp_path / "b.log", Bare(), clock=lambda: clock[0])
    assert log.start()
    clock[0] += FLOWS_INTERVAL_S * 3
    log.tick()
    log.close()
