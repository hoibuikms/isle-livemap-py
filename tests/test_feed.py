"""Server-feed: schema parsing, auth header, error states (local mock server), config."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from isle_livemap.feed import (
    ST_BAD, ST_BAD_URL, ST_OFF, ST_OK, ST_UNAUTH, ST_UNREACHABLE, DemoFeed, FeedError, FeedPoller, fetch,
    insecure_warning, parse_feed, resolve_feed_config, synthetic_payload, validate_url,
)
from isle_livemap.geo import Settings

GOOD = {"ts": 1, "entities": [
    {"id": "a", "kind": "player", "name": "N", "species": "S", "growth_pct": 50, "health_pct": 90, "x": 1.0, "y": 2.0},
    {"id": "b", "kind": "ai", "x": -5, "y": 7.5, "z": 3, "stamina_pct": 10},
]}


def test_parse_valid_and_optional_fields():
    r = parse_feed(json.dumps(GOOD))
    assert r.ts == 1 and r.dropped == 0 and [e.id for e in r.entities] == ["a", "b"]
    a, b = r.entities
    assert (a.name, a.species, a.growth_pct, a.health_pct, a.z) == ("N", "S", 50, 90, None)
    assert b.name == "" and b.health_pct is None and b.z == 3 and b.stamina_pct == 10


def test_parse_drops_malformed_entries():
    bad = [
        {"id": "", "kind": "ai", "x": 1, "y": 1}, {"id": "k", "kind": "boss", "x": 1, "y": 1},
        {"id": "nx", "kind": "ai", "x": "1", "y": 1}, {"id": "nan", "kind": "ai", "x": float("nan"), "y": 1},
        {"id": "bool", "kind": "ai", "x": True, "y": 1}, {"id": "noy", "kind": "ai", "x": 1},
        "str", 5, None, {"id": "dup", "kind": "ai", "x": 1, "y": 1}, {"id": "dup", "kind": "ai", "x": 2, "y": 2},
        {"id": "ok", "kind": "player", "x": 1, "y": 1, "health_pct": 250, "growth_pct": "x", "name": 5},
    ]
    r = parse_feed(json.dumps({"entities": bad}).replace("NaN", "NaN"))
    assert [e.id for e in r.entities] == ["dup", "ok"]
    assert r.dropped == len(bad) - 2
    ok = r.entities[1]
    assert ok.health_pct == 100 and ok.growth_pct is None and ok.name == ""


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"entities": 3}', b"{}", b"\xff\xfe", b'"x"'])
def test_parse_bad_documents(body):
    with pytest.raises(FeedError) as ei:
        parse_feed(body)
    assert ei.value.status == ST_BAD


def test_url_validation_and_http_warning():
    assert validate_url("file:///etc/passwd") and validate_url("ftp://x/y") and validate_url("nonsense")
    assert validate_url("https://h/feed") is None
    assert insecure_warning("http://example.com/feed")
    assert insecure_warning("http://8.8.8.8/feed")
    for ok in ("https://example.com/f", "http://127.0.0.1:8765/feed", "http://localhost/f", "http://192.168.1.5/f"):
        assert insecure_warning(ok) is None


def test_config_precedence():
    s = Settings(feed_url="http://s/", feed_token="st")
    assert resolve_feed_config(s, env={}) == ("http://s/", "st")
    assert resolve_feed_config(s, env={"ISLE_FEED_URL": "http://e/", "ISLE_FEED_TOKEN": "et"}) == ("http://e/", "et")
    assert resolve_feed_config(s, "http://c/", "ct", env={"ISLE_FEED_URL": "http://e/"}) == ("http://c/", "ct")


# ---- mock server ---------------------------------------------------------------
class _Srv:
    def __init__(self):
        outer = self
        self.seen = []
        self.mode = "ok"

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.seen.append((self.path, dict(self.headers)))
                m = outer.mode
                if m == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/steal")
                    self.end_headers()
                    return
                if m == "auth" and self.headers.get("Authorization") != "Bearer s3cret":
                    return self.send_error(401)
                if m == "500":
                    return self.send_error(500)
                body = b"garbage" if m == "bad" else json.dumps(GOOD).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/feed"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv():
    s = _Srv()
    yield s
    s.close()


def test_auth_header_sent_and_unauthorized(srv):
    srv.mode = "auth"
    with pytest.raises(FeedError) as ei:
        fetch(srv.url, "")
    assert ei.value.status == ST_UNAUTH
    with pytest.raises(FeedError) as ei:
        fetch(srv.url, "wrong")
    assert ei.value.status == ST_UNAUTH
    assert json.loads(fetch(srv.url, "s3cret"))["entities"]
    assert srv.seen[-1][1]["Authorization"] == "Bearer s3cret"


def test_no_auth_header_without_token(srv):
    fetch(srv.url, "")
    assert "Authorization" not in srv.seen[-1][1]


def test_redirect_not_followed(srv):
    srv.mode = "redirect"
    with pytest.raises(FeedError) as ei:
        fetch(srv.url, "tok")
    assert ei.value.status == ST_UNREACHABLE


def test_poller_states(srv):
    p = FeedPoller(srv.url, "", 1.0, timeout=1.0)
    p.poll_once()
    snap = p.snapshot()
    assert snap.status == f"{ST_OK} (2)" and len(snap.entities) == 2 and snap.version > 0
    srv.mode = "bad"
    with pytest.raises(FeedError):
        p.poll_once()
    assert p.snapshot().status == ST_BAD
    srv.mode = "500"
    with pytest.raises(FeedError):
        p.poll_once()
    assert p.snapshot().status == ST_UNREACHABLE
    srv.mode = "auth"
    with pytest.raises(FeedError):
        p.poll_once()
    assert p.snapshot().status == ST_UNAUTH


def test_poller_unreachable_and_off_and_bad_url():
    p = FeedPoller("http://127.0.0.1:1/feed", "", 1.0, timeout=0.5)
    with pytest.raises(FeedError):
        p.poll_once()
    assert p.snapshot().status == ST_UNREACHABLE and not p.snapshot().entities
    assert FeedPoller("").snapshot().status == ST_OFF and not FeedPoller("").snapshot().enabled
    assert FeedPoller("file:///etc/passwd").snapshot().status == ST_BAD_URL


def test_poller_thread_polls_and_stops(srv):
    p = FeedPoller(srv.url, "s3cret", 0.25)
    srv.mode = "auth"
    p.start()
    deadline = time.time() + 5
    while time.time() < deadline and not p.snapshot().entities:
        time.sleep(0.05)
    assert len(p.snapshot().entities) == 2
    p.stop()


def test_token_not_in_repr_or_status(srv):
    p = FeedPoller(srv.url, "topsecret")
    assert "topsecret" not in repr(p._cfg)
    assert "topsecret" not in p.snapshot().status


def test_synthetic_payload_roundtrips_schema():
    r = parse_feed(synthetic_payload(12.0, 0.0, 0.0))
    assert r.dropped == 0 and {e.kind for e in r.entities} == {"player", "ai"}
    d = DemoFeed(lambda: (0.0, 0.0)).snapshot()
    assert d.entities and d.enabled
    assert DemoFeed(lambda: None).snapshot().entities == ()
