"""Demo server-feed: synthetic moving players/AI on http://127.0.0.1:8765/feed.

    python tools/mock_feed.py [--port 8765] [--token SECRET]
    python -m isle_livemap --replay --feed-url http://127.0.0.1:8765/feed [--feed-token SECRET]

Binds to 127.0.0.1 only. Entities orbit the synthetic walker's route position
(same clock as --replay if you start the app right after this script).
Stdlib only. Not a game server; it just demonstrates the schema.
"""

from __future__ import annotations

import argparse
import hmac
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from isle_livemap.feed import synthetic_payload  # noqa: E402
from isle_livemap.replay import SyntheticSource  # noqa: E402


def make_server(port: int = 8765, token: str = "", host: str = "127.0.0.1") -> ThreadingHTTPServer:
    walker, t0 = SyntheticSource(), time.monotonic()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/feed":
                return self.send_error(404)
            if token:
                got = self.headers.get("Authorization", "")
                if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                    return self.send_error(401)
            t = time.monotonic() - t0
            x, y, _ = walker.pose(t)
            body = json.dumps(synthetic_payload(t, x, y)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # quiet; never print headers
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default="", help="require 'Authorization: Bearer TOKEN'")
    args = ap.parse_args()
    srv = make_server(args.port, args.token)
    print(f"mock feed on http://127.0.0.1:{args.port}/feed (token {'required' if args.token else 'not required'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
