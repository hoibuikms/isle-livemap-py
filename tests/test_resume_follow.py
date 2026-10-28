"""A creature must be followed without a gap in two cases that lost its track for 30-70 s in a recorded session:

* the server destroyed its handle (it left the relevancy range) and the same handle went on sending state batches with no
  creation record: the class, the PlayerState link and the anchor were gone, so the handle read as an unknown actor that
  is not creature-like until dozens of batches were counted;
* a creature without a creation record was placed once by the bootstrap and then ignored for the same reason.

Synthetic data only (the batch shapes come from the recorded sessions); the real ``_on_inbound`` loop is driven."""

from __future__ import annotations

import types

import pytest

import isle_livemap.decode as decode
from isle_livemap.capture import INBOUND, Datagram
from isle_livemap.decode import ACTOR_STATE_HEADER_BITS, IrisBatch, IrisPacket, KIND_STATE
from isle_livemap.peers import GamePeerSource
from isle_livemap.replay import BitWriter

HDR = 0x0C0000348
ME = (130_000.0, -320_000.0, 36_000.0)  # the local player, world cm


def state_payload(vec, handle: int, offset: int = 94, comp: int = 26, total: int = 300):
    """A datagram payload that is one state batch carrying a position vector (the layout the recordings show)."""
    w = BitWriter()
    w.write(HDR, ACTOR_STATE_HEADER_BITS)
    w.write(0xA5A5A5A5A5 & ((1 << (offset - ACTOR_STATE_HEADER_BITS)) - 1), offset - ACTOR_STATE_HEADER_BITS)
    w.write(comp | 64, 7)
    for v in vec:
        w.write(round(v * 100), comp)
    w.write(0, total - w.nbits)
    return w.to_bytes(), IrisBatch(handle, 0, w.nbits, True, False)


@pytest.fixture()
def source(monkeypatch) -> GamePeerSource:
    g = GamePeerSource()
    g.own_handle = 1
    g._own_position = lambda: ME  # noqa: SLF001
    g.identity.feed = lambda payload, packet: []  # no creation records in these packets
    monkeypatch.setattr(decode, "batch_kind", lambda bits, batch: KIND_STATE)
    monkeypatch.setattr(decode, "count_batches", lambda payload, packet: types.SimpleNamespace(creation=0, exports=0, destroyed=0))
    return g


def feed(g: GamePeerSource, monkeypatch, handle: int, vec, t: float, offset: int = 94) -> None:
    payload, batch = state_payload(vec, handle, offset=offset)
    packet = IrisPacket(1, True, True, [batch])
    monkeypatch.setattr(decode, "parse_iris_packet", lambda p: packet)
    g._on_inbound(Datagram(t, "10.0.0.2", 7777, "192.168.0.10", 50_000, payload, INBOUND))  # noqa: SLF001


def east(m: float, north_m: float = 0.0):
    return (ME[0] + m * 100.0, ME[1] + north_m * 100.0, ME[2])


# ---------------------------------------------------------------- the destroyed handle that goes on sending state
def test_a_destroyed_players_handle_that_goes_on_sending_state_is_followed_with_its_name(source, monkeypatch):
    g = source
    g._last_t = 100.0  # noqa: SLF001
    g._emit(5005, *east(300.0), "player", "Allosaurus", "P1")  # noqa: SLF001
    g._drop(5005)  # noqa: SLF001 - the server's destruction record
    assert g.store.get("5005") is None
    feed(g, monkeypatch, 5005, east(305.0), 138.6)  # 38 s later: a state batch, no creation record
    back = g.store.get("5005")
    assert back is not None and back.kind == "player" and back.name == "P1" and back.species == "Allosaurus"
    assert (back.x, back.y) == pytest.approx(east(305.0)[:2]) and back.pos_source == ""
    feed(g, monkeypatch, 5005, east(309.0, 2.0), 140.1, offset=101)  # the position moves to another bit offset: still followed
    assert (g.store.get("5005").x, g.store.get("5005").y) == pytest.approx(east(309.0, 2.0)[:2])
    assert g.store.get("5005").name == "P1"


def test_a_state_batch_right_after_the_destruction_record_is_a_late_packet_not_a_returning_creature(source, monkeypatch):
    g = source
    g._last_t = 100.0  # noqa: SLF001
    g._emit(5005, *east(300.0), "player", "Allosaurus", "P1")  # noqa: SLF001
    g._drop(5005)  # noqa: SLF001
    feed(g, monkeypatch, 5005, east(301.0), 100.5)  # 0.5 s later
    assert g.store.get("5005") is None
    assert 5005 not in g._creatures  # noqa: SLF001


def test_a_destroyed_handle_is_not_taken_back_after_the_resume_limit_or_when_the_server_created_it_again(source):
    g = source
    g._last_t = 100.0  # noqa: SLF001
    g._emit(5005, *east(300.0), "dino", "Allosaurus", "")  # noqa: SLF001
    g._drop(5005)  # noqa: SLF001
    assert g._resume(5005, 100.0 + g.RESUME_MAX_S + 1.0) == ("unknown", "", "")  # noqa: SLF001
    assert g._resume(5007, 110.0) == ("unknown", "", "")  # noqa: SLF001 - never destroyed
    assert g._resume(5005, 110.0) == ("dino", "Allosaurus", "")  # noqa: SLF001
    assert 5005 in g._resumed  # noqa: SLF001
    g._drop(5005)  # noqa: SLF001 - destroyed again: the next return starts over
    assert 5005 not in g._resumed  # noqa: SLF001


def test_the_resumed_handle_is_anchored_at_its_last_position(source):
    g = source
    g._last_t = 100.0  # noqa: SLF001
    g._emit(5005, *east(300.0), "player", "Allosaurus", "P1")  # noqa: SLF001
    g._drop(5005)  # noqa: SLF001
    g._resume(5005, 140.0)  # noqa: SLF001
    assert g.locator.last_known(5005) == pytest.approx(east(300.0))


def test_a_reset_forgets_the_destroyed_handles_of_the_old_connection(source):
    g = source
    g._last_t = 100.0  # noqa: SLF001
    g._emit(5005, *east(300.0), "player", "Allosaurus", "P1")  # noqa: SLF001
    g._drop(5005)  # noqa: SLF001
    assert 5005 in g._zombies  # noqa: SLF001
    g.on_reset()
    assert 5005 not in g._zombies and not g._resumed  # noqa: SLF001


# ---------------------------------------------------------------- the creature the bootstrap placed
def test_a_creature_the_bootstrap_placed_is_followed_from_the_next_batch_on(source, monkeypatch):
    g = source
    g.locator._trusted[HDR] = {94}  # noqa: SLF001 - another creature's position was learned at this offset of this header
    feed(g, monkeypatch, 5008, east(200.0), 10.0)
    assert g.store.get("5008") is None  # one candidate proves nothing
    feed(g, monkeypatch, 5008, east(210.0), 11.4)  # a second consistent batch: placed
    assert g.store.get("5008") is not None and g.store.get("5008").kind == "unknown"
    feed(g, monkeypatch, 5008, east(232.0, 3.0), 12.9, offset=88)  # 22 m further (15 m/s), the vector at another offset
    ent = g.store.get("5008")
    assert (ent.x, ent.y) == pytest.approx(east(232.0, 3.0)[:2]) and ent.pos_source == ""
    feed(g, monkeypatch, 5008, east(254.0, 6.0), 14.4, offset=97)
    assert (g.store.get("5008").x, g.store.get("5008").y) == pytest.approx(east(254.0, 6.0)[:2])


def test_an_actor_that_never_moves_like_a_creature_is_not_made_one(source, monkeypatch):
    g = source
    feed(g, monkeypatch, 5009, east(200.0), 10.0)
    feed(g, monkeypatch, 5009, east(900.0, 400.0), 11.4)  # 700 m in 1.4 s: not the same thing
    assert g.store.get("5009") is None and 5009 not in g._creatures  # noqa: SLF001
