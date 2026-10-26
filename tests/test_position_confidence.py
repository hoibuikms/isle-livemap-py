"""A position that is only a guess (the creation record's spawn point) is shown as one: dimmed and with "~" before the
distance, until a decoded fix confirms it; it never replaces a position that was followed."""

from isle_livemap.geo import Entity, Nearby, entity_label_lines
from isle_livemap.peers import GamePeerSource


def nb(**kw):
    return Nearby(Entity("1", "player", "Name", "Allosaurus", 0.0, 0.0, **kw), 0.0, 0.0, 724.0, 0.0, age_s=1.0, stale_s=45.0)


def test_a_spawn_position_is_dimmed_and_the_distance_has_a_tilde():
    spawn, fix = nb(pos_source="spawn"), nb()
    assert spawn.stale and not fix.stale
    assert entity_label_lines(spawn)[0] == "Name - Allosaurus - ~724 m"
    assert entity_label_lines(fix)[0] == "Name - Allosaurus - 724 m"


def test_a_decoded_fix_confirms_a_spawn_position_and_a_spawn_never_replaces_a_fix():
    peers = GamePeerSource()
    peers._emit(7, 100.0, 200.0, None, "player", "Allosaurus", "P1", "spawn")
    assert peers.store.get("7").pos_source == "spawn"
    peers._emit(7, 5_000.0, 200.0, None, "player", "Allosaurus", "P1")  # a decoded fix
    e = peers.store.get("7")
    assert e.pos_source == "" and e.x == 5_000.0
    peers._emit(7, 100.0, 200.0, None, "player", "Allosaurus", "P1", "spawn")  # a late spawn fallback must not win
    e = peers.store.get("7")
    assert e.pos_source == "" and e.x == 5_000.0
