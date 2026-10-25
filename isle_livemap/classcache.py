"""Remembers which class a creation record names, for the sessions that miss the server's export tables.

A creation record carries a 32-bit protocol id (a hash of the replicated state layout of the class) and the handle
of its archetype. The archetype's class path is sent once per connection, in the join tables. A capture that starts
after the join never sees them, but the protocol id is still in every creation record and is the same in every
connection: two captures of the same server (15 s of tables vs. none) agreed on 33 of 33 shared ids. So the path
learned in one session names the same class in the next.

Safety: a protocol id is only stored with the path the export tables of that connection gave; a later table that
names another path evicts the entry and blocks it for the rest of the process. A game update changes the layout
hash, so stale ids stop matching on their own; unseen entries expire after ``MAX_AGE_S``. Everything resolved from
this cache is flagged "inferred" so the minimap can mark it."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("isle_livemap.classcache")

SCHEMA = 1
MAX_AGE_S = 90 * 24 * 3600.0
MAX_ENTRIES = 5000
SAVE_INTERVAL_S = 30.0
SEED_PATH = Path(__file__).with_name("assets") / "class_seed.json"


def load_seed(path: Path | None = SEED_PATH) -> dict[int, str]:
    """Protocol id -> class path shipped with the app (``tools/build_class_seed.py``): the classes of captures that
    did see the join tables, so a first run that starts after joining can still name them. Never raises."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return {}
        return {int(k, 16): v for k, v in (data.get("entries") or {}).items()
                if isinstance(v, str) and v.startswith("/") and 0 <= int(k, 16) < 1 << 32}
    except (OSError, ValueError, TypeError):
        return {}


class ClassCache:
    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time,
                 seed: dict[int, str] | None = None) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._seed: dict[int, str] = dict(seed or {})  # shipped with the app: read-only, never written to ``path``
        self._entries: dict[int, dict] = {}  # protocol id -> {"path": str, "n": confirmations, "seen": unix time}
        self._blocked: set[int] = set()  # ids whose tables disagreed with the stored path in this process
        self._dirty = False
        self._saved_at = 0.0
        self.conflicts = 0
        self.error = ""

    # -- queries -------------------------------------------------------------
    def lookup(self, protocol_id: int) -> str | None:
        if protocol_id in self._blocked:
            return None
        entry = self._entries.get(protocol_id)
        return entry["path"] if entry is not None else self._seed.get(protocol_id)

    def source(self, protocol_id: int) -> str | None:
        """"cache" (learned on this machine), "seed" (shipped with the app) or None: where ``lookup`` gets its answer."""
        if protocol_id in self._blocked:
            return None
        if protocol_id in self._entries:
            return "cache"
        return "seed" if protocol_id in self._seed else None

    def __len__(self) -> int:
        return len(self._entries)

    # -- learning ------------------------------------------------------------
    def learn(self, protocol_id: int, path: str) -> None:
        """Record what the export tables of the current connection say. Only complete paths ("/Game/...")."""
        if not path.startswith("/") or protocol_id in self._blocked:
            return
        now = self._clock()
        with self._lock:
            entry = self._entries.get(protocol_id)
            seeded = self._seed.get(protocol_id)
            if entry is None and seeded is not None and seeded != path:  # a game update reused the id: trust no one
                self._blocked.add(protocol_id)
                self.conflicts += 1
                log.warning("class cache: protocol %#x now names %s, the shipped seed has %s; dropped", protocol_id, path, seeded)
            elif entry is None:
                if len(self._entries) >= MAX_ENTRIES:
                    return
                self._entries[protocol_id] = {"path": path, "n": 1, "seen": now}
                self._dirty = True
            elif entry["path"] == path:
                if now - entry["seen"] > 3600.0:  # keep the file quiet: refresh at most hourly
                    entry["seen"] = now
                    self._dirty = True
                entry["n"] = min(entry["n"] + 1, 1_000_000)
            else:
                del self._entries[protocol_id]
                self._blocked.add(protocol_id)
                self.conflicts += 1
                self._dirty = True
                log.warning("class cache: protocol %#x now names %s, was %s; dropped", protocol_id, path, entry["path"])

    # -- persistence ---------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None, clock: Callable[[], float] = time.time,
             seed: dict[int, str] | None = None) -> "ClassCache":
        """The cache file at ``path`` plus ``seed`` (None: the one shipped in ``assets/class_seed.json``)."""
        cache = cls(path, clock, load_seed() if seed is None else seed)
        if path is None:
            return cache
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != SCHEMA:
                return cache
            now = clock()
            for key, entry in (data.get("entries") or {}).items():
                protocol = int(key, 16)
                text, seen = entry["path"], float(entry["seen"])
                if isinstance(text, str) and text.startswith("/") and now - seen < MAX_AGE_S and 0 <= protocol < 1 << 32:
                    cache._entries[protocol] = {"path": text, "n": max(1, int(entry.get("n", 1))), "seen": seen}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as err:  # a damaged cache must never stop the app
            cache.error = f"{type(err).__name__}: {err}"
            log.warning("class cache %s ignored: %s", path, cache.error)
            cache._entries.clear()
        return cache

    def save(self, force: bool = False) -> bool:
        """Write the file if something changed (at most every ``SAVE_INTERVAL_S`` unless forced). Never raises."""
        if self.path is None or not self._dirty:
            return False
        now = self._clock()
        if not force and now - self._saved_at < SAVE_INTERVAL_S:
            return False
        with self._lock:
            payload = {"schema": SCHEMA, "entries": {f"{k:08x}": dict(v) for k, v in sorted(self._entries.items())}}
            self._dirty = False
        self._saved_at = now
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=self.path.name, suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
            return True
        except OSError as err:
            self.error = f"{type(err).__name__}: {err}"
            self._dirty = True
            log.warning("class cache not saved: %s", self.error)
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False
