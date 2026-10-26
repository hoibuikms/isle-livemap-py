import math

import pytest

from isle_livemap.geo import local_entity
from isle_livemap.nearby import format_table, from_lab, my_entity, nearby_entities, print_nearby
from isle_livemap.tracking import Vitals

ME = {"x": 100.0, "y": 50.0, "z": 200.0}
ALPHA = {"id": 1, "name": "Alpha", "species": "Rex", "health": 100, "growth": 0.8,
         "pos": {"x": 105.0, "y": 52.0, "z": 202.0}}
BETA = {"id": 2, "name": "Beta", "species": "Raptor", "health": 60, "growth": 0.5,
        "pos": {"x": 100.0, "y": 10.0, "z": 200.0}}
FAR = {"id": 3, "name": "Far", "species": "Rex", "health": 1, "growth": 1.0,
       "pos": {"x": 500.0, "y": 50.0, "z": 200.0}}


def test_example_entities_distance_is_horizontal_only():
    rows = nearby_entities(ME, [ALPHA, BETA])
    by = {r.entity.name: r for r in rows}
    assert by["Alpha"].distance_m == pytest.approx(math.sqrt(25 + 4))  # dx 5, dz 2: the height (y 52 vs 50) does not count
    assert by["Beta"].distance_m == pytest.approx(0.0)  # 40 m lower, straight below: on a flat map it is the same spot


def test_filters_and_orders_nearest_first():
    assert [r.entity.name for r in nearby_entities(ME, [FAR, BETA, ALPHA])] == ["Beta", "Alpha"]  # Beta is straight below: 0 m on a flat map
    assert [r.entity.name for r in nearby_entities(ME, [FAR, BETA, ALPHA], max_distance=5.0)] == ["Beta"]
    assert [r.entity.name for r in nearby_entities(ME, [FAR, BETA, ALPHA], max_distance=1000)][-1] == "Far"


def test_max_distance_is_inclusive_and_ignores_height():
    edge = {"name": "E", "pos": {"x": 200.0, "y": 50.0, "z": 200.0}}
    assert [r.entity.name for r in nearby_entities(ME, [edge], 100.0)] == ["E"]  # the same inclusive radius as the minimap
    high = {"name": "H", "pos": {"x": 150.0, "y": 5_000.0, "z": 200.0}}
    assert [r.entity.name for r in nearby_entities(ME, [high], 100.0)] == ["H"]  # 4.9 km above: still 50 m away on the map


def test_input_not_mutated():
    before = dict(ALPHA)
    nearby_entities(ME, [ALPHA])
    assert ALPHA == before


def test_lab_entity_matches_local_player_path():
    # same position/vitals through local_entity and from_lab must give identical Entities
    me = local_entity(type("P", (), {"x": 10_500.0, "y": 20_200.0, "z": 5_200.0})(),
                      Vitals(health=60.0, max_health=100.0, growth=0.5), "Raptor")
    lab = from_lab({"id": "local", "name": "You", "species": "Raptor", "health": 60, "growth": 0.5,
                    "pos": {"x": 105.0, "y": 52.0, "z": 202.0}})
    assert lab == me


def test_self_is_filtered():
    assert nearby_entities(ME, [{"id": "local", "name": "me", "pos": ME}]) == []


@pytest.mark.parametrize("bad", [
    {"name": "nopos"},
    {"name": "nonedict", "pos": None},
    {"name": "missingz", "pos": {"x": 1.0, "y": 1.0}},
    {"name": "text", "pos": {"x": "a", "y": 1.0, "z": 1.0}},
    {"name": "nan", "pos": {"x": float("nan"), "y": 0.0, "z": 0.0}},
    {"name": "bool", "pos": {"x": True, "y": 0.0, "z": 0.0}},
])
def test_malformed_entities_are_skipped(bad):
    assert [r.entity.name for r in nearby_entities(ME, [bad, ALPHA])] == ["Alpha"]


def test_missing_optional_fields_kept_and_shown_as_placeholders():
    e = {"pos": {"x": 101.0, "y": 50.0, "z": 200.0}}
    rows = nearby_entities(ME, [e])
    assert len(rows) == 1
    out = format_table(rows)
    assert "?" in out


def test_empty_list():
    assert nearby_entities(ME, []) == []
    out = format_table([])
    assert "Distance (m)" in out and "height" not in out.lower()


def test_invalid_my_pos_raises():
    with pytest.raises(KeyError):
        nearby_entities({"x": 0, "y": 0}, [ALPHA])


def test_print_table(capsys):
    rows = print_nearby(ME, [BETA, ALPHA])
    out = capsys.readouterr().out
    for col in ("Name", "Species", "Health", "Growth", "Distance (m)"):
        assert col in out
    assert "height" not in out.lower() and "higher" not in out and "lower" not in out
    assert "5.4" in out and "80%" in out and "Beta" in out
    assert out.index("Beta") < out.index("Alpha")  # nearest first, by the horizontal distance
    assert len(rows) == 2
