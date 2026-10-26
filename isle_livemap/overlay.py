"""PySide6 overlay: always-on-top, click-through, 60 fps, player-centred."""

from __future__ import annotations

import math
import os
from functools import lru_cache
import time
from dataclasses import replace
from typing import Sequence

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QWidget

from .capture import TelemetryPipeline
from .confidence import SPECIES_INFERRED, SPECIES_UNKNOWN, LabelConfidence, is_placeholder_name, species_tier
from .geo import (
    ARROW_POINTS, CROWDED_ON_SCREEN, PEER_OUTLINE_W,
    ASSET_DIR, MAP_HEIGHT, MAP_WIDTH, UNITS_PER_METER, WORLD_SCALE, X_OFFSET, Y_OFFSET, Interpolator, MarkerStore, Settings, build_rings, clamp_size_percent,
    compass_label, format_distance, load_zones, project_unclamped, unproject,
    ENTITY_STYLE, PEER_ARROW_SCALE, STALE_ALPHA, UNKNOWN_HEADING_ALPHA, EntitySmoother, Nearby, edge_anchor, peer_label_lines,
    PEER_STALE_S, PEER_TTL_S, SLOT_SLACK_PX, label_limits, place_label, priority_key, priority_tier, visible_entities,
    marker_extent, marker_radius, peer_style, truncate, _overlaps, display_name, vitals_readings, with_vitals,
    own_marker_stale, own_stale_text,
    GUARANTEED_CROWD, GUARANTEED_CROWD_LABELS, GUARANTEED_FULL_LABELS, GUARANTEED_MAX_LABELS, LEADER_MIN_PX, is_guaranteed, leader_end, least_overlap, spread_slots, label_slots, spread_slot, SPREAD_COUNT,
)
from .peers import status_is_warning

BASE_SIZE = 360  # minimap edge in px at 100 %
BASE_VIEW_FRACTION = 0.2  # minimap shows 20 % of map width at zoom 1
KIND_COLORS = {"waypoint": "#ffd34d", "nest": "#ff9a52", "water": "#4db8ff", "dinosaur": "#ff6b6b", "other": "#c8c8c8"}
GHOST_FADE_STEP_S, GHOST_FADE_STEPS = 10.0, 4  # a ghost gets fainter every 10 s, in 4 steps (45 s grace)
RADIUS_STICKY_FACTOR = 1.04  # a creature that was inside the view radius stays inside until 4 % beyond it
KIND_GLYPH = {"waypoint": "◆", "nest": "⌂", "water": "≈", "dinosaur": "☠", "other": "●"}
EDGE_INSET_PX = 7  # edge indicators sit this far inside the minimap circle
DOT_CLEARANCE_PX = 7.0  # no label may cover the dot of a player or dinosaur
SOFT_INFERRED, SOFT_UNKNOWN = 0.9, 0.8  # label head opacity of a remembered / unknown species (confirmed: 1.0)
LABEL_BG, LABEL_BG_STALE = QColor(8, 12, 16, 175), QColor(8, 12, 16, 140)
LABEL_BG_PLAYER, LABEL_BG_PLAYER_STALE = QColor(6, 8, 14, 220), QColor(6, 8, 14, 170)  # behind the bold label of a player: stronger than the fish's
BAR_TRACK, BAR_ST_FILL = QColor(0, 0, 0, 170), QColor(90, 170, 255)
BAR_UNKNOWN = QColor(150, 150, 150, 95)  # a reading never received: an empty grey track, so every creature has the same two bars
BAR_HP_H, BAR_ST_H, BAR_GAP = 3.0, 2.0, 1.5  # px
BAR_BOX_H = BAR_HP_H + BAR_GAP + BAR_ST_H
PULL_IN_MAX, PULL_IN_CROWDED = 3, 1  # labels pulled in towards the centre in one frame (a crowd of named players cannot cost a frame)
BAR_MAX, BAR_CROWDED = 40, 16  # bars under the most important this many players and dinosaurs (in a crowd of more than CROWD_GLYPHS: fewer)
NAMED_EXTRA_LABELS, NAMED_EXTRA_CROWDED = 8, 2  # players with a known name keep a one-line label beyond the caps of their tier
EDGE_RANGE_M = 2500.0  # players and dinosaurs up to this far are shown on the circle edge when beyond the view radius
EDGE_CAPTIONS = 6  # the nearest this many edge pointers also say who / what they are and how far
COMPACT_HOLD_S = 4.0  # a label that had to be shortened stays short this long (no flipping between one and two lines)
CLUSTER_PX = 12.0  # creatures of one species whose dots are closer than this share one label ("Carnotaurus ×5")
HALO_MAX = 40  # halos and bars for the most important this many peers (labels are limited in the same way)
CROWD_GLYPHS = 150  # full glyphs for the most important this many peers; the rest of a crowd are tiny triangles
LABEL_ATTEMPTS_PER_LABEL = 3  # placement tries per allowed label, so a crowd cannot cost 500 text measurements
SMALL_FONT_PT = 7  # labels of the 250 - 500 m band (the 8 pt of the nearest tier is easier to read where it matters most)
LAST_RESORT_PER_FRAME = 6  # labels per frame that may take the least-overlapping spot when every free one is gone
LEADER_ALPHA = 150
LABEL_STALE_ALPHA = 185  # a label of a stale / spawn-only creature stays readable (its arrow is the dimmed part)
SEARCH_BUDGET = 10  # guaranteed labels per frame that may search the whole list of spots; the rest of a crowd reuse the spot they had
FORCED_REVIEW_S = 1.0  # a label that sits on a spot overlapping others looks for a better one only every this many seconds
DOT_KEEP_CLEAR_MAX = 24  # labels keep clear of the arrows of this many nearest creatures (a preference; in a bigger crowd it is pointless)


@lru_cache(maxsize=128)
def _arrow_polygon(step: int) -> QPolygonF:
    """The local arrow's polygon (see MapView._draw_player) scaled to a peer, turned ``step`` * 3 degrees clockwise."""
    a = math.radians(step * 3.0)
    c, s = math.cos(a), math.sin(a)
    k = PEER_ARROW_SCALE
    return QPolygonF([QPointF(vx * k * c - vy * k * s, vx * k * s + vy * k * c) for vx, vy in ARROW_POINTS])


_OWN_ARROW = QPolygonF([QPointF(x, y) for x, y in ARROW_POINTS])  # drawn around its centroid: the middle of the arrow is the position
_CROWD_ARROW = QPolygonF([QPointF(0, -3.2), QPointF(2.4, 2.6), QPointF(-2.4, 2.6)])


def _qcolor(hex_color: str, alpha: int = 255) -> QColor:
    c = QColor(hex_color)
    c.setAlpha(alpha)
    return c


class MapView(QWidget):
    def __init__(self, pipeline: TelemetryPipeline, store: MarkerStore, settings: Settings, peers=None) -> None:
        super().__init__()
        self.pipeline, self.store, self.settings = pipeline, store, settings
        self.peers = peers  # a peers.PeerSource or None; the only source of other players
        self.ents = EntitySmoother(tau_s=Interpolator().tau)  # same time constant as the own marker
        self._stale_s = PEER_STALE_S
        self._peers_snap = None
        self._peers_version = -1
        self.peer_status = ""
        self.peer_unplaced = 0  # creatures the server replicates to us that have no position yet (see peers.PeerSnapshot.unplaced)
        self.own_age_s: float | None = None  # seconds since the last accepted own move (None: no fix)
        self.record_status = ""  # set by App once a second while --record is active
        self._marker_tools: dict[tuple, tuple[QPen, QColor]] = {}
        self._bar_colors: dict[int, QColor] = {}  # health bar fill by 5 % step (no QColor built per frame)
        self._pen_key = None  # (colour, stale, player) of the pen / brush currently set on the painter
        self._text_widths: dict[str, float] = {}  # label font is constant, so widths can persist across frames
        self._text_widths_small: dict[str, float] = {}  # the same for the smaller font of the 250 - 500 m band
        self._text_widths_bold: dict[str, float] = {}  # the first line of a player's label is bold: measured with the bold font
        self._text_widths_small_bold: dict[str, float] = {}
        self.debug_positions = bool(os.environ.get("ISLE_DEBUG_POSITIONS"))  # crosshair at every exact decoded position (tray: Show exact positions)
        self._near_prev: set[str] = set()  # creatures that were inside the view radius in the last frame (see RADIUS_STICKY_FACTOR)
        self._guar_prev: set[str] = set()  # creatures that were guaranteed a label in the last frame (see geo.is_guaranteed)
        self._forced: dict[str, float] = {}  # label of a peer -> when it was put on the least-overlapping spot (no free one): kept there, reviewed now and then
        self.trace_frames = False  # tools/tier_check.py --labels: keep what the last frame drew in ``frame_trace``
        self.frame_trace: dict = {}
        self._label_slot: dict[str, int] = {}  # peer id -> label slot used last frame: tried first, so labels do not hop
        self._grouped: set[str] = set()
        self._compact_until: dict[str, float] = {}  # peer id -> until when its label stays one line
        self._labelled: set[str] = set()  # peers that had a label last frame: they keep it over equally near newcomers
        self._conf = LabelConfidence()  # what each label may claim; less sure only after a few seconds (no flapping text)
        self.errors = 0  # frames in which tick() or paintEvent() raised (the frame is still scheduled)
        self.last_error = ""
        self._snap_ok = True
        self.interp = Interpolator()
        self.full_map = False
        self.edit_mode = False
        self.pan = QPointF(0.5, 0.5)  # full-map centre (normalised)
        self.full_zoom = 1.0
        self._drag: QPointF | None = None
        self._last = time.monotonic()
        self._last_pos_at = 0.0
        self.pix = QPixmap(str(ASSET_DIR / "gateway_map.jpg"))
        self.zones = load_zones()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.apply_size()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    # ---- geometry / modes -------------------------------------------------
    def apply_size(self) -> None:
        edge = round(BASE_SIZE * clamp_size_percent(self.settings.size_percent) / 100)
        if self.full_map and self.screen() is not None:
            g = self.screen().availableGeometry()
            edge = int(min(g.width(), g.height()) * 0.85)
            self.setFixedSize(edge, edge)
            self.move(g.center().x() - edge // 2, g.center().y() - edge // 2)
        else:
            self.setFixedSize(edge, edge)

    def set_interactive(self, on: bool) -> None:
        was = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, not on)
        if was:
            self.show()

    def toggle_edit(self) -> None:
        self.edit_mode = not self.edit_mode
        self.set_interactive(self.edit_mode)

    def toggle_full_map(self) -> None:
        self.full_map = not self.full_map
        self.apply_size()

    def zoom_by(self, factor: float) -> None:
        if self.full_map:
            self.full_zoom = min(max(self.full_zoom * factor, 1.0), 16.0)
        else:
            self.settings.zoom = min(max(self.settings.zoom * factor, 0.5), 16.0)

    def zoom_to_radius(self, radius_m: float) -> None:
        """Zoom the minimap so its edge (half the width) is radius_m metres from the player."""
        span = 2 * radius_m * UNITS_PER_METER / (MAP_WIDTH * WORLD_SCALE)
        self.settings.zoom = min(max(BASE_VIEW_FRACTION / span, 0.5), 16.0)

    def set_size_percent(self, pct: int) -> None:
        self.settings.size_percent = clamp_size_percent(pct)
        self.apply_size()

    # ---- frame ------------------------------------------------------------
    def tick(self) -> None:
        now = time.monotonic()
        dt, self._last = now - self._last, now
        try:
            self._advance(now, dt)
        except Exception as err:  # noqa: BLE001 - one bad frame must not freeze the minimap: draw what we have
            self._note_error("tick", err)
        finally:
            self.update()  # always: the dots keep moving (dead reckoning) even when nothing new arrives

    def _note_error(self, where: str, err: Exception) -> None:
        self.errors += 1
        self.last_error = f"{where}: {type(err).__name__}: {err}"
        if self.errors <= 3 or self.errors % 600 == 0:  # a per-frame failure must not flood the log at 60 lines/s
            import logging

            logging.getLogger("isle_livemap.overlay").exception("minimap %s failed (%d so far)", where, self.errors)

    def _advance(self, now: float, dt: float) -> None:
        t = self.pipeline.latest
        if t.position is not None and t.position_at != self._last_pos_at:
            self._last_pos_at = t.position_at
            self.interp.set_target(t.position.x, t.position.y, t.position.map_heading_degrees, t.position_at)
        elif t.position is None and self.interp.has_fix:
            # the pipeline was reset (new server, game closed): the old position is not the player's any more, and a
            # marker parked there with "nearby" measured from it is a stuck map. Say "waiting" until a new fix arrives.
            self.interp.has_fix = False
            self._last_pos_at = 0.0
        self.own_age_s = max(0.0, now - t.position_at) if t.position is not None and t.position_at and self.interp.has_fix else None
        self.interp.step(dt, now)
        if self.peers is not None:
            try:
                snap = self.peers.snapshot()
            except Exception as err:  # noqa: BLE001 - keep drawing the last good snapshot
                self._note_error("peer snapshot", err)
                snap = self._peers_snap
            if snap is not None:
                self.peer_status = snap.status if snap.enabled else ""
                self.peer_unplaced = snap.unplaced if snap.enabled else 0
                # the source decides how long silence is tolerated (game: deltas only, so idle actors go quiet)
                self.ents.ttl = getattr(self.peers, "ttl_s", PEER_TTL_S)
                self._stale_s = getattr(self.peers, "stale_s", PEER_STALE_S)
                # a source that removes creatures itself (the game: destruction records) needs no second, longer timer here
                self.ents.missing_grace_s = getattr(self.peers, "gone_grace_s", None)
                self.ents.ghost_grace_s = getattr(self.peers, "ghost_grace_s", None)
                self.ents.ghost_kinds = getattr(self.peers, "ghost_kinds", frozenset())
                self.ents.ghost_age_s = self._stale_s  # a ghost is drawn dimmed from its first frame
                self.ents.heading_only_when_moving = False  # the arrow keeps its last direction of travel while the creature rests
                # an unchanged store returns the same snapshot object; a refreshed last_seen is a new one
                if snap is not self._peers_snap or snap.version != self._peers_version:
                    self._peers_snap, self._peers_version = snap, snap.version
                    self.ents.update(snap.entities, now)
        else:
            self.peer_status = ""
            self.peer_unplaced = 0
        self.ents.prune(now)
        self.ents.step(dt, now)

    def nearby(self, radius_m: float | None = ..., now: float | None = None) -> list[Nearby]:
        """Other players relative to the local player, nearest first (``geo.visible_entities``,
        also used by the Nearby panel). Default radius is the configured one; None = unlimited.
        Empty without a position fix."""
        if not self.interp.has_fix:
            return []
        if radius_m is ...:
            radius_m = self.settings.peer_radius_m
        now = time.monotonic() if now is None else now
        out = visible_entities(self.ents.items_aged(now), self.interp.x, self.interp.y, radius_m,
                               self.settings.show_players, self.settings.show_ai, self.settings.peer_self_ids,
                               ttl_s=self.ents.ttl, stale_s=self._stale_s)
        ghosts = self.ents.ghosts()
        if ghosts:  # the server destroyed these; they are held at their last position and say for how long
            gone = {eid: self.ents.gone_s(eid, now) for eid in ghosts}
            out = [replace(n, gone_s=gone[n.entity.id]) if gone.get(n.entity.id) is not None else n for n in out]
        return out

    def view(self) -> tuple[float, float, float]:
        """(centre_left, centre_top, view width as a fraction of the map)."""
        if self.full_map:
            return self.pan.x(), self.pan.y(), 1.0 / self.full_zoom
        left, top = project_unclamped(self.interp.x, self.interp.y)
        if not self.interp.has_fix:
            left, top = 0.5, 0.5
        return left, top, BASE_VIEW_FRACTION / self.settings.zoom

    def to_screen(self, left: float, top: float) -> QPointF:
        cl, ct, span = self.view()
        px = self.width() / span  # pixels per unit of normalised map width
        return QPointF(self.width() / 2 + (left - cl) * px, self.height() / 2 + (top - ct) * px * MAP_HEIGHT / MAP_WIDTH)

    def world_to_screen(self, x: float, y: float) -> QPointF:
        """Where the world point (x, y), Unreal centimetres, is drawn: the one conversion for every marker. Sub-pixel (QPointF),
        centred on the own position at the same instant, north up, the same metres per pixel in x and y."""
        return self.to_screen(*project_unclamped(x, y))

    def from_screen(self, p: QPointF) -> tuple[float, float]:
        cl, ct, span = self.view()
        px = self.width() / span
        return cl + (p.x() - self.width() / 2) / px, ct + (p.y() - self.height() / 2) / (px * MAP_HEIGHT / MAP_WIDTH)

    # ---- painting ---------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        try:
            p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
            r = QRectF(self.rect())
            if not self.full_map:
                p.setClipRegion(self._circle_region())
            p.fillRect(r, QColor(10, 14, 18, 150 if self.full_map else 90))
            for layer in (self._draw_map, self._draw_zones, self._draw_rings, self._draw_markers, self._draw_entities,
                          self._draw_player, self._draw_debug_positions):
                self._paint_layer(layer, p)
            p.setClipping(False)
            for layer in (self._draw_border, self._draw_peer_status, self._draw_record_status, self._draw_unplaced):
                self._paint_layer(layer, p)
        finally:
            p.end()  # an unbalanced painter would blank every later frame

    def _paint_layer(self, layer, p: QPainter) -> None:
        """One layer failing (a bad value in one peer) must not take the others, or the next frame, with it."""
        try:
            layer(p)
        except Exception as err:  # noqa: BLE001
            self._note_error(layer.__name__, err)

    def _circle_region(self):
        from PySide6.QtGui import QRegion

        return QRegion(self.rect(), QRegion.RegionType.Ellipse)

    def _draw_map(self, p: QPainter) -> None:
        tl, br = self.to_screen(0, 0), self.to_screen(1, 1)
        p.drawPixmap(QRectF(tl, br), self.pix, QRectF(self.pix.rect()))

    def _draw_zones(self, p: QPainter) -> None:
        if self.settings.show_zones:
            for layer, color in (("patrol", QColor(255, 200, 60, 120)), ("migration", QColor(90, 200, 255, 150))):
                p.setPen(QPen(color, 1.2))
                p.setBrush(QColor(color.red(), color.green(), color.blue(), 28))
                for z in self.zones[layer]:
                    p.drawPolygon(QPolygonF([self.to_screen(a, b) for a, b in z["points"]]))
        if self.settings.show_food:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(120, 255, 140, 200))
            for layer in self.zones["food"]:
                for a, b in layer["points"]:
                    p.drawEllipse(self.to_screen(a, b), 2.2, 2.2)

    def _player_screen(self) -> QPointF:
        return self.to_screen(*project_unclamped(self.interp.x, self.interp.y))

    def _draw_rings(self, p: QPainter) -> None:
        if not (self.settings.show_rings and self.interp.has_fix):
            return
        c = self._player_screen()
        px = self.width() / self.view()[2]
        p.setFont(QFont("Segoe UI", 8))
        for ring in build_rings(self.settings.ring_radii_m):
            # metres are isotropic on screen: same radius in x and y
            rx = ry = ring.rx * px
            p.setPen(QPen(QColor(255, 255, 255, 225), 1.5, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, rx, ry)
            p.setPen(QColor(255, 255, 255, 210))
            p.drawText(QPointF(c.x() + 3, c.y() - ry - 2), ring.label)

    def _draw_markers(self, p: QPainter) -> None:
        if not self.interp.has_fix and not self.store.markers:
            return
        c = self._player_screen()
        p.setFont(QFont("Segoe UI", 8))
        for m in self.store.markers:
            pt = self.to_screen(*project_unclamped(m.x, m.y))
            color = QColor(KIND_COLORS.get(m.kind, "#c8c8c8"))
            if m.kind == "waypoint" and self.interp.has_fix:
                p.setPen(QPen(color, 1.5))
                p.drawLine(c, pt)
            p.setPen(color)
            p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
            p.drawText(pt + QPointF(-6, 5), KIND_GLYPH.get(m.kind, "●"))
            p.setFont(QFont("Segoe UI", 8))
            label = m.name
            if self.interp.has_fix:
                d, b = m.relative_to(self.interp.x, self.interp.y)
                label += f"  {format_distance(d)} {compass_label(b)}"
            p.setPen(QColor(0, 0, 0, 200))
            p.drawText(pt + QPointF(9, 5), label)
            p.setPen(QColor(255, 255, 255))
            p.drawText(pt + QPointF(8, 4), label)

    def _draw_entities(self, p: QPainter) -> None:
        # Minimap: only within the configured radius. Full map: every known peer.
        radius = self.settings.peer_radius_m
        far: list[Nearby] = []
        if self.full_map:
            near = self.nearby(None)
            near_ids: set[str] = set()
        else:  # beyond the radius, players and dinosaurs are not dropped: they become pointers on the circle edge
            reach = self.nearby(max(radius, EDGE_RANGE_M))
            was_near = self._near_prev  # a creature walking along the radius stays a dot until it is clearly beyond it (no flicker dot <-> pointer)
            near = [n for n in reach if n.distance_m <= radius * (RADIUS_STICKY_FACTOR if n.entity.id in was_near else 1.0)]
            near_ids = {n.entity.id for n in near}
            far = [n for n in reach if n.entity.id not in near_ids and n.entity.kind != "ai"]
        if not near and not far:
            self._label_slot, self._labelled = {}, set()
            if self.trace_frames:
                self.frame_trace = {"labels": {}, "glyph": {}, "pos": {}, "edge": set(), "leaders": 0}
            return
        bounds = (0.0, 0.0, float(self.width()), float(self.height()))
        # one view() per frame instead of one per entity (to_screen recomputes it every call)
        cl, ct, span = self.view()
        scale = self.width() / span
        sx, sy = scale / MAP_WIDTH / WORLD_SCALE, scale * (MAP_HEIGHT / MAP_WIDTH) / MAP_HEIGHT / WORLD_SCALE
        ox, oy = self.width() / 2 + (X_OFFSET / MAP_WIDTH - cl) * scale, self.height() / 2 + (Y_OFFSET / MAP_HEIGHT - ct) * scale * MAP_HEIGHT / MAP_WIDTH
        half = self.width() / 2
        lim = (half + 4) ** 2 if not self.full_map else None
        off = []
        on_screen: list = []
        w, h_px = self.width(), self.height()
        for n in near:
            pt = QPointF(ox + n.x * sx, oy + n.y * sy)
            if lim is None:
                inside = -10 <= pt.x() <= w + 10 and -10 <= pt.y() <= h_px + 10
            else:
                inside = (pt.x() - half) ** 2 + (pt.y() - h_px / 2) ** 2 <= lim
            (on_screen if inside else off).append((n, pt))
        far_pts = [(n, QPointF(ox + n.x * sx, oy + n.y * sy)) for n in far]
        edge_ids = {n.entity.id for n, _ in off} | {n.entity.id for n, _ in far_pts}
        if off or far_pts:
            self._draw_edge_indicators(p, off, far_pts)
        # one order for everything (geo.priority_key): creatures before server animals, then the 250 / 500 / 1000 m tier,
        # then players before dinosaurs, then the nearest; a peer that had a label keeps it over a newcomer at almost the
        # same distance
        before = self._labelled
        guar_prev = self._guar_prev

        def guar(n):
            return (not self.full_map) and is_guaranteed(n, n.entity.id in guar_prev)

        by_priority = sorted(on_screen, key=lambda it: priority_key(it[0], it[0].entity.id in before))
        ai_on_screen = sum(1 for it in by_priority if it[0].entity.kind == "ai")
        tier_caps, max_ai_labels = label_limits(len(by_priority) - ai_on_screen, ai_on_screen, self.full_map)
        max_labels = sum(tier_caps[:3])
        font = QFont("Segoe UI", 8)
        fm = QFontMetricsF(font)
        font_small = QFont("Segoe UI", SMALL_FONT_PT)
        fm_small = QFontMetricsF(font_small)
        font_b, font_small_b = QFont("Segoe UI", 8), QFont("Segoe UI", SMALL_FONT_PT)  # the first line of a player's label is bold
        font_b.setBold(True)
        font_small_b.setBold(True)
        fm_b, fm_small_b = QFontMetricsF(font_b), QFontMetricsF(font_small_b)
        widths_b, widths_small_b = self._text_widths_bold, self._text_widths_small_bold
        if len(widths_b) > 2000 or len(widths_small_b) > 2000:
            widths_b.clear()
            widths_small_b.clear()

        def lines_width(ls, small: bool, bold: bool) -> float:
            """Width of a label (px): the widest line, the first one measured with the bold font when ``bold``."""
            wd = 0.0
            for i, t in enumerate(ls):
                if bold and i == 0:
                    cache, m = (widths_small_b, fm_small_b) if small else (widths_b, fm_b)
                else:
                    cache, m = (widths_small, fm_small) if small else (widths, fm)
                w_ = cache.get(t)
                if w_ is None:
                    w_ = cache[t] = m.horizontalAdvance(t)
                if w_ > wd:
                    wd = w_
            return wd + 2
        p.setFont(font)
        centre, radius = QPointF(self.width() / 2, self.height() / 2), self.width() / 2 - 3

        ccx, ccy = centre.x(), centre.y()

        def in_circle(r, extra: float = 0.0) -> bool:  # the minimap clips to its circle: a label must not run past the edge
            if self.full_map:
                return True
            dx = max(abs(r[0] - ccx), abs(r[0] + r[2] - ccx))  # the corner of the rectangle that is furthest from the centre
            dy = max(abs(r[1] - ccy), abs(r[1] + r[3] - ccy))
            return dx * dx + dy * dy <= (radius + extra) ** 2

        def in_circle_loose(r) -> bool:  # for the slot a label already has (see geo.SLOT_SLACK_PX)
            return in_circle(r, SLOT_SLACK_PX)

        def pulled_in(ax: float, ay: float, w: float, h: float, below: float = 0.0) -> list:
            """Candidate rectangles for a label that finds no free spot next to a dot near the edge of the round view: the
            usual spot under the dot moved straight towards the centre in five steps."""
            x0, y0 = ax - w / 2, ay + 8.0 + below
            return [(x0 + (ccx - w / 2 - x0) * f, y0 + (ccy - h / 2 - y0) * f, w, h) for f in (i / 5.0 for i in range(1, 6))]

        searches = [0, 0]  # [whole-list searches, last-resort placements] of the guaranteed labels in this frame
        forced_now: dict[str, float] = {}

        def place_guaranteed(pt, lines, small, tier, below, prefer, slot, eid, now, bold):
            """A label that must be shown (a creature within 500 m on the minimap). In order: the spot it had; the usual spots and
            the spread-out ones, clear of other labels and of arrows, then clear of other labels only; one line instead of two
            (250 - 500 m band only); at last the spot that covers the least of the others (or, past ``SEARCH_BUDGET`` searches
            in a frame, the spot it had even if it overlaps). Returns (rect, lines)."""
            ax, ay = pt.x(), pt.y()
            fm_l = fm_small if small else fm

            def size(ls):
                return lines_width(ls, small, bold), fm_l.height() * len(ls)

            def more(wd, hd):
                return [*spread_slots(ax, ay, wd, hd, below), *pulled_in(ax, ay, wd, hd, below)]

            def candidate(wd, hd, idx):  # one spot by its index (see geo.label_slots / spread_slots / pulled_in), without building the lists
                if idx < 10:
                    return label_slots(ax, ay, wd, hd, bounds, below)[idx]
                if idx < 10 + SPREAD_COUNT:
                    return spread_slot(ax, ay, wd, hd, below, idx - 10)
                k_ = idx - 10 - SPREAD_COUNT
                return pulled_in(ax, ay, wd, hd, below)[k_] if k_ < 5 else None

            def covers_core(r):
                return any(r[0] < c[0] + c[2] and c[0] < r[0] + r[2] and r[1] < c[1] + c[3] and c[1] < r[1] + r[3] for c in cores)

            def inside(r):
                return r[0] >= bounds[0] and r[1] >= bounds[1] and r[0] + r[2] <= bounds[0] + bounds[2] and r[1] + r[3] <= bounds[1] + bounds[3]

            w_, h_ = size(lines)
            extra_ = ()
            r_ = None
            if prefer is not None and prefer >= 10:  # a spread-out spot it had: kept while it is still free (a tolerance of a few px)
                r_ = candidate(w_, h_, prefer)
                tol = SLOT_SLACK_PX
                if r_ is not None and inside(r_) and in_circle_loose(r_) and not any(
                        r_[0] < o[0] + o[2] - tol and o[0] < r_[0] + r_[2] - tol and r_[1] < o[1] + o[3] - tol and o[1] < r_[1] + r_[3] - tol
                        for o in kept_obstacles(())) and not covers_core(r_):
                    slot[0] = prefer
                    return r_, lines
                r_ = None
            elif prefer is not None:
                r_ = place_label(ax, ay, w_, h_, kept_obstacles(()), bounds, in_circle, prefer, slot, in_circle_loose, True, (), below=below)
                if r_ is not None and not covers_core(r_):  # a kept side only gives way to an arrow under its text (no tolerance there)
                    return r_, lines
                r_ = None
            if prefer is not None and eid in self._forced and now - self._forced[eid] < FORCED_REVIEW_S:
                # it sat on an overlapping spot a moment ago because nothing was free: stay there instead of searching again
                r_ = candidate(w_, h_, prefer)
                if r_ is not None and inside(r_) and in_circle_loose(r_):
                    forced_now[eid] = self._forced[eid]
                    slot[0] = prefer
                    return r_, lines
                r_ = None
            if searches[0] < SEARCH_BUDGET:
                searches[0] += 1
                extra_ = more(w_, h_)
                r_ = place_label(ax, ay, w_, h_, placed + dots, bounds, in_circle, prefer, slot, in_circle_loose, extra=extra_, below=below)
                if r_ is None:
                    r_ = place_label(ax, ay, w_, h_, placed + cores, bounds, in_circle, prefer, slot, in_circle_loose, extra=extra_, below=below)  # clear of other labels and of every arrow centre
                if r_ is None and tier == 1 and len(lines) > 1:  # between 250 and 500 m one line says who / what it is
                    lines = lines[:1]
                    self._compact_until[eid] = now + COMPACT_HOLD_S
                    w_, h_ = size(lines)
                    extra_ = more(w_, h_)
                    r_ = place_label(ax, ay, w_, h_, placed + dots, bounds, in_circle, prefer, slot, in_circle_loose, extra=extra_, below=below)
                    if r_ is None:
                        r_ = place_label(ax, ay, w_, h_, placed + cores, bounds, in_circle, prefer, slot, in_circle_loose, extra=extra_, below=below)  # clear of other labels and of every arrow centre
                if r_ is not None:
                    return r_, lines
            if not extra_:
                extra_ = more(w_, h_)
            cands = [*label_slots(ax, ay, w_, h_, bounds, below), *extra_]
            order = list(range(len(cands)))
            if prefer is not None and 0 <= prefer < len(cands):  # the spot it had comes first: a crowd does not shuffle its labels
                order.remove(prefer)
                order.insert(0, prefer)
            ordered = [cands[i] for i in order]
            searches[1] += 1
            r_ = least_overlap(ordered, placed if searches[1] <= LAST_RESORT_PER_FRAME else hard, bounds, in_circle, cores)
            if r_ is None:
                r_ = least_overlap(ordered, hard, bounds, in_circle, cores)
            if r_ is None:
                r_ = least_overlap(ordered, [], bounds, None)
            if r_ is not None:
                slot[0] = order[ordered.index(r_)]
                forced_now[eid] = now
            return r_, lines

        placed: list[tuple[float, float, float, float]] = []  # label rectangles already taken; the status lines are too
        top = 6.0 if self.full_map else 18.0
        room = self.width() * (0.9 if self.full_map else 0.62)
        for shown, row in ((self.peer_status, 0), (self.record_status, 1)):
            if shown:
                placed.append(((self.width() - room) / 2, top + 14 * row, room, 16.0))
        if self.peer_unplaced:
            ur = self._unplaced_rect()
            placed.append((ur.x(), ur.y(), ur.width(), ur.height()))
        if not self.full_map and self.interp.has_fix:  # no label covers your own arrow
            me = self._player_screen()
            placed.append((me.x() - 13.0, me.y() - 15.0, 26.0, 30.0))
        self._draw_edge_captions(p, far_pts, placed)
        n_strong = len(placed)
        if not self.full_map and self.settings.show_rings and self.interp.has_fix:  # the "500 m" captions of the rings are not covered
            own = self._player_screen()  # (by a new spot; a label that already sits there keeps its side: that would be a hop for a caption)
            px_m = self.width() / span
            for ring in build_rings(self.settings.ring_radii_m):
                placed.append((own.x() + 2.0, own.y() - ring.rx * px_m - 13.0, 40.0, 14.0))
        hard = list(placed)  # status lines, your own arrow, edge and ring captions: no label goes there, whatever the crowd
        n_hard = len(hard)
        keep_obstacles = placed[:n_strong]

        def kept_obstacles(extra_rects):
            """What a label that already has a side must stay clear of to keep it: the status lines, your arrow, the edge
            captions and the labels placed so far (not the ring captions: moving a label for them is a hop for no good reason)."""
            return [*keep_obstacles, *placed[n_hard:], *extra_rects]
        p.setFont(font)
        attempts = ai_labelled = named_extra = pulled_in_labels = 0
        creatures = len(by_priority) - ai_on_screen
        named_cap = NAMED_EXTRA_LABELS if creatures <= CROWDED_ON_SCREEN else NAMED_EXTRA_CROWDED
        pull_cap = PULL_IN_MAX if creatures <= CROWDED_ON_SCREEN else PULL_IN_CROWDED
        tier_used = [0] * len(tier_caps)
        slots_now: dict[str, int] = {}
        widths = self._text_widths  # text measuring is the slow part of the layer: measure each string once
        if len(widths) > 2000:
            widths.clear()
        total = len(by_priority)
        label_ops: list = []  # label pills and text, drawn BEFORE the glyphs: an arrow is never covered by somebody's label
        # dots of players and dinosaurs that can compete with a label: kept clear when possible (a preference, so two
        # creatures standing next to each other still both get a label)
        dots = []
        cores = []  # the middle of each arrow in ``dots``
        bars_on = bool(self.settings.show_vitals_bars)
        first_ai = len(by_priority) - ai_on_screen
        for n, pt in (*by_priority[:min(max_labels, DOT_KEEP_CLEAR_MAX)], *by_priority[first_ai:first_ai + 2 * max_ai_labels]):  # creatures, then the fish that can get a label
            c = max(DOT_CLEARANCE_PX, marker_extent(n.entity) + 3.0)  # a big dino's dot is wider than the default
            bars_h = (marker_radius(n.entity) + 3.0 + BAR_BOX_H + 1.0 - c) if bars_on and n.entity.kind != "ai" else 0.0  # its bars hang under it
            dots.append((pt.x() - c, pt.y() - c, 2 * c, 2 * c + max(0.0, bars_h)))
            cores.append((pt.x() - 3.0, pt.y() - 3.0, 6.0, 6.0))  # a kept label side only gives way to an arrow under its text
        now = time.monotonic()
        # creatures of one species standing together (a pack) share the label of the nearest one: "Carnotaurus ×5". Only
        # the creatures that can compete for a label are looked at, so a crowd of 500 costs nothing here.
        skip: set[str] = set()
        group_of: dict[str, int] = {}
        leaders: list[tuple[Nearby, QPointF]] = []
        scanned, scan_limit = 0, (tier_caps[1] + tier_caps[2]) * 6
        for n, pt in by_priority:
            e = n.entity
            if e.kind not in ("dino", "unknown") or not e.species or priority_tier(n.distance_m) == 0 or not is_placeholder_name(e.name) \
                    or guar(n):  # inside 500 m every creature keeps its own label
                if e.kind == "ai":
                    break  # sorted: only server animals from here on
                continue  # the creatures inside 250 m each keep their own full label
            scanned += 1
            if scanned > scan_limit:
                break
            for ln, lpt in leaders:
                if ln.entity.species == e.species and (lpt.x() - pt.x()) ** 2 + (lpt.y() - pt.y()) ** 2 < CLUSTER_PX ** 2:
                    group_of[ln.entity.id] += 1
                    skip.add(e.id)
                    break
            else:
                leaders.append((n, pt))
                group_of[e.id] = 1
        # players and dinosaurs claim label space before server animals (fish) do, the nearest tier first. On the minimap
        # every player-owned creature within 500 m (geo.is_guaranteed) is guaranteed a label with its name and species:
        # no tier cap, no pack label; the first GUARANTEED_FULL_LABELS (nearest first) have two lines, the next ones one,
        # and when the free spots next to the marker are gone the label is spread out with a leader line to its marker.
        guaranteed_used = 0
        band_total = 0
        guar_now: set[str] = set()
        band_ends: list[int] = []  # index just after each guaranteed creature in by_priority
        if not self.full_map:
            for idx, it in enumerate(by_priority):  # creatures come first, server animals last
                if it[0].entity.kind == "ai":
                    break
                if guar(it[0]):
                    band_total += 1
                    guar_now.add(it[0].entity.id)
                    band_ends.append(idx + 1)
        guaranteed_max = GUARANTEED_MAX_LABELS if band_total <= GUARANTEED_CROWD else GUARANTEED_CROWD_LABELS
        # a crowd beyond the label limit: the creatures between the last labelled one and the end of the band are not visited
        label_order = by_priority if band_total <= guaranteed_max else by_priority[:band_ends[guaranteed_max - 1]] + by_priority[band_ends[-1]:]
        widths_small = self._text_widths_small
        if len(widths_small) > 2000:
            widths_small.clear()
        leader_ops: list = []
        trace = {"labels": {}, "glyph": {}, "edge": edge_ids} if self.trace_frames else None
        for n, pt in label_order:
            guaranteed = guar(n)
            if not guaranteed and attempts >= (max_labels + max_ai_labels) * LABEL_ATTEMPTS_PER_LABEL:
                break
            if n.entity.id in skip:
                continue
            is_ai = n.entity.kind == "ai"
            tier = priority_tier(n.distance_m)
            below = BAR_BOX_H + 3.0 if bars_on and not is_ai else 0.0  # the slots under the arrow start below its bars
            short_only = False
            if guaranteed:
                if guaranteed_used >= guaranteed_max:
                    continue
                guaranteed_used += 1
                short_only = guaranteed_used > GUARANTEED_FULL_LABELS
            else:
                if is_ai:
                    if ai_labelled >= max_ai_labels:
                        continue
                elif tier_used[min(tier, len(tier_caps) - 1)] >= tier_caps[min(tier, len(tier_caps) - 1)]:
                    # the tier is full: a player whose name is known still gets a one-line label (the name is what a player
                    # is looked for by), up to NAMED_EXTRA_LABELS of them; everybody else waits for a free label
                    if is_placeholder_name(n.entity.name) or named_extra >= named_cap:
                        continue
                    short_only = True
                attempts += 1
            e = n.entity
            h_now, s_now = vitals_readings(e)
            sp, inferred, hp, st, vtier = self._conf.settle(n.entity.id, e.species, e.inferred, h_now, s_now, n.vitals_age_s, now)
            if (sp, inferred, hp, st) != (e.species, e.inferred, h_now, s_now):
                n = replace(n, entity=with_vitals(replace(e, species=sp, inferred=inferred), hp, st))
            lines = peer_label_lines(n, vtier, group_of.get(n.entity.id, 1))
            if short_only:
                lines = lines[:1]
            if len(lines) > 1 and now < self._compact_until.get(n.entity.id, 0.0) and not (guaranteed and tier == 0):
                lines = lines[:1]  # it did not fit in full a moment ago: stay short for a few seconds instead of flipping size
            small = guaranteed and tier == 1  # the 250 - 500 m band: a smaller font, so that more of them fit
            fm_l = fm_small if small else fm
            bold = not is_ai  # the first line of a player's / dinosaur's label is bold; the fish's stays regular
            w = lines_width(lines, small, bold)
            h = fm_l.height() * len(lines)
            eid = n.entity.id
            prefer, slot = self._label_slot.get(eid), [0]
            r = None
            if guaranteed:
                r, lines = place_guaranteed(pt, lines, small, tier, below, prefer, slot, eid, now, bold)
            else:
                if prefer is not None:  # the side it had: kept while it still fits, even if a dot has moved under it (no hopping)
                    r = place_label(pt.x(), pt.y(), w, h, kept_obstacles(()), bounds, in_circle, prefer, slot, in_circle_loose, True, below=below)
                if r is None:
                    r = place_label(pt.x(), pt.y(), w, h, placed + dots, bounds, in_circle, prefer, slot, in_circle_loose, below=below)
                if r is None and not is_ai and not short_only:  # a one-line extra label tries once: a crowd cannot cost frames
                    r = place_label(pt.x(), pt.y(), w, h, placed, bounds, in_circle, prefer, slot, in_circle_loose, below=below)
                if r is None and not is_ai and tier == 0 and n.gone_s is None:  # the creatures inside 250 m (full map) keep their full label, even over others
                    r = place_label(pt.x(), pt.y(), w, h, [], bounds, in_circle, prefer, slot, in_circle_loose, below=below)
                if r is None and not is_ai and len(lines) > 1:  # no room for two lines: at least say who / what it is
                    lines = lines[:1]
                    self._compact_until[eid] = now + COMPACT_HOLD_S
                    w, h = lines_width(lines, False, bold), fm.height()
                    r = place_label(pt.x(), pt.y(), w, h, placed + dots, bounds, in_circle, prefer, slot, in_circle_loose, below=below)
                    if r is None:
                        r = place_label(pt.x(), pt.y(), w, h, placed, bounds, in_circle, prefer, slot, in_circle_loose, below=below)
                    if r is None and tier == 0:
                        r = place_label(pt.x(), pt.y(), w, h, [], bounds, in_circle, prefer, slot, in_circle_loose, below=below)
            if r is None and not is_ai and not guaranteed and pulled_in_labels < pull_cap and not is_placeholder_name(e.name) and n.gone_s is None:
                pulled_in_labels += 1
                # a known name is never left off: no free spot fits inside the round view next to a dot near its edge, so the
                # label (one line) is pulled in towards the centre until it fits, even over other labels
                if len(lines) > 1:
                    lines = lines[:1]
                    w, h = lines_width(lines, False, bold), fm.height()
                pulled = pulled_in(pt.x(), pt.y(), w, h, below)
                r = place_label(pt.x(), pt.y(), w, h, placed, bounds, in_circle, None, slot, None, False, pulled, below=below)  # clear of other labels if it can be
                if r is None:
                    r = place_label(pt.x(), pt.y(), w, h, [], bounds, in_circle, None, slot, None, False, pulled, below=below)
            if r is None:
                continue
            placed.append(r)
            slots_now[eid] = slot[0]
            if is_ai:
                ai_labelled += 1
            elif short_only and not guaranteed:
                named_extra += 1
            elif not guaranteed:
                tier_used[min(tier, len(tier_caps) - 1)] += 1
            alpha = LABEL_STALE_ALPHA if n.stale else 255
            sp_tier = species_tier(sp, inferred)  # a species that is only remembered / unknown is drawn softer than a confirmed one
            head_alpha = int(alpha * (SOFT_INFERRED if sp_tier == SPECIES_INFERRED else SOFT_UNKNOWN if sp_tier == SPECIES_UNKNOWN else 1.0))
            st_ = peer_style(n.entity)
            color = _qcolor(st_.label_color or st_.color, head_alpha)
            label_ops.append((r, lines, alpha, color, head_alpha, small, bold))
            if not is_ai:
                nx, ny, gap = leader_end(r, pt.x(), pt.y())
                if gap > max(LEADER_MIN_PX, 1.5 * marker_radius(n.entity) + 6.0):  # displaced from its marker: a line joins them
                    leader_ops.append((pt.x(), pt.y(), nx, ny))
            if trace is not None:
                trace["labels"][eid] = (list(lines), r, small)
        if bars_on:  # under the arrows, before the label pills: a label is never covered by a bar
            band = min(band_total, guaranteed_max)
            self._draw_all_bars(p, by_priority, skip, max(BAR_MAX if creatures <= CROWD_GLYPHS else BAR_CROWDED, band))
        if leader_ops:  # thin lines from a displaced label to its marker, under the pills and the arrows
            p.setPen(QPen(QColor(200, 160, 255, LEADER_ALPHA), 1.0))
            for ax_, ay_, nx_, ny_ in leader_ops:
                p.drawLine(QPointF(ax_, ay_), QPointF(nx_, ny_))
        cur_font = font
        for r, lines, alpha, color, head_alpha, small, bold in label_ops:
            fm_d = fm_small if small else fm
            p.setPen(Qt.PenStyle.NoPen)  # a dark pill behind the text: readable on forest, rock and water alike (stronger for a player)
            if bold:
                p.setBrush(LABEL_BG_PLAYER if alpha == 255 else LABEL_BG_PLAYER_STALE)
            else:
                p.setBrush(LABEL_BG if alpha == 255 else LABEL_BG_STALE)
            p.drawRoundedRect(QRectF(r[0] - 2.0, r[1] - 0.5, r[2] + 2.0, r[3] + 1.0), 3.0, 3.0)
            for i, text in enumerate(lines):
                want = (font_small_b if small else font_b) if bold and i == 0 else (font_small if small else font)
                if want is not cur_font:
                    p.setFont(want)
                    cur_font = want
                base = QPointF(r[0], r[1] + fm_d.ascent() + i * fm_d.height())
                p.setPen(color if i == 0 else QColor(255, 255, 255, alpha))
                p.drawText(base, text)
        if cur_font is not font:
            p.setFont(font)
        # the glyphs go on top of every pill and caption: the most important peer (players before dinosaurs before fish, the
        # 250 m tier before the 500 m one) is painted last, so nothing covers an arrow that matters
        self._pen_key = None
        for i, (n, pt) in enumerate(reversed(by_priority)):  # players on top, far before near
            rank = total - i  # 1 = the most important peer
            if rank > CROWD_GLYPHS:
                self._draw_crowd_dot(p, n, pt)
            else:
                self._draw_entity_marker(p, n, pt, rank <= HALO_MAX)
            if trace is not None:
                trace["glyph"][n.entity.id] = rank <= CROWD_GLYPHS
        if trace is not None:
            trace["pos"] = {n.entity.id: (pt.x(), pt.y(), n.distance_m, n.entity.kind) for n, pt in by_priority}
            trace["leaders"] = len(leader_ops)
            self.frame_trace = trace
        self._guar_prev = guar_now
        self._near_prev = near_ids
        self._label_slot = slots_now
        self._forced = forced_now
        self._labelled = set(slots_now)
        if len(self._compact_until) > 400:
            self._compact_until = {k: v for k, v in self._compact_until.items() if v > now}
        self._grouped = skip  # creatures whose dot is covered by the "×N" label of a neighbour of the same species
        self._conf.keep_only({n.entity.id for n in near})

    def _bar_fill(self, pct: float) -> QColor:
        step = int(max(0.0, min(100.0, pct)) // 5)
        c = self._bar_colors.get(step)
        if c is None:  # red -> amber -> green with the health left
            f = step / 20.0
            c = self._bar_colors[step] = QColor(int(235 * min(1.0, 2.0 * (1.0 - f)) + 20), int(215 * min(1.0, 2.0 * f) + 25), 60)
        return c

    def _draw_all_bars(self, p: QPainter, by_priority: list, skip: set, cap: int = BAR_MAX) -> None:
        """Health (green to red) and stamina (blue) under the arrow of every player and dinosaur, the most important
        ``BAR_MAX`` of them, whether it has a label or not. A reading never received is an empty grey track, so every
        creature has the same two bars. Server animals have none. Rectangles of one colour go to the painter in one call."""
        tracks: list[QRectF] = []
        unknown: list[QRectF] = []
        stamina: list[QRectF] = []
        health: dict[int, list[QRectF]] = {}
        drawn = 0
        for n, pt in by_priority:
            e = n.entity
            if e.kind == "ai" or e.id in skip:
                continue
            drawn += 1
            if drawn > cap:
                break
            rad = marker_radius(e)
            w = max(14.0, 2.0 * rad + 6.0)
            x, y = pt.x() - w / 2, pt.y() + rad + 3.0
            y2 = y + BAR_HP_H + BAR_GAP
            tracks.append(QRectF(x - 0.5, y - 0.5, w + 1.0, BAR_BOX_H + 1.0))
            if e.health_pct is None:
                unknown.append(QRectF(x, y, w, BAR_HP_H))
            else:
                hp = max(0.0, min(100.0, e.health_pct))
                health.setdefault(int(hp // 5), []).append(QRectF(x, y, w * hp / 100.0, BAR_HP_H))
            if e.stamina_pct is None:
                unknown.append(QRectF(x, y2, w, BAR_ST_H))
            else:
                stamina.append(QRectF(x, y2, w * max(0.0, min(100.0, e.stamina_pct)) / 100.0, BAR_ST_H))
        if not tracks:
            return
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(BAR_TRACK)
        p.drawRects(tracks)
        if unknown:
            p.setBrush(BAR_UNKNOWN)
            p.drawRects(unknown)
        for step, rects in health.items():
            p.setBrush(self._bar_fill(step * 5.0))
            p.drawRects(rects)
        if stamina:
            p.setBrush(BAR_ST_FILL)
            p.drawRects(stamina)

    def _draw_crowd_dot(self, p: QPainter, n: Nearby, pt: QPointF) -> None:
        """A peer beyond the first CROWD_GLYPHS: a plain square in its colour (500 anti-aliased glyphs do not fit a frame)."""
        key = ("crowd", peer_style(n.entity).color, n.stale)
        if self._pen_key != key:
            c = _qcolor(peer_style(n.entity).color, STALE_ALPHA if n.stale else 255)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            self._pen_key = key
        if n.entity.kind == "ai":
            p.drawRect(QRectF(pt.x() - 1.8, pt.y() - 1.8, 3.6, 3.6))
        else:  # a far peer beyond the glyph budget is still an arrow, only tiny and unrotated
            x, y = pt.x(), pt.y()
            p.translate(x, y)
            p.drawPolygon(_CROWD_ARROW)
            p.translate(-x, -y)

    def _draw_entity_marker(self, p: QPainter, n: Nearby, pt: QPointF, decorate: bool = True) -> None:
        """Peer glyph: every dinosaur / player is the same purple arrow as the local one (no species, diet or size
        categories, no dots); it points along the direction of travel, north until that is known. Server animals are a
        small muted diamond. Dim when stale."""
        e = n.entity
        st = peer_style(e)
        known = e.heading is not None
        outline = decorate or e.kind == "ai"  # the outline (the costly part of an arrow) is for the creatures that matter
        ghost = 0 if n.gone_s is None else 1 + min(int(n.gone_s // GHOST_FADE_STEP_S), GHOST_FADE_STEPS - 1)  # 0: live; 1..N: older and fainter
        key = (st.color, n.stale, known, outline, ghost)
        pb = self._marker_tools.get(key)
        if pb is None:  # one pen/brush per (colour, stale, heading known, outline, ghost age): creating them per marker dominated the layer
            alpha = STALE_ALPHA if n.stale else (255 if known or e.kind == "ai" else UNKNOWN_HEADING_ALPHA)
            if ghost:
                alpha = int(STALE_ALPHA * (1.0 - 0.6 * ghost / GHOST_FADE_STEPS))  # a creature the server destroyed fades with every step
            pen = QPen(_qcolor(st.outline, min(230, alpha)), 1.5 if e.kind == "ai" else PEER_OUTLINE_W) if outline else QPen(Qt.PenStyle.NoPen)
            pb = self._marker_tools[key] = (pen, _qcolor(st.color, alpha))
        if self._pen_key != key:  # a run of equal markers (a crowd) sets pen and brush once
            p.setPen(pb[0])
            p.setBrush(pb[1])
            self._pen_key = key
        if e.kind == "ai":
            r = marker_radius(e) + 1
            p.drawPolygon(QPolygonF([pt + QPointF(0, -r), pt + QPointF(r, 0), pt + QPointF(0, r), pt + QPointF(-r, 0)]))
            return
        x, y = pt.x(), pt.y()
        p.translate(x, y)  # a cached polygon moved by the painter: cheaper than building 4 points per arrow and frame
        p.drawPolygon(_arrow_polygon(round(e.heading / 3.0) % 120 if known else 0))
        p.translate(-x, -y)

    def _draw_edge_indicators(self, p: QPainter, offscreen: list[tuple[Nearby, QPointF]],
                              far: list[tuple[Nearby, QPointF]] = ()) -> None:
        """Pointers on the circle edge: peers inside the radius but outside the circle, and (``far``) players and
        dinosaurs beyond the radius, the nearest few with who they are and how far ("Name 1.1 km")."""
        if not offscreen and not far:
            return
        cx, cy = self.width() / 2, self.height() / 2
        r = self.width() / 2 - EDGE_INSET_PX
        for n, pt in list(reversed(offscreen)) + list(reversed(far)):
            a = edge_anchor(cx, cy, pt.x(), pt.y(), r)
            if a is None:
                continue
            st = peer_style(n.entity)
            alpha = STALE_ALPHA if n.stale else 255
            p.save()
            p.translate(QPointF(*a))
            p.rotate(math.degrees(math.atan2(pt.x() - cx, -(pt.y() - cy))))  # points outward
            p.setPen(QPen(_qcolor(st.outline, min(230, alpha)), 1.2))
            p.setBrush(_qcolor(st.color, alpha))
            p.drawPolygon(QPolygonF([QPointF(0, -5), QPointF(4, 3), QPointF(-4, 3)]))
            p.restore()

    def _draw_edge_captions(self, p: QPainter, far: list[tuple[Nearby, QPointF]], placed: list) -> None:
        """"Name 1.1 km" next to the pointers of the nearest creatures beyond the radius. Each caption takes a free spot
        just inside its pointer (rectangles in ``placed`` are avoided and the caption is added to them, so labels keep
        clear of it too); no free spot: no caption, the pointer alone is still there."""
        if not far:
            return
        cx, cy = self.width() / 2, self.height() / 2
        r = self.width() / 2 - EDGE_INSET_PX
        cap_font = QFont("Segoe UI", 7)
        cap_font.setBold(True)  # the name of a far player, like the first line of a label
        p.setFont(cap_font)
        fm = QFontMetricsF(cap_font)
        for n, pt in sorted(far, key=lambda it: (is_placeholder_name(it[0].entity.name), it[0].distance_m))[:EDGE_CAPTIONS]:
            a = edge_anchor(cx, cy, pt.x(), pt.y(), r)  # players with a known name first, then the nearest
            if a is None:
                continue
            e = n.entity
            who = display_name(e)
            text = f"{truncate(who, 12)} {format_distance(n.distance_m)}"
            w, h = fm.horizontalAdvance(text) + 6, fm.height()
            ux, uy = (a[0] - cx) / r, (a[1] - cy) / r  # unit vector outward
            for inward in (12.0, 26.0, 40.0):
                qx, qy = a[0] - ux * inward, a[1] - uy * inward
                box = (min(max(qx - w / 2, 1.0), self.width() - w - 1.0), qy - h / 2, w, h)
                if not any(_overlaps(box, q) for q in placed):
                    break
            else:
                continue
            placed.append(box)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(LABEL_BG_PLAYER)
            p.drawRoundedRect(QRectF(box[0] - 2, box[1], w + 2, h), 3.0, 3.0)
            st_ = peer_style(e)
            p.setPen(_qcolor(st_.label_color or st_.color, STALE_ALPHA if n.stale else 235))
            p.drawText(QPointF(box[0], box[1] + fm.ascent()), text)

    def _draw_peer_status(self, p: QPainter) -> None:
        if not self.peer_status:
            return
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(QColor(255, 170, 90) if status_is_warning(self.peer_status) else QColor(255, 255, 255, 190))
        room = self.width() * (0.9 if self.full_map else 0.62)  # inside the circle chord at that height
        text = QFontMetricsF(p.font()).elidedText(self.peer_status, Qt.TextElideMode.ElideRight, room)
        p.drawText(QRectF(0, 6 if self.full_map else 18, self.width(), 16), Qt.AlignmentFlag.AlignHCenter, text)

    UNPLACED_H = 16.0

    def _unplaced_rect(self) -> QRectF:
        room = self.width() * (0.9 if self.full_map else 0.62)
        return QRectF((self.width() - room) / 2, self.height() - (22.0 if self.full_map else 36.0), room, self.UNPLACED_H)

    def _draw_unplaced(self, p: QPainter) -> None:
        """A line "3 creatures: position unknown": replicated to us (the server thinks they are relevant to you, so they are not far) but
        never seen moving and without a creation record: the map cannot say where they are. Better told than left out."""
        n = self.peer_unplaced
        if not n:
            return
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(QColor(255, 170, 90))
        r = self._unplaced_rect()
        # a creature the app never saw created has no position until the server sends its creation record again: a rejoin does
        what = f"{n} creature{'s' if n != 1 else ''}: " + ("no position - rejoin" if "rejoin" in self.peer_status.lower() else "position unknown")
        text = QFontMetricsF(p.font()).elidedText(what, Qt.TextElideMode.ElideRight, r.width())
        p.drawText(r, Qt.AlignmentFlag.AlignHCenter, text)

    def _draw_record_status(self, p: QPainter) -> None:
        if not self.record_status:
            return
        p.setFont(QFont("Segoe UI", 8))
        bad = "FAILED" in self.record_status or "STOPPED" in self.record_status
        p.setPen(QColor(255, 170, 90) if bad else QColor(255, 90, 90))
        room = self.width() * (0.9 if self.full_map else 0.62)
        text = QFontMetricsF(p.font()).elidedText(self.record_status, Qt.TextElideMode.ElideRight, room)
        p.drawText(QRectF(0, (6 if self.full_map else 18) + 14, self.width(), 16), Qt.AlignmentFlag.AlignHCenter, text)

    def _draw_player(self, p: QPainter) -> None:
        if not self.interp.has_fix:
            p.setPen(QColor(255, 255, 255, 220))
            p.setFont(QFont("Segoe UI", 10))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, f"Waiting for game…\n({self.pipeline.status_text()})")
            return
        c = self._player_screen()
        p.save()
        p.translate(c)
        p.rotate(self.interp.heading)
        st = ENTITY_STYLE["self"]
        stale = own_marker_stale(self.own_age_s)
        # no move for a few seconds (dead, loading, teleported): the arrow is where the last move put it, so it is drawn hollow
        # and dim, with a "?" - never as an exact position
        p.setPen(QPen(_qcolor(st.outline, 120 if stale else 255), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush if stale else _qcolor(st.color))
        p.drawPolygon(_OWN_ARROW)
        p.restore()
        if stale:
            p.setPen(QColor(255, 170, 90))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(QPointF(c.x() + 11, c.y() + 14), own_stale_text(self.own_age_s))

    def _draw_debug_positions(self, p: QPainter) -> None:
        """Off by default (tray: "Show exact positions", or ISLE_DEBUG_POSITIONS=1). A crosshair at the exact newest decoded
        position of every creature (yellow) and of you (magenta), and a thin line to the centre of the arrow that is drawn:
        the line is the distance the smoothing and dead reckoning put between the two (zero for a creature at rest)."""
        if not self.debug_positions or not self.interp.has_fix:
            return
        p.setBrush(Qt.BrushStyle.NoBrush)

        def cross(a: QPointF, b: QPointF, color: QColor) -> None:
            p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 130), 0.8))
            p.drawLine(a, b)
            p.setPen(QPen(color, 1.0))
            p.drawLine(QPointF(a.x() - 4.0, a.y()), QPointF(a.x() + 4.0, a.y()))
            p.drawLine(QPointF(a.x(), a.y() - 4.0), QPointF(a.x(), a.y() + 4.0))

        for n in self.nearby(None):
            e = self.ents.entities.get(n.entity.id)
            if e is not None:
                cross(self.world_to_screen(e.x, e.y), self.world_to_screen(n.x, n.y), QColor(255, 235, 60))
        pos = self.pipeline.latest.position
        if pos is not None:
            cross(self.world_to_screen(pos.x, pos.y), self._player_screen(), QColor(255, 60, 220))

    def _draw_border(self, p: QPainter) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 160, 0, 230) if self.edit_mode else QColor(255, 255, 255, 120), 2))
        if self.full_map:
            p.drawRect(self.rect().adjusted(1, 1, -1, -1))
        else:
            p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))

    # ---- mouse (edit mode only; otherwise the window is click-through) -------
    def mousePressEvent(self, e) -> None:  # noqa: N802
        if not self.edit_mode:
            return
        if e.button() == Qt.MouseButton.RightButton:
            self.request_marker_at(self.from_screen(e.position()))
        elif e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is None:
            return
        delta = e.globalPosition() - self._drag
        self._drag = e.globalPosition()
        if self.full_map:
            _, _, span = self.view()
            px = self.width() / span
            self.pan = QPointF(self.pan.x() - delta.x() / px, self.pan.y() - delta.y() / (px * MAP_HEIGHT / MAP_WIDTH))
        else:
            self.move(self.x() + round(delta.x()), self.y() + round(delta.y()))

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        self._drag = None
        self.settings.window_x, self.settings.window_y = self.x(), self.y()

    def wheelEvent(self, e) -> None:  # noqa: N802
        if not self.edit_mode:
            return
        step = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_size_percent(round(self.settings.size_percent * step))
        else:
            self.zoom_by(step)

    # hook set by the app: called with world (x, y) of a right-click
    def request_marker_at_world(self, x: float, y: float) -> None:  # replaced by app
        pass

    def request_marker_at(self, norm: tuple[float, float]) -> None:
        wx, wy = unproject(*norm)
        self.request_marker_at_world(wx, wy)

    def player_world(self) -> tuple[float, float] | None:
        return (self.interp.x, self.interp.y) if self.interp.has_fix else None
