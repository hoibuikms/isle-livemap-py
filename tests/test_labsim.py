import asyncio
import math

import pytest

from labsim import telemetry as t


def test_round_trip():
    n = t.Node(7, 1.5, -2.25, 300.0, 42, "Alpha")
    out = t.decode(t.encode(n))
    assert (out.id, out.x, out.y, out.z, out.health, out.name) == (7, 1.5, -2.25, 300.0, 42, "Alpha")


def test_round_trip_utf8_name():
    assert t.decode(t.encode(t.Node(1, 0, 0, 0, 1, "Hươu_Đỏ"))).name == "Hươu_Đỏ"


def test_empty_name():
    assert t.decode(t.encode(t.Node(1, 0, 0, 0, 1, ""))).name == ""


def test_short_packet_raises():
    with pytest.raises(ValueError):
        t.decode(b"\x00" * (t.HEADER.size - 1))


def test_invalid_utf8_is_replaced():
    data = t.HEADER.pack(1, 0, 0, 0, 5) + b"\xff\xfeOK"
    n = t.decode(data)
    assert n.health == 5 and n.name.endswith("OK") and "�" in n.name


ME = t.Node(0, 100.0, 50.0, 200.0, 100, "me")


def test_filter_and_sort():
    a = t.Node(1, 105, 52, 202, 100, "a")      # ~5.74
    b = t.Node(2, 300, 10, 400, 50, "b")       # far
    c = t.Node(3, 100, 50, 210, 80, "c")       # 10
    rows = t.nearby(ME, [b, c, a], 100.0)
    assert [r[2].name for r in rows] == ["a", "c"]
    assert rows[0][0] == pytest.approx(math.sqrt(25 + 4 + 4))


def test_delta_y_sign():
    rows = t.nearby(ME, [t.Node(1, 100, 60, 200, 1, "up"), t.Node(2, 100, 40, 200, 1, "down")])
    by = {r[2].name: r[1] for r in rows}
    assert by["up"] == 10 and by["down"] == -10


def test_max_distance_is_exclusive():
    assert t.nearby(ME, [t.Node(1, 200, 50, 200, 1, "edge")], 100.0) == []


def test_render_has_columns():
    out = t.render(t.nearby(ME, [t.Node(1, 105, 52, 202, 100, "a")]))
    assert "Distance (m)" in out and "+2.0 (higher)" in out


def test_store_expiry_and_player():
    now = [0.0]
    s = t.Store(ttl=5, clock=lambda: now[0])
    s.update(ME)
    s.update(t.Node(1, 0, 0, 0, 1, "a"))
    assert s.me is ME and len(s.entities()) == 1
    now[0] = 10
    assert s.entities() == []


def test_end_to_end_loopback():
    store = asyncio.run(t.run(duration=0.6, port=17777))
    assert store.me is not None and store.me.name == "me"
    assert len(store.entities()) == 8
