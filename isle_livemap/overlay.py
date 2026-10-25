"""PySide6 overlay: always-on-top, click-through, 60 fps, player-centred."""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QWidget

from .capture import TelemetryPipeline
from .geo import (
    ASSET_DIR, MAP_HEIGHT, MAP_WIDTH, UNITS_PER_METER, WORLD_SCALE, X_OFFSET, Y_OFFSET, Interpolator, MarkerStore, Settings, build_rings, clamp_size_percent,
    compass_label, format_distance, load_zones, project_unclamped, unproject,
    ENTITY_STYLE, PEER_ARROW_SCALE, STALE_ALPHA, EntitySmoother, Nearby, edge_anchor, local_entity, peer_label_lines,
    PEER_STALE_S, PEER_TTL_S, LABEL_RANK, SLOT_SLACK_PX, label_limits, place_label, vital_lines, visible_entities,
)
from .peers import status_is_warning
from .tracking import Vitals

BASE_SIZE = 360  # minimap edge in px at 100 %
BASE_VIEW_FRACTION = 0.2  # minimap shows 20 % of map width at zoom 1
KIND_COLORS = {"waypoint": "#ffd34d", "nest": "#ff9a52", "water": "#4db8ff", "dinosaur": "#ff6b6b", "other": "#c8c8c8"}
KIND_GLYPH = {"waypoint": "◆", "nest": "⌂", "water": "≈", "dinosaur": "☠", "other": "●"}
EDGE_INSET_PX = 7  # edge indicators sit this far inside the minimap circle
DOT_CLEARANCE_PX = 7.0  # no label may cover the dot of a player or dinosaur
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
        self._text_widths: dict[str, float] = {}  # label font is constant, so widths can persist across frames
        self._label_slot: dict[str, int] = {}  # peer id -> label slot used last frame: tried first, so labels do not hop
        self._labelled: set[str] = set()  # peers that had a label last frame: they keep it over equally near newcomers
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
        near = self.nearby(None if self.full_map else self.settings.peer_radius_m)
        if not near:
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
        if off:
            self._draw_edge_indicators(p, off)
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
        labelled = attempts = ai_labelled = 0
        slots_now: dict[str, int] = {}
        widths = self._text_widths  # text measuring is the slow part of the layer: measure each string once
        if len(widths) > 2000:
            widths.clear()
        for n, pt in reversed(by_priority):  # players on top, far before near
            self._draw_entity_marker(p, n, pt)
        # dots of players and dinosaurs that can compete with a label: kept clear when possible (a preference, so two
        # creatures standing next to each other still both get a label)
        dots = [(pt.x() - DOT_CLEARANCE_PX, pt.y() - DOT_CLEARANCE_PX, 2 * DOT_CLEARANCE_PX, 2 * DOT_CLEARANCE_PX)
                for n, pt in by_priority[:max_labels * 2] if n.entity.kind != "ai"]
        pos = self.pipeline.latest.position
        my_z = pos.z if pos is not None else None
        # players and dinosaurs claim label space before server animals (fish) do, each group nearest first
        for n, pt in by_priority:
            if labelled >= max_labels or attempts >= max_labels * LABEL_ATTEMPTS_PER_LABEL:
                break
            is_ai = n.entity.kind == "ai"
            if is_ai and ai_labelled >= max_ai_labels:
                continue
            attempts += 1
            lines = peer_label_lines(n, my_z, self.settings.show_vitals_detail)
            w = max(widths[t] if t in widths else widths.setdefault(t, fm.horizontalAdvance(t)) for t in lines) + 2
            h = fm.height() * len(lines)
            eid = n.entity.id
            prefer, slot = self._label_slot.get(eid), [0]
            r = place_label(pt.x(), pt.y(), w, h, placed + dots, bounds, in_circle, prefer, slot, in_circle_loose)
            if r is not None:
                placed.append(r)
            elif not is_ai:
                r = place_label(pt.x(), pt.y(), w, h, placed, bounds, in_circle, prefer, slot, in_circle_loose)
            if r is None:
                continue
            slots_now[eid] = slot[0]
            labelled += 1
            ai_labelled += is_ai
            alpha = STALE_ALPHA if n.stale else 255
            color = _qcolor(ENTITY_STYLE.get(n.entity.kind, ENTITY_STYLE["player"]).color, alpha)
            for i, text in enumerate(lines):
                base = QPointF(r[0], r[1] + fm.ascent() + i * fm.height())
                p.setPen(QColor(0, 0, 0, min(220, alpha)))
                p.drawText(base + QPointF(1, 1), text)
                p.setPen(color if i == 0 else QColor(255, 255, 255, alpha))
                p.drawText(base, text)
        self._label_slot = slots_now
        self._labelled = set(slots_now)

    def _draw_entity_marker(self, p: QPainter, n: Nearby, pt: QPointF) -> None:
        """Peer glyph: arrow when a heading is known, else a dot (diamond for ai); dim when stale.
        Smaller and cyan, so it never looks like the red local arrow."""
        st = ENTITY_STYLE.get(n.entity.kind, ENTITY_STYLE["player"])
        key = (n.entity.kind, n.stale)
        pb = self._marker_tools.get(key)
        if pb is None:  # one pen/brush per (kind, stale): creating them per marker dominated the layer
            alpha = STALE_ALPHA if n.stale else 255
            pb = self._marker_tools[key] = (QPen(_qcolor(st.outline, min(230, alpha)), 1.5), _qcolor(st.color, alpha))
        p.setPen(pb[0])
        p.setBrush(pb[1])
        if n.entity.heading is not None:
            p.save()
            p.translate(pt)
            p.rotate(n.entity.heading)
            k = PEER_ARROW_SCALE
            p.drawPolygon(QPolygonF([QPointF(0, -10 * k), QPointF(7 * k, 8 * k), QPointF(0, 4 * k), QPointF(-7 * k, 8 * k)]))
            p.restore()
        elif st.glyph == "diamond":
            r = st.radius + 1
            p.drawPolygon(QPolygonF([pt + QPointF(0, -r), pt + QPointF(r, 0), pt + QPointF(0, r), pt + QPointF(-r, 0)]))
        else:
            p.drawEllipse(pt, st.radius, st.radius)

    def _draw_edge_indicators(self, p: QPainter, offscreen: list[tuple[Nearby, QPointF]]) -> None:
        """In-radius players outside the minimap circle: a small pointer on the circle edge."""
        if not offscreen:
            return
        cx, cy = self.width() / 2, self.height() / 2
        r = self.width() / 2 - EDGE_INSET_PX
        for n, pt in reversed(offscreen):
            a = edge_anchor(cx, cy, pt.x(), pt.y(), r)
            if a is None:
                continue
            st = ENTITY_STYLE.get(n.entity.kind, ENTITY_STYLE["player"])
            alpha = STALE_ALPHA if n.stale else 255
            p.save()
            p.translate(QPointF(*a))
            p.rotate(math.degrees(math.atan2(pt.x() - cx, -(pt.y() - cy))))  # points outward
            p.setPen(QPen(_qcolor(st.outline, min(230, alpha)), 1.2))
            p.setBrush(_qcolor(st.color, alpha))
            p.drawPolygon(QPolygonF([QPointF(0, -5), QPointF(4, 3), QPointF(-4, 3)]))
            p.restore()

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
