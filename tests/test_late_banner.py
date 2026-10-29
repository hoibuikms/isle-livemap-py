"""The banner on the minimap that says "players are hidden, rejoin" (synthetic data).

The app can start after the join. The server describes the creatures around a player once, when they join, so a
creature that was already there and rests has no position: the map cannot show it. That used to be told in a line of
about 30 characters at the edge of the minimap, easy to miss and cut off before its end."""

from __future__ import annotations

import os
import re

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_peers_ui import FakePeers, make_view, tick_twice  # noqa: E402

from isle_livemap.decode import PawnVitals  # noqa: E402
from isle_livemap.geo import (  # noqa: E402
    HIDDEN_SECTION, HOW_TO_SEE_ALL_STEPS, Entity, banner_lines, hidden_header_row, nearby_table_rows, unplaced_row,
)
from isle_livemap.peers import GamePeerSource, PeerEntity, PeerSnapshot  # noqa: E402

CHAR_PX = 6.0


def width(text: str) -> float:
    return CHAR_PX * len(text)


# ---------------------------------------------------------------- the text: shortened, never elided
def test_no_banner_when_nothing_is_late_and_nothing_is_hidden():
    assert banner_lines(False, 0, width, 400.0) == []


def test_late_start_names_the_count_and_the_remedy_on_one_line_when_there_is_room():
    assert banner_lines(True, 3, width, 800.0) == ["LATE START: 3 players hidden - leave and rejoin the server"]


def test_the_count_is_singular_for_one_and_the_late_start_without_a_count_is_still_told():
    assert banner_lines(True, 1, width, 800.0) == ["LATE START: 1 player hidden - leave and rejoin the server"]
    assert banner_lines(True, 0, width, 800.0) == ["LATE START: players may be hidden - leave and rejoin the server"]


def test_the_creatures_banner_when_the_tables_were_seen():
    assert banner_lines(False, 1, width, 800.0) == ["1 creature: position unknown"]
    assert banner_lines(False, 3, width, 800.0) == ["3 creatures: position unknown"]


@pytest.mark.parametrize("late,hidden", [(True, 0), (True, 1), (True, 12), (False, 2), (False, 30)])
def test_every_line_fits_and_no_text_is_elided_at_any_width(late, hidden):
    for room in range(90, 520, 7):
        lines = banner_lines(late, hidden, width, float(room))
        assert 1 <= len(lines) <= 2
        assert all(width(t) <= room for t in lines), (room, lines)
        assert not any("…" in t or "..." in t for t in lines)


def test_the_count_survives_as_long_as_there_is_any_sensible_room():
    for room in range(100, 520, 11):
        lines = banner_lines(True, 12, width, float(room))
        assert any("12" in t for t in lines), (room, lines)


def test_a_narrow_room_gets_two_short_lines_with_the_remedy():
    lines = banner_lines(True, 3, width, 190.0)
    assert len(lines) == 2 and lines[0].startswith("LATE START") and "ejoin" in lines[1]


# ---------------------------------------------------------------- the source: counts, kinds, log lines, status
PAWN = [112, 190, 150, 232, 81, 104, 175] * 6


class Clock:
    t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakePipeline:
    def add_observer(self, *a):
        pass

    def remove_observer(self, *a):
        pass


def source(tables_seen: bool = False) -> GamePeerSource:
    g = GamePeerSource(pipeline=FakePipeline(), clock=Clock())
    g.packets = g.LATE_JOIN_PACKETS
    g._last_t = 100.0
    if tables_seen:
        g.identity.creations = 20
        g.identity.table_classes = 20
    return g


def feed(g: GamePeerSource, handle: int, sizes=PAWN, t: float = 100.0) -> None:
    for bits in sizes:
        g._note_shape(handle, bits)
        g._note_unplaced(handle, bits, t)


def test_the_late_start_flag_is_in_the_snapshot_and_counts_the_hidden_creatures():
    g = source()
    feed(g, 171102)
    feed(g, 255202)
    sn = g.snapshot()
    assert sn.late is True and sn.unplaced == 2 and g.hidden_creatures() == 2
    assert g.banner_kind() == "late"


def test_with_the_tables_seen_the_banner_is_the_position_unknown_count():
    g = source(tables_seen=True)
    assert g.banner_kind() == "" and g.snapshot().late is False
    feed(g, 171102)
    g._hidden_cache = (-1e9, [])  # noqa: SLF001 - the list is recomputed once a second of datagram time
    assert g.banner_kind() == "unknown" and g.snapshot().unplaced == 1


def test_a_creature_taken_off_the_map_for_being_active_with_only_a_spawn_point_counts_as_hidden():
    g = source(tables_seen=True)
    g._stale.add(9200)  # noqa: SLF001 - what peers does when an active creature has only a spawn point
    rows = {r["handle"]: r for r in g.hidden_players()}
    assert 9200 in rows and g.hidden_creatures() == 1
    feed(g, 9200)  # the same handle in the profile as well: one row, not two
    g._hidden_cache = (-1e9, [])  # noqa: SLF001
    assert [r["handle"] for r in g.hidden_players()].count(9200) == 1


def test_the_hidden_rows_carry_rate_age_and_the_vitals_that_were_read():
    g = source(tables_seen=True)
    feed(g, 171102, PAWN[:21], t=70.0)
    feed(g, 171102, PAWN[21:], t=100.0)
    g._last_t = 100.0
    g._vitals[171102] = PawnVitals(171102, health=1419.0, max_health=1650.0, stamina=330.0, max_stamina=500.0, verified=True)
    (row,) = g.hidden_players()
    assert row["hp"] == "1419/1650" and row["st"] == "330/500"
    assert row["since_s"] == pytest.approx(30.0) and row["rate"] == pytest.approx(len(PAWN) / 30.0)
    cells = unplaced_row(row)
    assert cells[2] == "1419/1650" and cells[4] == "330/500" and cells[7] == "position unknown"
    assert "42 state updates" in cells[8] and "known 30s" in cells[8]


def test_a_creature_nothing_was_read_for_shows_dashes_for_health_and_stamina():
    g = source(tables_seen=True)
    feed(g, 171102)
    (row,) = g.hidden_players()
    cells = unplaced_row(row)
    assert cells[2] == "–" and cells[4] == "–"


def test_the_banner_is_written_to_the_session_log_when_it_appears_and_when_it_goes():
    g = source(tables_seen=True)
    feed(g, 171102)
    g.snapshot()
    assert any(e.startswith("banner shown: 1 creature(s): position unknown") for e in g.drain_events())
    g._profile.clear()  # noqa: SLF001 - it moved and was placed
    g._hidden_cache = (-1e9, [])  # noqa: SLF001
    g._last_t += g.BANNER_LOG_GAP_S + 1
    g.snapshot()
    assert "banner cleared" in g.drain_events()


def test_a_banner_that_flips_within_a_few_seconds_is_logged_once_not_twice():
    g = source(tables_seen=True)
    feed(g, 171102)
    g.snapshot()
    g.drain_events()
    g._profile.clear()  # noqa: SLF001
    g._hidden_cache = (-1e9, [])  # noqa: SLF001
    g._last_t += 1.0
    g.snapshot()
    assert "banner cleared" not in g.drain_events()  # deferred: the gap has not passed


def test_the_late_start_banner_is_logged_with_its_count():
    g = source()
    feed(g, 171102)
    g.snapshot()
    assert any(e.startswith("banner shown: LATE START: 1 player(s) hidden") for e in g.drain_events())


def test_the_status_does_not_read_as_complete_after_a_late_start():
    g = source()
    g.store.update(Entity("9", "player", "Someone", "Deinosuchus", 1.0, 2.0))
    status = g.status()
    assert "INCOMPLETE" in status and "rejoin" in status.lower()
    assert "INCOMPLETE" not in source(tables_seen=True)._detail_status()


# ---------------------------------------------------------------- the Nearby table
def test_the_table_lists_the_hidden_creatures_in_a_section_of_their_own():
    near = []
    hidden = [{"handle": 1, "kind": "dino", "species": "Allosaurus", "name": "", "batches": 40, "silent_s": 1.0,
               "rate": 3.0, "since_s": 42.0, "hp": "10/20", "st": ""}]
    rows, headers = nearby_table_rows(near, hidden, 9)
    assert headers == [0] and rows[0] == hidden_header_row(1, 9)
    assert rows[0][0] == f"{HIDDEN_SECTION} (1)" and all(c == "" for c in rows[0][1:])
    assert rows[1][0] == "Player ?" and rows[1][1] == "Allosaurus" and len(rows[1]) == 9


def test_no_section_when_nothing_is_hidden():
    rows, headers = nearby_table_rows([], [], 9)
    assert rows == [] and headers == []


# ---------------------------------------------------------------- the how-to text
def test_the_how_to_steps_name_the_exact_order():
    text = " ".join(HOW_TO_SEE_ALL_STEPS)
    assert re.search(r"Start Isle LiveMap first", text) and "waiting for server traffic" in text
    assert text.index("Start") < text.index("waiting for server traffic") < text.index("rejoin")


# ---------------------------------------------------------------- the view
class BannerPeers(FakePeers):
    def __init__(self, entries=(), unplaced=0, late=False):
        super().__init__(entries)
        self.unplaced, self.late = unplaced, late

    def snapshot(self):
        return PeerSnapshot(tuple(self.entries), self.version, self.status, self.enabled, self.unplaced, self.late)


def test_the_view_lays_the_banner_out_inside_the_circle_with_every_line_inside_the_pill(tmp_path):
    view = make_view(tmp_path)
    view.resize(360, 360)
    view.peers = BannerPeers([], unplaced=3, late=True)
    tick_twice(view)
    lines, pill, text = view._banner_layout()
    assert lines and pill is not None and text is not None
    assert pill.bottom() <= view.height() and pill.left() >= 0 and pill.right() <= view.width()
    from PySide6.QtGui import QFontMetricsF

    fm = QFontMetricsF(view._banner_font())
    for line in lines:
        assert fm.horizontalAdvance(line) <= text.width() + 1.0, line
    assert not any("…" in t for t in lines)
    # the circle is narrower at the bottom: the corners of the pill lie inside it
    cx, cy, r = view.width() / 2, view.height() / 2, min(view.width(), view.height()) / 2
    for x in (pill.left(), pill.right()):
        assert (x - cx) ** 2 + (pill.bottom() - cy) ** 2 <= r * r


@pytest.mark.parametrize("size", [220, 280, 360, 520])
def test_the_banner_fits_at_every_minimap_size(tmp_path, size):
    view = make_view(tmp_path)
    view.resize(size, size)
    view.peers = BannerPeers([], unplaced=12, late=True)
    tick_twice(view)
    lines, pill, text = view._banner_layout()
    from PySide6.QtGui import QFontMetricsF

    fm = QFontMetricsF(view._banner_font())
    assert lines and all(fm.horizontalAdvance(t) <= text.width() + 1.0 for t in lines), (size, lines)
    assert any("12" in t for t in lines)


def test_the_banner_shows_while_the_condition_holds_and_is_gone_the_moment_it_clears(tmp_path):
    view = make_view(tmp_path)
    view.resize(360, 360)
    view.peers = BannerPeers([], unplaced=0, late=True)
    tick_twice(view)
    assert view._banner_layout()[0] and view._banner_t0 is not None
    view.peers.late = False
    view.peers.version += 1
    tick_twice(view)
    assert view._banner_layout()[0] == [] and view._banner_t0 is None
    view.peers.unplaced = 2
    view.peers.version += 1
    tick_twice(view)
    assert view._banner_layout()[0] == ["2 creatures: position unknown"] or len(view._banner_layout()[0]) == 2


def test_the_border_pulses_for_ten_seconds_only(tmp_path):
    import time as _time

    view = make_view(tmp_path)
    view.resize(360, 360)
    view.peers = BannerPeers([], unplaced=1, late=True)
    tick_twice(view)
    assert view.BANNER_PULSE_S == 10.0
    view._banner_t0 = _time.monotonic() - 11.0  # past the pulse: the border is steady
    img = view.grab().toImage()  # paints without raising
    assert not img.isNull()


def test_a_label_does_not_land_on_the_banner(tmp_path):
    view = make_view(tmp_path)
    view.resize(360, 360)
    view.peers = BannerPeers([], unplaced=2, late=True)
    tick_twice(view)
    pill = view._banner_layout()[1]
    assert pill is not None and pill.top() > view.height() / 2


def test_render_check_writes_a_picture_with_the_banner(tmp_path):
    view = make_view(tmp_path)
    view.resize(360, 360)
    e = Entity("1", "player", "P1", "Allosaurus", view.interp.x + 40_000, view.interp.y, 30_000.0, health_pct=80.0)
    view.peers = BannerPeers([PeerEntity(e, 0.0)], unplaced=3, late=True)
    tick_twice(view)
    out = tmp_path / "banner.png"
    assert view.grab().save(str(out)) and out.stat().st_size > 0
    os.environ["ISLE_BANNER_PNG"] = str(out)  # the path is for the person running the tests
