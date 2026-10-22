import math

import pytest

from isle_livemap.nearby import format_table, nearby_entities, print_nearby

ME = {"x": 100.0, "y": 50.0, "z": 200.0}
ALPHA = {"id": 1, "name": "Alpha", "species": "Rex", "health": 100, "growth": 0.8,
         "pos": {"x": 105.0, "y": 52.0, "z": 202.0}}
BETA = {"id": 2, "name": "Beta", "species": "Raptor", "health": 60, "growth": 0.5,
        "pos": {"x": 100.0, "y": 10.0, "z": 200.0}}
FAR = {"id": 3, "name": "Far", "species": "Rex", "health": 1, "growth": 1.0,
       "pos": {"x": 500.0, "y": 50.0, "z": 200.0}}


def test_example_entities_distance_and_height_sign():
    rows = nearby_entities(ME, [ALPHA, BETA])
    by = {r["name"]: r for r in rows}
    assert by["Alpha"]["distance"] == pytest.approx(math.sqrt(25 + 4 + 4))
    assert by["Alpha"]["height_diff"] == pytest.approx(2.0)    # higher
    assert by["Beta"]["distance"] == pytest.approx(40.0)
    assert by["Beta"]["height_diff"] == pytest.approx(-40.0)   # lower


def test_filters_and_orders_nearest_first():
    assert [r["name"] for r in nearby_entities(ME, [FAR, BETA, ALPHA])] == ["Alpha", "Beta"]
    assert [r["name"] for r in nearby_entities(ME, [FAR, BETA, ALPHA], max_distance=10)] == ["Alpha"]
    assert [r["name"] for r in nearby_entities(ME, [FAR, BETA, ALPHA], max_distance=1000)][-1] == "Far"


def test_max_distance_is_exclusive():
    edge = {"name": "E", "pos": {"x": 200.0, "y": 50.0, "z": 200.0}}
    assert nearby_entities(ME, [edge], 100.0) == []


def test_input_not_mutated():
    nearby_entities(ME, [ALPHA])
    assert "distance" not in ALPHA


@pytest.mark.parametrize("bad", [
    {"name": "nopos"},
    {"name": "nonedict", "pos": None},
    {"name": "missingz", "pos": {"x": 1.0, "y": 1.0}},
    {"name": "text", "pos": {"x": "a", "y": 1.0, "z": 1.0}},
    {"name": "nan", "pos": {"x": float("nan"), "y": 0.0, "z": 0.0}},
    {"name": "bool", "pos": {"x": True, "y": 0.0, "z": 0.0}},
])
def test_malformed_entities_are_skipped(bad):
    assert [r["name"] for r in nearby_entities(ME, [bad, ALPHA])] == ["Alpha"]


def test_missing_optional_fields_kept_and_shown_as_placeholders():
    e = {"pos": {"x": 101.0, "y": 50.0, "z": 200.0}}
    rows = nearby_entities(ME, [e])
    assert len(rows) == 1
    out = format_table(rows)
    assert "?" in out and "level" in out


def test_empty_list():
    assert nearby_entities(ME, []) == []
    out = format_table([])
    assert "Height diff (m)" in out


def test_invalid_my_pos_raises():
    with pytest.raises(KeyError):
        nearby_entities({"x": 0, "y": 0}, [ALPHA])


def test_print_table(capsys):
    rows = print_nearby(ME, [BETA, ALPHA])
    out = capsys.readouterr().out
    for col in ("Name", "Species", "Health", "Growth", "Distance (m)", "Height diff (m)"):
        assert col in out
    assert "+2.0 (higher)" in out and "-40.0 (lower)" in out and "80%" in out
    assert out.index("Alpha") < out.index("Beta")
    assert len(rows) == 2
