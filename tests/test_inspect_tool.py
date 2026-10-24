"""tools/inspect_capture.py on a pcap built from the join fixtures: the class histogram and creature lines."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("scapy")

from test_e2e_identity import build_join_pcap  # noqa: E402


def load_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "inspect_capture.py"
    spec = importlib.util.spec_from_file_location("inspect_capture_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_inspect_reports_classes_species_and_decoded_positions(tmp_path, join_packets):
    pcap = build_join_pcap(tmp_path / "join.pcap", join_packets)
    lines: list[str] = []
    stats = load_tool().inspect(pcap, None, first=0, max_bytes=16, out=lines.append)
    text = "\n".join(lines)
    assert stats["creation"] >= 100 and stats["exports"] > 0 and stats["destroyed"] >= 5
    assert "class histogram of" in text and "BP_PlayerState" in text and "BP_Longear" in text
    assert "Gallimimus [player]" in text and "Pteranodon" in text and "Rainbow Fish [ai]" in text
    assert "creatures with a position" in text and "player names decoded from PlayerState records" in text
    assert "destruction records matching a creation seen in the capture" in text
    # names are other people's data: counted, not printed, unless asked
    assert "use --names" in text


def test_inspect_lists_names_only_on_request(tmp_path, join_packets):
    pcap = build_join_pcap(tmp_path / "join.pcap", join_packets)
    lines: list[str] = []
    load_tool().inspect(pcap, None, first=0, max_bytes=16, out=lines.append, names=True)
    assert "use --names" not in "\n".join(lines)
