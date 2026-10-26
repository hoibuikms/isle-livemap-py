"""The server's own copy of the own location (three doubles in an inbound packet) checked against the decoded own position:
in every recorded connection they agree to a few metres, so the own marker needs no frame calibration; a disagreement is
counted and logged, never applied."""

import struct

from isle_livemap.capture import INBOUND, OUTBOUND, Datagram, TelemetryPipeline
from isle_livemap.decode import find_server_locations
from isle_livemap.replay import encode_move_payload

CLIENT, SERVER = ("192.168.0.10", 50_000), ("203.0.113.5", 7777)


def doubles_at(bit_shift: int, x_m: float, y_m: float, z_m: float, pad: int = 9, tail: int = 11) -> bytes:
    """A packet with the three doubles (cm) starting ``bit_shift`` bits into byte ``pad``; the rest is filler."""
    raw = b"".join(struct.pack("<d", v * 100.0) for v in (x_m, y_m, z_m))
    value = (int.from_bytes(raw, "little") << bit_shift) | (0b10110 if bit_shift else 0)
    body = value.to_bytes(len(raw) + 1, "little")
    return bytes([0x11] * pad) + body + bytes([0x22] * tail)


def locked(x=1700.0, y=2070.0, z=203.0):
    pipe = TelemetryPipeline()
    for i in range(20):
        payload = encode_move_payload(x * 100.0, y * 100.0, z * 100.0, 10.0, 40.0 + i * 0.1)
        pipe.handle(Datagram(100.0 + i * 0.1, *CLIENT, *SERVER, payload, OUTBOUND))
    assert pipe.latest.position is not None
    return pipe, 102.0


def inbound(pipe, t, payload):
    pipe.handle(Datagram(t, *SERVER, *CLIENT, payload, INBOUND))


def test_a_triple_of_doubles_is_found_at_any_bit_offset():
    for shift in range(8):
        found = find_server_locations(doubles_at(shift, 1702.6, 2071.4, 203.3))
        assert any(abs(x - 170260) < 1 and abs(y - 207140) < 1 and abs(z - 20330) < 1 for x, y, z in found), shift


def test_small_or_missing_triples_are_not_locations():
    assert find_server_locations(b"\x00" * 64) == []
    assert find_server_locations(doubles_at(0, 12.0, 0.0, 0.1)) == []  # near the origin: other data
    assert find_server_locations(b"\x01\x02") == []


def test_a_repeated_server_location_that_agrees_is_counted_and_not_far():
    pipe, t = locked()
    inbound(pipe, t, doubles_at(3, 1702.0, 2071.0, 203.3))
    assert pipe._counts["server_fixes"] == 0  # one packet is not a confirmation
    inbound(pipe, t + 0.2, doubles_at(5, 1702.0, 2071.0, 203.3, pad=12))
    assert pipe._counts["server_fixes"] == 1 and pipe._counts["server_fixes_far"] == 0 and pipe.frame_checks == []


def test_a_server_location_far_from_the_decoded_position_is_logged_but_never_applied():
    pipe, t = locked()
    before = (pipe.latest.position.x, pipe.latest.position.y)
    inbound(pipe, t, doubles_at(0, 2500.0, 1200.0, 203.0))
    inbound(pipe, t + 0.3, doubles_at(2, 2500.0, 1200.0, 203.0, pad=14))
    assert pipe._counts["server_fixes_far"] == 1
    at, metres, (sx, sy, sz) = pipe.frame_checks[-1]
    assert metres > 1000.0 and abs(sx - 250_000.0) < 1.0
    assert (pipe.latest.position.x, pipe.latest.position.y) == before  # the marker is not moved by it


def test_an_unrelated_triple_with_another_height_or_a_single_packet_is_ignored():
    pipe, t = locked()
    inbound(pipe, t, doubles_at(0, 1700.0, 2070.0, 900.0))
    inbound(pipe, t + 0.2, doubles_at(0, 1700.0, 2070.0, 900.0, pad=13))  # 700 m above the own height
    inbound(pipe, t + 3.0, doubles_at(0, 1700.0, 2070.0, 203.0))  # the repeat comes too late
    assert pipe._counts["server_fixes"] == 0
