# TRD: Show Other Players on the Minimap (parity with the Local Player), packet-sourced

- Status: rev 8, implemented. Rev 8 (section 13) answers most of the items that were blocked on a join capture: class, species, kind, player names, destruction records, health and stamina of others, and the positions of fish and pteranodons. Sections 11 and 12 describe the state BEFORE that capture; where they say "NOT decodable" for name, class, health or destruction, section 13 supersedes them.
- Date: 2026-10-22
- Repo: isle-livemap-py, branch `main`
- Revision note: rev 1 sourced other players from the server feed URL. New constraint: NO feed URL. Other players must come from the same local data path as the local player and the nearby radar. Every feed dependency is removed or marked DEPRECATED (section 9).
- Revision note rev 3: decision recorded that decoding other players from real game traffic is allowed. The policy question and all policy gates are removed. The real-game backend is now a first-class target backend (the lab backend remains for development and testing). The only open unknown on that path is technical feasibility (Q1, spike TRD-050).
- Revision note rev 4: the real-game backend (`peer_source = "game"`) is a personal tool for the repo owner only, used to verify things from their own side. It is NOT a distributed or user-facing feature, stays opt-in via config/flag, and is not made the default for anyone. The lab backend remains the dev/testing backend. Q2 is resolved accordingly. Passive capture (REQ-023) is restated as a design choice matching the existing repo approach, not a restriction or compliance boundary; no memory-access or injection work is added. No end-user distribution or UX-polish tasks were present, so none were removed; the Peers dialog (TRD-033) is kept as a plain owner-facing settings replacement for the Feed dialog. Numbering of REQ/TRD/Q/T items is unchanged.
- Revision note rev 6 (DECISION, supersedes the rev 4 "opt-in, never the default" wording): other nearby players come from the GAME, through the same capture pipeline that provides the current player, always. There is no source choice for the user. `GamePeerSource` is the default and only user-facing source; the `peer_source` setting, the `--game-peers` flag, the Peers settings dialog (TRD-033) and its tray entry are removed. Lab and synthetic stay as developer/test tooling selected only by CLI (`--lab-peers [PORT]`, bare `--replay`). `--replay FILE.pcap` uses the game path. Old settings files containing `peer_source` still load (the key is ignored). Kept as minimal tray items: "Show other players" (`show_players`), "Nearby radius..." (`peer_radius_m`), "Vitals detail on labels" (`show_vitals_detail`); `peer_self_ids` is settings-file only. The owner's personal-tool scope is the reason game-as-default is acceptable. Where earlier text below says opt-in, owner-only opt-in, `peer_source`, `--game-peers` or the Peers dialog, read it as superseded by this note.
- Task legend: `□` not started, `☐` in progress, `✓` done

---

## 1. Summary and Scope

Goal: other players appear on the minimap "the same way the local player is": same coordinate path (`geo`), same vitals decoding and formatting (`vital_pct`, `vital_lines`), a heading indicator when heading is known, and a consistent look, while staying visually distinct from the local player.

Key findings of the investigation:
1. The repo's only live source is local passive capture of the game's UDP traffic (`capture.LiveCapture` -> `TelemetryPipeline`). It decodes **only the player's own** state: outbound client movement (position + yaw) and inbound replication for the own pawn (vitals, self chosen by heartbeat). Nothing in the repo decodes another player's position, name or vitals from game traffic.
2. The "nearby radar" (`nearby.py`, `labsim/`, `tools/payload_probe.py`, `net_diag.py`) is built on an **assumed, simulated** 18-byte record (id, x, y, z, health, name). The code says so itself (`payload_sniffer.py`: "assumed, simulated ... not any real game's protocol"; `net_diag.py`: "PLACEHOLDER PAYLOAD LAYOUT ... replace when real layout known"). It is loopback-only lab tooling and is not yet connected to the overlay.
3. The overlay renders other entities from `MapView.ents` (`geo.EntitySmoother`) fed by `feed.snapshot()`. Removing the feed removes the only thing that currently populates it.

So the work is: (a) introduce a source-neutral, in-process peer source that yields `geo.Entity` into the existing overlay path, (b) wire the one source that actually exists and is verifiable (the lab record) so the whole pipeline is testable end to end, (c) deliver an opt-in real-game backend for other actors, as a personal tool for the repo owner to verify things from their own side (not a distributed or user-facing feature), starting with a technical feasibility spike (TRD-050) because the decoder does not exist and it is unknown whether inbound replication exposes other actors' fields (Q1); the lab backend stays for development and testing. Parity and robustness work (staleness, self-exclusion, heading fallback, styling) carries over from rev 1.

In scope:
- Peer source abstraction + lab-record adapter (loopback) + synthetic demo source for `--replay`.
- Self-exclusion by packet identity, per-entity staleness/despawn, graceful degradation for missing heading/name/vitals.
- Minimap marker/label parity, edge cases, tests, removal of feed code.
- Owner-only real-game backend (opt-in): technical feasibility spike (TRD-050) followed by the decoder and backend (TRD-051) for other actors in real game traffic (position, name, vitals).

Out of scope:
- Distribution, packaging or end-user UX polish of the real-game backend; making it the default source.
- Memory access, injection or sending packets (the real-game backend is passive capture only, by design choice, REQ-023).
- Any server-side component, HTTP polling, feed URL/token/schema.
- Changing local-player telemetry decoding (`MovementTracker`, `VitalsTracker`).
- Uncommitted work in `feed.py` / `tests/test_feed.py` (treated as the user's; this TRD does not touch it, see TRD-070).

## 2. Current-State Analysis

### 2.1 Local player (`overlay.py`, `capture.py`)
- `TelemetryPipeline.handle`: OUTBOUND datagram -> `MovementTracker.track` -> `MovementCandidate` (x, y, z in Unreal cm, `unreal_yaw_degrees`, `map_heading_degrees`). INBOUND datagram from the learned server -> `VitalsTracker.track` -> `Vitals` for the own pawn only.
- `MapView.tick()` -> `geo.Interpolator` (tau 0.09 s) -> player-centred view via `project_unclamped`; `_draw_player` draws the red heading arrow last; `_draw_hud` uses `vital_lines(local_entity(...))`.

### 2.2 Shared geo path (`geo.py`)
- Unreal cm, north = -Y. `Entity(id, kind, name, species, x, y, z, growth/health/stamina/hunger/thirst_pct)`: **no heading field**.
- `vital_pct(cur, max)` is the single vitals decoder (used by `local_entity`, `nearby.from_lab`, and, in the working tree, `feed._pct`).
- `nearby_entities(items, px, py, radius, show_players, show_ai)` (2D, no self filter) is what `MapView.nearby()` uses. `nearby_3d(me, entities, r)` filters `e.id != me.id` and sorts by 3D distance, but is used only by `nearby.py`. **Later change:** the minimap is flat, so `nearby_3d`, the 3D distance and the height delta were removed; `nearby.py` now uses `visible_entities` (2D) like the map and the Nearby panel (see `docs/PEERS.md`, "Distance, direction and why a dot can be missing").
- `local_entity` uses id `"local"`. Packet ids never equal `"local"`, so the `nearby_3d` self filter is a no-op for packet-sourced data unless self is removed upstream (relevant to section 4.4).
- `EntitySmoother`: per-id smoothing (tau 0.15 s), drops vanished ids immediately. `Settings`: `show_players`, `show_ai`, `feed_radius_m` (the radius is not really feed-specific), plus the feed fields (DEPRECATED, section 9).

### 2.3 What each existing packet path can supply for other entities

| Path | Format | id | Position | Heading | Name | Vitals | Self marker | Staleness |
|---|---|---|---|---|---|---|---|---|
| Real capture (`capture.py`/`decode.py`/`tracking.py`) | UE5.5 bit-packed; own movement + own Iris vitals | Iris `net_ref_handle` exists per batch (`IrisBatch`), but only the own handle is used | Own only. Others: NOT decoded | Own yaw only | None | Own only (growth, health, stamina, hunger, thirst) | `VitalsObservation.net_ref_handle` = own handle | n/a |
| `labsim/payload_sniffer.py` `NodeRecord` | `<IfffH` + UTF-8 name, loopback 127.0.0.1:7777 | uint32 | x, y, z float32, **Y-up metres** | none | yes (UTF-8) | one int, "status / health" | none in the sniffer | none |
| `labsim/telemetry.py` `Store` | `<i3fh` + name (same 18-byte header, signed id/health) | int32 | same | none | yes | one int `health` | `PLAYER_ID = 0` is "me", routed to `store.me`, never into entities | `NODE_TTL` 5.0 s per node, expired on read |
| `tools/payload_probe.py` | `<ifffh` + name | int32 | same | none | yes | `status` | none | none |
| `net_diag.py` (Windows) | placeholder `<i3f` records repeated | int32 | x, y, z | none | none | none | first record seen = root (heuristic) | `NODE_TTL` 5.0 s |
| `nearby.from_lab(dict)` | dict `{id, name, species, health 0-100, growth 0-1, pos{x,y,z}}` | str(id) | Y-up m converted once by `_cm` to geo cm | none | optional | health, growth via `vital_pct` | none (self removed by caller) | none |

Consequences:
- Fields that can truly be supplied today for another entity (lab source): id, position (x, y, z), name, and one health value. Not supplied: heading, species, growth, stamina, hunger, thirst, kind (all packet entities are treated as `player`), explicit despawn.
- The real-game decoder for other actors does not exist. Whether inbound replication exposes other pawns' location/name/vitals in a decodable form is **unknown** (technical feasibility, Q1). `parse_iris_packet` already exposes per-object batches (`net_ref_handle`) but skips destruction records and decodes no foreign attributes.
- Wire-format inconsistencies in the lab code: `payload_sniffer` uses unsigned `I`/`H`, `telemetry`/`payload_probe` use signed `i`/`h`; `NodeRecord` has flat x/y/z/status while `nearby.from_lab` expects a nested `pos` dict. There is no `NodeRecord -> geo.Entity` adapter yet.

### 2.4 Rendering of other entities today
`MapView.tick` pulls `self.feed.snapshot()` into `EntitySmoother`; `_draw_entities` draws dots (player) / diamonds (ai) with a two-line label (`entity_label_lines`), crowding rule (20 on screen, 4 labels); `_draw_feed_status` prints feed status. `__main__` builds `FeedPoller` or `DemoFeed` (`--replay`).

### 2.5 Gaps this TRD addresses

| # | Gap |
|---|-----|
| G1 | No packet-sourced peer source exists; the overlay only knows the feed. |
| G2 | No adapter from `NodeRecord`/`Node` to `geo.Entity`; unit mismatch (Y-up m vs geo cm) must be handled once at the boundary. |
| G3 | Other players have no heading and `Entity` has no heading field. |
| G4 | Self-exclusion: no identity rule for packet data; the `nearby_3d` filter uses `"local"` and never matches packet ids. |
| G5 | Staleness exists only in lab tooling (`Store` TTL); not per-entity in the overlay. `EntitySmoother` drops vanished ids instantly (flicker) and has no age. |
| G6 | Style/colour constants are scattered; minimap label shows HP/growth only. |
| G7 | Out-of-view players are clipped silently. |
| G8 | No decoder for other actors in real game traffic; technical feasibility unverified. |
| G9 | Feed code, settings, CLI flags, docs and tests must be retired. |

## 3. Requirements

Source and data model:
- REQ-001: Other players SHALL come only from a local in-process `PeerSource` (new `isle_livemap/peers.py`) whose backends read local packet data. No network client, URL, token or HTTP code SHALL be used for entities.
- REQ-002: `PeerSource.snapshot()` SHALL return an immutable `PeerSnapshot(entities: tuple[PeerEntity, ...], version: int, status: str, enabled: bool)`, thread-safe, where `PeerEntity` = `geo.Entity` + `last_seen` (monotonic seconds) + optional `heading`. Backends run on their own threads; the overlay never blocks on them.
- REQ-003: Backend A (lab) SHALL consume `labsim.payload_sniffer.NodeRecord` (socket backend by default, loopback 127.0.0.1 only) via one new adapter `from_record(NodeRecord) -> geo.Entity`. Lab Y-up metres SHALL be converted to geo cm once, at this boundary, reusing `nearby._cm` (no second conversion).
- REQ-004: Backend S (synthetic) SHALL replace `DemoFeed` for `--replay`: moving peers anchored to the replayed local position, with at least one entry carrying the self id (to exercise REQ-009).
- REQ-005: Backend G (real game, other actors) is an owner-only personal tool, opt-in via `peer_source = "game"` in config or the CLI flag (never the default, not a distributed or user-facing feature). It is built in two steps: a technical feasibility spike (TRD-050) that determines which of position, name and vitals inbound replication exposes for other actors (Q1), then the decoder and `GamePeerSource` (TRD-051) for the fields the spike proves decodable. Until the decoder lands, or while no peers are decoded, the app SHALL show no real-game peers and say so in the status text.
- REQ-006: The 18-byte lab record SHALL have one canonical parser. `payload_sniffer.parse_payload` is canonical; the signed/unsigned difference with `telemetry`/`payload_probe` is resolved by Q3.

Field handling and graceful degradation:
- REQ-007: Fields are filled only when the source provides them. Lab/real-source defaults: kind = `player`; species = `""`; growth/stamina/hunger/thirst = `None`; heading = `None`; name = decoded UTF-8 (NULs stripped, invalid bytes replaced) or `""`.
- REQ-008: Vitals SHALL use only `geo.vital_pct` / `vital_lines` / `entity_label_lines`. The lab health int maps to `vital_pct(health, 100.0)` (same as `from_lab`); values outside 0..100 clamp via `vital_pct`. Missing vitals are omitted everywhere on the minimap (labels and `vital_lines`); only the Nearby table prints `-`/`?` because its columns need a cell (rev 7). The minimap SHALL NOT invent growth, species or other missing values.
- REQ-009: Name fallback: empty name shows the id (`Player #<id>`), via the existing `e.name or e.id` rule. Label never blanks.
- REQ-010: Heading: `geo.Entity` SHALL gain `heading: float | None = None` (last field, normalised with `decode.normalize_degrees`). Packet sources in v1 supply `None`, so other players draw as a dot (REQ-014). Optional client-side heading from successive positions is NOT in v1 (Q4).

Self-exclusion:
- REQ-011: The local player SHALL be excluded by packet identity, never by position. Lab: id == `labsim.telemetry.PLAYER_ID` (0) is dropped at the source and kept only as the lab "me". Real game: the peer whose Iris `net_ref_handle` equals the own handle (`VitalsObservation.net_ref_handle`) is dropped. A configurable extra `self_ids` set (`Settings.peer_self_ids`) is supported. A real nearby player at identical coordinates but a different id SHALL still be shown.
- REQ-012: The existing `geo.nearby_3d` `id != me.id` filter SHALL be extended to also drop ids in the self-id set, so the Nearby panel, the map and `nearby.py` all agree.

Staleness and despawn:
- REQ-013: Each peer carries `last_seen`. Age = now - last_seen. Dimmed after `PEER_STALE_S` = 3 s; removed after `PEER_TTL_S` = 5 s (matches `telemetry.NODE_TTL` and `net_diag.NODE_TTL`). The packet sources provide no explicit despawn message, so TTL is the only despawn rule (Iris destruction records are skipped by the current parser; reading them is a possible later improvement, Q6). The old 6 s grace from rev 1 is dropped. A missed packet within TTL does not flicker (REQ-016). Rev 7: these 3 s / 5 s apply to lab/synthetic peers. The source supplies its own `stale_s` / `ttl_s`; the game source uses 8 s / 30 s (`GAME_PEER_STALE_S`, `GAME_PEER_TTL_S`) because Iris sends changes only, so an idle actor is silent, and destruction records already despawn immediately.

Rendering:
- REQ-014: Player with heading -> rotated arrow; without heading -> dot. Distinct from the local player: different colour, smaller, outlined; local arrow stays on top. One `ENTITY_STYLE` table replaces scattered constants.
- REQ-015: Minimap label = name (species when present) + compact line through `entity_label_lines`: known vitals (`HP x%`, `G y%`), distance, compass bearing, then `[dZ]`. Unknown fields are omitted, never printed as `?` (rev 7); a game peer therefore reads `Actor #<handle>` / `757 m N +116m`. An optional detail line (`show_vitals_detail`) uses `vital_lines`.
- REQ-016: `EntitySmoother` SHALL smooth x/y (and heading when present, `shortest_arc`), keep each id until its TTL (not drop on first miss), and snap on teleports > 200 000 units. Rev 7: with a clock (`step(dt, now)`) it also dead-reckons x/y along the velocity of the last two distinct samples (at most one measured interval, at most 1 s; off for a gap > 3 s, an unchanged re-send, a teleport or > 60 m/s). It never derives a heading.
- REQ-017: A player inside the radar radius but outside the view gets an edge indicator on the circle; beyond the radius nothing is drawn.
- REQ-018: Crowding rules unchanged: above 20 visible only the nearest 4 labelled; dots always drawn far to near. `show_players` applies before label placement. `show_ai` is inert for packet sources (no AI kind is available) and SHALL be hidden or disabled in the UI (Q5).
- REQ-019: Rendering SHALL tolerate: no source, source disabled/unavailable (e.g. no Npcap, no scapy, port taken), empty, all-stale; and SHALL draw no peers when the local player has no fix.

Non-functional and removal:
- REQ-020: Entity-layer paint with 500 entities under 4 ms; labels bounded by REQ-018.
- REQ-021: New selection/age/geometry logic lives in `geo.py` or `peers.py` (pure Python, no Qt, headless-testable). `overlay.py` only draws. Backend threads never raise into the loop; failures are counted (as `PayloadSniffer.malformed`) and surfaced in `status`.
- REQ-022: Feed removal: no code path reads `feed_url`, `feed_token`, `feed_interval_s`, `ISLE_FEED_TOKEN`, `--feed-url`, `--feed-token`, `FeedPoller`, `DemoFeed`, `parse_feed`. Old `settings.json` containing those keys SHALL still load (unknown keys ignored, already how `Settings.load` works); `feed_radius_m` SHALL migrate to `peer_radius_m`.
- REQ-023: Design choice: lab backends stay loopback-only and read-only. The real-game backend uses passive capture, the same approach as `capture.py`, because it matches the existing repo design and needs nothing more. The design does not include memory access, injection or sending, and no work for those is planned.

## 4. Architecture and Design

### 4.1 Principle
Reuse the local-player path. A peer is a `geo.Entity` differing only by style role. Source specifics (units, record layout, TTL, self id) live in the backend adapter; everything downstream (smoothing, filter, labels, drawing) is source-neutral.

### 4.2 Data flow

```
 lab loopback UDP 127.0.0.1:7777 ──> PayloadSniffer(socket) ──> parse_payload ──> NodeRecord
        (Backend A)                                                │ from_record (Y-up m -> cm, vital_pct)
 --replay ──> SyntheticPeers (Backend S) ───────────────────────────┤
 real game inbound Iris batches ──> GamePeerSource [TRD-050 spike, TRD-051] (Backend G)
                                                                    v
                                         PeerStore: dict id -> (Entity, last_seen); drops self ids; TTL
                                                                    v
                              PeerSource.snapshot() -> PeerSnapshot(version, status, enabled)
                                                                    v
 MapView.tick(): if version changed -> EntitySmoother.update(entities, now)   (per-id, TTL, heading-aware)
              -> geo.visible_entities(items, me, radius, toggles, self_ids, now)  [NEW pure]
              -> _draw_entities / _draw_edge_indicators / labels (entity_label_lines, place_label)
              -> _draw_player() local arrow last (unchanged)
 Local player (unchanged): LiveCapture -> TelemetryPipeline.latest -> Interpolator -> geo.local_entity
```

### 4.3 Module changes

`isle_livemap/peers.py` (new, pure Python except the sniffer thread)
- `PeerEntity`, `PeerSnapshot`, `PeerStore` (thread-safe, `clock` injectable like `telemetry.Store`), `from_record`, `GamePeerSource` (real game, TRD-051: reads other actors' Iris batches from the capture path and emits peers; fields the spike proves undecodable stay `None`), `LabPeerSource` (wraps `PayloadSniffer(on_record=store.update, backend="socket")`; `status` reports captured/malformed counts, "lab: listening 127.0.0.1:7777", or the startup error), `SyntheticPeers`.
- `PeerSource` protocol: `start()`, `stop()`, `snapshot()`. Same shape as the old feed object, so `MapView(..., peers=...)` is a rename of its `feed` argument.

`isle_livemap/geo.py`
- `Entity.heading` (REQ-010). `EntitySmoother` gains `last_seen`, TTL, heading smoothing, `items_aged()` (existing `items()` contract kept for tests). New `visible_entities(...)`, `is_self`, `edge_anchor`. `nearby_3d` self-id extension (REQ-012). `Settings`: add `peer_radius_m`, `peer_self_ids`, `show_vitals_detail`, `peer_source` (`"off" | "lab" | "game"`); keep `show_players`; mark `show_ai` inert.

`isle_livemap/overlay.py`
- `feed` -> `peers`; `_draw_feed_status` -> `_draw_peer_status`; `ENTITY_STYLE`; `_draw_entity_marker` (arrow if heading else dot, dim when stale); `_draw_edge_indicators`; `nearby()` delegates to `visible_entities`.

`isle_livemap/app.py`, `__main__.py`
- Replace the Feed settings dialog (URL, token, interval) with a Peers dialog (source on/off, radius, self ids, detail toggle). `__main__`: add `--lab-peers [PORT]` (dev/testing) and an opt-in owner-only real-game peer option (`--game-peers` or `peer_source = "game"`; never the default; wired to `GamePeerSource` once TRD-051 lands); `--replay` uses `SyntheticPeers`; remove `--feed-url/--feed-token`, `resolve_feed_config`, `feed.start/stop`.

Touched only by the real-game backend (TRD-050/051): `capture.py`, `decode.py`, `tracking.py` (additive, own-player decoding unchanged). Otherwise not touched.

### 4.4 Self-exclusion method
1. Source level (authoritative): lab `id == PLAYER_ID` goes to `store.me`; real game own `net_ref_handle` (from `VitalsObservation`) is excluded. The real-game own handle is only known after the heartbeat lock, so before the lock a peer with an unknown handle may appear; it is removed retroactively once the lock arrives (the store purges that id).
2. Config level: `peer_self_ids` for ids the user knows are theirs (e.g. second client).
3. Geo level: `nearby_3d`/`visible_entities` also drop ids in the self-id set.
4. Explicitly rejected: dropping the nearest entity or any entity at the local coordinates.

### 4.5 Differentiation (self vs other)

| Aspect | Local player | Other player | Notes |
|---|---|---|---|
| Glyph | Red arrow 10 px, interpolated heading | Cyan dot (arrow only if a source supplies heading), outlined | v1 lab/real sources give no heading |
| Z-order | Top | Below self, far to near | |
| Label | HUD box bottom-left | Name or `Player #id`, species only if known, `[HP x%] [G y%] dist bearing [dZ]` | unknown parts are omitted |
| Vitals | Full (`vital_lines`) | Health only from lab; full set only if a real decoder supplies it | |
| Stale | n/a | Lab: dim 3 s, removed 5 s. Game: dim 8 s, removed 30 s or on a destruction record | |

### 4.6 Key decisions
1. One neutral peer interface; the lab backend is the dev/test reference and the real game is an opt-in owner-only backend, so the pipeline can be built and verified before the decoder is proven.
2. Lab backend is the verifiable reference: it exercises the real overlay path with true packet bytes, not a fake dict.
3. No guessing: absent fields stay `None` and are not printed (fallback glyphs only for the marker).
4. TTL (5 s) is the single despawn rule, aligned with existing lab constants.
5. Identity-based self exclusion only.

## 5. Edge Cases

| Case | Expected behaviour |
|---|---|
| Source off / not started | No peers; status text; local player unaffected (REQ-019). |
| Lab port in use / no scapy / no privileges | `LabPeerSource.status` shows the startup error; app keeps running. Socket backend is default (no privileges). Never use socket and a second receiver on the same port. |
| Truncated or garbage datagram | `parse_payload` returns None; counted in `malformed`, never raised. |
| Invalid UTF-8 / NUL-padded name | Replaced / stripped; empty name falls back to `Player #id` (REQ-009). |
| NaN/inf or implausible coordinates (wrong endianness or layout) | Record dropped and counted; no entity created. |
| Health outside 0..100 or negative | Clamped by `vital_pct`; shown as a percentage, never crashes. |
| Name or health absent (real game, if not decodable) | `Actor #id` (lab: `Player #id`) with distance, bearing, dZ; no `HP ?`. |
| No heading (all v1 sources) | Dot marker; no arrow, no inferred direction. |
| Self id appears in packets | Dropped at the source (REQ-011). Same coordinates, different id: shown. |
| Own handle learned late (real game) | Provisional peer is purged once own handle locks. |
| Same id, packet burst | Latest wins; `version` increments once per snapshot change. |
| Peer silent | Lab: dimmed at 3 s, removed at 5 s. Game: dimmed at 8 s, removed at 30 s or by a destruction record (REQ-013); reappearing id resets age without a position jump. |
| Teleport > 200 000 units | Smoother snaps. |
| Out of radius / in radius but out of view | Not drawn / edge indicator (REQ-017). |
| No local fix | No peers or edge indicators; "Waiting for game..." stays. |
| Lab units mixup (Y-up m vs geo cm) | Converted only in `from_record`; test T4 guards axis order (lab z -> geo y, lab y -> geo height). |
| 500 entities | Dots always; max 4 labels when crowded; REQ-020 budget. |
| `show_players` off | Players and edge indicators hidden. |
| Old settings.json with feed keys | Ignored, no error (REQ-022). |

## 6. Test Plan (pytest)

Pure-logic tests need no Qt (`tests/test_peers.py`, `test_geo.py`, `test_entities.py`). Rendering tests follow `tests/test_feed_ui.py` pattern (offscreen Qt, `importorskip("PySide6")`), moved to `tests/test_peers_ui.py`.

| ID | Test | Covers |
|---|---|---|
| T1 | `from_record`: id, name, health via `vital_pct`, other vitals `None`, heading `None`, kind `player` | REQ-003, 007, 008, 010 |
| T2 | Real bytes: `labsim.telemetry.encode(Node)` -> `PayloadSniffer.parse_payload` -> `from_record` round trip (signed/unsigned parity once Q3 is decided) | REQ-003, 006 |
| T3 | `PeerStore`: latest wins, TTL expiry with injected clock, `version` bumps only on change, thread-safety smoke | REQ-002, 013 |
| T4 | Axis/unit check: lab (x, y=height, z) -> geo (x, y, height) cm; distance equals `nearby.nearby_entities` for the same input | REQ-003 |
| T5 | Self: id 0 never enters entities; `peer_self_ids` dropped; same-coords different-id kept; provisional handle purged on own-handle lock | REQ-011, 012 |
| T6 | `nearby_3d` / `visible_entities` agree on set and order, strict radius, toggles | REQ-012, 018 |
| T7 | `EntitySmoother`: TTL keeps id until 5 s, stale flag at 3 s, reappear resets age with no jump, teleport snap, heading shortest arc when heading present | REQ-013, 016 |
| T8 | Vitals parity: peer and `local_entity` with equal health give identical `vital_lines` fragments and label text; missing fields print `?` | REQ-008, 015 |
| T9 | Name/id fallback and truncation in `entity_label_lines` | REQ-009 |
| T10 | `edge_anchor` on circle, direction preserved, zero vector | REQ-017 |
| T11 | Settings: round trip, `feed_radius_m` -> `peer_radius_m` migration, old feed keys ignored | REQ-022 |
| T12 | Loopback e2e (like `test_labsim` end-to-end): simulator -> socket sniffer -> `LabPeerSource` -> `MapView.nearby()` | REQ-001, 003 |
| T13 | UI: peers in view, off view, stale, `show_players` off, full map, no fix, source error status; no exception | REQ-014, 017, 019 |
| T14 | Perf smoke: 500 entities under budget (marked `slow`) | REQ-020 |
| T15 | Static guard: no module imports `feed`/`urllib` for entities; no `feed_url` reads (grep test) | REQ-001, 022 |
| T16 | Regression: `test_labsim`, `test_payload_sniffer`, `test_nearby`, `test_movement`, `test_vitals`, `test_pipeline` pass unchanged | all |

Run: `QT_QPA_PLATFORM=offscreen pytest -q`. Gate: suite green, new `geo.py`/`peers.py` code 90% covered. `test_feed*.py` are deleted only under TRD-070.

## 7. Task Breakdown

Hours. Dependencies in brackets. Critical path: TRD-001, 010, 011, 020, 030, 031, 040, 060, 070, 080.

Phase 0, decisions
- ✓ TRD-001 Resolve Q3 (record format); Q2 was resolved in rev 4; Q1 is answered by TRD-050 (1h)

Phase 1, peer source (parallel with Phase 2 after TRD-001)
- ✓ TRD-010 `peers.py`: `PeerEntity`, `PeerSnapshot`, `PeerStore` (TTL, version, self-id drop) (3h) [TRD-001]
- ✓ TRD-011 `from_record` + `LabPeerSource` over `PayloadSniffer` (socket), status text (3h) [TRD-010]
- ✓ TRD-012 Canonicalise the record parser per Q3 (adjust `telemetry`/`probe` or document) (1h) [TRD-001]
- ✓ TRD-013 `SyntheticPeers` incl. a self-id entry (replaces `DemoFeed` demo) (2h) [TRD-010]

Phase 2, geo logic
- ✓ TRD-020 `Entity.heading`; `EntitySmoother` TTL/age/heading; `items_aged()` (3h) [TRD-001]
- ✓ TRD-021 `visible_entities`, `is_self`, `nearby_3d` self-id extension (3h) [TRD-020]
- ✓ TRD-022 `edge_anchor` (1h)
- ✓ TRD-023 Settings: `peer_radius_m` (+migration), `peer_self_ids`, `peer_source`, `show_vitals_detail` (1h)

Phase 3, rendering and app
- ✓ TRD-030 `ENTITY_STYLE`; `_draw_entity_marker` dot/arrow/dim (3h) [TRD-020]
- ✓ TRD-031 `_draw_entities` on `visible_entities`; peers rename; `_draw_peer_status`; optional detail line (2h) [TRD-021, TRD-030, TRD-010]
- ✓ TRD-032 `_draw_edge_indicators` (2h) [TRD-022, TRD-031]
- ✓ TRD-033 Peers settings dialog replacing Feed dialog; hide/disable `show_ai`; status text (2h) [TRD-023]

Phase 4, wiring
- ✓ TRD-040 `__main__`: `--lab-peers`, `--replay` -> `SyntheticPeers`, real-game peer option wired to `GamePeerSource` when available, start/stop peers, drop feed wiring (2h) [TRD-011, TRD-013, TRD-031]
- ✓ TRD-041 Docs: `docs/PEERS.md` (sources, record layout, units, self-id, TTL, limits) replacing the deleted SERVER_FEED.md idea (2h) [TRD-011]

Phase 5, real-game backend (owner-only, opt-in; start TRD-050 immediately, it does not depend on Phase 1 to 4)
- ✓ TRD-050 Technical feasibility spike (time-box 8h): from existing fixtures and captured pcaps, can inbound Iris batches (per `net_ref_handle`) yield other actors' position, name and vitals, and with which ids? Output: a written field list (decodable, partial, or not decodable) that scopes TRD-051; no production code. Answers Q1.
- ✓ TRD-051 `GamePeerSource` (position only; see Findings) (Backend G): decode other actors' fields proven by TRD-050 into `PeerEntity`, drop own handle, provisional-purge logic, status text, fixture-based tests (estimate set after TRD-050, placeholder 12h) [TRD-010, TRD-050]

Phase 6, tests and removal
- ✓ TRD-060 Tests T1 to T15 (6h) [TRD-010..033, TRD-040]
- ✓ TRD-070 Feed retirement per section 9: delete `feed.py`, `tools/mock_feed.py`, `test_feed.py`, `test_feed_ui.py` (port still-useful cases), strip settings/CLI/UI. DONE: user ruled "no more feed URL"; files deleted, deprecated Settings fields removed (load-time `feed_radius_m` migration kept) (3h) [TRD-040, TRD-060]
- ☐ TRD-080 Full suite, `--replay` manual check, lab loopback manual check, code review, perf check (2h) [all]

Remaining: TRD-080 manual checks with the real game (lab loopback, `--game-peers` on a live session) and the code review gate; everything else is done or blocked on the capture (Q10).

Total about 41 h excluding TRD-050 (8h time-box) and TRD-051 (placeholder 12h, re-estimated after TRD-050). Suggested delegation after approval: backend-developer for Phases 1, 2 and 4; a Qt-capable developer for Phase 3; test-runner and code-reviewer for gates; documentation-specialist for TRD-041.

## 8. Risks

| Risk | Mitigation |
|---|---|
| Real game may not expose decodable foreign actor data, or only partially (technical feasibility) | Time-boxed feasibility spike first (TRD-050); source-neutral interface and lab backend let the rest of the pipeline be built and verified meanwhile; undecodable fields degrade to `?` (REQ-007). |
| The lab record is an assumption, not the game protocol | Labelled as such everywhere; real decoder is a separate backend. |
| Wrong self id before the own-handle lock | Provisional purge (4.4); T5. |
| Axis or unit mixup | One conversion point, T4. |
| Merge conflict with the user's uncommitted `feed.py`/`test_feed.py` | TRD-070 is blocked on the user's decision; nothing edits those files earlier. |
| Ghosts of departed players for up to 5 s | Dimming at 3 s; constants tunable. |

## 9. Feed-URL Dependencies: DEPRECATED, removed under TRD-070

| Item | Location | Action |
|---|---|---|
| `FeedPoller`, `DemoFeed`, `parse_feed`, `parse_entity`, `resolve_feed_config`, `describe_net_error`, `FeedSnapshot` | `isle_livemap/feed.py` (incl. uncommitted edits) | DEPRECATED. Delete in TRD-070 after user resolves the working-tree edits. |
| `Settings.feed_url`, `feed_token`, `feed_interval_s` | `geo.py` | DEPRECATED. Removed; old files still load. `feed_radius_m` -> `peer_radius_m`. |
| `ISLE_FEED_TOKEN`, `--feed-url`, `--feed-token` | `__main__.py` | DEPRECATED, removed. |
| Feed settings dialog (URL, token, interval); "feed: off (set a feed URL...)" status | `app.py` | Replaced by Peers dialog and status. |
| `MapView.feed`, `_feed_version`, `feed_status`, `_draw_feed_status` | `overlay.py` | Renamed to peers equivalents. |
| `tools/mock_feed.py`, `tests/test_feed.py`, `tests/test_feed_ui.py` | tools, tests | Deleted; reusable UI cases ported to `test_peers_ui.py`. |
| Rev 1 items: `heading_deg` feed schema, `local_player_name`, feed-id self-exclusion, `docs/SERVER_FEED.md`, "packets are never used" rule | this TRD rev 1, `feed.py` docstring | Superseded by sections 3 and 4. |

Until TRD-070 lands, nothing new may depend on any item above.

## 10. Open Questions

- Q1 (feasibility): ANSWERED by TRD-050 (section 11): position and handle yes; name, vitals, heading, class no (need a new capture).
- Q2 (scope): RESOLVED in rev 4, amended in rev 6 (game backend is now the default, see the rev 6 note). Lab loopback backend = dev/testing (behind `--lab-peers`). Real-game backend = personal tool for the repo owner only, opt-in via config/flag, not distributed, not user-facing, not the default.
- Q3 (format): Canonical lab record: signed (`<i3fh`, telemetry/probe) or unsigned (`<IfffH`, sniffer)? And does the int16 mean health 0..100 or a status code? Proposed: treat as health 0..100 as `nearby.from_lab` does; confirm.
- Q4 (heading): Infer heading from successive positions when absent? Proposed: no in v1.
- Q5 (AI kind): Packets give no AI/player distinction. Hide `show_ai`? Proposed: yes, disable it.
- Q6 (despawn): Should Iris destruction records (currently skipped by `parse_iris_packet`) feed explicit despawns for the real-game backend? Proposed: later, once TRD-050 shows they are decodable.
- Q7 (self for a second client): Is `peer_self_ids` enough, or should the app auto-learn own handle only?
- Q8 (radius/label detail): Default `peer_radius_m` stays 1000 m; show only HP by default with a detail toggle (proposed)?
- Q9 (feed.py deletion): RESOLVED by removal. The user said "no more feed URL"; `feed.py`, its tests and `tools/mock_feed.py` were deleted.
- Q10 (needs new capture): name, class, vitals, heading and a validated despawn for the game backend. See `docs/PEERS.md`, "Capture that would unlock the rest". Q3/Q6/Q7/Q8 keep their proposed defaults (implemented as proposed); Q4 = no (no heading inference); Q5 = `show_ai` inert.


## 11. TRD-050 Findings (spike on the repo's recorded inbound packets) and TRD-051 result

Method: no new capture. Every packet in `tests/fixtures/recorded_payloads.json` was run through the existing `decode.parse_iris_packet` (the envelope walk the own-vitals decoder uses) and every non-own batch was inspected (shared bit prefixes, size classes, scans for the repo's own quantised-vector and GAS-attribute layouts). The own-pawn pipeline is unchanged.

How the local player is obtained today, and what is reused:
- Position/yaw: outbound move packets, `decode_movement` + `MovementTracker`. Not applicable to others (the client never sends other actors' moves).
- Vitals: inbound replication. `parse_iris_packet` returns one `IrisBatch` per replicated object keyed by `net_ref_handle`; `VitalsTracker` looks for the survival heartbeat in every batch and picks the own handle (2 consistent hits). All batches in the fixtures (25 per packet) belong to other objects, so the stream is already decoded per actor with handles. TRD-051 reuses exactly this: the pipeline passes each inbound datagram plus `VitalsTracker.self_handle` to the peer source.

| Field of another actor | Result | Where / evidence |
|---|---|---|
| Handle (identity) | Decodable | `IrisBatch.net_ref_handle`. Same actor keeps its handle across packets (e.g. 206308 in two TrailingAttributes packets moved 1.3 m). |
| Position x, y, z | Decodable | Batches of 155 and 158 bits: bits 0..32 fixed state header `0x20E822A8` (identical in all 55 recorded batches (53 distinct handles, 9 packets; re-counted in the e2e verification)); bits 33..39 `FVector_NetQuantize100` header (`64 | comp`); then 3 x `comp` signed raw (cm x 100), the same quantisation as the own outbound move; then 37 trailing bits. Size is exactly `77 + 3*comp` (155 -> 26 bits, 158 -> 27 bits; 27 only when a component exceeds 335 544 cm). All 55 decode canonically to plausible island coordinates, z about 25 000 to 38 000 cm. Code: `decode.decode_actor_location(s)`. |
| Destruction | Partially decodable | The envelope walk already parses destruction records and discarded them. `IrisPacket.destroyed_handles` now exposes the first packed u64 of each record. One recorded record (`DetachedDamagedAttributes`: handle 86078, in the same numeric range as live handles). NOT validated against a recorded despawn (no fixture shows that handle live and then destroyed), so it is applied only to handles already tracked; TTL stays the fallback. Bunch close is channel level (Iris uses one channel), so it says nothing per actor. |
| Name | NOT decodable | No string/ASCII payload in any other-actor batch; names/classes would arrive as object exports or net tokens, which the walk skips (`has_exports` is False in every fixture). |
| Class (player vs AI vs animal) | NOT decodable | Same reason. The 155/158 location state is shared by all such actors, so entries may include AI/animals. They are all shown as `player` named `Actor #<handle>`. |
| Health / growth / stamina / hunger / thirst | NOT decodable | Other batches are 16 to 516 bits. Some 364-516 bit batches contain isolated GAS slots (float, flag, repeated float; e.g. handle 80984 bits 289 and 355) but no fixture ties a slot to a named attribute for a non-own actor, and the heartbeat triple used for self never occurs in them. Left None (`?`). |
| Heading | NOT decodable | The 37 trailing bits are likely rotation/velocity, but nothing in the repo or fixtures confirms a layout. Left None (dot glyph). |

Self exclusion: the own pawn has no 155/158 location batch in any fixture, so self normally never appears; still, exactly as designed in 4.4, a peer whose handle equals `VitalsTracker.self_handle` is dropped and a provisional peer is purged when the lock arrives. Respawn (new handle) replaces the exclusion; server change / pipeline reset clears the store (handles are per connection).

What a new capture would add (needed for name/class/vitals/heading/despawn validation): one pcap of your own session (Wireshark/Npcap on the game's UDP port, the format `--replay FILE.pcap` reads), about 60 s, recorded while (1) standing still next to ONE other known player whose character name and approximate health/growth you note, then (2) that player walking away, dying or logging off while you keep capturing (despawn), then (3) a second capture with that player alone in range of your client but with you not moving. Provide the pcap plus the player's name/species/handles you can read in-game; with that the 37-bit tail (heading), the export section (class/name) and the attribute slots can be matched against ground truth. Without it those fields stay `None`.

Implementation (TRD-051): `decode.py` (additive: `destroyed_handles`, `ActorLocation`, `decode_actor_location(s)`), `tracking.VitalsTracker.self_handle` (read-only property), `capture.TelemetryPipeline.add_observer/remove_observer` (passive taps, exceptions swallowed; own-player decoding unchanged), `peers.GamePeerSource` + `PeerStore.remove/clear`, factory `pipeline=` argument, tests in `tests/test_game_peers.py`.

### Verification addendum (end-to-end, real `__main__` wiring)
- `tests/test_e2e_peers.py` replays a pcap built from the recorded payloads through `__main__.main` (offscreen) and inspects rendered pixels. Own handle 103940 locks, the five actors 752..838 m NNW of the recorded own fix are drawn as dots at their projected positions, edge pointers appear when they are off view, lab and synthetic paths use the same axes.
- Frame: land/sea test on the bundled map: 43/53 decoded positions on land as-is, 5/53 with x mirrored, 23/53 with both axes mirrored. Mirroring only y (37/53) is not excluded. The recorded own fix and the inbound packets are not proven to come from one session.
- Player vs other actors: not distinguishable. All 55 location batches are 155/158 bits with one header; the 53 handles span 69776..208258 and interleave with the own pawn's handle range (103940); the own pawn has no such batch (433/1491-bit batches only). Actors are spread up to ~8 km from the recorded own fix, so the 1000 m default radius shows only a few.


## 12. Rev 7 (live feedback: laggy dots, `?` placeholders, vitals/species parity)

- Lag: decode and store writes are immediate (0.04..3 ms per packet); the repaint runs at 16 ms in the same tick as the own marker. The lag came from (a) dots only moving after the next sample with a slower filter than the own marker (0.15 s vs 0.09 s), (b) idle actors, which Iris does not re-send, being dimmed at 3 s and deleted at 5 s. Fixed with dead reckoning, the same tau and 8 s / 30 s game timeouts (REQ-013, REQ-016). The server's own send interval and network latency remain. Fixtures hold no packet timestamps, so that interval is not measured.
- Labels: no `?` placeholders on the minimap (REQ-008, REQ-015).
- Parity with the own player: not possible. Own slots are named by position inside batches that carry the hunger/thirst heartbeat or a full frame; other actors' 364..516 bit batches hold lone slots with no heartbeat and no maxima, and no creation/export data exists in any fixture, so species and name are unavailable. Nothing was implemented for this; see `docs/PEERS.md`.


## 13. Rev 8: the join capture (identity, species, positions, vitals of others)

Date: 2026-10-22. Input: `session_20261022_212006.pcap` (the app's own recorder: it starts before the server is joined), analysed offline with `python -I`, copied to an empty directory first, scripts kept elsewhere. Nothing from the capture is committed; `tests/fixtures/recorded_join_payloads.json` holds 46 inbound datagrams as payload bytes only (no IP/UDP headers, no addresses; Epic/EOS account ids, SteamID64 strings and player names were overwritten in place with synthetic text of the same length so every packet still parses).

### 13.1 The capture

193.6 s; 3003 inbound and 3961 outbound datagrams of one flow, plus ONE outbound datagram of 1300 bytes from a second local port to another host at 169.17 s (see 13.9). First creation record at 5.64 s, join burst 11..14 s. Of 2958 inbound datagrams that carry an Iris sequence number, 62 sequence numbers (2.1 %) are missing, 40 of them before 15 s (bursts of 20 at 5.6 s, 12 at 9.8 s, 6 at 11.2 s). The creation records of the own pawn (handle 85864) and of six creatures (84700, 77060, 7442, 45338, 80364, 47752) are not in the capture although their state arrives from 12.2..29.5 s on; the most likely reason is those missing datagrams (capture loss during the join burst; the log's `dropped=0` counts only the app's queue). This is a conjecture, not measured.

### 13.2 Envelope: two bugs found with this capture

1. **Destruction record = packed u64 handle + one bit** (always 1 in 431 records). The old reading (handle, "has info" bit, optional second handle, trailing bit) is only right when that bit happens to be 1 in the right place; otherwise it ate neighbouring bits and produced handles such as 3590495952, 16240516816 or 172, and the walk of 245 datagrams ended at an "inline destruction" with 0 batches parsed. With the new layout all 2849 parseable data streams end exactly on their last batch (`batch_count - destruction_count` batches follow the records).
   Before / after: datagrams not parsed as Iris 111 -> 44 (the 18-byte non-replication keep-alives); incomplete envelopes 247 -> 0; batches 34 964 -> 39 665 (33 362 state, 5 419 creation, 884 other prefixes); destruction records 288 -> 431; of the destruction handles, the share that is a handle seen in the capture 258/288 (90 %, and 11 above 200 000) -> 431/431 (100 %, none above 200 000); 430 of the 431 match a creation record seen earlier. Real handles are below ~90 000.
2. Export sections are now parsed, not skipped (6122 export-bearing batches, all parse).

### 13.3 Exports, net tokens, strings

An export section follows its batch: (1) net-token string exports (index, authority bit, 3-bit type id; type 1 = names, type 2 = gameplay tags such as `Ability.Skill.Use`), (2) object-reference exports, (3) the must-be-mapped handle list. An object reference is a chain of levels (handle, then optionally a name token with its string inline the first time, then the outer): `Default__BP_PlayerController_C` inside `/Game/TheIsle/Core/Player/BP_PlayerController`. Full path = names joined by ".". Name tokens are only sent once per connection; the three default sub-objects of every pawn (tokens 414..416) were defined before the capture and stay unnamed. A handle can be referenced in a must-be-mapped list without ever being exported in the capture: archetype 1693 (85 fruit actors, whose mesh asset `Papaya_Fruit_3` is exported) and archetype 2221 (the pawn archetype of six pteranodons).
Encoded net strings (flag bit 1) are one code point per base-128 group, most significant group first, bit 7 set on all but the last (`0x81 0x60` = U+00E0, `0x82 0x10` = U+0110, `0x80 0xBD 0x27` = U+1EA7). That is how Vietnamese names come out readable ("Cường Sinh Tồn" style); a PlayerState name is a packed-u32 length followed by those bytes.

### 13.4 Creation records

Layout (LSB-first): 5 prefix bits (3 zero, has-state, is-creation), has-baseline (+2 bits), factory (3), protocol id (32), has-archetype, archetype reference (flags 0,0,1 in all 5338 records that have one, then packed u64), persistent-level bit, has-location, valid bit, quantised vector. Level-placed actors (81) have no archetype: they are named by their own export (`BP_EdiblePlantsSpawnable_C_UAID_...` inside `PersistentLevel`). 4747 of 5419 records carry a location.
**The creation vector is 1/16 of the world position.** BP_Longear, handle 61004: creation raw 2812035, later 193 state vectors (cm x 100) have x = 44992560 = 16 x 2812035 exactly; four BP_Fish_RainbowFish actors give 15.9916..16.0024 in x and y. The reference's "divide by 8" is not what the data shows. Independent frame checks: the rainbow-fish positions in the older fixtures, (932, -3140) and (701, -3502) m, lie in the same lake region as this capture's (890, -3092) and (1241, -3355) m (63 m apart for the closest pair) although the handles of the two sessions differ; the older fixtures' Longear-layout actors are spread over the island (x -2.5..5.9 km, y -3.5..2.9 km), fish schools. One vector in a handle-0 batch at 20.8 s (the player's spawn) equals the outbound own position to the cm, so a quantised vector in the replication stream has the own-position scale and frame.
The fixed 155/158-bit layout of the older fixtures is the state of **BP_Longear, an AI fish**, in this capture (same 33-bit header, same size equation, 193 of 193 batches); the older fixtures' 53 handles that were drawn as "players" are therefore most likely fish (the same file also holds three rainbow-fish batches). Only the handle's class can prove it, and those fixtures hold no creation record for them.

### 13.5 Classes, kinds, species

126 distinct class labels (124 resolved paths, plus two unresolved buckets) in 5419 creation records (`tools/inspect_capture.py`). 5307 are world objects: plants and fruit (34 classes, 3962 records), gore and meat (341), `TICorpseSmell` (295), PlayerStates (388), level actors (81). Creatures (27 records):

| Species (from) | Creation records | Notes |
|---|---|---|
| Rainbow Fish (`Core/Characters/Fishes/Schools/BP_Fish_RainbowFish`) | 19 | kind ai; layout 0x1680A4328 |
| Pteranodon (archetype 2221, path never exported; 5 of 6 records name Pteranodon animation montages, none another species) | 6 (5 with a PlayerState, so kind player) | species from assets, flagged as such in code |
| Longear (`Core/AI/Characters/Fish/BP_Longear`) | 1 | kind ai |
| Gallimimus (`Core/Characters/Dinosaurs/Gallimimus/BP_Gallimimus`) | 1 (at 182 s) | kind player (PlayerState reference) |

No Triceratops, Tyrannosaurus or Deinosuchus pawn was ever created in the capture, but their assets appear: animation montages of Deinosuchus (handles 84700, 77060, 7442), Triceratops (47752) and Pteranodon (45338) in the export sections of those handles' batches, and 13 species' call sounds (`MS_<Species>_Call_*`) in the own handle's batches (sounds of creatures that called nearby; not tied to a handle). Those are evidence, not classes, and are not used. `species.py`: `CLASS_RULES` (`/Characters/Dinosaurs/` -> dino, `/AI/Characters/` and `/Characters/Fishes/` -> ai, any other known path -> other, no path -> unknown), `DISPLAY_NAMES`, cleaned class name otherwise; tested in `tests/test_identity.py`.

### 13.6 Player names

PlayerState creation records (388) hold the EOS account id as a string and the player name (packed-u32 length + base-128 code points) 460..520 bits in (469 or 477, one byte of variation); 382 of 388 decode to plausible text. A pawn's creation record references its PlayerState (packed u64 handle preceded by a constant 8-bit marker `1 1 0 0 0 1 0 1` 36 bits before the byte count, at bit 638..873 of the record; the 12 bits just before the count vary, so the earlier fixed anchor `0 1 0 0 0 0 0 0 0 0 0 1` missed two Deinosuchus pawns; found in 8 of 9 pawn records, and resolved later if the PlayerState arrives after the pawn): that gives a name and proves "player". It is structural, not confirmed by a person standing next to the creature (the PlayerState never references the pawn back in the capture). AI animals have no name. Names and account ids are other people's data: the tool counts them, `--names` lists them, the fixture has synthetic ones, and no log line contains a name.

### 13.7 Positions: why only one handle decoded, and what decodes now

Creature state batches (handles with a creature class): 3080 (193 Longear, 1197 Rainbow Fish, 1674 Pteranodon, 16 Gallimimus). The old check (header 0x20E822A8 + 77+3*comp size) passed 193 (all Longear): **2887 fail**. They carry 386 distinct 33-bit headers (the first 5 bits are the state prefix, the other 28 a change mask: which members changed) and 308 distinct sizes; the top headers are 0x1680A4328 (421), 0x060000128 (393), 0x060000108 (309), 0x20E822A8 (193); the top sizes 48 bits (741: tiny updates), 192 (363), 158 (194), 70 (117). 28 % of the batches are 48/70-bit updates that cannot hold a position.
What now decodes (all checked by plausibility AND continuity of the same handle):
- Fixed layouts (header + size equation): 0x20E822A8 @33 (Longear, 193 batches) and **0x1680A4328 @69, tail 38** (Rainbow Fish, 421 batches, comp 26/27; x and y equal 16 x the creation position, z runs away at 8 m/s so it is not claimed). 614 batches.
- Pawns: the same header has the vector at different offsets (0xC0000348 at 61, 79, 91, 94; no size equation). `ActorLocationTracker` accepts a vector only when it lies within 20 m + 100 m/s x dt (dt capped at 3 s) of the actor's last accepted position or of its creation position (anchors expire after 120 s), and trusts a (header, offset) pair after 2 such confirmations; 52 samples of 3 pteranodons (speeds up to 47 m/s between consecutive packets). False-positive check: with every creation anchor moved 300 km, the same tracker accepts 0 of about 25 000 anchored state batches (and trusts no layout), against 52 accepted with the true anchors.
- Creation position (x16) for every creature record that carries one.
At the end of the capture the minimap shows, around the perch of the player 105 079, -244 599: 3 Rainbow Fish, 2 Pteranodon, 1 Gallimimus within 1 km (picture in `/tmp/isle_real`, not committed).

### 13.8 Vitals of other creatures

A 9-slot block (66-bit GAS slots: float, flag, float): health, maxHealth, stamina, maxStamina, maxHealth, maxHealth/2, maxHealth/2, maxHealth, maxHealth. 26 blocks in the batches of 8 handles (74400: 11, 80364: 6, 80376: 3, 77060: 2, and four with one). Seven equality constraints per block; values continuous: pteranodon 74400 health 78.4 -> 85.8 % over 160 s (max 14.7 -> 20.7, growing), stamina 52..76 %. Implemented: `decode.decode_vitals_block` -> `Entity.health_pct`, `stamina_pct`. A false positive found on the way (denormal floats at 5e-36 passing a relative check) is covered by a test. NOT determinable: growth (the slot one position earlier is present only when growth changed: never beside a block), hunger and thirst of others.

### 13.9 The 21:23:02 "reset" and the frozen vitals

1. At 169.17 s the game sent a 1300-byte datagram (payload starts `01 02 "sdping"`) from a second local port to another host. The pipeline took any outbound destination for the server: reset (observers wiped), and 26 ms later the next game datagram "learned" the real server again: second reset. Fixed: a different destination replaces the server only after 5 datagrams in a row while the old one has been silent for 1 s; the first learning after `reset()` notifies nobody. Replaying the whole capture with both sockets: 3 -> 0 reset notifications (the first of the three was the initial server learning).
2. The own lock was already lost at 153.3 s: from 141.2 s the heartbeat batch is hunger, thirst, STAMINA, hunger repeated (the extra slot falls from 154 to 0 at 8.3 per second between 142 s and 161 s, in the range of the character's max-stamina slots, 139..165: stamina), which the triple-only match never saw; the 12 s timeout dropped the lock and no later batch matched. `_find_heartbeat` accepts up to two slots between thirst and the repeat. Lock now holds from 13.2 s to the last recognised heartbeat at 161.3 s. At 162.2 s the spacing between thirst and the repeated hunger is 68 bits instead of 66 or 132 (stamina 0) and the matcher does not recognise it; no heartbeat-sized batch follows and `State.Dead` / `GE_ExecuteHandleDead` are exported at 172.5 s: the character died, so the last freeze is the game, not the decoder.

### 13.10 Still unknown

Growth, hunger, thirst and heading of others; land dinosaurs (none came into relevance); whether the PlayerState link names the right player (needs a known neighbour); the structure of handle-0 batches (they mention the six creation-less creatures at join); the semantics of the destruction record's bit and of the one undecoded bit in export chains; why creation vectors are 1/16; the reason the rainbow-fish z runs away; layouts of land dinosaurs.

## 14. Rev 9: sessions that miss the join tables

The server sends the class and player tables once, at join. A capture that starts later (app launched mid-session)
has creation records but no class paths. Measured on two captures of one server (`session_20261025_123033`, tables
present; `session_20261025_131510`, mid-connection, 515 creations, 0 class paths):

* **Protocol id is stable.** Every creation record carries a 32-bit protocol id (a hash of the class's replicated
  layout). 33 of the 34 ids of the mid-connection capture also occur in the first one, with the same archetype
  handle and one class path each (511 of 515 creations). `classcache.ClassCache` stores `protocol id -> archetype
  class path` in `<data dir>/class_cache.json` (schema 1, 90-day expiry, atomic writes), learned only from a
  connection's own export tables. A table that later names another path evicts the entry for the process. Complete
  paths only (`/Game/...`): a cut-off path could pass a dinosaur off as a world object. Cache hits are flagged
  `Entity.inferred` and drawn as `(Species~)`.
* **Result on the mid-connection capture:** player names come back (the 9 PlayerStates are recognised by protocol id),
  pawns link to them through the PlayerState reference, fruit/plants/gore are filtered again.
* **Animals without a creation record.** Server animals repeat a 155 or 158 bit state batch (33 of 33 resolved
  animals, 73 never-created actors; no dinosaur or world object does). Eight state batches, 90 % of them that size,
  make an unclassified actor `ai`. The 14 leftover `unknown` dots of the mid-connection capture all became `ai`.
* **Not recoverable:** species of pawns created before the app started and not in the cache (e.g. a dinosaur class
  never seen with tables). Tables are not resent mid-connection: a new class only exports its own archetype
  (`IdentityTracker.tables_missed` is true when under 10 % of the creations got a class from the tables). The hint
  "rejoin the server" remains for actors that stay `unknown`.
* **Early sniffing.** `LiveCapture` opens the Npcap sniffers before the game is found and keeps unowned UDP in a
  `PreCaptureBuffer` (15 s, 4096 packets); when the game's port becomes known the buffered packets of that port are
  decoded first, with their original timestamps. Not testable off Windows: unit tests cover the buffer only.
