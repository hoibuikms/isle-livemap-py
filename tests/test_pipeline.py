"""Capture-side pure logic + end-to-end synthetic pipeline."""

from __future__ import annotations

import math
import struct

import pytest

from isle_livemap.capture import (
    INBOUND, OUTBOUND, Datagram, TelemetryPipeline, build_capture_filter, classify_direction, parse_udp_owner_table,
)
from isle_livemap.decode import decode_movement
from isle_livemap.geo import bearing_deg, project
from isle_livemap.replay import SyntheticSource, encode_move_payload


def test_capture_filter_ports_sorted_both_directions():
    assert build_capture_filter([7778, 7777]) == "udp and (src port 7777 or dst port 7777 or src port 7778 or dst port 7778)"


def test_capture_filter_outbound_only_and_empty():
    assert build_capture_filter([7778, 7777], include_inbound=False) == "udp and (src port 7777 or src port 7778)"
    assert build_capture_filter([]) == "udp and (false)"


@pytest.mark.parametrize("src,dst,expected", [(50_000, 7_777, INBOUND), (7_777, 50_000, OUTBOUND)])
def test_direction_uses_game_owned_local_port(src, dst, expected):
    assert classify_direction(src, dst, {7_777}) == expected


@pytest.mark.parametrize("src,dst", [(50_000, 50_001), (7_777, 7_777)])
def test_direction_ignores_unrelated_or_loopback(src, dst):
    assert classify_direction(src, dst, {7_777}) is None


def test_udp_owner_table_parsing():
    def row(addr, port, pid):
        return struct.pack("<III", addr, ((port & 0xFF) << 8) | (port >> 8), pid)

    blob = struct.pack("<I", 3) + row(0, 7777, 42) + row(0, 9999, 7) + row(0, 7778, 42)
    assert parse_udp_owner_table(blob, 42) == {7777, 7778}
    assert parse_udp_owner_table(b"", 42) == set()
    assert parse_udp_owner_table(struct.pack("<I", 99), 42) == set()  # count larger than data


def test_encoder_round_trips_through_real_decoder():
    payload = encode_move_payload(153_610.82, 283_609.52, 20_389.74, 172.71, 123.5)
    cands = decode_movement(payload)
    hit = [c for c in cands if abs(c.x - 153_610.82) < 0.01 and abs(c.y - 283_609.52) < 0.01]
    assert len(hit) == 1
    assert hit[0].z == pytest.approx(20_389.74, abs=0.01)
    assert hit[0].unreal_yaw_degrees == pytest.approx(172.71, abs=0.01)
    assert hit[0].client_timestamp == 123.5


def test_synthetic_stream_locks_and_matches_truth():
    src = SyntheticSource()
    pipe = TelemetryPipeline()
    last = None
    for d in src.datagrams(20.0):
        snap = pipe.handle(d)
        if snap is not None:
            last = snap
    assert last is not None and last.position is not None
    # the decoded fix tracks the generator's ground truth to quantisation error
    t_end = (20.0 * 30 - 1) / 30
    x, y, _ = src.pose(t_end)
    assert math.hypot(last.position.x - x, last.position.y - y) < 600  # <6 m (within 1-2 packets of the end)
    ph = project(last.position.x, last.position.y)
    assert 0.0 < ph[0] < 1.0 and 0.0 < ph[1] < 1.0


def test_synthetic_vitals_come_from_recorded_packets():
    src = SyntheticSource()
    pipe = TelemetryPipeline()
    for d in src.datagrams(8.0):
        pipe.handle(d)
    v = pipe.latest.vitals
    assert v is not None
    assert v.growth == pytest.approx(0.10464542, abs=1e-6)
    assert v.health == pytest.approx(4.6011972, abs=1e-6)
    assert v.thirst == pytest.approx(837.14087, abs=1e-4)


def test_inbound_from_other_endpoint_is_ignored():
    pipe = TelemetryPipeline()
    from isle_livemap.replay import load_demo_vitals

    pkt = load_demo_vitals()[1]
    for i in range(4):
        pipe.handle(Datagram(float(i), "6.6.6.6", 7777, "192.168.0.10", 50000, pkt, INBOUND))
    assert pipe.latest.vitals is None


def test_heading_in_stream_follows_travel_direction():
    src = SyntheticSource()
    (x0, y0, yaw0), (x1, y1, _) = src.pose(1.0), src.pose(1.2)
    travel = bearing_deg(x0, y0, x1, y1)
    assert (yaw0 + 90) % 360 == pytest.approx(travel, abs=0.5)
