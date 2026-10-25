"""Automatic per-launch session files: ``session_YYYYmmdd_HHMMSS.pcap`` + ``session_YYYYmmdd_HHMMSS.log``.

Every live launch records the game's datagrams (recorder.py) and a concise plain-text event log into one
directory, so the data needed to decode species/names later (actor creation records are sent only once, when an
actor first becomes relevant) is on disk without the player having to remember ``--record``.

Default directory: ``<data dir>/logs/sessions`` (Windows: ``%APPDATA%\\isle-livemap\\logs\\sessions``), or the
``record_dir`` setting. Disabled by ``--no-record`` or ``record_sessions=false``; never active for ``--replay`` or
``--lab-peers``.

Retention (``prune_sessions``), run at startup and whenever a pcap rolls to a new part:
  * keep the newest N sessions (default 10; a session = its pcap part files + its log), delete older ones;
  * keep the directory under a total size cap (default 1 GB) by deleting the oldest files first;
  * the files of the running session are never deleted;
  * only names matching ``session_<stamp>[_partN].pcap|.log|.log.1`` are ever touched; anything else is left alone.

Size cap per pcap file (default 200 MB): the recorder closes the file and continues in
``session_<stamp>_part2.pcap`` etc., so a long session keeps its NEWEST data and the oldest parts are what
retention removes first. (Trade-off: join-time creation records live in part 1; with the default caps that is
the first ~200 MB of one session.)

Everything here is fail-safe: errors are reported (``SessionFiles.error``) and logged, never raised into the
capture thread.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

DEFAULT_KEEP_SESSIONS = 10
DEFAULT_TOTAL_CAP_MB = 1024.0
DEFAULT_FILE_CAP_MB = 200.0
SNAPSHOT_INTERVAL_S = 10.0
LOG_MAX_BYTES = 5 * 1024 * 1024
MAX_TRACEBACKS_PER_SITE = 5

SESSION_RE = re.compile(r"^session_(\d{8}_\d{6})(?:_part(\d+))?\.(pcap|log|log\.1)$")


def default_session_dir(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from .geo import data_dir as _data_dir

        data_dir = _data_dir()
    return Path(data_dir) / "logs" / "sessions"


def newest_session_pcap(directory: Path) -> Path | None:
    """Newest ``session_*.pcap`` in ``directory`` (by stamp, then part number), or None."""
    best = None
    try:
        for p in directory.iterdir():
            m = SESSION_RE.match(p.name)
            if m and m.group(3) == "pcap" and p.is_file():
                key = (m.group(1), int(m.group(2) or 1))
                if best is None or key > best[0]:
                    best = (key, p)
    except OSError:
        return None
    return best[1] if best else None


@dataclass
class _Entry:
    stamp: str
    part: int
    kind: str
    path: Path
    size: int


def _entries(directory: Path) -> list[_Entry]:
    out: list[_Entry] = []
    try:
        for p in directory.iterdir():
            m = SESSION_RE.match(p.name)
            if not m or p.is_symlink() or not p.is_file():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            out.append(_Entry(m.group(1), int(m.group(2) or 1), m.group(3), p, size))
    except OSError:
        pass
    return out


def prune_sessions(directory: Path, keep_sessions: int = DEFAULT_KEEP_SESSIONS,
                   total_cap_bytes: int = int(DEFAULT_TOTAL_CAP_MB * 1048576),
                   protect: tuple[Path, ...] = ()) -> list[Path]:
    """Delete the oldest session files until at most ``keep_sessions`` sessions and ``total_cap_bytes`` remain.
    Files in ``protect`` (and nothing outside the session naming pattern) are never deleted. Returns the deleted paths."""
    keep_sessions = max(1, int(keep_sessions))
    protected = {Path(p).name for p in protect}
    entries = _entries(Path(directory))
    deleted: list[Path] = []

    def remove(e: _Entry) -> None:
        try:
            e.path.unlink()
        except OSError:
            return
        entries.remove(e)
        deleted.append(e.path)

    live_stamps = {e.stamp for e in entries if e.path.name in protected}
    stamps = sorted({e.stamp for e in entries})
    excess = len(stamps) - keep_sessions
    for stamp in stamps:
        if excess <= 0:
            break
        if stamp in live_stamps:
            continue
        for e in [e for e in entries if e.stamp == stamp]:
            remove(e)
        excess -= 1

    order = sorted((e for e in entries if e.path.name not in protected),
                   key=lambda e: (e.stamp, e.kind != "pcap", e.part))  # oldest first, pcaps before their log
    total = sum(e.size for e in entries)
    for e in order:
        if total <= total_cap_bytes:
            break
        remove(e)
        total -= e.size
    return deleted


class _SafeRotatingHandler(logging.handlers.RotatingFileHandler):
    def handleError(self, record) -> None:  # noqa: N802 - logging API; never print or raise from the capture thread
        pass


class SessionLog:
    """Concise event log of one session. Attach to the pipeline (and optionally the recorder and peer source)."""

    def __init__(self, path: Path, pipeline, *, recorder=None, peers=None,
                 interval: float = SNAPSHOT_INTERVAL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.path = Path(path)
        self.pipeline, self.recorder, self.peers = pipeline, recorder, peers
        self.interval, self._clock = interval, clock
        self.error = ""
        self._logger: logging.Logger | None = None
        self._handler: logging.Handler | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._own: int | None = None
        self._server: str | None = None
        self._peer_stem: str | None = None
        self._next_snapshot = 0.0
        self._first_creation = False
        self._errors: dict[str, int] = {}
        self.creation = self.exports = self.destroyed = self.packets = 0
        self.handles: set[int] = set()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        try:
            handler = _SafeRotatingHandler(self.path, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            lg = logging.getLogger(f"isle_livemap.session.{id(self)}")
            lg.setLevel(logging.INFO)
            lg.propagate = False
            lg.addHandler(handler)
        except Exception as err:  # noqa: BLE001 - logging must never stop the app
            self.error = f"cannot open session log {self.path}: {err}"
            return False
        self._logger, self._handler = lg, handler
        self.event("app start")
        try:
            self.pipeline.add_observer(self.on_inbound, self.on_reset)
            self.pipeline.error_hook = self.on_error
        except Exception as err:  # noqa: BLE001
            self.error = str(err)
            self.event("could not attach to the capture pipeline: %s", err)
            return False
        self._next_snapshot = self._clock() + self.interval
        self._thread = threading.Thread(target=self._run, daemon=True, name="isle-sessionlog")
        self._thread.start()
        return True

    def close(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        try:
            self.pipeline.remove_observer(self.on_inbound, self.on_reset)
            if self.pipeline.error_hook == self.on_error:
                self.pipeline.error_hook = None
        except Exception:  # noqa: BLE001
            pass
        if self._logger is not None:
            self.snapshot("final")
            self.event("app stop")
            if self._handler is not None:
                self._logger.removeHandler(self._handler)
                try:
                    self._handler.close()
                except Exception:  # noqa: BLE001
                    pass
            self._logger = self._handler = None

    # -- writing (never raises) ---------------------------------------------
    def event(self, msg: str, *args) -> None:
        lg = self._logger
        if lg is None:
            return
        try:
            lg.info(msg, *args)
        except Exception:  # noqa: BLE001
            pass

    def on_error(self, where: str) -> None:
        """Called by the pipeline from inside an except block; logs the traceback (first few per site)."""
        n = self._errors.get(where, 0) + 1
        self._errors[where] = n
        if n <= MAX_TRACEBACKS_PER_SITE:
            self.event("decoder exception in %s (#%d):\n%s", where, n, traceback.format_exc().rstrip())
        elif n == MAX_TRACEBACKS_PER_SITE + 1:
            self.event("decoder exception in %s repeated; further ones are not logged", where)

    # -- pipeline callbacks (decode worker thread) ---------------------------
    def on_reset(self) -> None:
        self.event("server/game reset: handles are no longer valid")
        self._own = None

    def on_inbound(self, d, own_handle: int | None = None) -> None:
        try:
            self._inbound(d, own_handle)
        except Exception:  # noqa: BLE001
            self.on_error("session log inbound")

    def _inbound(self, d, own_handle) -> None:
        self.packets += 1
        if own_handle is not None and own_handle != self._own:
            if self._own is None:
                self.event("own handle locked: %s", own_handle)
            else:
                self.event("own handle changed: %s -> %s", self._own, own_handle)
            self._own = own_handle
        from .decode import Bits, batch_kind, KIND_CREATION, count_batches, parse_iris_packet

        packet = parse_iris_packet(d.payload)
        if packet is None:
            return
        c = count_batches(d.payload, packet)
        self.creation += c.creation
        self.exports += c.exports
        self.destroyed += c.destroyed
        if len(self.handles) < 200_000:
            self.handles.update(b.net_ref_handle for b in packet.batches)
        if c.creation and not self._first_creation:
            self._first_creation = True
            bits = Bits(d.payload)
            first = next(b for b in packet.batches if batch_kind(bits, b) == KIND_CREATION)
            self.event("first creation record: handle=%s, %d bits, exports=%d (packet #%d, %d bytes)",
                       first.net_ref_handle, first.data_bit_count, c.exports, self.packets, len(d.payload))
        for h in packet.destroyed_handles:
            self.event("destruction record: handle=%s", h)

    # -- periodic -----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.wait(1.0):
            self.tick()

    def tick(self, force_snapshot: bool = False) -> None:
        """Once a second from the helper thread: server / peer-status transitions and the 10 s counters line."""
        try:
            server = self.pipeline.server
            if server != self._server:
                self._server = server
                self.event("server learned: %s", server) if server else self.event("server cleared")
            if self.peers is not None:
                stem = re.sub(r"\d+", "#", self.peers.status().split("(")[0]).strip()
                if stem != self._peer_stem:
                    self._peer_stem = stem
                    self.event("peer source: %s", self.peers.status())
            now = self._clock()
            if force_snapshot or now >= self._next_snapshot:
                self._next_snapshot = now + self.interval
                self.snapshot()
        except Exception:  # noqa: BLE001
            self.on_error("session log tick")

    def snapshot(self, label: str = "counters") -> None:
        try:
            c = self.pipeline.counts()
            actors = len(self.peers.store.entities()) if self.peers is not None else 0
            rec = self.recorder
            summary = getattr(self.peers, "summary", None)
            self.event("%s: in=%d out=%d decoded=%d dropped=%d | creation=%d exports=%d destroyed=%d | "
                       "actors tracked=%d distinct handles=%d | worker queue max=%d slow decodes=%d capture errors=%d "
                       "worker restarts=%d | recorded=%s%s",
                       label, c.get("inbound", 0), c.get("outbound", 0), c.get("decoded", 0), c.get("dropped", 0),
                       self.creation, self.exports, self.destroyed, actors, len(self.handles),
                       c.get("queue_max", 0), c.get("slow_decodes", 0), c.get("capture_errors", 0), c.get("worker_restarts", 0),
                       f"{rec.packets} pkts in {rec.path.name}" if rec is not None else "no",
                       f" | {summary()}" if callable(summary) else "")
        except Exception:  # noqa: BLE001
            pass


class SessionFiles:
    """Everything one launch creates: the (rolling) pcap recorder and the session log, plus retention."""

    def __init__(self, directory: Path, pipeline, *, peers=None, keep_sessions: int = DEFAULT_KEEP_SESSIONS,
                 total_cap_mb: float = DEFAULT_TOTAL_CAP_MB, file_cap_mb: float = DEFAULT_FILE_CAP_MB,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self.dir = Path(directory)
        self.pipeline, self.peers = pipeline, peers
        self.keep, self.total_cap = keep_sessions, int(total_cap_mb * 1048576)
        self.file_cap_mb = file_cap_mb
        self._now = now
        self.stamp = ""
        self.recorder = None
        self.log: SessionLog | None = None
        self.error = ""
        self._part = 1

    @property
    def pcap_path(self) -> Path:
        return self.dir / f"session_{self.stamp}.pcap"

    @property
    def log_path(self) -> Path:
        return self.dir / f"session_{self.stamp}.log"

    def start(self) -> bool:
        """Create the directory, prune, open the log and the pcap. Returns False (never raises) on failure."""
        from .recorder import PcapRecorder

        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            stamp = self._now()
            while True:  # two launches in the same second must not overwrite each other
                self.stamp = stamp.strftime("%Y%m%d_%H%M%S")
                if not self.pcap_path.exists() and not self.log_path.exists():
                    break
                stamp = stamp.replace(microsecond=0) + timedelta(seconds=1)
        except Exception as err:  # noqa: BLE001
            self.error = f"cannot use session directory {self.dir}: {err}"
            return False
        self.log = SessionLog(self.log_path, self.pipeline, peers=self.peers)
        log_ok = self.log.start()
        self.recorder = PcapRecorder(self.pcap_path, self.file_cap_mb, rollover=self._next_part,
                                     on_event=self.log.event if log_ok else None)
        self.log.recorder = self.recorder
        self._prune()
        rec_ok = self.recorder.attach(self.pipeline)
        if not log_ok:
            self.error = self.log.error
        if not rec_ok:
            self.error = self.recorder.error
        return rec_ok or log_ok

    def _protect(self) -> tuple[Path, ...]:
        rec = self.recorder
        return tuple(p for p in (self.log_path, self.pcap_path, rec.path if rec is not None else None) if p)

    def _prune(self, extra: tuple[Path, ...] = ()) -> None:
        try:
            gone = prune_sessions(self.dir, self.keep, self.total_cap, self._protect() + extra)
            if gone and self.log is not None:
                self.log.event("retention removed %d old file(s): %s", len(gone), ", ".join(p.name for p in gone[:6])
                               + (" ..." if len(gone) > 6 else ""))
        except Exception as err:  # noqa: BLE001
            if self.log is not None:
                self.log.event("retention failed: %s", err)

    def _next_part(self) -> Path:
        self._part += 1
        nxt = self.dir / f"session_{self.stamp}_part{self._part}.pcap"
        self._prune(extra=(nxt,))
        return nxt

    def close(self) -> None:
        if self.recorder is not None:
            self.recorder.close()
        if self.log is not None:
            self.log.close()
