"""Server-feed client: the ONLY source of other players / AI.

The feed is a JSON document published by a server (and mod) that YOU control; see
docs/SERVER_FEED.md. Nothing about other entities is read from game network
traffic or game memory. Without a feed URL no other entity is ever shown.

World units in the feed match the local telemetry: Unreal centimetres, same axes.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .geo import Entity, vital_pct

log = logging.getLogger("isle_livemap.feed")

MAX_BODY_BYTES = 1_000_000
MAX_ENTITIES = 500
REQUEST_TIMEOUT_S = 3.0
MAX_BACKOFF_S = 15.0
KINDS = ("player", "ai")

ST_OFF = "feed: off"
ST_OK = "feed: ok"
ST_UNREACHABLE = "feed: unreachable"
ST_UNAUTH = "feed: unauthorized"
ST_BAD = "feed: bad data"
ST_BAD_URL = "feed: invalid URL"


class FeedError(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


# ----------------------------------------------------------------- parsing
def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _pct(v) -> float | None:
    """Feed vitals are already 0-100; decoded by the same ``geo.vital_pct`` as the local player's."""
    return vital_pct(_num(v), 100.0)


def parse_entity(raw) -> Entity | None:
    """One feed entry -> Entity, or None if malformed (x/y/id/kind are required)."""
    if not isinstance(raw, dict):
        return None
    eid = raw.get("id")
    if isinstance(eid, bool) or not isinstance(eid, (str, int)) or not str(eid).strip():
        return None
    kind = raw.get("kind")
    if kind not in KINDS:
        return None
    x, y = _num(raw.get("x")), _num(raw.get("y"))
    if x is None or y is None:
        return None
    name, species = raw.get("name", ""), raw.get("species", "")
    return Entity(
        id=str(eid).strip()[:64], kind=kind,
        name=name[:60] if isinstance(name, str) else "",
        species=species[:40] if isinstance(species, str) else "",
        x=x, y=y, z=_num(raw.get("z")),
        growth_pct=_pct(raw.get("growth_pct")), health_pct=_pct(raw.get("health_pct")),
        stamina_pct=_pct(raw.get("stamina_pct")), hunger_pct=_pct(raw.get("hunger_pct")),
        thirst_pct=_pct(raw.get("thirst_pct")),
    )


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    ts: float | None
    entities: tuple[Entity, ...]
    dropped: int


def parse_feed(body: bytes | str | dict) -> ParsedFeed:
    """Validate a feed document. Raises FeedError(ST_BAD) if it is not usable at
    all; malformed individual entries (and duplicates by id) are dropped."""
    try:
        doc = json.loads(body) if isinstance(body, (bytes, str)) else body
    except (ValueError, UnicodeDecodeError):
        raise FeedError(ST_BAD) from None
    if not isinstance(doc, dict) or not isinstance(doc.get("entities"), list):
        raise FeedError(ST_BAD)
    out: dict[str, Entity] = {}
    dropped = 0
    for raw in doc["entities"]:
        e = parse_entity(raw)
        if e is None or e.id in out or len(out) >= MAX_ENTITIES:
            dropped += 1
            continue
        out[e.id] = e
    return ParsedFeed(_num(doc.get("ts")), tuple(out.values()), dropped)


# ----------------------------------------------------------------- URL checks
def validate_url(url: str) -> str | None:
    """Return an error text if the URL is unusable (only http/https are allowed)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "not a valid URL"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "URL must start with http:// or https://"
    return None


def is_local_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def insecure_warning(url: str) -> str | None:
    """Warn when a token would travel in clear text over a non-local network."""
    if validate_url(url):
        return None
    parts = urlsplit(url)
    if parts.scheme == "http" and not is_local_host(parts.hostname or ""):
        return (f"feed URL uses plain http to non-local host '{parts.hostname}': "
                "the token and data are visible on the network; use https")
    return None


def resolve_feed_config(settings, cli_url: str | None = None, cli_token: str | None = None,
                        env: dict | None = None) -> tuple[str, str]:
    """Precedence: CLI flag > environment (ISLE_FEED_URL / ISLE_FEED_TOKEN) > settings."""
    import os

    env = os.environ if env is None else env
    url = cli_url or env.get("ISLE_FEED_URL") or settings.feed_url or ""
    token = cli_token or env.get("ISLE_FEED_TOKEN") or settings.feed_token or ""
    return url.strip(), token.strip()


# ----------------------------------------------------------------- HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: they could carry the bearer token to another host."""

    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def _opener() -> urllib.request.OpenerDirector:
    # No proxies (the token must go straight to the configured server), no redirects.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def describe_net_error(err: BaseException, url: str) -> str:
    """Short reason for a failed request: refused / timeout / DNS / other."""
    reason = getattr(err, "reason", err)
    parts = urlsplit(url)
    where = f"{parts.hostname}:{parts.port or (443 if parts.scheme == 'https' else 80)}"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return f"timeout after {REQUEST_TIMEOUT_S:g}s at {where} - host not answering/firewall"
    if isinstance(reason, ConnectionRefusedError):
        return f"connection refused at {where} - is the feed server running on that port?"
    if isinstance(reason, socket.gaierror):
        return f"cannot resolve host '{parts.hostname}'"
    if isinstance(reason, ConnectionResetError):
        return f"connection reset by {where} (http vs https mismatch?)"
    return f"{type(reason).__name__}: {reason}"[:80]


def fetch(url: str, token: str = "", timeout: float = REQUEST_TIMEOUT_S) -> bytes:
    """GET the feed. Raises FeedError with a user-facing status. Never logs the token."""
    if validate_url(url):
        raise FeedError(ST_BAD_URL)
    headers = {"Accept": "application/json", "User-Agent": "isle-livemap-feed/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _opener().open(req, timeout=timeout) as resp:
            body = resp.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise FeedError(ST_UNAUTH) from None
        hint = " - wrong path? check the URL" if err.code == 404 else ""
        raise FeedError(f"{ST_UNREACHABLE} (HTTP {err.code}{hint})") from None
    except (urllib.error.URLError, OSError, ValueError) as err:  # incl. timeouts, refused, bad URL
        raise FeedError(f"{ST_UNREACHABLE} ({describe_net_error(err, url)})") from None
    if len(body) > MAX_BODY_BYTES:
        raise FeedError(ST_BAD)
    return body


# ----------------------------------------------------------------- sources
@dataclass(frozen=True, slots=True)
class FeedSnapshot:
    entities: tuple[Entity, ...]
    version: int  # changes whenever entities change; -1 while stale / off
    status: str
    enabled: bool


@dataclass
class _Config:
    url: str = ""
    token: str = field(default="", repr=False)  # never shown in repr/logs
    interval_s: float = 1.0


class FeedPoller:
    """Background thread polling the feed URL. ``snapshot()`` is thread-safe."""

    def __init__(self, url: str = "", token: str = "", interval_s: float = 1.0, timeout: float = REQUEST_TIMEOUT_S) -> None:
        self._lock = threading.Lock()
        self._cfg = _Config()
        self._timeout = timeout
        self._entities: tuple[Entity, ...] = ()
        self._version = 0
        self._status = ST_OFF
        self._last_ok = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="isle-feed")
        self.configure(url, token, interval_s)

    # -- config
    @property
    def url(self) -> str:
        return self._cfg.url

    @property
    def token_set(self) -> bool:
        return bool(self._cfg.token)

    @property
    def token(self) -> str:
        return self._cfg.token

    def configure(self, url: str, token: str, interval_s: float = 1.0) -> None:
        url = (url or "").strip()
        warning = insecure_warning(url) if url else None
        if warning:
            log.warning("%s", warning)
        with self._lock:
            self._cfg = _Config(url, (token or "").strip(), min(max(float(interval_s), 0.25), 30.0))
            self._entities, self._last_ok = (), 0.0
            self._version += 1
            self._status = ST_OFF if not url else ("feed: connecting" if not validate_url(url) else ST_BAD_URL)
        self._wake.set()

    # -- lifecycle
    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- reading
    def snapshot(self) -> FeedSnapshot:
        with self._lock:
            cfg, now = self._cfg, time.monotonic()
            stale = self._last_ok == 0.0 or now - self._last_ok > max(5.0, 5 * cfg.interval_s)
            ents = () if stale else self._entities
            return FeedSnapshot(ents, -1 if stale else self._version, self._status, bool(cfg.url))

    def poll_once(self) -> None:
        """One fetch + state update (used by the thread; also directly testable)."""
        with self._lock:
            cfg = self._cfg
        try:
            parsed = parse_feed(fetch(cfg.url, cfg.token, self._timeout))
        except FeedError as err:
            with self._lock:
                if cfg is self._cfg:
                    self._status = err.status
            raise
        with self._lock:
            if cfg is not self._cfg:  # reconfigured meanwhile: discard
                return
            self._entities, self._last_ok = parsed.entities, time.monotonic()
            self._version += 1
            self._status = f"{ST_OK} ({len(parsed.entities)})"

    def _run(self) -> None:
        fails = 0
        while not self._stop.is_set():
            with self._lock:
                cfg = self._cfg
            delay = 0.5
            if cfg.url:
                try:
                    self.poll_once()
                    fails, delay = 0, cfg.interval_s
                except FeedError:
                    fails += 1
                    delay = min(cfg.interval_s * 2 ** min(fails, 5), MAX_BACKOFF_S)
            self._wake.wait(delay)
            self._wake.clear()


# ----------------------------------------------------------------- demo data
def synthetic_payload(t: float, anchor_x: float, anchor_y: float) -> dict:
    """Moving fake players/AI orbiting an anchor point (world cm). Used by
    tools/mock_feed.py and by --replay's internal demo feed."""
    spec = [  # id, kind, name, species, radius_m, angular speed rad/s, phase, hp, growth
        ("p1", "player", "Rex_Hunter", "Tyrannosaurus", 180, 0.020, 0.0, 92, 100),
        ("p2", "player", "Sprinter", "Deinonychus", 420, -0.030, 1.5, 55, 62),
        ("p3", "player", "VeryLongPlayerNameThatWillBeTruncated", "Triceratops", 700, 0.012, 3.0, 100, 80),
        ("p4", "player", "FarAway", "Pachycephalosaurus", 3000, 0.010, 4.0, 70, 40),
        ("a1", "ai", "Dryosaurus", "Dryosaurus", 250, 0.045, 0.7, 100, 100),
        ("a2", "ai", "Hypsilophodon", "Hypsilophodon", 320, -0.040, 2.2, 80, 35),
        ("a3", "ai", "Compsognathus", "Compsognathus", 520, 0.050, 3.9, 100, 20),
        ("a4", "ai", "Omniraptor", "Omniraptor", 850, -0.020, 5.1, 65, 75),
    ]
    ents = []
    for eid, kind, name, species, r_m, w, ph, hp, gr in spec:
        a = ph + w * t
        e = {
            "id": eid, "kind": kind, "name": name, "species": species,
            "growth_pct": gr, "health_pct": hp,
            "x": round(anchor_x + math.cos(a) * r_m * 100.0, 1),
            "y": round(anchor_y + math.sin(a) * r_m * 100.0, 1),
            "z": 30_000.0,
        }
        if kind == "player":
            e.update(stamina_pct=70, hunger_pct=60, thirst_pct=80)
        ents.append(e)
    return {"ts": int(time.time()), "entities": ents}


class DemoFeed:
    """Internal synthetic feed for ``--replay`` (no server). Same interface as
    FeedPoller; positions are generated around ``anchor()`` -> (x, y) | None."""

    def __init__(self, anchor, update_s: float = 1.0) -> None:
        self._anchor, self._period = anchor, update_s
        self._t0 = time.monotonic()
        self.url, self.token_set, self.token = "(demo)", False, ""

    def configure(self, *a, **k) -> None:  # demo ignores settings
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def snapshot(self) -> FeedSnapshot:
        a = self._anchor()
        if a is None:
            return FeedSnapshot((), -1, "feed: demo (waiting for position)", True)
        step = int((time.monotonic() - self._t0) / self._period)  # one update per period
        doc = parse_feed(synthetic_payload(step * self._period, *a))
        return FeedSnapshot(doc.entities, step, f"feed: demo ({len(doc.entities)})", True)
