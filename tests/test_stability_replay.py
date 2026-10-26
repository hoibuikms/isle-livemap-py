"""The measurement tool (tools/stability_replay.py) and its budgets.

The recorded sessions it was built for are not in the repository (raw captures); point ``ISLE_CAPTURES`` at a folder with
``session_*.pcap`` files to replay them against the budgets:  ISLE_CAPTURES=/path/to/captures pytest tests/test_stability_replay.py"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("scapy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import stability_replay as sr  # noqa: E402
from test_e2e_peers import build_pcap  # noqa: E402

CLEAN = {
    "duration_s": 120.0, "tick_exceptions": 0, "errors": {"decode": 0, "pipeline": 0, "view": 0},
    "stuck": {"frozen_dot_s": 0.0, "drift_longest_run_s": 0.2, "own_marker_frozen_s": 0.0},
    "dot_after_destroy_s": {"max": 1.6}, "vanished_then_back": 0, "jumps_per_min": 5.0, "own_jumps_per_min": 2.0,
    "label": {"slot_hops_per_min": 1.0, "slot_flipflops_per_min": 0.0, "text_reversals_per_min": 0.0, "on_off_toggles_per_min": 0.0},
    "lag_ms": {"p50": 40.0, "n": 900}, "own_lag_ms": {"n": 900},
    "err_cm": {"p50": 100.0, "p95": 500.0, "hold_p50": 300.0, "hold_p95": 800.0},
    "own_err_cm": {"p95": 60.0, "hold_p95": 120.0},
    "time_to_first_dot_s": {"p50": 0.03, "p95": 2.0}, "worker": {"max_wait_ms": 20.0, "max_queue": 5, "busy_share": 0.005},
    "decode_ms": {"p99": 0.8, "max": 12.0}, "tick_ms": {"p95": 0.1}, "paint_ms": {"p95": 6.0},
    "multi_change_examples": [], "headline_changes": 1,
}


def test_a_clean_result_meets_every_budget():
    assert sr.check_budgets(CLEAN) == []


@pytest.mark.parametrize("path,value,words", [
    (("stuck", "frozen_dot_s"), 5.0, "frozen_dot_s"),
    (("stuck", "drift_longest_run_s"), 9.0, "drift_longest_run_s"),
    (("dot_after_destroy_s", "max"), 30.0, "dot_after_destroy_s"),
    (("errors", "decode"), 1, "errors.decode"),
    (("worker", "max_wait_ms"), 900.0, "max_wait_ms"),
    (("label", "slot_flipflops_per_min"), 0.5, "flipflops"),
    (("err_cm", "p50"), 290.0, "dot error p50"),
    (("own_err_cm", "p95"), 119.0, "own dot error p95"),
])
def test_each_budget_catches_its_violation(path, value, words):
    r = copy.deepcopy(CLEAN)
    node = r
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    bad = sr.check_budgets(r)
    assert bad and any(words in line for line in bad), bad


def test_flapping_actor_and_headline_are_violations():
    r = copy.deepcopy(CLEAN)
    r["multi_change_examples"] = [("7", "kind", ["unknown", "ai", "unknown"])]
    r["headline_changes"] = 9
    bad = sr.check_budgets(r)
    assert any("more than once" in b for b in bad) and any("headline" in b for b in bad)


def test_small_samples_do_not_trip_the_relative_error_budgets():
    r = copy.deepcopy(CLEAN)
    r["lag_ms"]["n"] = 6
    r["err_cm"]["p50"] = 5000.0
    assert sr.check_budgets(r) == []


def test_replay_of_the_recorded_fixture_runs_end_to_end_and_meets_the_budgets(tmp_path):
    pcap = build_pcap(tmp_path / "real.pcap")
    r = sr.run(pcap, None, paint_hz=5.0)
    assert r["datagrams"] > 100 and r["tick_exceptions"] == 0 and r["errors"] == {"decode": 0, "pipeline": 0, "view": 0}
    assert r["time_to_first_dot_s"]["n"] >= 5  # the recorded actors get a dot
    assert r["stuck"]["own_marker_frozen_s"] == 0.0 and r["decode_ms"]["max"] < 50.0
    assert sr.check_budgets(r) == []


CAPTURES = sorted(Path(os.environ["ISLE_CAPTURES"]).expanduser().glob("session_*.pcap")) if os.environ.get("ISLE_CAPTURES") else []


@pytest.mark.skipif(not CAPTURES, reason="set ISLE_CAPTURES to a folder with recorded session_*.pcap files")
@pytest.mark.parametrize("pcap", CAPTURES, ids=[p.name for p in CAPTURES])
def test_recorded_session_meets_the_stability_budgets(pcap):
    from isle_livemap.classcache import ClassCache

    cache = ClassCache.load(None)  # the shipped seed only: what a fresh install knows
    r = sr.run(pcap, cache)
    assert sr.check_budgets(r) == []


# ---- choppiness (how fluid the dots move at the frame rate)
def _series(step, seconds=20.0, fps=60.0):
    """A creature moving east at 5 m/s, positions every 0.5 s; ``step(t, true_x)`` is the displayed x at frame time t."""
    sm = [(i * 0.5, i * 250.0, 0.0) for i in range(int(seconds / 0.5))]
    frames = [(k / fps, step(k / fps, 500.0 * k / fps / 1.0 * 0.5), 0.0) for k in range(int(seconds * fps))]
    return {"a": [(t, x * 1.0, y) for t, x, y in frames]}, {"a": sm}, {"a": "player"}


def test_a_dot_that_moves_with_the_creature_is_not_choppy():
    frames, samples, kinds = _series(lambda t, x: 500.0 * t)  # 500 cm/s: 5 m/s, continuous
    r = sr.choppiness(frames, samples, kinds)["player"]
    assert r["intervals"] >= 30 and r["stall_frac"] == 0.0 and r["spike_frac"] == 0.0


def test_a_dot_that_waits_and_then_jumps_is_choppy():
    frames, samples, kinds = _series(lambda t, x: 250.0 * int(t / 0.5))  # stands still, then jumps to the new sample
    r = sr.choppiness(frames, samples, kinds)["player"]
    assert r["stall_frac"] > 0.8 and r["spike_frac"] > 0.0 and r["max_stall_run_s"] > 0.2


def test_choppiness_budgets_exist_and_a_few_intervals_say_nothing():
    for key in ("choppy.player.stall_frac", "choppy.ai.spike_frac"):
        assert any(b[0] == key for b in sr.BUDGETS)
    frames, samples, kinds = _series(lambda t, x: 500.0 * t, seconds=5.0)  # 10 intervals
    assert sr.choppiness(frames, samples, kinds)["player"]["stall_frac"] is None
