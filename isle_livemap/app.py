"""Application wiring: tray menu, dialogs, hotkeys, data sources."""

from __future__ import annotations

import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPixmap, QShortcut, QColor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QSystemTrayIcon, QVBoxLayout,
)

from .capture import TelemetryPipeline
from .feed import insecure_warning, validate_url
from .geo import (
    MARKER_KINDS, SIZE_MAX, SIZE_MIN, MarkerStore, Settings, entity_summary, format_distance, parse_radii,
)
from .overlay import MapView

# (name, Windows modifiers, virtual key) -- modifiers: ALT=1 CTRL=2 SHIFT=4, NOREPEAT=0x4000
HOTKEYS = {
    "full_map": ("Alt+M", 0x1, ord("M")),
    "edit": ("Ctrl+Shift+O", 0x2 | 0x4, ord("O")),
    "add_marker": ("Ctrl+Shift+P", 0x2 | 0x4, ord("P")),
    "markers": ("Ctrl+Shift+L", 0x2 | 0x4, ord("L")),
    "nearby": ("Ctrl+Shift+N", 0x2 | 0x4, ord("N")),
    "zoom_in": ("Ctrl+Shift+=", 0x2 | 0x4, 0xBB),
    "zoom_out": ("Ctrl+Shift+-", 0x2 | 0x4, 0xBD),
}


class _HotkeyFilter(QAbstractNativeEventFilter):
    """Windows RegisterHotKey (a documented OS facility; not a keyboard hook)."""

    WM_HOTKEY = 0x0312

    def __init__(self, handlers: dict[int, callable]) -> None:
        super().__init__()
        self.handlers = handlers

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        if event_type == b"windows_generic_MSG" or str(event_type) == "windows_generic_MSG":
            import ctypes
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == self.WM_HOTKEY and msg.wParam in self.handlers:
                self.handlers[msg.wParam]()
                return True, 0
        return False, 0


class MarkerDialog(QDialog):
    def __init__(self, parent, title: str = "New marker") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.name, self.notes, self.kind = QLineEdit(), QLineEdit(), QComboBox()
        self.kind.addItems(MARKER_KINDS)
        form = QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("Type", self.kind)
        form.addRow("Notes", self.notes)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class MarkersPanel(QDialog):
    def __init__(self, view: MapView, store: MarkerStore) -> None:
        super().__init__()
        self.view, self.store = view, store
        self.setWindowTitle("Markers")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.list = QListWidget()
        btn = QPushButton("Delete selected")
        btn.clicked.connect(self.delete_selected)
        lay = QVBoxLayout(self)
        lay.addWidget(self.list)
        lay.addWidget(btn)
        self.resize(360, 320)

    def refresh(self) -> None:
        self.list.clear()
        pos = self.view.player_world()
        for m in self.store.markers:
            text = f"[{m.kind}] {m.describe(*pos)}" if pos else f"[{m.kind}] {m.name}"
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole, m.id)
            self.list.addItem(it)

    def showEvent(self, e) -> None:  # noqa: N802
        self.refresh()
        super().showEvent(e)

    def delete_selected(self) -> None:
        for it in self.list.selectedItems():
            self.store.remove(it.data(Qt.ItemDataRole.UserRole))
        self.refresh()


class NearbyPanel(QDialog):
    """Players / AI from the server feed, nearest first (refreshes while visible)."""

    def __init__(self, view: MapView) -> None:
        super().__init__()
        self.view = view
        self.setWindowTitle("Nearby (server feed)")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.status = QLabel()
        self.list = QListWidget()
        lay = QVBoxLayout(self)
        lay.addWidget(self.status)
        lay.addWidget(self.list)
        self.resize(520, 340)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)

    def refresh(self) -> None:
        self.status.setText(self.view.feed_status or "feed: off (set a feed URL in Feed settings)")
        pos = self.view.pipeline.latest.position
        my_z = pos.z if pos is not None else None
        rows = [entity_summary(n, my_z) for n in self.view.nearby()]
        if [self.list.item(i).text() for i in range(self.list.count())] != rows:
            self.list.clear()
            self.list.addItems(rows)

    def showEvent(self, e) -> None:  # noqa: N802
        self.refresh()
        self.timer.start(500)
        super().showEvent(e)

    def hideEvent(self, e) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(e)


class FeedDialog(QDialog):
    """URL / token / radius of the user's own server feed."""

    def __init__(self, parent, settings: Settings, current_url: str, token_set: bool) -> None:
        super().__init__(parent)
        self.setWindowTitle("Server feed settings")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.url = QLineEdit(current_url)
        self.url.setPlaceholderText("https://my-server.example/feed")
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("(unchanged)" if token_set else "(none)")
        self.clear_token = QCheckBox("Clear token")
        self.radius = QDoubleSpinBox()
        self.radius.setRange(50, 20_000)
        self.radius.setSuffix(" m")
        self.radius.setValue(settings.feed_radius_m)
        self.warn = QLabel()
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color: #c60;")
        self.url.textChanged.connect(self._check)
        form = QFormLayout(self)
        form.addRow(QLabel("Only a server you own/control can provide other players and AI."))
        form.addRow("URL", self.url)
        form.addRow("Token", self.token)
        form.addRow("", self.clear_token)
        form.addRow("Minimap radius", self.radius)
        form.addRow(self.warn)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self._check()

    def _check(self) -> None:
        url = self.url.text().strip()
        self.warn.setText((validate_url(url) if url else None) or insecure_warning(url) or "")


class App(QObject):
    def __init__(self, qapp: QApplication, pipeline: TelemetryPipeline, store: MarkerStore, settings: Settings,
                 feed=None) -> None:
        super().__init__()
        self.qapp, self.settings, self.store = qapp, settings, store
        self.pipeline = pipeline
        self.feed = feed
        self.view = MapView(pipeline, store, settings, feed)
        self.view.request_marker_at_world = self.add_marker_at
        self.panel = MarkersPanel(self.view, store)
        self.nearby_panel = NearbyPanel(self.view)
        actions = {
            "full_map": self.view.toggle_full_map, "edit": self.view.toggle_edit,
            "add_marker": self.add_marker_here, "markers": self.show_panel, "nearby": self.show_nearby,
            "zoom_in": lambda: self.view.zoom_by(1.25), "zoom_out": lambda: self.view.zoom_by(0.8),
        }
        self._actions = actions
        self._install_hotkeys(actions)
        self._build_tray()
        if settings.window_x is not None and settings.window_y is not None:
            self.view.move(settings.window_x, settings.window_y)
        else:
            g = qapp.primaryScreen().availableGeometry()
            self.view.move(g.right() - self.view.width() - 24, g.top() + 24)
        self.view.show()
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(lambda: self.tray.setToolTip(f"Isle LiveMap: {self.pipeline.status_text()}"))
        self._status_timer.start(1000)

    def _install_hotkeys(self, actions) -> None:
        self._shortcuts = []
        for key, (seq, _mod, _vk) in HOTKEYS.items():  # in-app shortcuts (work when a dialog/window is focused)
            sc = QShortcut(QKeySequence(seq), self.view)
            sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            sc.activated.connect(actions[key])
            self._shortcuts.append(sc)
        self._filter = None
        if sys.platform == "win32":  # global hotkeys while the game has focus
            import ctypes

            handlers = {}
            for i, (key, (_s, mod, vk)) in enumerate(HOTKEYS.items(), start=1):
                if ctypes.windll.user32.RegisterHotKey(None, i, mod | 0x4000, vk):
                    handlers[i] = actions[key]
            self._filter = _HotkeyFilter(handlers)
            self.qapp.installNativeEventFilter(self._filter)
            self.qapp.aboutToQuit.connect(lambda: [ctypes.windll.user32.UnregisterHotKey(None, i) for i in handlers])

    def _build_tray(self) -> None:
        pm = QPixmap(16, 16)
        pm.fill(QColor("#ff4040"))
        self.tray = QSystemTrayIcon(QIcon(pm), self)
        menu = QMenu()
        for text, key in (
            ("Full map (Alt+M)", "full_map"), ("Edit mode (Ctrl+Shift+O)", "edit"),
            ("Add marker here (Ctrl+Shift+P)", "add_marker"), ("Markers… (Ctrl+Shift+L)", "markers"),
            ("Nearby list… (Ctrl+Shift+N)", "nearby"),
        ):
            a = QAction(text, menu)
            a.triggered.connect(self._actions[key])
            menu.addAction(a)
        menu.addSeparator()
        for text, fn in (
            ("Zoom to feed radius", lambda: self.view.zoom_to_radius(self.settings.feed_radius_m)),
            ("Size…", self.ask_size), ("Range rings…", self.ask_rings), ("Species label…", self.ask_species),
            ("Feed settings…", self.ask_feed),
        ):
            a = QAction(text, menu)
            a.triggered.connect(fn)
            menu.addAction(a)
        for text, attr in (
            ("Zones", "show_zones"), ("Food sources", "show_food"), ("Range rings", "show_rings"),
            ("Show players (feed)", "show_players"), ("Show AI (feed)", "show_ai"),
        ):
            a = QAction(text, menu, checkable=True)
            a.setChecked(getattr(self.settings, attr))
            a.toggled.connect(lambda on, at=attr: setattr(self.settings, at, on))
            menu.addAction(a)
        menu.addSeparator()
        q = QAction("Quit", menu)
        q.triggered.connect(self.quit)
        menu.addAction(q)
        self.tray.setContextMenu(menu)
        self._menu = menu
        self.tray.show()

    # ---- actions ----------------------------------------------------------
    def show_panel(self) -> None:
        self.panel.show()
        self.panel.raise_()

    def show_nearby(self) -> None:
        self.nearby_panel.show()
        self.nearby_panel.raise_()

    def ask_feed(self) -> None:
        if self.feed is None or not hasattr(self.feed, "token"):
            return
        dlg = FeedDialog(None, self.settings, self.feed.url if self.feed.url != "(demo)" else "", self.feed.token_set)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        token = "" if dlg.clear_token.isChecked() else (dlg.token.text().strip() or self.feed.token)
        self.settings.feed_url, self.settings.feed_token = dlg.url.text().strip(), token
        self.settings.feed_radius_m = dlg.radius.value()
        self.settings.normalise()
        self.view.zoom_to_radius(self.settings.feed_radius_m)
        self.feed.configure(self.settings.feed_url, self.settings.feed_token, self.settings.feed_interval_s)

    def add_marker_here(self) -> None:
        pos = self.view.player_world()
        if pos is None:
            self.tray.showMessage("Isle LiveMap", "No position yet.", QSystemTrayIcon.MessageIcon.Warning, 2000)
            return
        self.add_marker_at(*pos)

    def add_marker_at(self, x: float, y: float) -> None:
        dlg = MarkerDialog(None)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            try:
                self.store.add(dlg.name.text(), dlg.kind.currentText(), x, y, dlg.notes.text())
            except ValueError as err:
                self.tray.showMessage("Isle LiveMap", str(err), QSystemTrayIcon.MessageIcon.Warning, 3000)

    def ask_size(self) -> None:
        v, ok = QInputDialog.getInt(None, "Overlay size", "Percent:", self.settings.size_percent, SIZE_MIN, SIZE_MAX, 5)
        if ok:
            self.view.set_size_percent(v)

    def ask_rings(self) -> None:
        text, ok = QInputDialog.getText(None, "Range rings", "Radii (m or km, comma separated):",
                                        text=", ".join(format_distance(r) for r in self.settings.ring_radii_m))
        radii = parse_radii(text) if ok else []
        if radii:
            self.settings.ring_radii_m = radii

    def ask_species(self) -> None:
        text, ok = QInputDialog.getText(None, "Species label", "Shown on the HUD (not detectable from traffic):",
                                        text=self.settings.species)
        if ok:
            self.settings.species = text.strip()[:30]

    def quit(self) -> None:
        self.settings.window_x, self.settings.window_y = self.view.x(), self.view.y()
        self.settings.save()
        self.qapp.quit()
