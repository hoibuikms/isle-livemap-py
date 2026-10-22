"""PySide6 overlay: always-on-top, click-through, 60 fps, player-centred."""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QWidget

from .capture import TelemetryPipeline
from .geo import (
    ASSET_DIR, MAP_HEIGHT, MAP_WIDTH, Interpolator, MarkerStore, Settings, build_rings, clamp_size_percent,
    compass_label, format_distance, load_zones, project_unclamped, unproject,
    EntitySmoother, Nearby, entity_label_lines, nearby_entities, place_label,
)
from .tracking import Vitals

BASE_SIZE = 360  # minimap edge in px at 100 %
BASE_VIEW_FRACTION = 0.2  # minimap shows 20 % of map width at zoom 1
KIND_COLORS = {"waypoint": "#ffd34d", "nest": "#ff9a52", "water": "#4db8ff", "dinosaur": "#ff6b6b", "other": "#c8c8c8"}
KIND_GLYPH = {"waypoint": "◆", "nest": "⌂", "water": "≈", "dinosaur": "☠", "other": "●"}
STALE_AFTER_S = 3.0
PLAYER_COLOR, AI_COLOR = QColor("#4dd0ff"), QColor("#ff8a3d")
MAX_LABELS_MINIMAP, MAX_LABELS_FULL = 8, 24
CROWDED_ON_SCREEN = 20  # above this only the nearest few get labels
CROWDED_LABELS = 4


class MapView(QWidget):
    def __init__(self, pipeline: TelemetryPipeline, store: MarkerStore, settings: Settings, feed=None) -> None:
        super().__init__()
        self.pipeline, self.store, self.settings = pipeline, store, settings
        self.feed = feed  # FeedPoller / DemoFeed or None; only source of other entities
        self.ents = EntitySmoother()
        self._feed_version = -1
        self.feed_status = ""
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

    def set_size_percent(self, pct: int) -> None:
        self.settings.size_percent = clamp_size_percent(pct)
        self.apply_size()

    # ---- frame ------------------------------------------------------------
    def tick(self) -> None:
        now = time.monotonic()
        dt, self._last = now - self._last, now
        t = self.pipeline.latest
        if t.position is not None and t.position_at != self._last_pos_at:
            self._last_pos_at = t.position_at
            self.interp.set_target(t.position.x, t.position.y, t.position.map_heading_degrees)
        self.interp.step(dt)
        if self.feed is not None:
            snap = self.feed.snapshot()
            self.feed_status = snap.status if snap.enabled else ""
            if snap.version != self._feed_version:
                self._feed_version = snap.version
                self.ents.update(snap.entities)
        self.ents.step(dt)
        self.update()

    def nearby(self, radius_m: float | None = ...) -> list[Nearby]:
        """Entities relative to the local player, nearest first. Default radius is
        the configured one; pass None for unlimited."""
        if not self.interp.has_fix:
            return []
        if radius_m is ...:
            radius_m = self.settings.feed_radius_m
        return nearby_entities(self.ents.items(), self.interp.x, self.interp.y, radius_m,
                               self.settings.show_players, self.settings.show_ai)

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
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect())
        if not self.full_map:
            p.setClipRegion(self._circle_region())
        p.fillRect(r, QColor(10, 14, 18, 150 if self.full_map else 90))
        self._draw_map(p)
        self._draw_zones(p)
        self._draw_rings(p)
        self._draw_markers(p)
        self._draw_entities(p)
        self._draw_player(p)
        p.setClipping(False)
        self._draw_border(p)
        self._draw_hud(p)
        self._draw_feed_status(p)
        p.end()

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
        # Minimap: only within the configured radius. Full map: everything the feed sent.
        near = self.nearby(None if self.full_map else self.settings.feed_radius_m)
        if not near:
            return
        bounds = (0.0, 0.0, float(self.width()), float(self.height()))
        pts = [(n, self.to_screen(*project_unclamped(n.x, n.y))) for n in near]
        pts = [(n, pt) for n, pt in pts if -10 <= pt.x() <= bounds[2] + 10 and -10 <= pt.y() <= bounds[3] + 10]
        max_labels = MAX_LABELS_FULL if self.full_map else MAX_LABELS_MINIMAP
        if len(pts) > CROWDED_ON_SCREEN:
            max_labels = min(max_labels, CROWDED_LABELS)
        font = QFont("Segoe UI", 8)
        fm = QFontMetricsF(font)
        p.setFont(font)
        placed: list[tuple[float, float, float, float]] = []
        labelled = 0
        for n, pt in reversed(pts):  # far first so near dots end up on top
            self._draw_entity_dot(p, n, pt)
        for n, pt in pts:  # near first so near labels win placement
            if labelled >= max_labels:
                break
            lines = entity_label_lines(n)
            w = max(fm.horizontalAdvance(t) for t in lines) + 2
            h = fm.height() * 2
            r = place_label(pt.x(), pt.y(), w, h, placed, bounds)
            if r is None:
                continue
            labelled += 1
            color = PLAYER_COLOR if n.entity.kind == "player" else AI_COLOR
            for i, text in enumerate(lines):
                base = QPointF(r[0], r[1] + fm.ascent() + i * fm.height())
                p.setPen(QColor(0, 0, 0, 220))
                p.drawText(base + QPointF(1, 1), text)
                p.setPen(color if i == 0 else QColor(255, 255, 255))
                p.drawText(base, text)

    def _draw_entity_dot(self, p: QPainter, n: Nearby, pt: QPointF) -> None:
        p.setPen(QPen(QColor(0, 0, 0, 230), 1.5))
        if n.entity.kind == "player":  # circle
            p.setBrush(PLAYER_COLOR)
            p.drawEllipse(pt, 5, 5)
        else:  # diamond
            p.setBrush(AI_COLOR)
            p.drawPolygon(QPolygonF([pt + QPointF(0, -6), pt + QPointF(6, 0), pt + QPointF(0, 6), pt + QPointF(-6, 0)]))

    def _draw_feed_status(self, p: QPainter) -> None:
        if not self.feed_status:
            return
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(QColor(255, 255, 255, 190) if "ok" in self.feed_status or "demo" in self.feed_status else QColor(255, 170, 90))
        p.drawText(QRectF(0, 6 if self.full_map else 18, self.width(), 16), Qt.AlignmentFlag.AlignHCenter, self.feed_status)

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
        p.setPen(QPen(QColor(0, 0, 0), 1.5))
        p.setBrush(QColor(255, 64, 64))
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
        if v is not None:
            for label, cur, mx in (
                ("Growth", v.growth, 1.0), ("Health", v.health, v.max_health), ("Stamina", v.stamina, v.max_stamina),
                ("Hunger", v.hunger, v.max_hunger), ("Water", v.thirst, v.max_thirst),
            ):
                f = Vitals.fraction(cur, mx)
                if f is not None:
                    lines.append(f"{label} {f * 100:.0f}%")
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
