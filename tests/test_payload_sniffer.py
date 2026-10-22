import asyncio
import socket
import struct
import threading
import time

import pytest

from labsim import payload_sniffer as ps
from labsim import telemetry as t


def pack(id_=7, x=1.5, y=-2.25, z=300.0, status=42, name=b"Alpha"):
    return struct.pack("<IfffH", id_, x, y, z, status) + name


def test_header_size():
    assert ps.RECORD.size == 18


def test_parse_hand_packed():
    r = ps.parse_payload(pack())
    assert (r.id, r.x, r.y, r.z, r.status, r.name) == (7, 1.5, -2.25, 300.0, 42, "Alpha")


def test_parse_raw_bytes_little_endian():
    raw = bytes([1, 0, 0, 0]) + struct.pack("<fff", 0, 0, 0) + bytes([0x34, 0x12])
    r = ps.parse_payload(raw)
    assert r.id == 1 and r.status == 0x1234 and r.name == ""


def test_parse_utf8_name():
    assert ps.parse_payload(pack(name="Hươu".encode())).name == "Hươu"


@pytest.mark.parametrize("data", [b"", b"\x00", b"\x00" * 17])
def test_too_short_returns_none(data):
    assert ps.parse_payload(data) is None


def test_non_bytes_returns_none():
    assert ps.parse_payload(None) is None and ps.parse_payload("text") is None


def test_invalid_utf8_replaced():
    r = ps.parse_payload(pack(name=b"\xff\xfeOK"))
    assert r.status == 42 and r.name.endswith("OK") and "�" in r.name


def test_parser_matches_labsim_encoder():
    r = ps.parse_payload(t.encode(t.Node(3, 1.0, 2.0, 3.0, 55, "Node_3")))
    assert (r.id, r.status, r.name) == (3, 55, "Node_3")


def test_handle_counts_and_survives_bad_input_and_callback_errors():
    seen = []

    def cb(rec):
        if rec.id == 99:
            raise RuntimeError("boom")
        seen.append(rec)

    s = ps.PayloadSniffer(cb)
    for d in (b"short", pack(id_=99), pack(id_=1)):
        s._handle(d)
    assert s.captured == 3 and s.malformed == 2 and [r.id for r in seen] == [1]


def test_unknown_backend():
    with pytest.raises(ValueError):
        ps.PayloadSniffer(lambda r: None, backend="eth0")


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_socket_backend_loopback_with_simulator():
    port = _free_port()
    got = []
    with ps.PayloadSniffer(got.append, port=port, backend="socket") as sn:
        stop = asyncio.Event()

        async def go():
            task = asyncio.create_task(t.simulator(stop, port=port, count=3, hz=50))
            await asyncio.sleep(0.3)
            stop.set()
            await task

        asyncio.run(go())
        assert _wait(lambda: len({r.id for r in got}) == 4)  # me + 3 entities
    assert sn.malformed == 0 and {r.name for r in got} >= {"me", "Node_1"}


def test_socket_backend_port_in_use_reports_error():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        with pytest.raises(RuntimeError):
            ps.PayloadSniffer(lambda r: None, port=s.getsockname()[1], backend="socket").start()


def test_scapy_backend_loopback():
    pytest.importorskip("scapy")
    port = _free_port()
    got = []
    sn = ps.PayloadSniffer(got.append, port=port, backend="scapy")
    try:
        sn.start()
    except RuntimeError as exc:
        pytest.skip(f"scapy capture unavailable: {exc}")
    try:
        time.sleep(0.5)  # let the sniffer attach
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            for _ in range(5):
                tx.sendto(pack(id_=5), ("127.0.0.1", port))
                time.sleep(0.05)
        if sn.error is not None:
            pytest.skip(f"scapy capture unavailable: {sn.error}")
        if not _wait(lambda: got):
            pytest.skip("no packets captured (missing privileges?)")
        assert got[0].id == 5
    finally:
        sn.stop()
