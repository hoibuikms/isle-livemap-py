"""Honest labels: confidence tiers, their wording on the minimap, hysteresis, the "labels:" log line and the correction log.

Synthetic data only (no captures, names or addresses)."""

from __future__ import annotations

import os
import time

import pytest

from isle_livemap.classcache import ClassCache
from isle_livemap.confidence import (
    DWELL_S, SPECIES_CONFIRMED, SPECIES_INFERRED, SPECIES_UNKNOWN, VITALS_FRESH_S, VITALS_KNOWN, VITALS_NONE, VITALS_STALE,
    LabelConfidence, age_tag, is_placeholder_name, species_tier, vitals_tier,
)
from isle_livemap.geo import Entity, Nearby, entity_label_lines, peer_label_lines
from isle_livemap.peers import GamePeerSource, PeerEntity


def nb(kind="dino", species="Allosaurus", name="Actor #7", inferred=False, hp=None, st=None, vage=None, eid="7", **kw):
    e = Entity(eid, kind, name, species, 0.0, 0.0, health_pct=hp, stamina_pct=st, inferred=inferred, **kw)
    return Nearby(e, 0.0, 0.0, 100.0, 90.0, vitals_age_s=vage)


# ---------------------------------------------------------------- tiers
def test_species_tiers():
    assert species_tier("Allosaurus", False) == SPECIES_CONFIRMED
    assert species_tier("Allosaurus", True) == SPECIES_INFERRED
    assert species_tier("", False) == SPECIES_UNKNOWN and species_tier("", True) == SPECIES_UNKNOWN


def test_vitals_tiers_and_age_tags():
    assert vitals_tier(None, None, None) == VITALS_NONE
    assert vitals_tier(80.0, None, None) == VITALS_KNOWN  # no timestamp (local player, lab feed): fresh
    assert vitals_tier(80.0, 50.0, VITALS_FRESH_S) == VITALS_KNOWN
    assert vitals_tier(80.0, 50.0, VITALS_FRESH_S + 0.1) == VITALS_STALE
    assert [age_tag(a) for a in (21, 59, 60, 119, 120, 299, 300, 599, 600, 5000)] == [
        "20s+", "20s+", "1m+", "1m+", "2m+", "2m+", "5m+", "5m+", "10m+", "10m+"]
    assert age_tag(None) == ""


def test_placeholder_names_are_not_names():
    assert is_placeholder_name("Actor #5") and is_placeholder_name("Unknown #5") and is_placeholder_name("")
    assert not is_placeholder_name("Rex Fan")


# ---------------------------------------------------------------- wording
def test_confirmed_species_is_plain_inferred_has_a_tilde_unknown_is_a_question_mark():
    assert entity_label_lines(nb(species="Allosaurus"))[0] == "Allosaurus"
    assert entity_label_lines(nb(species="Allosaurus", inferred=True))[0] == "Allosaurus~"
    assert entity_label_lines(nb(species=""))[0] == "Dino ? #7"
    assert entity_label_lines(nb(kind="unknown", species=""))[0] == "Creature ? #7"
    assert entity_label_lines(nb(kind="ai", species=""))[0] == "Animal ? #7"
    assert entity_label_lines(nb(kind="player", name="Rex Fan", species="Allosaurus"))[0] == "Rex Fan (Allosaurus)"
    assert entity_label_lines(nb(kind="player", name="Rex Fan", species="Allosaurus", inferred=True))[0] == "Rex Fan (Allosaurus~)"
    assert entity_label_lines(nb(kind="player", name="Rex Fan", species=""))[0] == "Rex Fan (?)"


def test_a_name_is_never_invented_for_a_creature_without_a_player_state():
    head = entity_label_lines(nb(kind="dino", name="Actor #7", species="Allosaurus"))[0]
    assert "Actor" not in head and "(" not in head


def test_health_is_known_stale_or_a_question_mark():
    assert entity_label_lines(nb(hp=86.0, st=33.4, vage=2.0))[1].startswith("HP 86% ST 33% 100 m")
    assert entity_label_lines(nb(hp=86.0, st=33.4, vage=45.0))[1].startswith("HP 86% ST 33% 100 m")  # unchanged values are not re-sent
    stale = entity_label_lines(nb(hp=86.0, st=33.4, vage=130.0))[1]
    assert stale.startswith("HP 86% ST 33% [2m+] 100 m")
    assert entity_label_lines(nb(hp=86.0, vage=400.0))[1].startswith("HP 86% [5m+] ")
    assert entity_label_lines(nb())[1] == "HP ? 100 m E"  # never read: said so
    assert entity_label_lines(nb(kind="ai", species="Longear"))[1] == "100 m E"  # fish have no health line at all
    assert entity_label_lines(nb(kind="player", name="Rex", hp=None, st=33.0))[1].startswith("HP ? ST 33% ")


def test_the_vitals_argument_overrides_what_the_age_says():
    n = nb(hp=86.0, vage=45.0)  # younger than VITALS_FRESH_S: the tier passed in decides
    assert "[" not in entity_label_lines(n, vitals=VITALS_KNOWN)[1]
    assert "[20s+]" in entity_label_lines(n, vitals=VITALS_STALE)[1]


def test_growth_hunger_thirst_of_others_are_never_printed_or_guessed():
    for lines in (peer_label_lines(nb(hp=80.0), detail=True), peer_label_lines(nb(), detail=True), peer_label_lines(nb(kind="ai"))):
        text = " ".join(lines)
        assert "G " not in text and "Hunger" not in text and "Water" not in text and "Growth" not in text and "TH" not in text
    # the status line says it once
    g = GamePeerSource(pipeline=object())
    g.packets = 1
    g._emit(1, 0.0, 0.0, 0.0, "dino", "Rex", "Actor #1")
    assert "growth, hunger and thirst not available" in g.status()


# ---------------------------------------------------------------- hysteresis
def settle(c, species, inferred, hp, vage, now, eid="a"):
    return c.settle(eid, species, inferred, hp, None, vage, now)


def test_more_sure_is_shown_at_once_less_sure_only_after_the_dwell():
    c = LabelConfidence()
    assert settle(c, "", False, None, None, 0.0)[:2] == ("", False)
    assert settle(c, "Allosaurus", True, None, None, 1.0)[:2] == ("Allosaurus", True)  # unknown -> inferred: now
    assert settle(c, "Allosaurus", False, None, None, 1.1)[:2] == ("Allosaurus", False)  # inferred -> confirmed: now
    # the species disappears for a moment: the label keeps saying it
    assert settle(c, "", False, None, None, 2.0)[:2] == ("Allosaurus", False)
    assert settle(c, "", False, None, None, 2.0 + DWELL_S - 0.1)[:2] == ("Allosaurus", False)
    assert settle(c, "", False, None, None, 2.0 + DWELL_S)[:2] == ("", False)  # it lasted: now it says unknown


def test_a_flicker_inside_the_dwell_never_reaches_the_label():
    c = LabelConfidence()
    settle(c, "Allosaurus", False, 80.0, 1.0, 0.0)
    shown = set()
    for i in range(40):  # the species and the reading come and go every half second
        up = i % 2 == 0
        sp, inf, hp, _, tier = settle(c, "Allosaurus" if up else "", False, 80.0 if up else None, 1.0 if up else None, 0.5 * i)
        shown.add((sp, hp, tier))
    assert shown == {("Allosaurus", 80.0, VITALS_KNOWN)}


def test_a_reading_goes_stale_after_the_dwell_and_is_known_again_at_once():
    c = LabelConfidence()
    assert settle(c, "Rex", False, 70.0, 5.0, 0.0)[4] == VITALS_KNOWN
    assert settle(c, "Rex", False, 70.0, VITALS_FRESH_S + 5.0, 1.0)[4] == VITALS_KNOWN  # raw says stale; held
    assert settle(c, "Rex", False, 70.0, VITALS_FRESH_S + 8.0, 1.0 + DWELL_S)[4] == VITALS_STALE
    assert settle(c, "Rex", False, 71.0, 0.5, 40.0)[4] == VITALS_KNOWN  # a new reading
    assert settle(c, "Rex", False, 71.0, 0.5, 40.0)[2] == 71.0


def test_state_of_creatures_that_left_is_forgotten():
    c = LabelConfidence()
    settle(c, "Rex", False, 70.0, 1.0, 0.0, eid="a")
    c.keep_only({"b"})
    assert settle(c, "", False, None, None, 1.0, eid="a")[:2] == ("", False)  # a new creature under an old id: nothing held over


# ---------------------------------------------------------------- the map view
def test_map_view_holds_a_flickering_species_instead_of_flapping_the_label(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from test_peers_ui import FakePeers, make_view, tick_twice  # noqa: PLC0415

    import isle_livemap.overlay as overlay

    view = make_view(tmp_path)
    x, y = view.interp.x + 40_000, view.interp.y - 20_000

    def entry(species, inferred=False, hp=None):
        e = Entity("7", "dino", "Actor #7", species, x, y, 30_000.0, health_pct=hp, inferred=inferred)
        return PeerEntity(e, time.monotonic())

    texts = []
    real = overlay.peer_label_lines
    monkeypatch.setattr(overlay, "peer_label_lines", lambda n, *a, **k: texts.append(real(n, *a, **k)[0]) or real(n, *a, **k))
    view.peers = FakePeers([entry("Allosaurus", hp=80.0)])
    tick_twice(view)
    view.grab()
    for i in range(6):  # the species is lost and found again within the dwell: the label never says unknown
        view.peers.set([entry("" if i % 2 else "Allosaurus", hp=80.0)])
        tick_twice(view)
        view.grab()
    assert texts and set(texts) == {"Allosaurus"}


# ---------------------------------------------------------------- the labels: line and the correction log
def make_source(cache=None):
    return GamePeerSource(pipeline=object(), class_cache=cache)


def test_labels_line_lists_creatures_with_confidence_and_counts_fish():
    g = make_source()
    g._emit(1, 10_000.0, 0.0, 0.0, "dino", "Allosaurus", "Actor #1")
    g._emit(2, 20_000.0, 0.0, 0.0, "player", "", "Rex Fan")
    g._emit(3, 30_000.0, 0.0, 0.0, "ai", "Longear", "Actor #3")
    g._vitals_t[1] = g._clock()
    line = g.labels_line()
    assert line.startswith("labels: creatures=2 fish=1 | ")
    assert "1 dino Allosaurus HP?" in line and '2 player ? "Rex Fan" HP?' in line and "Longear" not in line


def test_labels_signature_ignores_values_that_change_all_the_time_but_not_identity():
    g = make_source()
    g._emit(1, 10_000.0, 0.0, 0.0, "dino", "Allosaurus", "Actor #1")
    first = g.labels_signature()
    g._emit(1, 90_000.0, 0.0, 0.0, "dino", "Allosaurus", "Actor #1")  # it moved
    assert g.labels_signature() == first
    g._emit(1, 90_000.0, 0.0, 0.0, "dino", "Allosaurus", "Rex Fan")  # a name got linked
    assert g.labels_signature() != first


def test_session_log_writes_the_labels_line_on_change_and_every_interval(tmp_path):
    from isle_livemap.sessionlog import LABELS_INTERVAL_S, LABELS_MIN_GAP_S, SessionLog

    class Pipe:
        server = None

        def add_observer(self, *a): ...
        def remove_observer(self, *a): ...
        def counts(self): return {}

    now = [0.0]
    g = make_source()
    log = SessionLog(tmp_path / "s.log", Pipe(), peers=g, clock=lambda: now[0])
    assert log.start()
    log._stop.set()  # drive tick() by hand

    def tick(t):
        now[0] = t
        log.tick()

    def labels():
        return [ln for ln in (tmp_path / "s.log").read_text(encoding="utf-8").splitlines() if " labels: " in ln]

    tick(100.0)
    assert labels() == []  # nothing but fish: nothing to say
    g._emit(1, 10_000.0, 0.0, 0.0, "dino", "Allosaurus", "Actor #1")
    tick(101.0)
    assert len(labels()) == 1
    tick(101.0 + LABELS_MIN_GAP_S / 2)
    g._emit(2, 10_000.0, 0.0, 0.0, "dino", "Pteranodon", "Actor #2")  # changed, but inside the minimum gap
    tick(101.0 + LABELS_MIN_GAP_S / 2 + 0.1)
    assert len(labels()) == 1
    tick(101.0 + LABELS_MIN_GAP_S + 0.2)
    assert len(labels()) == 2 and "Pteranodon" in labels()[-1]
    tick(101.0 + LABELS_MIN_GAP_S + 0.2 + LABELS_INTERVAL_S - 1)
    assert len(labels()) == 2  # unchanged: quiet
    tick(101.0 + LABELS_MIN_GAP_S + 0.2 + LABELS_INTERVAL_S + 1)
    assert len(labels()) == 3  # periodic
    log.close()


def test_a_remembered_species_that_the_tables_contradict_is_corrected_and_logged_once():
    g = make_source()
    g._emit(1, 0.0, 0.0, 0.0, "dino", "Allosaurus", "Actor #1")
    g.store.update(Entity("1", "dino", "Actor #1", "Allosaurus", 0.0, 0.0, inferred=True))  # as if it came from the cache
    g._emit(1, 100.0, 0.0, 0.0, "dino", "Carnotaurus", "Actor #1")  # the tables say otherwise now
    g._emit(1, 200.0, 0.0, 0.0, "dino", "Carnotaurus", "Actor #1")
    events = g.drain_events()
    assert len(events) == 1 and "handle 1" in events[0] and "Allosaurus~" in events[0] and "Carnotaurus" in events[0]
    assert g.drain_events() == []
    e = g.store.get("1")
    assert e.species == "Carnotaurus" and not e.inferred


def test_a_dropped_cache_entry_is_reported_once():
    cache = ClassCache(seed={0xAB: "/Game/X/BP_A.Default__BP_A_C"})
    g = make_source(cache)
    cache.learn(0xAB, "/Game/X/BP_B.Default__BP_B_C")  # the tables disagree with the seed
    out = g.drain_events()
    assert len(out) == 1 and "dropped" in out[0]
    assert g.drain_events() == []
