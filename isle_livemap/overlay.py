"""PySide6 overlay: always-on-top, click-through, 60 fps, player-centred."""

from __future__ import annotations

import math
import time
from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QWidget

from .capture import TelemetryPipeline
from .confidence import SPECIES_INFERRED, SPECIES_UNKNOWN, LabelConfidence, is_placeholder_name, species_tier
from .geo import (
    ASSET_DIR, MAP_HEIGHT, MAP_WIDTH, UNITS_PER_METER, WORLD_SCALE, X_OFFSET, Y_OFFSET, Interpolator, MarkerStore, Settings, build_rings, clamp_size_percent,
    compass_label, format_distance, load_zones, project_unclamped, unproject,
    ENTITY_STYLE, PEER_ARROW_SCALE, STALE_ALPHA, EntitySmoother, Nearby, edge_anchor, local_entity, peer_label_lines,
    PEER_STALE_S, PEER_TTL_S, LABEL_RANK, SLOT_SLACK_PX, label_limits, place_label, vital_lines, visible_entities,
    marker_extent, marker_radius, peer_style, truncate, _overlaps,
)
from .peers import status_is_warning
from .tracking import Vitals

BASE_SIZE = 360  # minimap edge in px at 100 %
BASE_VIEW_FRACTION = 0.2  # minimap shows 20 % of map width at zoom 1
KIND_COLORS = {"waypoint": "#ffd34d", "nest": "#ff9a52", "water": "#4db8ff", "dinosaur": "#ff6b6b", "other": "#c8c8c8"}
KIND_GLYPH = {"waypoint": "◆", "nest": "⌂", "water": "≈", "dinosaur": "☠", "other": "●"}
EDGE_INSET_PX = 7  # edge indicators sit this far inside the minimap circle
DOT_CLEARANCE_PX = 7.0  # no label may cover the dot of a player or dinosaur
SOFT_INFERRED, SOFT_UNKNOWN = 0.75, 0.6  # label head opacity of a remembered / unknown species (confirmed: 1.0)
HALO = QColor(255, 255, 255, 70)  # soft light disc under a dinosaur dot: contrast on dark and light map areas
HALO_PLAYER = QColor(255, 255, 255, 105)
LABEL_BG, LABEL_BG_STALE = QColor(8, 12, 16, 125), QColor(8, 12, 16, 60)
BAR_TRACK, BAR_ST_FILL = QColor(0, 0, 0, 170), QColor(90, 170, 255)
BAR_HP_H, BAR_ST_H = 3.0, 2.0  # px
EDGE_RANGE_M = 2500.0  # players and dinosaurs up to this far are shown on the circle edge when beyond the view radius
EDGE_CAPTIONS = 6  # the nearest this many edge pointers also say who / what they are and how far
COMPACT_HOLD_S = 4.0  # a label that had to be shortened stays short this long (no flipping between one and two lines)
CLUSTER_PX = 12.0  # creatures of one species whose dots are closer than this share one label ("Carnotaurus ×5")
HALO_MAX = 40  # halos and bars for the most important this many peers (labels are limited in the same way)
CROWD_GLYPHS = 150  # full glyphs for the most important this many peers; the rest of a crowd are small squares
LABEL_ATTEMPTS_PER_LABEL = 3  # placement tries per allowed label, so a crowd cannot cost 500 text measurements


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
        self.record_status = ""  # set by App once a second while --record is active
        self._marker_tools: dict[tuple[str, bool], tuple[QPen, QColor]] = {}
        self._bar_colors: dict[int, QColor] = {}  # health bar fill by 5 % step (no QColor built per frame)
        self._pen_key = None  # (colour, stale, player) of the pen / brush currently set on the painter
        self._text_widths: dict[str, float] = {}  # label font is constant, so widths can persist across frames
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
        self.interp.step(dt, now)
        if self.peers is not None:
            try:
                snap = self.peers.snapshot()
            except Exception as err:  # noqa: BLE001 - keep drawing the last good snapshot
                self._note_error("peer snapshot", err)
                snap = self._peers_snap
            if snap is not None:
                self.peer_status = snap.status if snap.enabled else ""
                # the source decides how long silence is tolerated (game: deltas only, so idle actors go quiet)
                self.ents.ttl = getattr(self.peers, "ttl_s", PEER_TTL_S)
                self._stale_s = getattr(self.peers, "stale_s", PEER_STALE_S)
                # a source that removes creatures itself (the game: destruction records) needs no second, longer timer here
                self.ents.missing_grace_s = getattr(self.peers, "gone_grace_s", None)
                self.ents.ghost_grace_s = getattr(self.peers, "ghost_grace_s", None)
                self.ents.ghost_kinds = getattr(self.peers, "ghost_kinds", frozenset())
                self.ents.ghost_age_s = self._stale_s  # a ghost is drawn dimmed from its first frame
                self.ents.heading_only_when_moving = bool(getattr(self.peers, "heading_from_movement", False))
                # an unchanged store returns the same snapshot object; a refreshed last_seen is a new one
                if snap is not self._peers_snap or snap.version != self._peers_version:
                    self._peers_snap, self._peers_version = snap, snap.version
                    self.ents.update(snap.entities, now)
        else:
            self.peer_status = ""
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
        return visible_entities(self.ents.items_aged(now), self.interp.x, self.interp.y, radius_m,
                                self.settings.show_players, self.settings.show_ai, self.settings.peer_self_ids,
                                ttl_s=self.ents.ttl, stale_s=self._stale_s)

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
                          self._draw_player):
                self._paint_layer(layer, p)
            p.setClipping(False)
            for layer in (self._draw_border, self._draw_hud, self._draw_peer_status, self._draw_record_status):
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
        else:  # beyond the radius, players and dinosaurs are not dropped: they become pointers on the circle edge
            reach = self.nearby(max(radius, EDGE_RANGE_M))
            near = [n for n in reach if n.distance_m <= radius]
            far = [n for n in reach if n.distance_m > radius and n.entity.kind != "ai"]
        if not near and not far:
            self._label_slot, self._labelled = {}, set()
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
        groups = {kind: [] for kind in LABEL_RANK}  # on-screen peers by kind, nearest first inside each group
        other: list = []
        w, h_px = self.width(), self.height()
        for n in near:
            pt = QPointF(ox + n.x * sx, oy + n.y * sy)
            if lim is None:
                inside = -10 <= pt.x() <= w + 10 and -10 <= pt.y() <= h_px + 10
            else:
                inside = (pt.x() - half) ** 2 + (pt.y() - h_px / 2) ** 2 <= lim
            (groups.get(n.entity.kind, other) if inside else off).append((n, pt))
        far_pts = [(n, QPointF(ox + n.x * sx, oy + n.y * sy)) for n in far]
        if off or far_pts:
            self._draw_edge_indicators(p, off, far_pts)
        labelled_before = self._labelled
        if labelled_before:  # a peer that had a label keeps it over a newcomer at almost the same distance
            for items in groups.values():
                items.sort(key=lambda it: it[0].distance_m * (0.8 if it[0].entity.id in labelled_before else 1.0))
        by_priority = [it for kind in sorted(groups, key=LABEL_RANK.get) for it in groups[kind]] + other
        ai_on_screen = len(groups["ai"])
        max_labels, max_ai_labels = label_limits(len(by_priority) - ai_on_screen, ai_on_screen, self.full_map)
        font = QFont("Segoe UI", 8)
        fm = QFontMetricsF(font)
        p.setFont(font)
        centre, radius = QPointF(self.width() / 2, self.height() / 2), self.width() / 2 - 3

        def in_circle(r, extra: float = 0.0) -> bool:  # the minimap clips to its circle: a label must not run past the edge
            return self.full_map or all(math.hypot(x - centre.x(), y - centre.y()) <= radius + extra
                                        for x in (r[0], r[0] + r[2]) for y in (r[1], r[1] + r[3]))

        def in_circle_loose(r) -> bool:  # for the slot a label already has (see geo.SLOT_SLACK_PX)
            return in_circle(r, SLOT_SLACK_PX)

        placed: list[tuple[float, float, float, float]] = []  # label rectangles already taken; the status lines are too
        top = 6.0 if self.full_map else 18.0
        room = self.width() * (0.9 if self.full_map else 0.62)
        for shown, row in ((self.peer_status, 0), (self.record_status, 1)):
            if shown:
                placed.append(((self.width() - room) / 2, top + 14 * row, room, 16.0))
        self._draw_edge_captions(p, far_pts, placed)
        p.setFont(font)
        labelled = attempts = ai_labelled = 0
        slots_now: dict[str, int] = {}
        widths = self._text_widths  # text measuring is the slow part of the layer: measure each string once
        if len(widths) > 2000:
            widths.clear()
        total = len(by_priority)
        self._pen_key = None
        for i, (n, pt) in enumerate(reversed(by_priority)):  # players on top, far before near
            rank = total - i  # 1 = the most important peer
            if rank > CROWD_GLYPHS:
                self._draw_crowd_dot(p, n, pt)
            else:
                self._draw_entity_marker(p, n, pt, rank <= HALO_MAX)
        # dots of players and dinosaurs that can compete with a label: kept clear when possible (a preference, so two
        # creatures standing next to each other still both get a label)
        dots = []
        for n, pt in by_priority[:max_labels * 2]:
            if n.entity.kind != "ai":
                c = max(DOT_CLEARANCE_PX, marker_extent(n.entity) + 3.0)  # a big dino's dot is wider than the default
                dots.append((pt.x() - c, pt.y() - c, 2 * c, 2 * c))
        pos = self.pipeline.latest.position
        my_z = pos.z if pos is not None else None
        now = time.monotonic()
        # creatures of one species standing together (a pack) share the label of the nearest one: "Carnotaurus ×5". Only
        # the creatures that can compete for a label are looked at, so a crowd of 500 costs nothing here.
        skip: set[str] = set()
        group_of: dict[str, int] = {}
        leaders: list[tuple[Nearby, QPointF]] = []
        for n, pt in by_priority[:max_labels * 6]:
            e = n.entity
            if e.kind not in ("dino", "unknown") or not e.species:
                continue
            for ln, lpt in leaders:
                if ln.entity.species == e.species and (lpt.x() - pt.x()) ** 2 + (lpt.y() - pt.y()) ** 2 < CLUSTER_PX ** 2:
                    group_of[ln.entity.id] += 1
                    skip.add(e.id)
                    break
            else:
                leaders.append((n, pt))
                group_of[e.id] = 1
        # players and dinosaurs claim label space before server animals (fish) do, each group nearest first
        for n, pt in by_priority:
            if labelled >= max_labels or attempts >= max_labels * LABEL_ATTEMPTS_PER_LABEL:
                break
            if n.entity.id in skip:
                continue
            is_ai = n.entity.kind == "ai"
            if is_ai and ai_labelled >= max_ai_labels:
                continue
            attempts += 1
            e = n.entity
            sp, inferred, hp, st, vtier = self._conf.settle(n.entity.id, e.species, e.inferred, e.health_pct, e.stamina_pct,
                                                            n.vitals_age_s, now)
            if (sp, inferred, hp, st) != (e.species, e.inferred, e.health_pct, e.stamina_pct):
                n = replace(n, entity=replace(e, species=sp, inferred=inferred, health_pct=hp, stamina_pct=st))
            lines = peer_label_lines(n, my_z, self.settings.show_vitals_detail, vtier, group_of.get(n.entity.id, 1))
            if len(lines) > 1 and now < self._compact_until.get(n.entity.id, 0.0):
                lines = lines[:1]  # it did not fit in full a moment ago: stay short for a few seconds instead of flipping size
            w = max(widths[t] if t in widths else widths.setdefault(t, fm.horizontalAdvance(t)) for t in lines) + 2
            h = fm.height() * len(lines)
            eid = n.entity.id
            prefer, slot = self._label_slot.get(eid), [0]
            r = place_label(pt.x(), pt.y(), w, h, placed + dots, bounds, in_circle, prefer, slot, in_circle_loose)
            if r is None and not is_ai:
                r = place_label(pt.x(), pt.y(), w, h, placed, bounds, in_circle, prefer, slot, in_circle_loose)
            if r is None and not is_ai and len(lines) > 1:  # no room for two lines: at least say who / what it is
                lines = lines[:1]
                self._compact_until[eid] = now + COMPACT_HOLD_S
                w, h = widths[lines[0]] + 2 if lines[0] in widths else fm.horizontalAdvance(lines[0]) + 2, fm.height()
                r = place_label(pt.x(), pt.y(), w, h, placed + dots, bounds, in_circle, prefer, slot, in_circle_loose)
                if r is None:
                    r = place_label(pt.x(), pt.y(), w, h, placed, bounds, in_circle, prefer, slot, in_circle_loose)
            if r is None:
                continue
            placed.append(r)
            slots_now[eid] = slot[0]
            labelled += 1
            ai_labelled += is_ai
            alpha = STALE_ALPHA if n.stale else 255
            tier = species_tier(sp, inferred)  # a species that is only remembered / unknown is drawn softer than a confirmed one
            head_alpha = int(alpha * (SOFT_INFERRED if tier == SPECIES_INFERRED else SOFT_UNKNOWN if tier == SPECIES_UNKNOWN else 1.0))
            color = _qcolor(peer_style(n.entity).color, head_alpha)
            p.setPen(Qt.PenStyle.NoPen)  # a soft dark pill behind the text: readable on forest, rock and water alike
            p.setBrush(LABEL_BG if alpha == 255 else LABEL_BG_STALE)
            p.drawRoundedRect(QRectF(r[0] - 2.0, r[1] - 0.5, r[2] + 2.0, r[3] + 1.0), 3.0, 3.0)
            for i, text in enumerate(lines):
                base = QPointF(r[0], r[1] + fm.ascent() + i * fm.height())
                a = head_alpha if i == 0 else alpha
                p.setPen(QColor(0, 0, 0, min(220, a)))
                p.drawText(base + QPointF(1, 1), text)
                p.setPen(color if i == 0 else QColor(255, 255, 255, alpha))
                p.drawText(base, text)
        self._label_slot = slots_now
        self._labelled = set(slots_now)
        if len(self._compact_until) > 400:
            self._compact_until = {k: v for k, v in self._compact_until.items() if v > now}
        self._grouped = skip  # creatures whose dot is covered by the "×N" label of a neighbour of the same species
        if self.settings.show_vitals_bars:  # the label prints HP / ST; a bar is the same value for a creature without a label
            for n, pt in by_priority[:HALO_MAX]:
                if n.entity.id not in slots_now and n.entity.id not in skip and self._has_bars(n):
                    rad = marker_radius(n.entity)
                    bw = max(14.0, 2.0 * rad + 6.0)
                    box = (pt.x() - bw / 2, pt.y() + rad + 3.0, bw, BAR_HP_H + (BAR_ST_H + 1.5 if n.entity.stamina_pct is not None else 0.0))
                    if not any(_overlaps(box, q) for q in placed):  # never on top of somebody's label
                        self._draw_bars(p, n, pt, rad)
        self._conf.keep_only({n.entity.id for n in near})

    def _has_bars(self, n: Nearby) -> bool:
        e = n.entity
        return bool(self.settings.show_vitals_bars and e.kind != "ai" and e.health_pct is not None and not n.stale)

    def _bar_fill(self, pct: float) -> QColor:
        step = int(max(0.0, min(100.0, pct)) // 5)
        c = self._bar_colors.get(step)
        if c is None:  # red -> amber -> green with the health left
            f = step / 20.0
            c = self._bar_colors[step] = QColor(int(235 * min(1.0, 2.0 * (1.0 - f)) + 20), int(215 * min(1.0, 2.0 * f) + 25), 60)
        return c

    def _draw_bars(self, p: QPainter, n: Nearby, pt: QPointF, rad: float) -> None:
        """Health (and stamina when known) as thin bars under the dot: readable at a glance, no text to read."""
        e = n.entity
        w = max(14.0, 2.0 * rad + 6.0)
        x, y = pt.x() - w / 2, pt.y() + rad + 3.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(BAR_TRACK)
        p.drawRect(QRectF(x - 0.5, y - 0.5, w + 1.0, BAR_HP_H + 1.0))
        p.setBrush(self._bar_fill(e.health_pct))
        p.drawRect(QRectF(x, y, w * max(0.0, min(100.0, e.health_pct)) / 100.0, BAR_HP_H))
        if e.stamina_pct is not None:
            y2 = y + BAR_HP_H + 1.5
            p.setBrush(BAR_TRACK)
            p.drawRect(QRectF(x - 0.5, y2 - 0.5, w + 1.0, BAR_ST_H + 1.0))
            p.setBrush(BAR_ST_FILL)
            p.drawRect(QRectF(x, y2, w * max(0.0, min(100.0, e.stamina_pct)) / 100.0, BAR_ST_H))

    def _draw_crowd_dot(self, p: QPainter, n: Nearby, pt: QPointF) -> None:
        """A peer beyond the first CROWD_GLYPHS: a plain square in its colour (500 anti-aliased glyphs do not fit a frame)."""
        key = ("crowd", peer_style(n.entity).color, n.stale)
        if self._pen_key != key:
            c = _qcolor(peer_style(n.entity).color, STALE_ALPHA if n.stale else 255)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            self._pen_key = key
        p.drawRect(QRectF(pt.x() - 1.8, pt.y() - 1.8, 3.6, 3.6))

    def _draw_entity_marker(self, p: QPainter, n: Nearby, pt: QPointF, decorate: bool = True) -> None:
        """Peer glyph: a dot with a soft halo (so it reads on any map colour); an arrow only while the creature moves;
        a diamond for server animals. Players have a white ring, dinosaurs are coloured by diet. Dim when stale."""
        e = n.entity
        st = peer_style(e)
        player = e.kind == "player"
        key = (st.color, n.stale, player)
        pb = self._marker_tools.get(key)
        if pb is None:  # one pen/brush per (colour, stale, player): creating them per marker dominated the layer
            alpha = STALE_ALPHA if n.stale else 255
            ring = QPen(QColor(255, 255, 255, min(235, alpha)), 1.6) if player else QPen(_qcolor(st.outline, min(230, alpha)), 1.5)
            pb = self._marker_tools[key] = (ring, _qcolor(st.color, alpha))
        rad = marker_radius(e)  # species size class (fish: fixed small), see geo.marker_radius
        if decorate and e.kind != "ai":  # the halo is for the creatures that matter; a crowd of 500 does not get 500 discs
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(HALO_PLAYER if player else HALO)
            p.drawEllipse(pt, rad + 2.6, rad + 2.6)
            self._pen_key = None
        if self._pen_key != key:  # a run of equal markers (a crowd) sets pen and brush once
            p.setPen(pb[0])
            p.setBrush(pb[1])
            self._pen_key = key
        if e.heading is not None:  # the source only gives a heading while the creature is moving (EntitySmoother.is_moving)
            p.save()
            p.translate(pt)
            p.rotate(e.heading)
            k = PEER_ARROW_SCALE * rad / 4.0
            p.drawPolygon(QPolygonF([QPointF(0, -10 * k), QPointF(7 * k, 8 * k), QPointF(0, 4 * k), QPointF(-7 * k, 8 * k)]))
            p.restore()
        elif st.glyph == "diamond":
            r = rad + 1
            p.drawPolygon(QPolygonF([pt + QPointF(0, -r), pt + QPointF(r, 0), pt + QPointF(0, r), pt + QPointF(-r, 0)]))
        else:
            p.drawEllipse(pt, rad, rad)

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
        p.setFont(QFont("Segoe UI", 7))
        fm = QFontMetricsF(p.font())
        for n, pt in far[:EDGE_CAPTIONS]:
            a = edge_anchor(cx, cy, pt.x(), pt.y(), r)
            if a is None:
                continue
            e = n.entity
            who = e.name if e.name and not is_placeholder_name(e.name) else (e.species or "?")
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
            p.setBrush(LABEL_BG)
            p.drawRoundedRect(QRectF(box[0] - 2, box[1], w + 2, h), 3.0, 3.0)
            p.setPen(_qcolor(peer_style(e).color, STALE_ALPHA if n.stale else 235))
            p.drawText(QPointF(box[0], box[1] + fm.ascent()), text)

    def _draw_peer_status(self, p: QPainter) -> None:
        if not self.peer_status:
            return
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(QColor(255, 170, 90) if status_is_warning(self.peer_status) else QColor(255, 255, 255, 190))
        room = self.width() * (0.9 if self.full_map else 0.62)  # inside the circle chord at that height
        text = QFontMetricsF(p.font()).elidedText(self.peer_status, Qt.TextElideMode.ElideRight, room)
        p.drawText(QRectF(0, 6 if self.full_map else 18, self.width(), 16), Qt.AlignmentFlag.AlignHCenter, text)

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
        p.setPen(QPen(_qcolor(st.outline), 1.5))
        p.setBrush(_qcolor(st.color))
        p.drawPolygon(QPolygonF([QPointF(0, -10), QPointF(7, 8), QPointF(0, 4), QPointF(-7, 8)]))
        p.restore()

    def _draw_border(self, p: QPainter) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 160, 0, 230) if self.edit_mode else QColor(255, 255, 255, 120), 2))
        if self.full_map:
            p.drawRect(self.rect().adjusted(1, 1, -1, -1))
        else:
            p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))

    def _draw_hud(self, p: QPainter) -> None:
        v: Vitals | None = self.pipeline.latest.vitals
        lines: list[str] = []
        name = self.settings.species.strip()
        if name:
            lines.append(name.upper())
        lines += vital_lines(local_entity(self.pipeline.latest.position, v))
        if not lines:
            return
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        h = 16 * len(lines) + 8
        box = QRectF(6, self.height() - h - 6, 120, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(box, 5, 5)
        p.setPen(QColor(255, 255, 255))
        for i, text in enumerate(lines):
            p.drawText(QPointF(box.x() + 8, box.y() + 18 + i * 16), text)

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
