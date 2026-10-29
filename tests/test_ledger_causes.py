"""The causes of a missed or vanishing creature that tools/tier_check.py --ledger found, one test each (synthetic data).

The ledger compares every creature the raw stream says exists with what the minimap drew. What it found on our side:
  * a creature the server replicates but that has no position was not mentioned at all while the join tables were missed;
  * the server destroys and re-creates a creature far more often than a 12 s ghost covers, and a ghost said nothing;
  * a creature that rests for minutes was dropped after 180 s although no destruction record came;
  * a creature walking along the view radius flickered between a dot and an edge pointer."""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_peers_ui import FakePeers, make_view, tick_twice  # noqa: E402

from isle_livemap.geo import (  # noqa: E402
    GHOST_GUARANTEE_S, Entity, EntitySmoother, Nearby, entity_label_lines, gone_text, is_guaranteed, priority_key, unplaced_row,
)
from isle_livemap.peers import GamePeerSource, PeerEntity, PeerSnapshot  # noqa: E402


class Clock:
    t = 1000.0

    def __call__(self) -> float:
        return self.t


def dino(eid="1", x=0.0, y=0.0, **kw):
    return Entity(eid, kw.pop("kind", "dino"), kw.pop("name", "P1"), kw.pop("species", "Allosaurus"), x, y, 30_000.0, **kw)


# ---------------------------------------------------------------- a creature the server replicates but we cannot place
def test_a_creature_with_a_creation_maximum_health_and_no_position_is_listed_after_a_few_batches():
    g = GamePeerSource(clock=Clock())
    g._last_t = 100.0
    g._creatures.add(100_700)  # its creation record carried a maxHealth: certainly a creature, whatever the class says
    for _ in range(3):
        g._note_unplaced(100_700, 120, 99.0)
    rows = g.unplaced_creatures()
    assert [r["handle"] for r in rows] == [100_700] and rows[0]["batches"] == 3 and g.hidden_creatures() == 1
    assert g.snapshot().unplaced == 1  # the overlay and the Nearby panel read it from the snapshot


def test_a_resting_creature_that_was_quiet_for_minutes_is_still_listed():
    g = GamePeerSource(clock=Clock())
    g._creatures.add(100_701)
    for _ in range(5):
        g._note_unplaced(100_701, 120, 10.0)
    g._last_t = 10.0 + 200.0  # 200 s without a state batch, still no destruction record
    assert g.hidden_creatures() == 1
    g._hidden_cache = (-1e9, [])
    g._last_t = 10.0 + 400.0  # long enough that a missed destruction record is the likelier explanation
    assert g.hidden_creatures() == 0


def test_a_server_animal_or_a_placed_creature_is_not_listed():
    g = GamePeerSource(clock=Clock())
    g._last_t = 100.0
    g._creatures.update((100_702, 100_703))
    for h in (100_702, 100_703):
        for _ in range(5):
            g._note_unplaced(h, 120, 99.0)
    g.store.update(Entity("100703", "dino", "P1", "Allosaurus", 0.0, 0.0))
    for _ in range(12):
        g._note_shape(100_704, 155)
        g._note_unplaced(100_704, 155, 99.0)  # the repeated animal state
    assert [r["handle"] for r in g.unplaced_creatures()] == [100_702]


def test_the_nearby_table_names_what_it_knows_of_a_creature_without_a_position():
    row = unplaced_row({"handle": 5, "kind": "dino", "species": "Tyrannosaurus", "name": "", "batches": 42, "silent_s": 3.0})
    assert row[0] == "Player ?" and row[1] == "Tyrannosaurus" and row[7] == "position unknown" and row[8] == "42 state updates"
    assert unplaced_row({"handle": 6, "kind": "player", "species": "", "name": "P1", "batches": 5})[:2] == ("P1", "?")


class CountingPeers(FakePeers):
    def __init__(self, entries=(), unplaced=0):
        super().__init__(entries)
        self.unplaced = unplaced

    def snapshot(self):
        return PeerSnapshot(tuple(self.entries), self.version, self.status, self.enabled, self.unplaced)


def test_the_minimap_says_how_many_creatures_have_no_position(tmp_path):
    view = make_view(tmp_path)
    view.resize(360, 360)
    view.peers = CountingPeers([], unplaced=3)
    tick_twice(view)
    assert view.peer_unplaced == 3
    view.peers.unplaced = 0
    view.peers.version += 1
    tick_twice(view)
    assert view.peer_unplaced == 0


def test_a_label_never_lands_on_the_line_that_says_how_many_are_unplaced(tmp_path):
    view = make_view(tmp_path)
    view.resize(360, 360)
    view.peers = CountingPeers([], unplaced=2)
    tick_twice(view)
    r = view._banner_layout()[1]
    assert r is not None and r.bottom() <= view.height() and r.width() > 100


# ---------------------------------------------------------------- the server destroys a creature: a ghost that says so
def test_a_ghost_is_held_45_seconds_and_knows_how_long_it_has_been_gone():
    assert GamePeerSource.GHOST_GRACE_S == 45.0
    s = EntitySmoother(ttl_s=600.0)
    s.missing_grace_s, s.ghost_grace_s, s.ghost_kinds, s.ghost_age_s = 1.5, 45.0, frozenset(("player", "dino")), 45.0
    s.update([dino("d"), dino("f", kind="ai", species="Longear")], now=0.0)
    s.update([], now=1.0)
    s.prune(30.0)
    assert set(s.entities) == {"d"} and s.ghosts() == ["d"]  # the fish left at once, the dino is still there
    assert s.gone_s("d", 30.0) == pytest.approx(29.0) and s.gone_s("f", 30.0) is None
    s.prune(45.5)
    assert "d" in s.entities
    s.prune(46.5)
    assert s.entities == {} and s.ghosts() == []


def test_a_creature_that_is_still_replicated_is_not_a_ghost():
    s = EntitySmoother(ttl_s=600.0)
    s.missing_grace_s, s.ghost_grace_s, s.ghost_kinds = 1.5, 45.0, frozenset(("dino",))
    s.update([dino("d")], now=0.0)
    assert s.gone_s("d", 100.0) is None and s.ghosts() == []


def test_the_label_of_a_ghost_says_how_long_ago_the_server_destroyed_it_in_steps_of_ten_seconds():
    assert [gone_text(s) for s in (0.0, 9.9, 10.0, 29.0, 59.0, 61.0)] == ["gone <10s", "gone <10s", "gone 10s", "gone 20s", "gone 50s", "gone 1m"]
    n = Nearby(dino("1", name="P1"), 0.0, 0.0, 300.0, 0.0, gone_s=23.0)
    assert entity_label_lines(n)[0] == "P1 - Allosaurus - 300 m (gone 20s)"
    assert entity_label_lines(Nearby(dino("1", name="P1"), 0.0, 0.0, 300.0, 0.0))[0] == "P1 - Allosaurus - 300 m"


def test_a_ghost_never_takes_a_place_from_a_live_creature():
    live, ghost = Nearby(dino("1"), 0.0, 0.0, 200.0, 0.0), Nearby(dino("2"), 0.0, 0.0, 100.0, 0.0, gone_s=30.0)
    assert priority_key(live) < priority_key(ghost)  # in the same distance tier, although the ghost was nearer
    assert priority_key(Nearby(dino("3"), 0.0, 0.0, 600.0, 0.0)) > priority_key(ghost)  # the distance tiers still come first
    assert is_guaranteed(live) and not is_guaranteed(ghost)  # the guaranteed label is for live creatures: a ghost only gets a free spot
    assert GHOST_GUARANTEE_S == 0.0


def test_the_view_hands_the_ghost_age_to_the_label_and_the_marker_fades(tmp_path):
    view = make_view(tmp_path)
    view.resize(360, 360)
    view.trace_frames = True
    src = GamePeerSource(clock=Clock())
    view.peers = FakePeers([PeerEntity(dino("d", view.interp.x + 20_000.0, view.interp.y), time.monotonic())])
    view.peers.ttl_s, view.peers.gone_grace_s, view.peers.ghost_grace_s = src.ttl_s, src.gone_grace_s, src.ghost_grace_s
    view.peers.ghost_kinds, view.peers.stale_s = src.ghost_kinds, src.stale_s
    tick_twice(view)
    assert all(n.gone_s is None for n in view.nearby())
    view.peers.set([])  # the server destroyed it
    tick_twice(view)
    view.tick()
    ghosts = [n for n in view.nearby() if n.entity.id == "d"]
    assert ghosts and ghosts[0].gone_s is not None and ghosts[0].stale


# ---------------------------------------------------------------- a creature that rests for minutes
def test_a_silent_dinosaur_stays_until_the_server_destroys_it_but_a_fish_does_not():
    clock = Clock()
    g = GamePeerSource(clock=clock)
    assert g.TTL_S >= 600.0 and g.TTL_AI_S <= 60.0
    g.store.update(dino("1", 5.0, 5.0))
    g.store.update(dino("2", 5.0, 5.0, kind="ai", species="Longear"))
    clock.t += 300.0  # 300 s without a single report: recorded creatures rest that long
    assert g.store.get("1") is not None and g.store.get("2") is None


# ---------------------------------------------------------------- a creature walking along the view radius
def _trace(view, entities):
    view.peers.set([PeerEntity(e, time.monotonic()) for e in entities])
    tick_twice(view)
    view.grab()
    return view.frame_trace


def test_a_creature_walking_along_the_view_radius_does_not_flicker_between_dot_and_pointer(tmp_path):
    from isle_livemap.geo import Settings

    view = make_view(tmp_path, settings=Settings(peer_radius_m=1000.0))
    view.resize(360, 360)
    view.trace_frames = True
    view.ents.tau = 0.0  # the smoother glides a dot towards its sample; the test moves it in one step
    view.peers = FakePeers([])
    at = lambda m: [dino("w", view.interp.x + m * 100.0, view.interp.y)]  # noqa: E731
    assert "w" in _trace(view, at(990.0))["pos"]  # inside: a dot
    assert "w" in _trace(view, at(1030.0))["pos"]  # 3 % beyond the radius: still the dot (it was inside a moment ago)
    assert "w" in _trace(view, at(1030.0))["pos"]
    tr = _trace(view, at(1100.0))  # clearly beyond: the pointer on the circle edge
    assert "w" not in tr["pos"] and "w" in tr["edge"]
    assert "w" in _trace(view, at(1030.0))["edge"]  # and it does not come back as a dot until it is inside the radius again
    assert "w" in _trace(view, at(990.0))["pos"]
