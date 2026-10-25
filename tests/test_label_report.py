"""tools/label_report.py: the per-creature table, the CSV, the clock handling and the --at view, on the sanitised join capture."""

from __future__ import annotations

import csv
import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("scapy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_e2e_identity import build_join_pcap  # noqa: E402

EPOCH0 = 1_700_000_000.0  # first datagram of build_join_pcap


def load_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "label_report.py"
    spec = importlib.util.spec_from_file_location("label_report_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def pcap(tmp_path, join_packets):
    pk = [r for r in join_packets if r["cap_packet"] <= 3018 or r["cap_packet"] in (5030, 6643)]
    return build_join_pcap(tmp_path / "session_20260101_000000.pcap", pk)


def test_table_lists_every_creature_with_species_source_and_confidence(pcap):
    tool = load_tool()
    result = tool.collect(pcap, tool.make_cache(None, use_seed=False))
    rows = {r["handle"]: r for r in result["rows"]}
    assert {"Pteranodon", "Rainbow Fish", "Gallimimus"} <= {r["species"] for r in rows.values()}
    assert {r["source"] for r in rows.values() if r["species"]} <= {"table", "asset"}  # no cache was given
    assert rows[86612]["kind"] == "player" and rows[86612]["name"] and rows[86612]["source"] == "table"
    assert rows[74400]["hp"] == pytest.approx(78.4, abs=0.1)  # a pawn whose full health block was seen
    assert rows[73832]["placed"] is False  # replicated (a creation record) but never given a position
    text = "\n".join(tool.render_table(result, EPOCH0, 0.0, include_ai=True, hide_names=False))
    assert "species" in text and "source" in text and "Pteranodon" in text and "never placed" in text
    assert rows[86612]["name"] in text


def test_hide_names_removes_every_player_name_from_the_table_and_the_csv(pcap, tmp_path):
    tool = load_tool()
    result = tool.collect(pcap, tool.make_cache(None, use_seed=False))
    names = {r["name"] for r in result["rows"] if r["name"]}
    assert names
    text = "\n".join(tool.render_table(result, EPOCH0, 0.0, include_ai=True, hide_names=True))
    assert "<name hidden>" in text and not any(n in text for n in names)
    out = tmp_path / "r.csv"
    tool.write_csv(out, result, EPOCH0, 0.0, hide_names=True)
    body = out.read_text(encoding="utf-8")
    assert not any(n in body for n in names)
    rows = list(csv.DictReader(body.splitlines()))
    assert {"handle", "species", "source", "hp_pct", "dist_m_first"} <= set(rows[0])
    assert len(rows) == len(result["rows"])


def test_fish_are_listed_only_on_request(pcap):
    tool = load_tool()
    result = tool.collect(pcap, tool.make_cache(None, use_seed=False))
    assert " ai " not in "\n".join(tool.render_table(result, EPOCH0, 0.0, include_ai=False, hide_names=True)).split("\n\n")[0]
    assert " ai " in "\n".join(tool.render_table(result, EPOCH0, 0.0, include_ai=True, hide_names=True)).split("\n\n")[0]


def test_the_class_cache_is_reported_as_the_source_when_the_tables_were_missed(pcap, join_packets, monkeypatch):
    from isle_livemap.classcache import ClassCache
    from isle_livemap.decode import IdentityTracker
    from test_classcache import learn_from_tables  # noqa: PLC0415

    tool = load_tool()
    cache = ClassCache()
    learn_from_tables(join_packets, cache)  # an earlier session that saw the tables
    monkeypatch.setattr(IdentityTracker, "_record_exports", lambda self, section: None)  # this one missed them
    result = tool.collect(pcap, cache)
    named = [r for r in result["rows"] if r["species"]]
    assert named and {r["source"] for r in named} == {"cache"}
    assert all(r["species_tier"] == "inferred" for r in named)
    table = "\n".join(tool.render_table(result, EPOCH0, 0.0, include_ai=True, hide_names=True))
    assert "Pteranodon~" in table or "Rainbow Fish~" in table  # "~" in the table exactly as on the map


# ---------------------------------------------------------------- clocks
def test_log_gives_the_utc_offset_of_the_recording_machine(tmp_path):
    tool = load_tool()
    pcap = tmp_path / "session_x.pcap"
    # the machine ran at UTC+7: 07:13:19 UTC is 14:13:19 on its clock
    (tmp_path / "session_x.log").write_text(
        "2026-01-01 14:13:19,100 app start\n2026-01-01 14:14:19,900 app stop\n", encoding="utf-8")
    first = 1_767_251_599.0  # 2026-01-01 07:13:19 UTC
    assert tool.log_utc_offset_s(pcap, first, first + 60.0) == 7 * 3600.0
    assert tool.log_utc_offset_s(tmp_path / "other.pcap", first, first + 60.0) is None


def test_at_accepts_wall_clock_relative_seconds_and_refuses_what_the_capture_does_not_cover():
    tool = load_tool()
    first = 1_767_251_599.0  # 07:13:19 UTC = 14:13:19 at UTC+7
    last = first + 120.0
    off = 7 * 3600.0
    assert tool.resolve_at("14:14:19", first, last, off) == pytest.approx(60.0)
    assert tool.resolve_at("14:14", first, last, off) == pytest.approx(41.0)
    assert tool.resolve_at("+30", first, last, off) == 30.0 and tool.resolve_at("45s", first, last, off) == 45.0
    with pytest.raises(ValueError, match="covers 14:13:19 to 14:15:19"):
        tool.resolve_at("14:10:00", first, last, off)
    with pytest.raises(ValueError, match="only 120 s"):
        tool.resolve_at("+500", first, last, off)
    with pytest.raises(ValueError, match="HH:MM:SS"):
        tool.resolve_at("noon", first, last, off)


def test_a_capture_across_midnight_resolves_the_next_day():
    tool = load_tool()
    first = 1_767_225_599.0  # 2025-12-31 23:59:59 UTC
    assert tool.resolve_at("00:00:30", first, first + 120.0, 0.0) == pytest.approx(31.0)


# ---------------------------------------------------------------- --at
def test_at_prints_the_labels_that_were_on_the_map(pcap):
    pytest.importorskip("PySide6")
    tool = load_tool()
    lines: list[str] = []
    assert tool.main([str(pcap), "--at", "+1.8", "--tz-offset", "0", "--all", "--hide-names"], out=lines.append) == 0
    text = "\n".join(lines)
    assert "Minimap at +1.8 s (" in text and "status line:" in text
    assert "label:" in text and "src=" in text
    assert "Pteranodon" in text or "Rainbow Fish" in text  # the real labels of the real MapView
    assert "<name hidden>" in text or "name=" not in text


def test_at_without_qt_still_lists_the_labels(pcap):
    tool = load_tool()
    result = tool._labels_at_without_qt(pcap, 1.8, tool.make_cache(None, use_seed=False), None)
    assert result["rendered"] is False and result["dots"]
    out = tool.render_at(result, 1.8, EPOCH0, 0.0, include_ai=True, hide_names=True)
    assert any("PySide6 is not installed" in ln for ln in out) and any("label:" in ln for ln in out)


def test_missing_file_and_bad_time_are_messages_not_tracebacks(tmp_path, pcap):
    tool = load_tool()
    lines: list[str] = []
    assert tool.main([str(tmp_path / "nope.pcap")], out=lines.append) == 2 and "no such file" in lines[0]
    lines.clear()
    assert tool.main([str(pcap), "--at", "99:99:99"], out=lines.append) == 2
    assert "HH:MM:SS" in lines[-1] or "covers" in lines[-1]
