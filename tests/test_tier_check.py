"""tools/tier_check.py: the creatures inside the 250 / 500 m tiers and how their positions are marked."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("scapy")

from test_e2e_identity import build_join_pcap  # noqa: E402


def load_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "tier_check.py"
    spec = importlib.util.spec_from_file_location("tier_check_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_check_runs_on_a_capture_and_never_leaves_a_spawn_position_unmarked(tmp_path, join_packets):
    tool = load_tool()
    res = tool.check(build_join_pcap(tmp_path / "join.pcap", join_packets))
    assert set(res) >= {"seconds", "tiers", "sources", "unmarked", "listed"}
    assert res["unmarked"] == []


def test_main_without_a_capture_prints_the_usage():
    assert load_tool().main([]) == 2


def test_label_coverage_runs_on_a_capture_and_reports_every_creature_within_500_m(tmp_path, join_packets):
    pytest.importorskip("PySide6")
    tool = load_tool()
    res = tool.label_coverage(build_join_pcap(tmp_path / "join.pcap", join_packets))
    assert set(res) >= {"seconds", "creature_seconds", "ok", "misses", "no_name_s", "no_species_s", "clipped", "hides_arrow", "busiest"}
    assert res["ok"] == res["creature_seconds"] and not res["misses"]  # nobody is missed (there may be nobody within 500 m)


def test_main_labels_without_a_capture_prints_the_usage():
    assert load_tool().main(["--labels"]) == 2
