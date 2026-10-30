"""Why a dinosaur or player could be missing from the minimap, and the label wording around it (synthetic data).

A creature leaves the map only when the server destroys it. Everything else that used to hide one is covered here:
beyond the view radius (now a pointer on the circle edge), a destroyed-and-returning actor (now a dimmed ghost), a pack
that had no room for labels (now one "×N" label), a creature that was first unknown and later identified."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_other_players_data import colour_near, render  # noqa: E402

import isle_livemap.overlay as overlay  # noqa: E402
from isle_livemap.geo import (  # noqa: E402
    ENTITY_STYLE, Entity, EntitySmoother, entity_label_lines, entity_row, entity_summary, nearby_entities, where_text,
)
from isle_livemap.peers import GamePeerSource  # noqa: E402


def E(eid, kind="dino", x=0.0, y=0.0, **kw):
    return Entity(eid, kind, kw.pop("name", f"Actor #{eid}"), kw.pop("species", "Allosaurus"), x, y, kw.pop("z", None), **kw)


# ---------------------------------------------------------------- distance and direction, worded the same everywhere
def test_where_is_a_flat_distance_and_a_compass_direction_and_no_height():
    assert where_text(724.0, 45.0) == "724 m NE"
    assert where_text(1500.0, 200.0) == "1.5 km S"


def test_label_summary_and_table_say_the_same_thing():
    n = nearby_entities([(E("1", x=0.0, y=-72_400.0, z=14_000.0, name="P1"), 0.0, -72_400.0)], 0, 0)[0]  # north = -Y
    assert entity_label_lines(n)[0] == "P1 - Allosaurus - 724 m"  # the minimap label: distance only
    assert entity_summary(n).endswith("724 m N (0°)")  # 40 m higher than you: it does not show and does not count
    row = entity_row(n)
    assert row[7] == "724 m" and row[8] == "N (0°)" and len(row) == 9
    below = nearby_entities([(E("2", x=-72_400.0, y=0.0, z=-6_000.0), -72_400.0, 0.0)], 0, 0)[0]  # west
    assert entity_summary(below).endswith("724 m W (270°)") and below.distance_m == n.distance_m
    assert "▲" not in " ".join(row) + entity_summary(n) and "▼" not in entity_summary(below)


# ---------------------------------------------------------------- beyond the radius: a pointer, not nothing
def test_a_player_beyond_the_view_radius_is_a_pointer_on_the_circle_edge(tmp_path):
    def far_player(view):
        return [E("9", "player", view.interp.x + 150_000.0, view.interp.y, species="Deinosuchus", name="P1")]

    view, img = render(tmp_path, far_player, peer_radius_m=1000.0)
    fish, img2 = render(tmp_path, lambda v: [E("9", "ai", v.interp.x + 150_000.0, v.interp.y, species="Longear")], peer_radius_m=1000.0)
    empty, img3 = render(tmp_path, lambda v: [], peer_radius_m=1000.0)

    def changed(a, b, w, h):  # pixels around the edge anchor that differ between two frames of the same map
        x0, y0 = w - overlay.EDGE_INSET_PX - 3, h / 2
        n = 0
        for yy in range(int(y0) - 12, int(y0) + 13):
            for xx in range(int(x0) - 12, int(x0) + 13):
                ca, cb = a.pixelColor(xx, yy), b.pixelColor(xx, yy)
                n += abs(ca.red() - cb.red()) + abs(ca.green() - cb.green()) + abs(ca.blue() - cb.blue()) > 60
        return n

    assert changed(img, img3, view.width(), view.height()) >= 8  # the pointer of a player beyond the radius is on the edge
    assert changed(img2, img3, empty.width(), empty.height()) <= 2  # fish do not clutter the edge


# ---------------------------------------------------------------- a pack shares one label
def test_dinosaurs_of_one_species_standing_together_share_one_label(tmp_path, monkeypatch):
    import isle_livemap.geo as geo

    monkeypatch.setattr("isle_livemap.overlay.is_guaranteed", lambda n, was=False, radius_m=0.0: geo.is_guaranteed(n, was))  # the fixed 500 m guarantee: this test is about the ranking beyond it
    seen = []
    real = overlay.peer_label_lines

    def spy(n, vitals=None, group=1):
        seen.append((n.entity.id, group))
        return real(n, vitals, group)

    monkeypatch.setattr(overlay, "peer_label_lines", spy)
    pack = lambda v: [E(str(i), x=v.interp.x + 60_000.0 + i * 120.0, y=v.interp.y, species="Carnotaurus") for i in range(4)]  # noqa: E731 (600 m: beyond the 250 m tier, where every creature keeps its own label)
    render(tmp_path, pack)
    assert [g for _, g in seen if g > 1] and max(g for _, g in seen) == 4
    assert len({eid for eid, _ in seen}) == 1  # one label for the four of them
    head, tail = entity_label_lines(nearby_entities([(E("0", species="Carnotaurus", health_pct=50.0), 20_000.0, 0.0)], 0, 0)[0], group=4)
    assert head == "Carnotaurus ×4 - 200 m" and tail == ""  # the vitals of one animal are not those of the pack


# ---------------------------------------------------------------- the server destroys, the actor returns: a ghost in between
def test_a_destroyed_player_or_dino_is_held_as_a_dimmed_ghost_but_a_fish_leaves_at_once():
    s = EntitySmoother(ttl_s=180.0)
    s.missing_grace_s, s.ghost_grace_s, s.ghost_kinds, s.ghost_age_s = 1.5, 12.0, frozenset(("player", "dino")), 45.0
    s.update([E("d", "dino", 100.0, 100.0), E("f", "ai", 200.0, 200.0, species="Longear")], now=0.0)
    s.update([], now=1.0)  # the source stopped listing both
    s.prune(3.0)
    assert set(s.entities) == {"d"} and s.is_ghost("d")  # the fish is gone after 1.5 s, the dino is not
    ghost = s.items_aged(3.0)
    assert len(ghost) == 1 and ghost[0][3] >= 45.0  # drawn as stale from its first ghost frame
    s.prune(12.5)
    assert "d" in s.entities
    s.prune(13.5)
    assert s.entities == {}  # held 12 s after the destruction, then gone


def test_a_ghost_that_comes_back_is_a_normal_dot_again():
    s = EntitySmoother(ttl_s=180.0)
    s.missing_grace_s, s.ghost_grace_s, s.ghost_kinds, s.ghost_age_s = 1.5, 12.0, frozenset(("dino",)), 45.0
    s.update([E("d", "dino", 100.0, 100.0)], now=0.0)
    s.update([], now=1.0)
    s.prune(5.0)
    assert s.is_ghost("d")
    s.update([E("d", "dino", 150.0, 100.0)], now=6.0)  # the server created it again
    assert not s.is_ghost("d") and s.items_aged(6.0)[0][3] < 45.0


# ---------------------------------------------------------------- identity arriving after the creature was drawn
def test_an_unknown_creature_becomes_a_dino_then_a_player_with_its_name_but_never_goes_back():
    g = GamePeerSource()
    old = E("7", "unknown", name="Unknown #7", species="")
    dino = g._upgrade_identity(old, 7, "dino", "")
    assert dino.kind == "dino" and dino.name == "Actor #7"
    player = g._upgrade_identity(dino, 7, "player", "P1")
    assert player.kind == "player" and player.name == "P1"
    assert g._upgrade_identity(player, 7, "unknown", "") == player  # a later batch without the link does not undo it
    assert g._upgrade_identity(dino, 7, "dino", "") is dino  # nothing new: the same object


# ---------------------------------------------------------------- resting creatures are not dimmed after 8 s
def test_a_resting_creature_is_not_dimmed_after_a_few_seconds():
    from isle_livemap.geo import GAME_PEER_STALE_S

    assert GAME_PEER_STALE_S >= 30.0  # Iris sends nothing for a creature at rest: silence is not age


# ---------------------------------------------------------------- the same handle returns after a destruction record
def test_a_destroyed_creature_that_returns_nearby_keeps_its_identity_but_a_distant_one_does_not():
    g = GamePeerSource()
    g._last_t = 100.0
    g.store.update(E("5", "dino", 10_000.0, 20_000.0, species="Carnotaurus"))
    g._drop(5)
    assert g.store.get("5") is None
    g._last_t = 106.0
    g._emit(5, 10_800.0, 20_000.0, 0.0, "unknown", "", "")  # 8 m from where it was, 6 s later: the same animal
    back = g.store.get("5")
    assert back.kind == "dino" and back.species == "Carnotaurus"
    g.store.remove("5")
    g._emit(5, 10_000.0 + 900_000.0, 20_000.0, 0.0, "unknown", "", "")  # 9 km away: not that creature
    assert g.store.get("5").kind == "unknown"
