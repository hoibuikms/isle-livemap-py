"""The minimap label spec: format, AI names, distance-tier priority, labels that stay, no stats of your own on the map."""

from __future__ import annotations

import os
import random

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_other_players_data import render  # noqa: E402

import isle_livemap.overlay as overlay  # noqa: E402
from isle_livemap.confidence import VITALS_STALE  # noqa: E402
from dataclasses import replace  # noqa: E402
from isle_livemap.geo import (  # noqa: E402
    Entity, Nearby, entity_label_lines, entity_row, entity_summary, peer_label_lines, points_text, vitals_line,
)


def E(eid, kind="dino", x=0.0, y=0.0, **kw):
    return Entity(eid, kind, kw.pop("name", f"Actor #{eid}"), kw.pop("species", "Allosaurus"), x, y, kw.pop("z", None), **kw)


# ---------------------------------------------------------------- the two lines
def test_player_label_is_name_species_distance_then_health_and_stamina_in_points():
    e = E("1", "player", name="Rex Fan", species="Deinosuchus", health_pct=86.0, stamina_pct=66.0, health=1419.0, max_health=1650.0,
          stamina=330.0, max_stamina=500.0, max_src="block")
    assert peer_label_lines(Nearby(e, 0.0, 0.0, 724.0, 45.0)) == ["Rex Fan - Deinosuchus - 724 m", "HP 1419/1650 (86%)  ST 330/500 (66%)"]


def test_a_dinosaur_without_a_player_name_has_the_same_two_line_shape():
    e = E("2", species="Carnotaurus", health_pct=100.0, stamina_pct=50.0, health=900.0, max_health=900.0, stamina=250.0,
          max_stamina=500.0, max_src="block")
    assert peer_label_lines(Nearby(e, 0.0, 0.0, 120.0, 0.0)) == ["Player ? - Carnotaurus - 120 m", "HP 900/900 (100%)  ST 250/500 (50%)"]


def test_unknown_values_are_dino_and_dashes_never_guesses():
    e = E("3", "player", name="Rex Fan", species="")
    assert peer_label_lines(Nearby(e, 0.0, 0.0, 724.0, 0.0)) == ["Rex Fan - Dino - 724 m"]  # nothing was read: no second line
    only_pct = E("3", "player", name="Rex Fan", species="Allosaurus", health_pct=40.0)  # no raw value (lab feed): the percentage
    assert peer_label_lines(Nearby(only_pct, 0.0, 0.0, 10.0, 0.0))[1] == "HP 40%  ST –"


def test_an_old_reading_keeps_its_values_and_gets_an_age_tag():
    e = E("4", health_pct=70.0, stamina_pct=20.0, health=700.0, max_health=1000.0, stamina=100.0, max_stamina=500.0, max_src="block")
    assert vitals_line(e, VITALS_STALE, 130.0) == "HP 700/1000 (70%)  ST 100/500 (20%) [2m+]"
    assert vitals_line(E("4"), VITALS_STALE, 130.0) == ""  # nothing was ever read: no line at all


def test_server_animals_say_ai_and_their_name_in_one_line():
    assert peer_label_lines(Nearby(E("5", "ai", species="Catfish"), 0.0, 0.0, 120.0, 0.0)) == ["AI - Catfish - 120 m"]
    assert peer_label_lines(Nearby(E("5", "ai", species="Forktail"), 0.0, 0.0, 1500.0, 0.0)) == ["AI - Forktail - 1.5 km"]
    assert peer_label_lines(Nearby(E("5", "ai", species=""), 0.0, 0.0, 120.0, 0.0)) == ["AI - Animal - 120 m"]


def test_the_label_has_no_compass_or_height_they_are_in_the_table_and_the_summary():
    e = E("6", "player", name="Rex", species="Allosaurus", z=14_000.0)
    text = " ".join(entity_label_lines(Nearby(e, 0.0, 0.0, 724.0, 45.0)))
    assert "NE" not in text and "▲" not in text


# ---------------------------------------------------------------- the nearest creatures keep their labels
def crowd(view, rng, n_near=10, n_far=6, fish=40):
    """n_near dinosaurs within 250 m packed close together, n_far dinosaurs at 700-900 m, a shoal of fish at 80-300 m."""
    out = []
    for i in range(n_near):
        out.append(E(f"n{i}", "dino", view.interp.x + (150 + rng.random() * 20) * 100.0, view.interp.y + (rng.random() * 30 - 15) * 100.0,
                     species="Carnotaurus", z=0.0))
    for i in range(n_far):
        out.append(E(f"f{i}", "dino", view.interp.x - (700 + 30 * i) * 100.0, view.interp.y + rng.random() * 5_000.0, species="Allosaurus"))
    for i in range(fish):
        out.append(E(f"s{i}", "ai", view.interp.x + (rng.random() * 220 - 110) * 100.0, view.interp.y + (80 + rng.random() * 200) * 100.0,
                     species="Longear"))
    return out


def test_every_creature_within_250_m_keeps_its_full_label_in_a_crowd():
    import tempfile
    from pathlib import Path

    rng = random.Random(7)
    view, _ = render(Path(tempfile.mkdtemp()), lambda v: crowd(v, rng))
    near_ids = {f"n{i}" for i in range(10)}
    assert near_ids <= view._labelled, near_ids - view._labelled
    assert not (near_ids & set(view._compact_until))  # none of them was cut down to a single line


def test_labels_go_to_the_near_tier_before_the_far_one_and_fish_never_take_a_dinosaur_slot():
    import tempfile
    from pathlib import Path

    rng = random.Random(3)
    view, _ = render(Path(tempfile.mkdtemp()), lambda v: crowd(v, rng, n_near=12, n_far=12))
    labelled = view._labelled
    assert sum(1 for i in labelled if i.startswith("n")) == 12  # the whole 250 m tier
    assert sum(1 for i in labelled if i.startswith("s")) <= 3  # fish stay within their own small budget
    assert all(i.startswith(("n", "f", "s")) for i in labelled)


def test_the_label_of_a_creature_inside_250_m_is_not_dropped_when_there_is_no_free_space(monkeypatch):
    import tempfile
    from pathlib import Path

    rng = random.Random(1)
    real = overlay.place_label

    def only_empty_lists_fit(ax, ay, w, h, placed, bounds, *a, **k):
        return real(ax, ay, w, h, placed, bounds, *a, **k) if not placed else None  # every normal attempt fails

    monkeypatch.setattr(overlay, "place_label", only_empty_lists_fit)
    view, _ = render(Path(tempfile.mkdtemp()), lambda v: crowd(v, rng, n_near=3, n_far=0, fish=0))
    assert {"n0", "n1", "n2"} <= view._labelled  # the fallback tries again without the other labels in the way


# ---------------------------------------------------------------- your own stats are not on the minimap
def test_own_player_stats_are_not_drawn_on_the_minimap():
    assert not hasattr(overlay.MapView, "_draw_hud")
    assert not hasattr(overlay, "vital_lines") and not hasattr(overlay, "local_entity")


# ---------------------------------------------------------------- a creature that is created while we listen is drawn at once
def test_a_creature_with_a_max_health_is_drawn_at_its_spawn_position_even_before_its_class_is_known(join_packets, monkeypatch):
    from test_identity import replay

    from isle_livemap.peers import GamePeerSource

    # handle 73832 of the recorded join was created on a corpse-smell actor (a carcass, see the next test): the carcass rule
    # is switched off here so that the spawn-position path is tested with the one creature of the recording that never moved
    monkeypatch.setattr(GamePeerSource, "CORPSE_SMELL_CM", -1.0)
    pk = [r for r in join_packets if r["cap_packet"] <= 3018 or r["cap_packet"] in (5030, 6643)]
    g = replay(pk)
    e = g.store.get("73832")  # a pawn that never moved: its creation record is the only position it ever gives
    assert e is not None and e.kind in ("dino", "player", "unknown")
    assert all(p.entity.kind != "other" for p in g.store.entities())  # plants, fruit and items carry no maxHealth: never drawn


def test_a_creature_created_exactly_on_a_corpse_smell_actor_is_a_carcass_and_is_not_drawn(join_packets):
    from test_identity import replay

    pk = [r for r in join_packets if r["cap_packet"] <= 3018 or r["cap_packet"] in (5030, 6643)]
    g = replay(pk)
    # recorded: the Pteranodon 73832 was created 1.4 cm from a TICorpseSmell actor, and never moved or sent a position
    assert 73832 in g._corpses and g.store.get("73832") is None
    assert g.filtered_corpses >= 0 and 73832 not in {r["handle"] for r in g.unplaced_creatures()}


def test_a_label_keeps_the_side_it_has_or_reports_that_it_no_longer_fits():
    from isle_livemap.geo import place_label

    bounds, slot = (0.0, 0.0, 400.0, 400.0), [0]
    r = place_label(200.0, 200.0, 80.0, 26.0, [], bounds, None, 2, slot, None, True)
    assert r is not None and slot == [2]  # slot 2 is the side to the right
    assert place_label(200.0, 200.0, 80.0, 26.0, [r], bounds, None, 2, [0], None, True) is None  # taken: the caller looks elsewhere
    assert place_label(200.0, 200.0, 80.0, 26.0, [], bounds, None, None, [0], None, True) is None  # nothing preferred, nothing tried


# ---------------------------------------------------------------- real values: where the maxima come from
def test_a_maximum_that_is_only_the_creation_records_is_marked_with_a_tilde():
    e = E("8", health_pct=86.0, health=2274.0, max_health=2606.0, max_src="creation")
    assert vitals_line(e, "known", None) == "HP 2274/~2606 (86%)  ST –"  # the creature may have grown since: the maximum is approximate
    assert vitals_line(replace(e, max_src="block"), "known", None) == "HP 2274/2606 (86%)  ST –"


def test_a_stamina_value_without_its_maximum_is_still_shown():
    e = E("9", health_pct=86.0, stamina_pct=None, health=2274.0, max_health=2620.0, stamina=624.0, max_src="block")
    assert vitals_line(e, "known", None) == "HP 2274/2620 (86%)  ST 624/–"  # the 8-slot block carries no maximum stamina


def test_a_health_value_without_any_maximum_is_still_shown():
    e = E("10", health=1500.0)
    assert vitals_line(e, "known", None) == "HP 1500/–  ST –"


def test_small_values_keep_a_decimal_so_a_hatchling_is_not_rounded_away():
    e = E("11", health_pct=100.0, stamina_pct=74.0, health=1.6, max_health=1.6, stamina=79.7, max_stamina=107.9, max_src="block")
    assert vitals_line(e, "known", None) == "HP 1.6/1.6 (100%)  ST 80/108 (74%)"
    assert points_text(9.96) == "10" and points_text(9.94) == "9.9"  # the rounding boundary never prints "10.0"


def test_the_table_and_the_tooltip_use_the_same_values_as_the_label():
    e = E("12", "player", name="Rex Fan", species="Allosaurus", health_pct=86.0, stamina_pct=66.0, health=1419.0, max_health=1650.0,
          stamina=330.0, max_stamina=500.0, max_src="block")
    n = Nearby(e, 0.0, 0.0, 724.0, 45.0)
    row = entity_row(n)
    assert row[2] == "1419/1650" and row[4] == "330/500"
    assert "HP 1419/1650" in entity_summary(n) and "ST 330/500" in entity_summary(n)
    assert vitals_line(e, "known", None) == "HP 1419/1650 (86%)  ST 330/500 (66%)"


def test_the_growth_stage_leads_the_vitals_line_and_stands_alone_without_vitals():
    e = Entity(id="1", kind="player", name="Rex Fan", species="Deinosuchus", x=0.0, y=0.0, stage="Juvenile",
               health=1419.0, max_health=1650.0, stamina=330.0, max_stamina=500.0, health_pct=86.0, stamina_pct=66.0)
    assert vitals_line(e, "known", None) == "Juvenile  HP 1419/1650 (86%)  ST 330/500 (66%)"
    assert vitals_line(replace(e, health=None, stamina=None, max_health=None, max_stamina=None, health_pct=None, stamina_pct=None), "none", None) == "Juvenile"
