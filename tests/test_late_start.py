"""Starting the app after joining: creatures that cannot be placed are counted and the overlay says what to do.

Synthetic data only. Sizes and shapes come from the recorded sessions in which the creation records of a few
sleeping pawns were missed: their state batches (60-400 bits, never the 155/158 bit animal state) kept coming with
no position, so the minimap showed nothing and gave no reason."""

from __future__ import annotations

import json

import pytest

from isle_livemap import peers
from isle_livemap.classcache import SCHEMA, SEED_PATH, ClassCache, load_seed
from isle_livemap.peers import Entity, GamePeerSource

DEINOSUCHUS = "/Game/TheIsle/Core/Characters/Dinosaurs/Deinosuchus/BP_Deinosuchus.Default__BP_Deinosuchus_C"


class FakePipeline:
    def add_observer(self, *a):
        pass

    def remove_observer(self, *a):
        pass


class Clock:
    def __call__(self) -> float:
        return 1_000.0


def source(tables_seen: bool = False, packets: int | None = None) -> GamePeerSource:
    g = GamePeerSource(pipeline=FakePipeline(), clock=Clock())
    g.packets = g.LATE_JOIN_PACKETS if packets is None else packets
    g._last_t = 100.0
    if tables_seen:
        g.identity.creations = 20
        g.identity.table_classes = 20
    return g


def feed(g: GamePeerSource, handle: int, sizes: list[int], t: float = 100.0) -> None:
    for bits in sizes:
        g._note_unplaced(handle, bits, t)


PAWN = [112, 190, 150, 232, 81, 104, 175] * 6  # 42 batches, all inside the pawn band


# ---------------------------------------------------------------- what counts as a hidden creature
def test_pawn_shaped_actor_without_a_position_is_a_hidden_creature():
    g = source()
    feed(g, 171102, PAWN)
    assert g.hidden_creatures() == 1


def test_world_objects_animals_and_short_histories_are_not_hidden_creatures():
    g = source()
    feed(g, 2, ([27, 84, 97] * 40))  # the game state: 59 % of it inside the band, below the share needed
    feed(g, 877, ([48] * 25 + [118] * 6 + [80] * 3))  # the sky actor
    feed(g, 30, [158] * 60)  # a fish
    feed(g, 31, PAWN[:12])  # a pawn seen only briefly
    assert g.hidden_creatures() == 0


def test_stale_old_body_and_placed_creatures_are_left_out():
    g = source()
    feed(g, 40, PAWN, t=10.0)  # last state 90 s before the newest packet
    feed(g, 41, PAWN)
    g._past_own.add(41)  # the body the own player left behind when respawning
    feed(g, 42, PAWN)
    g.store.update(Entity("42", "dino", "Someone", "Deinosuchus", 1.0, 2.0))
    feed(g, 43, PAWN)
    g.own_handle = 43
    assert g.hidden_creatures() == 0


def test_respawn_remembers_the_old_pawn(b64):
    from tests.test_game_peers import inbound

    g = source(packets=0)
    g.on_inbound(inbound(b64("SecondHeartbeat")), 100)
    g.on_inbound(inbound(b64("SecondHeartbeat")), 200)
    assert g.own_handle == 200 and g._past_own == {100}


def test_a_class_known_dinosaur_needs_fewer_batches():
    from isle_livemap.decode import ActorInfo

    g = source()
    g.identity.actors[77] = ActorInfo(77, 5, None, 0x1234)
    g.identity.objects = {}
    g.class_cache = g.identity.class_cache = ClassCache(None, seed={0x1234: DEINOSUCHUS})
    feed(g, 77, [120] * 12)
    assert g.hidden_creatures() == 1


# ---------------------------------------------------------------- what the overlay line says
def test_headline_tells_a_late_start_to_rejoin_and_leads_the_status():
    g = source()
    feed(g, 171102, PAWN)
    assert g.headline() == "Rejoin server: 1 hidden"
    status = g.status()
    assert status.startswith("Rejoin server: 1 hidden | ")
    assert peers.status_is_warning(status)
    assert len(g.headline()) <= 32  # the overlay elides the line to about 62 % of the minimap width


def test_headline_with_tables_seen_reports_creatures_that_have_no_position():
    g = source(tables_seen=True)
    feed(g, 171102, PAWN)
    feed(g, 255202, PAWN)
    assert g.headline() == "2 creatures: no position yet"


def test_headline_says_nobody_nearby_only_when_that_is_known():
    seen = source(tables_seen=True)
    assert seen.headline() == "No other players nearby"
    late = source()  # the tables were missed: sleeping creatures cannot be placed, so nobody-nearby is not known
    assert late.headline() == "Started late: rejoin to see all"
    seen.store.update(Entity("9", "player", "Someone", "Deinosuchus", 1.0, 2.0))
    assert seen.headline() == ""


def test_headline_is_quiet_until_enough_traffic_has_been_seen():
    g = source(packets=5)
    feed(g, 171102, PAWN)
    assert g.headline() == "" and "Rejoin" not in g.status()


def test_summary_mentions_hidden_creatures_for_the_session_log():
    g = source()
    feed(g, 171102, PAWN)
    assert "1 creature(s) replicated but never placed" in g.summary()


def test_reset_forgets_hidden_creatures():
    g = source()
    feed(g, 171102, PAWN)
    g.on_reset()
    g._last_t = 100.0
    assert g.hidden_creatures() == 0


# ---------------------------------------------------------------- the shipped class seed
def test_shipped_seed_loads_and_names_deinosuchus():
    seed = load_seed()
    assert len(seed) >= 40 and all(p.startswith("/") for p in seed.values())
    assert any("Characters/Dinosaurs/Deinosuchus/" in p for p in seed.values())
    assert json.loads(SEED_PATH.read_text(encoding="utf-8"))["schema"] == SCHEMA


def test_seed_answers_lookup_but_is_not_written_to_the_user_file(tmp_path):
    path = tmp_path / "classes.json"
    cache = ClassCache(path, seed={0xABC: DEINOSUCHUS})
    assert cache.lookup(0xABC) == DEINOSUCHUS and len(cache) == 0
    cache.learn(0xDEF, "/Game/Other.Thing_C")
    assert cache.save(force=True)
    assert list(json.loads(path.read_text(encoding="utf-8"))["entries"]) == ["00000def"]


def test_a_table_that_disagrees_with_the_seed_blocks_that_id():
    cache = ClassCache(None, seed={0xABC: DEINOSUCHUS})
    cache.learn(0xABC, "/Game/TheIsle/Core/Characters/Dinosaurs/Allosaurus/BP_Allosaurus.Default__BP_Allosaurus_C")
    assert cache.lookup(0xABC) is None and cache.conflicts == 1
    cache.learn(0xABC, DEINOSUCHUS)  # blocked for the rest of the process: no flip-flopping
    assert cache.lookup(0xABC) is None


def test_a_table_that_agrees_with_the_seed_keeps_it():
    cache = ClassCache(None, seed={0xABC: DEINOSUCHUS})
    cache.learn(0xABC, DEINOSUCHUS)
    assert cache.lookup(0xABC) == DEINOSUCHUS and cache.conflicts == 0


def test_load_uses_the_shipped_seed_by_default_and_a_missing_seed_file_is_fine(tmp_path):
    assert ClassCache.load(tmp_path / "none.json").lookup(next(iter(load_seed()))) is not None
    assert load_seed(tmp_path / "missing.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_seed(bad) == {}


@pytest.mark.parametrize("entry", ["xyz", "123456789"])
def test_damaged_seed_entries_are_ignored(tmp_path, entry):
    path = tmp_path / "seed.json"
    path.write_text(json.dumps({"schema": SCHEMA, "entries": {entry: "/Game/A.B_C", "0000000a": "/Game/A.B_C"}}), encoding="utf-8")
    try:
        assert load_seed(path).get(0xA) in (None, "/Game/A.B_C")
    except ValueError:  # pragma: no cover - load_seed must not raise
        pytest.fail("load_seed raised on a damaged entry")
