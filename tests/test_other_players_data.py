"""What the game tells us about other creatures: maxHealth from the creation record (and the health it makes possible),
the census / audit tools, the friendlier 2D markers (diet colours, arrow only while moving, health bars).

Synthetic data only. Shapes come from the recorded sessions: a creation record of ~4000 bits holds maxHealth twice and
maxHealth / 2 four times as floats 34 bits apart (402662: 2606.108, 2606.108, 1303.054 x4); the pair health + stamina
travels in 179-bit state batches (health at bit 47, stamina 66 bits later)."""

from __future__ import annotations

import os
import struct
import sys
import time
from pathlib import Path

import pytest

from isle_livemap.decode import (
    ACTOR_STATE_HEADER_BITS, Bits, IrisBatch, PawnVitalsTracker, creation_max_health,
)
from isle_livemap.geo import DIET_STYLE, ENTITY_STYLE, NEUTRAL_DINO_STYLE, Entity, EntitySmoother, peer_style
from isle_livemap.replay import BitWriter
from isle_livemap.species import diet_of

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

HDR = 0x0C0000348
SLOT = 66


def raw32(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def creation_batch(max_health: float | None, *, half_runs: int = 4, pair_first: bool = True, total: int = 1800,
                   handle: int = 9, gap: int = 120):
    """A creation record: prefix 0,0,0,1,1, filler, maxHealth twice, maxHealth / 2 ``half_runs`` times (34 bit stride)."""
    w = BitWriter()
    w.write(0b11000, 5)
    w.write(0, 300 - w.nbits)

    def run(value: float, count: int) -> None:
        for _ in range(count):
            w.write(raw32(value), 32)
            w.write(0, 2)

    if max_health is not None:
        if pair_first:
            run(max_health, 2)
            w.write(0, gap)
            run(max_health / 2, half_runs)
        else:
            run(max_health / 2, half_runs)
            w.write(0, gap)
            run(max_health, 2)
    w.write(0, total - w.nbits)
    return Bits(w.to_bytes()), IrisBatch(handle, 0, w.nbits, True, False)


def pair_batch(health: float, stamina: float, *, health_at: int = 47, total: int = 179, handle: int = 9):
    w = BitWriter()
    w.write(HDR, ACTOR_STATE_HEADER_BITS)
    w.write(0, health_at - w.nbits)
    for value in (health, stamina):
        w.write(raw32(value), 32)
        w.write(1, 1)
        w.write(raw32(value), 32)
        w.write(0, 1)
    w.write(0, total - w.nbits)
    return Bits(w.to_bytes()), IrisBatch(handle, 0, w.nbits, True, False)


# ---------------------------------------------------------------- maxHealth in the creation record
def test_max_health_is_read_from_the_creation_record():
    assert creation_max_health(*creation_batch(2606.1077)) == pytest.approx(2606.1077, rel=1e-6)


def test_the_maxima_may_be_far_apart_and_in_either_order():
    assert creation_max_health(*creation_batch(161.93, gap=700, total=2400)) == pytest.approx(161.93, rel=1e-6)
    assert creation_max_health(*creation_batch(71.74, pair_first=False)) == pytest.approx(71.74, rel=1e-6)


@pytest.mark.parametrize("kwargs", [dict(max_health=None), dict(max_health=1000.0, half_runs=3), dict(max_health=1000.0, total=1400)])
def test_no_maxHealth_without_the_whole_shape(kwargs):
    assert creation_max_health(*creation_batch(**kwargs)) is None


def test_a_state_batch_is_not_a_creation_record():
    bits, batch = pair_batch(2193.0, 535.0, total=1800)
    assert creation_max_health(bits, batch) is None


def test_half_that_does_not_match_the_pair_is_not_a_maximum():
    w = BitWriter()
    w.write(0b11000, 5)
    w.write(0, 300 - w.nbits)
    for value, count in ((1000.0, 2), (433.0, 4)):  # 433 is not 1000 / 2
        for _ in range(count):
            w.write(raw32(value), 32)
            w.write(0, 2)
        w.write(0, 100)
    w.write(0, 1800 - w.nbits)
    assert creation_max_health(Bits(w.to_bytes()), IrisBatch(9, 0, w.nbits, True, False)) is None


# ---------------------------------------------------------------- health from the pair, seeded by the creation record
def seeded(max_health=2606.1077):
    t = PawnVitalsTracker()
    t.note_creation(9, max_health)
    return t


def test_a_percentage_needs_the_pair_twice_at_the_same_place():
    t = seeded()
    assert t.update(*pair_batch(2192.47, 533.8)) is None  # one sighting proves nothing
    v = t.update(*pair_batch(2193.03, 535.8))
    assert v is not None and v.health_pct == pytest.approx(84.15, abs=0.05) and t.seeded == 1
    assert v.max_from_creation and v.stamina_pct is None  # maxStamina is in no creation record: no stamina percentage


def test_later_pairs_are_followed_and_a_bite_is_a_big_step():
    t = seeded()
    t.update(*pair_batch(2192.47, 533.8))
    t.update(*pair_batch(2193.03, 535.8))
    assert t.update(*pair_batch(2194.2, 538.0)).health_pct == pytest.approx(84.2, abs=0.1)
    bite = t.update(*pair_batch(1500.0, 540.0))  # 26 % of the maximum in one step, at the confirmed offset
    assert bite is not None and bite.health_pct == pytest.approx(57.6, abs=0.1)


def test_a_confirmed_offset_places_a_new_pawn_at_once():
    t = seeded()
    t.update(*pair_batch(2192.47, 533.8))
    t.update(*pair_batch(2193.03, 535.8))
    t.note_creation(10, 1206.4683)  # a second pawn of the same class: the offset is already known
    v = t.update(*pair_batch(1000.0, 569.0, handle=10))
    assert v is not None and v.health_pct == pytest.approx(82.9, abs=0.1)


def test_health_above_the_creation_maximum_raises_it_a_growing_creature():
    t = seeded(2606.1077)
    t.update(*pair_batch(2590.0, 533.8))
    t.update(*pair_batch(2600.0, 535.8))
    v = t.update(*pair_batch(2625.0, 538.0))  # it grew: the record's maximum was from before
    assert v.health == 2625.0 and v.max_health == 2625.0 and v.health_pct == 100.0


def block_batch(health, max_health, stamina, max_stamina, handle=9):
    """The 9-slot block: health, maxHealth, stamina, maxStamina, maxHealth, maxHealth / 2 twice, maxHealth twice."""
    w = BitWriter()
    w.write(HDR, ACTOR_STATE_HEADER_BITS)
    w.write(0, 100 - w.nbits)
    for value in (health, max_health, stamina, max_stamina, max_health, max_health / 2, max_health / 2, max_health, max_health):
        w.write(raw32(value), 32)
        w.write(1, 1)
        w.write(raw32(value), 32)
        w.write(0, 1)
    w.write(0, 800 - w.nbits)
    return Bits(w.to_bytes()), IrisBatch(handle, 0, w.nbits, True, False)


def test_a_full_block_replaces_the_creation_maximum():
    t = seeded(2606.1077)
    v = t.update(*block_batch(2255.0, 2620.4941, 623.95, 700.0))
    assert v is not None and v.verified and not v.max_from_creation
    assert v.max_health == pytest.approx(2620.4941) and v.health_pct == pytest.approx(86.05, abs=0.05) and v.stamina_pct == pytest.approx(89.1, abs=0.1)


def test_nothing_without_a_maxima_or_for_a_single_stamina_slot():
    t = PawnVitalsTracker()
    assert t.update(*pair_batch(2193.0, 535.0)) is None  # no creation record, no block
    t = seeded()
    w = BitWriter()
    w.write(HDR, ACTOR_STATE_HEADER_BITS)
    w.write(0, 113 - w.nbits)
    w.write(raw32(569.0), 32)
    w.write(1, 1)
    w.write(raw32(569.0), 32)
    w.write(0, 179 - w.nbits)
    single = (Bits(w.to_bytes()), IrisBatch(9, 0, w.nbits, True, False))
    assert t.update(*single) is None and t.update(*single) is None  # stamina alone is not health (569 / 2606 would be 22 %)


def test_forget_and_reset_drop_the_seed():
    t = seeded()
    t.forget(9)
    assert t.get(9) is None
    t.note_creation(9, 100.0)
    t.reset()
    assert t.get(9) is None and t.seeded == 0


# ---------------------------------------------------------------- an arrow only while the creature moves
def entity(eid="a", x=0.0, y=0.0, **kw):
    return Entity(eid, kw.pop("kind", "dino"), "", kw.pop("species", "Allosaurus"), x, y, 0.0, **kw)


def test_heading_is_shown_only_while_the_creature_is_moving():
    sm = EntitySmoother()
    sm.ttl = 1000.0
    sm.heading_only_when_moving = True
    sm.update([entity(x=0.0, heading=90.0)], 0.0)
    sm.update([entity(x=300.0, heading=90.0)], 1.0)  # 3 m/s
    shown = lambda now: sm.items_aged(now)[0][0].heading
    assert shown(1.2) == pytest.approx(90.0)
    assert shown(1.2 + 6.0) is None  # no new fix for a long time: it stopped, the arrow goes
    sm.update([entity(x=600.0, heading=90.0)], 8.0)  # the first fix after a long rest has nothing to measure against
    sm.update([entity(x=900.0, heading=90.0)], 9.0)
    assert sm.is_moving("a", 9.1) and not sm.is_moving("a", 20.0)


def test_other_sources_keep_their_headings():
    sm = EntitySmoother()  # heading_only_when_moving stays False (a lab feed sends a real heading)
    sm.ttl = 1000.0
    sm.update([entity(x=0.0, heading=45.0)], 0.0)
    assert sm.items_aged(100.0)[0][0].heading == pytest.approx(45.0)


def test_a_creature_that_never_moved_has_no_arrow():
    sm = EntitySmoother()
    sm.heading_only_when_moving = True
    sm.update([entity(x=100.0, heading=10.0)], 0.0)
    assert sm.items_aged(0.5)[0][0].heading is None and not sm.is_moving("a", 0.5)


# ---------------------------------------------------------------- colours
def test_diet_by_species_ignores_case_marks_and_rex():
    assert diet_of("Deinosuchus") == "carnivore" and diet_of("deinosuchus~") == "carnivore"
    assert diet_of("TyrannosaurusRex") == "carnivore" and diet_of("Stegosaurus") == "herbivore"
    assert diet_of("Gallimimus") == "omnivore" and diet_of("Zzzsaurus") == "" and diet_of(None) == ""


def test_players_are_cyan_dinosaurs_by_diet_unknown_neutral_animals_muted():
    assert peer_style(entity(kind="player", species="Deinosuchus")) is ENTITY_STYLE["player"]
    assert peer_style(entity(species="Allosaurus")) is DIET_STYLE["carnivore"]
    assert peer_style(entity(species="Triceratops~")) is DIET_STYLE["herbivore"]
    assert peer_style(entity(species="")) is NEUTRAL_DINO_STYLE and peer_style(entity(kind="unknown", species="")) is NEUTRAL_DINO_STYLE
    assert peer_style(entity(kind="ai", species="Longear")) is ENTITY_STYLE["ai"]
    colours = {s.color for s in (*DIET_STYLE.values(), NEUTRAL_DINO_STYLE, ENTITY_STYLE["player"], ENTITY_STYLE["ai"], ENTITY_STYLE["self"])}
    assert len(colours) == 7  # every meaning has its own colour


# ---------------------------------------------------------------- drawn: bars under the dot, halo
def render(tmp_path, entities, **settings):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from test_peers_ui import FakePeers, make_view, tick_twice

    from isle_livemap.geo import Settings
    from isle_livemap.peers import PeerEntity

    view = make_view(tmp_path, settings=Settings(**settings))
    view.resize(400, 400)
    es = [PeerEntity(e, time.monotonic()) for e in entities(view)]
    view.peers = FakePeers(es)
    tick_twice(view)
    return view, view.grab().toImage()


def colour_near(img, x, y, rgb, r=6, tol=40):
    for yy in range(max(0, int(y) - r), min(img.height(), int(y) + r + 1)):
        for xx in range(max(0, int(x) - r), min(img.width(), int(x) + r + 1)):
            c = img.pixelColor(xx, yy)
            if abs(c.red() - rgb[0]) <= tol and abs(c.green() - rgb[1]) <= tol and abs(c.blue() - rgb[2]) <= tol:
                return True
    return False


def one_dino(health):
    return lambda view: [Entity("1", "dino", "", "Allosaurus", view.interp.x + 15_000.0, view.interp.y, 30_000.0, health_pct=health)]


def test_a_dinosaur_is_drawn_in_its_diet_colour(tmp_path):
    view, img = render(tmp_path, one_dino(None))
    sx, sy = _screen(view, 15_000.0)
    assert colour_near(img, sx, sy, (255, 126, 182), r=7)  # carnivore pink


def _screen(view, dx_cm):
    from isle_livemap.geo import project_unclamped

    pt = view.to_screen(*project_unclamped(view.interp.x + dx_cm, view.interp.y))
    return pt.x(), pt.y()


def test_the_health_bar_is_for_a_creature_without_a_label_and_can_be_switched_off(tmp_path, monkeypatch):
    import isle_livemap.overlay as overlay

    sx = sy = None
    bar = (20, 240, 85)  # full health: green
    view, img = render(tmp_path, one_dino(100.0))  # labelled: the label already says "HP 100%", no second copy as a bar
    sx, sy = _screen(view, 15_000.0)
    assert not colour_near(img, sx, sy + 9, bar, r=5, tol=45)
    monkeypatch.setattr(overlay, "label_limits", lambda *a, **k: ((0, 0, 0, 0), 0))  # no room for labels: the bar carries the value
    view2, img2 = render(tmp_path, one_dino(100.0))
    assert colour_near(img2, sx, sy + 9, bar, r=5, tol=45)
    view3, img3 = render(tmp_path, one_dino(100.0), show_vitals_bars=False)
    assert not colour_near(img3, sx, sy + 9, bar, r=5, tol=45)


# ---------------------------------------------------------------- the tools
def test_census_signatures():
    import attribute_census as c

    block = {100: 300.0, 166: 400.0, 232: 90.0, 364: 400.0}  # health, maxHealth, stamina; maxHealth again 198 bits later
    assert [b[:3] for b in c.full_blocks(block)] == [(166, 300.0, 400.0)] and next(c.full_blocks(block))[3] is None
    with_growth = {**block, 34: 0.25}
    assert next(c.full_blocks(with_growth))[3] == 0.25
    assert not list(c.full_blocks({100: 500.0, 166: 400.0, 364: 400.0}))  # health above the maximum
    assert c.has_hunger_chain({10: 31.09, 76: 848.6, 142: 31.09}) and c.has_hunger_chain({10: 31.09, 76: 848.6, 208: 31.09})
    assert not c.has_hunger_chain({10: 31.09, 76: 848.6, 142: 30.0})


def test_audit_helpers():
    import position_audit as a

    rows = [(0.0, 0.0, 0.0), (1.0, 800.0, 0.0), (2.0, 800.0, 0.0), (50.0, 840.0, 0.0)]
    assert a.steps(rows) == pytest.approx([8.0, 0.0, 0.00833], abs=1e-4)
    assert [(g, round(d, 1)) for g, d in a.rest_jumps(rows)] == [(48.0, 0.4)]
