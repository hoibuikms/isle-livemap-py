# Server feed (nearby players and AI)

The overlay can draw nearby players and AI on the minimap and full map. **The only
source of other entities is a feed published by a server you own and control** (for
example your own Evrima dev server plus a mod or sidecar you write). The overlay does
not decode other entities from game network traffic, does not read game memory and does
not inject anything. On a server you do not control there is no feed, so no other
entities are shown.

## Configuration

Precedence: CLI flag, then environment variable, then saved settings.

| What | CLI | Env | Settings / tray |
|---|---|---|---|
| URL | `--feed-url URL` | `ISLE_FEED_URL` | tray "Feed settings..." |
| Bearer token | `--feed-token T` | `ISLE_FEED_TOKEN` | tray "Feed settings..." |

* Prefer the environment variable for the token: command-line arguments are visible to
  other processes. A token entered in the dialog is saved in `settings.json` in plain
  text. The token is never logged or shown in the UI.
* Poll interval defaults to 0.5 s (`feed_interval_s` in `settings.json`, 0.25 to 30).
  Errors back off exponentially up to 15 s.
* Minimap radius defaults to 1 km (dialog, `feed_radius_m`). The full map shows every
  entity the feed sends.
* Redirects are not followed and system proxies are ignored, so the token only goes to
  the host you configured. Responses over 1 MB or more than 500 entities are rejected or
  truncated.
* Plain `http://` is accepted but a warning is logged and shown in the settings dialog
  unless the host is loopback or a private/link-local address. Use `https://` for
  anything that crosses the internet.

Status text: `feed: ok (N)`, `feed: unreachable` (connection error, timeout, non-2xx
other than 401/403, redirect), `feed: unauthorized` (401/403), `feed: bad data`,
`feed: invalid URL`. Entities disappear if no successful poll for 5 s (or 5 intervals).

## Schema

`GET <url>` returns `application/json`:

```json
{
  "ts": 1767225600,
  "entities": [
    {"id": "76561198000000001", "kind": "player", "name": "Rex_Hunter",
     "species": "Tyrannosaurus", "growth_pct": 100, "health_pct": 92,
     "stamina_pct": 70, "hunger_pct": 60, "thirst_pct": 80,
     "x": 51234.5, "y": -48765.0, "z": 30000.0},
    {"id": "ai-1042", "kind": "ai", "name": "Dryosaurus", "species": "Dryosaurus",
     "growth_pct": 100, "health_pct": 100, "x": 52000.0, "y": -47000.0}
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `ts` | number, optional | Unix seconds when the snapshot was taken (informational) |
| `id` | string (or integer) | Required, unique and stable per entity (used for smoothing); duplicates dropped |
| `kind` | `"player"` or `"ai"` | Required |
| `x`, `y` | number | Required. **World units: Unreal centimetres, same axes as the local telemetry** (100 units = 1 m; north = -Y; same origin as the Gateway map projection in `geo.py`). In UE, X/Y of `GetActorLocation()` |
| `z` | number, optional | Same unit; not used for distance (distance is horizontal) |
| `name`, `species` | string, optional | Truncated to 60 / 40 chars |
| `growth_pct`, `health_pct` | number 0..100, optional | Clamped; shown as `?` if missing |
| `stamina_pct`, `hunger_pct`, `thirst_pct` | number 0..100, optional | Parsed, currently not drawn |

Entries with a missing or invalid `id`, `kind`, `x` or `y` (including NaN/Infinity or
strings) are dropped; the rest of the document is still used. If the body is not a JSON
object with an `entities` array the status is `feed: bad data`.

Unit assumption: the overlay assumes the feed uses the same coordinate space as the
position it decodes for your own character. This was not verified against a real
server; if your mod reports metres, multiply by 100. Check by comparing the feed
position of your own character with the local marker/ring distance.

## Exposing the feed from your server (guide)

Any approach that yields the JSON above works. A typical setup:

1. In your server-side mod/plugin, on a timer (1 s is plenty), iterate the player
   controllers and AI pawns and collect id, name, species, growth, health, and the
   actor location (X, Y, Z in cm).
2. Serialise to the schema above and either (a) serve it from a tiny HTTP endpoint in
   the mod, or (b) write it to a file that a small sidecar (nginx, Caddy, or a 20-line
   Python/Node server) serves.
3. Require `Authorization: Bearer <secret>` (compare in constant time) and put the
   endpoint behind TLS (a reverse proxy such as Caddy does this easily). Do not expose
   it unauthenticated to the internet: it reveals every player's position. Consider
   limiting the output to players who opted in.
4. Point the overlay at it: `ISLE_FEED_URL=https://host/feed ISLE_FEED_TOKEN=... python -m isle_livemap`.

## Trying it without a server

    python tools/mock_feed.py --token demo          # serves http://127.0.0.1:8765/feed
    ISLE_FEED_TOKEN=demo python -m isle_livemap --replay --feed-url http://127.0.0.1:8765/feed

`python -m isle_livemap --replay` alone uses an internal synthetic feed around the demo
walker, with no HTTP involved.
