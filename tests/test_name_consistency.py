"""A player name that is known stays on the label; one that was never sent reads "Player ?"; the health and stamina
bars hang under every player and dinosaur.

Synthetic data only (names P1, P2)."""

from __future__ import annotations

import math
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from isle_livemap.decode import ActorInfo, IrisPacket, IdentityTracker  # noqa: E402
from isle_livemap.geo import NO_NAME, Entity, Nearby, display_name, entity_label_lines, entity_row, entity_summary  # noqa: E402
from isle_livemap.peers import GamePeerSource  # noqa: E402
from test_stability import advance, clock, view_with  # noqa: E402,F401


class FakePipeline:
    def add_observer(self, *a):
        pass

    def remove_observer(self, *a):
        pass


def source() -> GamePeerSource:
    g = GamePeerSource(pipeline=FakePipeline(), clock=lambda: 1_000.0)
    g._last_t = 100.0
    return g


# ---------------------------------------------------------------- the identity tracker keeps names
def destroy(tr: IdentityTracker, *handles: int) -> None:
    tr.feed(b"", IrisPacket(1, True, True, [], None, tuple(handles)))


def test_a_pawn_keeps_its_name_when_the_server_destroys_its_player_state():
    tr = IdentityTracker()
    tr.player_names[179074] = "P1"
    info = tr.actors[500] = ActorInfo(500, None, None, 0)
    info.ref_candidates, info.player_state = (179074,), 179074
    assert tr.name_of(500) == "P1"
    destroy(tr, 179074)
    assert 179074 not in tr.player_names and tr.name_of(500) == "P1"


def test_a_pawn_created_after_its_player_state_was_destroyed_still_links_to_it():
    tr = IdentityTracker()
    tr.player_names[179074] = "P1"
    destroy(tr, 179074)
    late = tr.actors[501] = ActorInfo(501, None, None, 0)
    late.ref_candidates = (179074, 42)
    tr._resolve_player_state(late)  # noqa: SLF001
    assert late.player_state == 179074 and tr.name_of(501) == "P1"


def test_a_live_player_state_name_wins_over_a_retired_one_on_the_same_handle():
    tr = IdentityTracker()
    tr._retired_names[179074] = "P1"  # noqa: SLF001
    tr.player_names[179074] = "P2"  # the handle was re-created for somebody else
    info = tr.actors[500] = ActorInfo(500, None, None, 0)
    info.player_state = 179074
    assert tr.name_of(500) == "P2"


def test_the_retired_names_are_forgotten_with_the_connection():
    tr = IdentityTracker()
    tr.player_names[7] = "P1"
    destroy(tr, 7)
    tr.reset()
    assert not tr._retired_names and tr.name_of(7) is None  # noqa: SLF001


# ---------------------------------------------------------------- the peer source keeps names
def test_a_known_name_and_the_player_kind_survive_a_batch_that_does_not_carry_the_link():
    g = source()
    g._emit(9, 0.0, 0.0, None, "player", "Allosaurus", "P1")  # noqa: SLF001
    g._emit(9, 500.0, 0.0, None, "dino", "Allosaurus", "")  # noqa: SLF001 - this batch resolved no name
    e = g.store.get("9")
    assert (e.kind, e.name) == ("player", "P1")


def test_a_nameless_creature_is_still_nameless_and_a_new_name_replaces_the_old():
    g = source()
    g._emit(9, 0.0, 0.0, None, "dino", "Allosaurus", "")  # noqa: SLF001
    assert g.store.get("9").name == "Actor #9"
    g._emit(9, 0.0, 0.0, None, "player", "Allosaurus", "P1")  # noqa: SLF001
    g._emit(9, 0.0, 0.0, None, "player", "Allosaurus", "P2")  # noqa: SLF001
    assert g.store.get("9").name == "P2"


def test_a_player_created_again_on_the_same_handle_near_where_it_was_is_the_same_player():
    g = source()
    back = ("player", "Allosaurus", "P1", False, 10_000.0, 0.0, 90.0)  # the _zombies entry made when the server destroyed it
    assert g._revive(back, "dino", "", "", (10_500.0, 0.0, 0.0), 100.0) == ("player", "Allosaurus", "P1")  # noqa: SLF001
    # far away (a reused handle), or the new record resolved a name of its own: not revived
    assert g._revive(back, "dino", "", "", (900_000.0, 0.0, 0.0), 100.0) == ("dino", "", "")  # noqa: SLF001
    assert g._revive(back, "player", "Rex", "P2", (10_500.0, 0.0, 0.0), 100.0) == ("player", "Rex", "P2")  # noqa: SLF001
    assert g._revive(None, "dino", "", "", (0.0, 0.0, 0.0), 100.0) == ("dino", "", "")  # noqa: SLF001


# ---------------------------------------------------------------- what the label and the tables say
def nb(kind="dino", name="Actor #1", species="Allosaurus", dist=100.0):
    return Nearby(Entity("1", kind, name, species, 0.0, 0.0), 0.0, 0.0, dist, 90.0)


def test_a_creature_whose_name_was_never_sent_reads_player_question_mark_in_label_table_and_tooltip():
    n = nb()
    assert entity_label_lines(n)[0] == f"{NO_NAME} - Allosaurus - 100 m"
    assert entity_row(n)[0] == NO_NAME and f"[dino] {NO_NAME}" in entity_summary(n)
    assert "Actor" not in "".join(entity_row(n)) + entity_summary(n)


def test_a_known_name_is_in_the_label_table_and_tooltip_and_a_server_animal_is_ai_and_its_species():
    n = nb("player", "P1")
    assert entity_label_lines(n)[0] == "P1 - Allosaurus - 100 m" and entity_row(n)[0] == "P1"
    assert display_name(Entity("5", "ai", "Actor #5", "Catfish", 0.0, 0.0)) == "AI - Catfish"
    assert display_name(Entity("5", "unknown", "Unknown #5", "", 0.0, 0.0)) == NO_NAME


def test_a_pack_label_is_for_nameless_creatures_only():
    assert entity_label_lines(nb(dist=600.0), group=3)[0] == "Allosaurus ×3 - 600 m"
    named = entity_label_lines(nb("player", "P1", dist=600.0), group=3)[0]
    assert named.startswith("P1")  # a name is never swallowed by a "×N" label


# ---------------------------------------------------------------- the map view
def at(view, i, metres, degrees, name="", **kw):
    from test_peers_ui import peer

    a = math.radians(degrees)
    return peer(view, i, metres * math.cos(a), metres * math.sin(a), name=name, **kw)


def test_a_player_with_a_known_name_keeps_a_one_line_label_when_the_distance_tier_is_full(tmp_path, clock):
    view, fp = view_with(tmp_path, clock)
    crowd = [at(view, i, 600.0 + i * 10.0, 40.0 * i) for i in range(1, 7)]  # six nameless players fill the 1000 m tier (4 labels)
    named = at(view, 9, 990.0, 300.0, name="P1")  # the furthest of them all
    fp.set([*crowd, named])
    advance(view, clock, 0.3)
    view.grab()
    assert "9" in view._labelled
    fp.set([*crowd, at(view, 9, 990.0, 300.0)])  # without the name it has to wait for a free label
    advance(view, clock, 0.3)
    view.grab()
    assert "9" not in view._labelled


def test_inside_500_m_every_creature_has_a_label_whether_its_name_is_known_or_not(tmp_path, clock):
    view, fp = view_with(tmp_path, clock)
    crowd = [at(view, i, 300.0 + i * 10.0, 40.0 * i) for i in range(1, 7)]  # six nameless players
    fp.set([*crowd, at(view, 9, 490.0, 300.0)])
    advance(view, clock, 0.3)
    view.grab()
    assert {"1", "2", "3", "4", "5", "6", "9"} <= view._labelled


def test_the_bars_hang_under_every_player_and_dinosaur_with_a_grey_track_where_nothing_is_known(tmp_path, clock):
    view, fp = view_with(tmp_path, clock)
    fp.set([at(view, 1, 150.0, 0.0, name="P1", hp=100.0), at(view, 2, 150.0, 90.0, name="P2", hp=None)])
    advance(view, clock, 0.3)
    calls: list[tuple[str, int]] = []
    from PySide6.QtGui import QPainter

    real = QPainter.drawRects
    QPainter.drawRects = lambda self, rects: (calls.append((self.brush().color().name(), len(rects))), real(self, rects))[1]  # type: ignore[method-assign]
    try:
        view.grab()
    finally:
        QPainter.drawRects = real  # type: ignore[method-assign]
    tracks = [c for c in calls if c[0] == "#000000"]
    greys = [c for c in calls if c[0] == "#969696"]
    assert tracks and tracks[0][1] == 2  # a dark track behind the bars of both creatures
    assert greys and sum(n for _, n in greys) == 3  # P1's stamina, P2's health and stamina: nothing known, an empty grey track
