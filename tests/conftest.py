import base64
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def recorded() -> dict[str, str]:
    return json.loads((FIXTURES / "recorded_payloads.json").read_text())


@pytest.fixture(scope="session")
def b64(recorded):
    return lambda name: base64.b64decode(recorded[name])


@pytest.fixture(scope="session")
def hexb(recorded):
    return lambda name: bytes.fromhex(recorded[name])


@pytest.fixture(scope="session")
def join_packets() -> list[dict]:
    """Inbound datagrams of the live join capture (payload bytes only), in capture order. Each entry: group,
    cap_packet (its number in the capture), t (seconds since the capture's first packet), payload (bytes)."""
    data = json.loads((FIXTURES / "recorded_join_payloads.json").read_text())
    return [{**r, "payload": base64.b64decode(r["payload"])} for r in data["packets"]]


def retimed(payload: bytes, seconds: float) -> bytes:
    """A recorded outbound move with its client clock advanced by ``seconds``. The recorded payloads are single frames
    of a live stream; repeating one unchanged is a constant timestamp, which is not a move (tracking._MIN_TS_RATE)."""
    import struct

    from isle_livemap import decode as D

    cands = D.decode_movement(payload)
    if not cands:
        return payload
    bits = D.Bits(payload)
    best = max(cands, key=lambda c: c.client_timestamp)  # the move itself carries a positive clock
    ts_off = None
    for accel_bits in range(D._MIN_COMP_BITS, D._MAX_COMP_BITS + 1):
        accel_off = best.location_bit_offset - D._HEADER_BITS - accel_bits * 3
        off = accel_off - D._FLOAT_BITS
        if off < 0:
            continue
        if (bits.read(accel_off, D._HEADER_BITS) & 63) != accel_bits:
            continue
        if D.f32(bits.read(off, D._FLOAT_BITS)) == best.client_timestamp:
            ts_off = off
            break
    assert ts_off is not None, "client timestamp field not found"
    new = struct.unpack("<I", struct.pack("<f", best.client_timestamp + seconds))[0]
    big = bits.big & ~(D.mask(32) << ts_off) | (new << ts_off)
    return big.to_bytes(len(payload), "little")
