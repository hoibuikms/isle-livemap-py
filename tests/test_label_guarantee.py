"""Every player-owned creature within 500 m is drawn and has its own label with a name and a species.

Synthetic crowds (names P1, P2 ...); the replay check on recorded sessions is tools/tier_check.py --labels."""

from __future__ import annotations

import math
import os
import random

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from isle_livemap.geo import (  # noqa: E402
    GUARANTEED_CROWD, GUARANTEED_CROWD_LABELS, GUARANTEED_FULL_LABELS, GUARANTEED_MAX_LABELS, LEADER_MIN_PX, SPREAD_COUNT,
    Entity, Nearby, is_guaranteed, leader_end, least_overlap, spread_slot, spread_slots,
)
from test_peers_ui import FakePeers  # noqa: E402
from test_stability import advance, clock, view_with  # noqa: E402,F401


def crowd(view, n, seed=1, named=True, species="Allosaurus", max_m=490.0):
    """n players at random places within ``max_m`` of you, 15 m apart at least."""
    from isle_livemap.peers import PeerEntity

    rnd = random.Random(seed)
    placed, out = [], []
    while len(out) < n:
        r, a = max_m * math.sqrt(rnd.random()), rnd.random() * math.tau
        x, y = r * math.cos(a), r * math.sin(a)
        if any(math.hypot(x - px, y - py) < 15.0 for px, py in placed):
            continue
        placed.append((x, y))
        i = len(out) + 1
        e = Entity(str(i), "player", f"P{i}" if named else f"Actor #{i}", species, view.interp.x + x * 100.0, view.interp.y + y * 100.0, 30_000.0, health_pct=80.0)
        out.append(PeerEntity(e, 0.0))
    return out


def draw(view, fp, clock, entities):
    from isle_livemap.peers import PeerEntity
    import time

    fp.set([PeerEntity(p.entity, time.monotonic()) for p in entities])
    view.trace_frames = True
    advance(view, clock, 0.4)
    view.grab()
    view.grab()
    return view.frame_trace


@pytest.mark.parametrize("n", [1, 5, 10, 20, 30])
def test_every_creature_within_500_m_has_an_arrow_and_its_own_label_with_name_and_species(tmp_path, clock, n):
    view, fp = view_with(tmp_path, clock)
    tr = draw(view, fp, clock, crowd(view, n))
    near = {k: v for k, v in tr["pos"].items() if v[3] != "ai" and v[2] <= 500.0}
    assert len(near) == n
    for eid in near:
        assert tr["glyph"][eid], f"{eid}: only a crowd triangle"
        lines, rect, _small = tr["labels"][eid]
        name, species, dist = lines[0].split(" - ")
        assert name == f"P{eid}" and species == "Allosaurus" and dist.endswith(" m")


@pytest.mark.parametrize("n", [31, 45])
def test_beyond_30_creatures_the_labels_are_one_line_but_never_missing(tmp_path, clock, n):
    view, fp = view_with(tmp_path, clock)
    tr = draw(view, fp, clock, crowd(view, n))
    near = sorted((v[2], k) for k, v in tr["pos"].items() if v[2] <= 500.0)
    assert len(near) == n and all(k in tr["labels"] for _d, k in near)
    full = [k for _d, k in near if len(tr["labels"][k][0]) > 1]
    assert len(full) <= GUARANTEED_FULL_LABELS
    assert all(len(tr["labels"][k][0]) == 1 for _d, k in near[GUARANTEED_FULL_LABELS:])  # name - species - distance, no second line


def test_a_pathological_crowd_keeps_every_arrow_and_labels_the_nearest_ones(tmp_path, clock):
    view, fp = view_with(tmp_path, clock)
    n = GUARANTEED_CROWD + 15
    tr = draw(view, fp, clock, crowd(view, n, max_m=495.0))
    near = sorted((v[2], k) for k, v in tr["pos"].items() if v[2] <= 500.0)
    assert len(near) == n and all(tr["glyph"][k] for _d, k in near)
    labelled = [k for _d, k in near if k in tr["labels"]]
    assert len(labelled) == GUARANTEED_CROWD_LABELS and labelled == [k for _d, k in near[:GUARANTEED_CROWD_LABELS]]


def test_labels_stay_inside_the_round_view_and_clear_of_your_own_arrow(tmp_path, clock):
    view, fp = view_with(tmp_path, clock)
    tr = draw(view, fp, clock, crowd(view, 25, seed=4))
    half = view.width() / 2
    me = (half - 13.0, half - 15.0, 26.0, 30.0)
    for eid, (_lines, r, _small) in tr["labels"].items():
        cx, cy = max(abs(r[0] - half), abs(r[0] + r[2] - half)), max(abs(r[1] - half), abs(r[1] + r[3] - half))
        assert cx * cx + cy * cy <= (half + 3.0) ** 2, f"{eid} is clipped"
        assert not (r[0] < me[0] + me[2] and me[0] < r[0] + r[2] and r[1] < me[1] + me[3] and me[1] < r[1] + r[3]), f"{eid} covers your arrow"


def test_a_creature_without_a_player_name_reads_player_question_mark_and_dino_without_a_species(tmp_path, clock):
    view, fp = view_with(tmp_path, clock)
    tr = draw(view, fp, clock, crowd(view, 3, named=False, species=""))
    for lines, _r, _s in tr["labels"].values():
        assert lines[0].startswith("Player ? - Dino - ")


def test_a_name_that_arrives_later_replaces_the_placeholder(tmp_path, clock):
    from dataclasses import replace

    view, fp = view_with(tmp_path, clock)
    es = crowd(view, 2, named=False)
    tr = draw(view, fp, clock, es)
    assert tr["labels"]["1"][0][0].startswith("Player ? - ")
    es[0] = type(es[0])(replace(es[0].entity, name="P1"), 0.0)
    tr = draw(view, fp, clock, es)
    assert tr["labels"]["1"][0][0].startswith("P1 - Allosaurus - ")


def test_creatures_beyond_500_m_are_not_guaranteed_and_a_server_animal_never_is():
    far = Nearby(Entity("1", "player", "P1", "Allosaurus", 0.0, 0.0), 0.0, 0.0, 501.0, 0.0)
    near = Nearby(Entity("1", "player", "P1", "Allosaurus", 0.0, 0.0), 0.0, 0.0, 500.0, 0.0)
    fish = Nearby(Entity("2", "ai", "", "Catfish", 0.0, 0.0), 0.0, 0.0, 10.0, 0.0)
    assert is_guaranteed(near) and not is_guaranteed(far) and not is_guaranteed(fish)


# ---------------------------------------------------------------- the placement helpers
def test_spread_slots_have_a_fixed_length_and_order_so_an_index_means_one_place():
    a = spread_slots(100.0, 100.0, 80.0, 12.0, 11.0)
    assert len(a) == SPREAD_COUNT and a == [spread_slot(100.0, 100.0, 80.0, 12.0, 11.0, i) for i in range(SPREAD_COUNT)]
    first_ring_down = a[0]
    assert first_ring_down[1] > 100.0  # the first spot is under the marker (below its bars)
    assert all(abs(r[2] - 80.0) < 1e-9 for r in a)


def test_leader_end_is_the_nearest_point_of_the_label_and_the_gap_is_its_distance():
    x, y, gap = leader_end((50.0, 50.0, 40.0, 10.0), 30.0, 55.0)
    assert (x, y) == (50.0, 55.0) and gap == pytest.approx(20.0)
    assert leader_end((50.0, 50.0, 40.0, 10.0), 60.0, 55.0)[2] == 0.0  # the marker is under the label
    assert LEADER_MIN_PX > 0


def test_least_overlap_prefers_a_free_spot_then_the_one_that_covers_least():
    cands = [(0.0, 0.0, 50.0, 10.0), (0.0, 20.0, 50.0, 10.0), (0.0, 40.0, 50.0, 10.0)]
    bounds = (0.0, 0.0, 100.0, 100.0)
    assert least_overlap(cands, [(0.0, 0.0, 50.0, 10.0)], bounds) == cands[1]
    assert least_overlap(cands, [(0.0, 0.0, 50.0, 10.0), (0.0, 20.0, 50.0, 10.0), (10.0, 40.0, 50.0, 10.0)], bounds) == cands[0] \
        or least_overlap(cands, [(0.0, 0.0, 50.0, 10.0), (0.0, 20.0, 50.0, 10.0), (10.0, 40.0, 50.0, 10.0)], bounds) == cands[2]
    assert least_overlap([(-20.0, 0.0, 50.0, 10.0)], [], bounds) is None  # nothing inside the bounds
    assert GUARANTEED_MAX_LABELS > GUARANTEED_FULL_LABELS


def test_the_last_resort_goes_over_labels_before_it_goes_over_an_arrow():
    from isle_livemap.geo import least_overlap

    bounds = (0.0, 0.0, 200.0, 100.0)
    over_a_label = (10.0, 10.0, 60.0, 12.0)  # overlaps a label by 60 x 6 px
    over_an_arrow = (100.0, 10.0, 60.0, 12.0)  # overlaps nothing but the middle of an arrow
    labels = [(10.0, 16.0, 60.0, 12.0)]
    arrows = [(125.0, 13.0, 6.0, 6.0)]
    assert least_overlap([over_a_label, over_an_arrow], labels, bounds) == over_an_arrow  # without the arrows it only counts labels
    assert least_overlap([over_a_label, over_an_arrow], labels, bounds, avoid=arrows) == over_a_label
