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


def test_ledger_runs_on_a_capture_and_lists_what_the_raw_stream_holds(tmp_path, join_packets):
    pytest.importorskip("PySide6")
    tool = load_tool()
    res = tool.ledger(build_join_pcap(tmp_path / "join.pcap", join_packets))
    assert set(res) >= {"seconds", "cells", "by_actor", "rows", "unplaced_ledger_s", "unplaced_counted_s", "gone_age", "removed_examples"}
    assert res["removed_examples"] == []  # nothing the server still replicated was taken off the map
    assert tool.print_ledger("join.pcap", res) in (True, False)  # printing never raises


def test_ledger_budgets_flag_a_creature_that_vanished_and_an_unannounced_unplaced_one(capsys):
    from collections import Counter

    tool = load_tool()
    base = {"seconds": 10, "rows": [], "port": None, "unplaced_handles": set(), "uncounted": Counter(), "unplaced_s": 0,
            "unplaced_ledger_s": 0, "unplaced_counted_s": 0, "removed_examples": [], "gone_age": Counter()}
    shown = Counter({(0, tool.LEDGER_DRAWN): 100})
    assert tool.print_ledger("x", {**base, "cells": shown}) is True
    assert tool.print_ledger("x", {**base, "cells": Counter({(0, tool.LEDGER_DRAWN): 90, (0, "removed while still replicated"): 10})}) is False
    unplaced = Counter({("none", "no position (never placed)"): 50, (0, tool.LEDGER_DRAWN): 100})
    assert tool.print_ledger("x", {**base, "cells": unplaced, "unplaced_ledger_s": 50, "unplaced_counted_s": 10}) is False  # the counter missed 40 of 50
    assert tool.print_ledger("x", {**base, "cells": unplaced, "unplaced_ledger_s": 50, "unplaced_counted_s": 49}) is True
    assert tool.print_ledger("x", {**base, "cells": shown, "removed_examples": [(5, 1, 100, 3, 40)]}) is False


def test_main_ledger_without_a_capture_prints_the_usage():
    assert load_tool().main(["--ledger"]) == 2
