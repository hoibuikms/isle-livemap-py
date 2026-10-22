# TRD: Show Other Players on the Minimap (parity with the Local Player), packet-sourced

- Status: DRAFT rev 4, awaiting approval (no code has been written)
- Date: 2026-10-22
- Repo: isle-livemap-py, branch `main`
- Revision note: rev 1 sourced other players from the server feed URL. New constraint: NO feed URL. Other players must come from the same local data path as the local player and the nearby radar. Every feed dependency is removed or marked DEPRECATED (section 9).
- Revision note rev 3: decision recorded that decoding other players from real game traffic is allowed. The policy question and all policy gates are removed. The real-game backend is now a first-class target backend (the lab backend remains for development and testing). The only open unknown on that path is technical feasibility (Q1, spike TRD-050).
- Revision note rev 4: the real-game backend (`peer_source = "game"`) is a personal tool for the repo owner only, used to verify things from their own side. It is NOT a distributed or user-facing feature, stays opt-in via config/flag, and is not made the default for anyone. The lab backend remains the dev/testing backend. Q2 is resolved accordingly. Passive capture (REQ-023) is restated as a design choice matching the existing repo approach, not a restriction or compliance boundary; no memory-access or injection work is added. No end-user distribution or UX-polish tasks were present, so none were removed; the Peers dialog (TRD-033) is kept as a plain owner-facing settings replacement for the Feed dialog. Numbering of REQ/TRD/Q/T items is unchanged.
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
- `nearby_entities(items, px, py, radius, show_players, show_ai)` (2D, no self filter) is what `MapView.nearby()` uses. `nearby_3d(me, entities, r)` filters `e.id != me.id` and sorts by 3D distance, but is used only by `nearby.py`.
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
- REQ-008: Vitals SHALL use only `geo.vital_pct` / `vital_lines` / `entity_label_lines`. The lab health int maps to `vital_pct(health, 100.0)` (same as `from_lab`); values outside 0..100 clamp via `vital_pct`. Missing vitals render as `?` (labels) and are omitted (`vital_lines`). The minimap SHALL NOT invent growth, species or other missing values.
- REQ-009: Name fallback: empty name shows the id (`Player #<id>`), via the existing `e.name or e.id` rule. Label never blanks.
- REQ-010: Heading: `geo.Entity` SHALL gain `heading: float | None = None` (last field, normalised with `decode.normalize_degrees`). Packet sources in v1 supply `None`, so other players draw as a dot (REQ-014). Optional client-side heading from successive positions is NOT in v1 (Q4).

Self-exclusion:
- REQ-011: The local player SHALL be excluded by packet identity, never by position. Lab: id == `labsim.telemetry.PLAYER_ID` (0) is dropped at the source and kept only as the lab "me". Real game: the peer whose Iris `net_ref_handle` equals the own handle (`VitalsObservation.net_ref_handle`) is dropped. A configurable extra `self_ids` set (`Settings.peer_self_ids`) is supported. A real nearby player at identical coordinates but a different id SHALL still be shown.
- REQ-012: The existing `geo.nearby_3d` `id != me.id` filter SHALL be extended to also drop ids in the self-id set, so the Nearby panel, the map and `nearby.py` all agree.

Staleness and despawn:
- REQ-013: Each peer carries `last_seen`. Age = now - last_seen. Dimmed after `PEER_STALE_S` = 3 s; removed after `PEER_TTL_S` = 5 s (matches `telemetry.NODE_TTL` and `net_diag.NODE_TTL`). The packet sources provide no explicit despawn message, so TTL is the only despawn rule (Iris destruction records are skipped by the current parser; reading them is a possible later improvement, Q6). The old 6 s grace from rev 1 is dropped. A missed packet within TTL does not flicker (REQ-016).

Rendering:
- REQ-014: Player with heading -> rotated arrow; without heading -> dot. Distinct from the local player: different colour, smaller, outlined; local arrow stays on top. One `ENTITY_STYLE` table replaces scattered constants.
- REQ-015: Minimap label = name (species when present) + compact line `HP x% G y% dist [dZ]` through `entity_label_lines`; an optional detail line (`show_vitals_detail`) uses `vital_lines`. Missing fields print `?`.
- REQ-016: `EntitySmoother` SHALL smooth x/y (and heading when present, `shortest_arc`), keep each id until its TTL (not drop on first miss), and snap on teleports > 200 000 units.
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
| Label | HUD box bottom-left | Name or `Player #id`, species only if known, `HP x% G ? dist [dZ]` | G shows `?` |
| Vitals | Full (`vital_lines`) | Health only from lab; full set only if a real decoder supplies it | |
| Stale | n/a | Dim at 3 s, removed at 5 s | |

### 4.6 Key decisions
1. One neutral peer interface; the lab backend is the dev/test reference and the real game is an opt-in owner-only backend, so the pipeline can be built and verified before the decoder is proven.
2. Lab backend is the verifiable reference: it exercises the real overlay path with true packet bytes, not a fake dict.
3. No guessing: absent fields stay `None` and render as `?` or fallback glyphs.
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
| Name or health absent (real game, if not decodable) | `Player #id`, `HP ?`. |
| No heading (all v1 sources) | Dot marker; no arrow, no inferred direction. |
| Self id appears in packets | Dropped at the source (REQ-011). Same coordinates, different id: shown. |
| Own handle learned late (real game) | Provisional peer is purged once own handle locks. |
| Same id, packet burst | Latest wins; `version` increments once per snapshot change. |
| Peer silent 3 to 5 s | Dimmed, then removed at 5 s (REQ-013); reappearing id resets age without a position jump. |
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
- □ TRD-001 Resolve Q3 (record format); Q2 was resolved in rev 4; Q1 is answered by TRD-050 (1h)

Phase 1, peer source (parallel with Phase 2 after TRD-001)
- □ TRD-010 `peers.py`: `PeerEntity`, `PeerSnapshot`, `PeerStore` (TTL, version, self-id drop) (3h) [TRD-001]
- □ TRD-011 `from_record` + `LabPeerSource` over `PayloadSniffer` (socket), status text (3h) [TRD-010]
- □ TRD-012 Canonicalise the record parser per Q3 (adjust `telemetry`/`probe` or document) (1h) [TRD-001]
- □ TRD-013 `SyntheticPeers` incl. a self-id entry (replaces `DemoFeed` demo) (2h) [TRD-010]

Phase 2, geo logic
- □ TRD-020 `Entity.heading`; `EntitySmoother` TTL/age/heading; `items_aged()` (3h) [TRD-001]
- □ TRD-021 `visible_entities`, `is_self`, `nearby_3d` self-id extension (3h) [TRD-020]
- □ TRD-022 `edge_anchor` (1h)
- □ TRD-023 Settings: `peer_radius_m` (+migration), `peer_self_ids`, `peer_source`, `show_vitals_detail` (1h)

Phase 3, rendering and app
- □ TRD-030 `ENTITY_STYLE`; `_draw_entity_marker` dot/arrow/dim (3h) [TRD-020]
- □ TRD-031 `_draw_entities` on `visible_entities`; peers rename; `_draw_peer_status`; optional detail line (2h) [TRD-021, TRD-030, TRD-010]
- □ TRD-032 `_draw_edge_indicators` (2h) [TRD-022, TRD-031]
- □ TRD-033 Peers settings dialog replacing Feed dialog; hide/disable `show_ai`; status text (2h) [TRD-023]

Phase 4, wiring
- □ TRD-040 `__main__`: `--lab-peers`, `--replay` -> `SyntheticPeers`, real-game peer option wired to `GamePeerSource` when available, start/stop peers, drop feed wiring (2h) [TRD-011, TRD-013, TRD-031]
- □ TRD-041 Docs: `docs/PEERS.md` (sources, record layout, units, self-id, TTL, limits) replacing the deleted SERVER_FEED.md idea (2h) [TRD-011]

Phase 5, real-game backend (owner-only, opt-in; start TRD-050 immediately, it does not depend on Phase 1 to 4)
- ✓ TRD-050 Technical feasibility spike (time-box 8h): from existing fixtures and captured pcaps, can inbound Iris batches (per `net_ref_handle`) yield other actors' position, name and vitals, and with which ids? Output: a written field list (decodable, partial, or not decodable) that scopes TRD-051; no production code. Answers Q1.
- ✓ TRD-051 `GamePeerSource` (position only; see Findings) (Backend G): decode other actors' fields proven by TRD-050 into `PeerEntity`, drop own handle, provisional-purge logic, status text, fixture-based tests (estimate set after TRD-050, placeholder 12h) [TRD-010, TRD-050]

Phase 6, tests and removal
- □ TRD-060 Tests T1 to T15 (6h) [TRD-010..033, TRD-040]
- □ TRD-070 Feed retirement per section 9: delete `feed.py`, `tools/mock_feed.py`, `test_feed.py`, `test_feed_ui.py` (port still-useful cases), strip settings/CLI/UI. BLOCKED until the user commits or discards the uncommitted `feed.py`/`test_feed.py` edits (3h) [TRD-040, TRD-060]
- □ TRD-080 Full suite, `--replay` manual check, lab loopback manual check, code review, perf check (2h) [all]

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
- Q2 (scope): RESOLVED in rev 4. Lab loopback backend = dev/testing (behind `--lab-peers`). Real-game backend = personal tool for the repo owner only, opt-in via config/flag, not distributed, not user-facing, not the default.
- Q3 (format): Canonical lab record: signed (`<i3fh`, telemetry/probe) or unsigned (`<IfffH`, sniffer)? And does the int16 mean health 0..100 or a status code? Proposed: treat as health 0..100 as `nearby.from_lab` does; confirm.
- Q4 (heading): Infer heading from successive positions when absent? Proposed: no in v1.
- Q5 (AI kind): Packets give no AI/player distinction. Hide `show_ai`? Proposed: yes, disable it.
- Q6 (despawn): Should Iris destruction records (currently skipped by `parse_iris_packet`) feed explicit despawns for the real-game backend? Proposed: later, once TRD-050 shows they are decodable.
- Q7 (self for a second client): Is `peer_self_ids` enough, or should the app auto-learn own handle only?
- Q8 (radius/label detail): Default `peer_radius_m` stays 1000 m; show only HP by default with a detail toggle (proposed)?
- Q9 (feed.py deletion): Will the user commit or discard the uncommitted `feed.py`/`test_feed.py` edits so TRD-070 can proceed?


## 11. TRD-050 Findings (spike on the repo's recorded inbound packets) and TRD-051 result

Method: no new capture. Every packet in `tests/fixtures/recorded_payloads.json` was run through the existing `decode.parse_iris_packet` (the envelope walk the own-vitals decoder uses) and every non-own batch was inspected (shared bit prefixes, size classes, scans for the repo's own quantised-vector and GAS-attribute layouts). The own-pawn pipeline is unchanged.

How the local player is obtained today, and what is reused:
- Position/yaw: outbound move packets, `decode_movement` + `MovementTracker`. Not applicable to others (the client never sends other actors' moves).
- Vitals: inbound replication. `parse_iris_packet` returns one `IrisBatch` per replicated object keyed by `net_ref_handle`; `VitalsTracker` looks for the survival heartbeat in every batch and picks the own handle (2 consistent hits). All batches in the fixtures (25 per packet) belong to other objects, so the stream is already decoded per actor with handles. TRD-051 reuses exactly this: the pipeline passes each inbound datagram plus `VitalsTracker.self_handle` to the peer source.

| Field of another actor | Result | Where / evidence |
|---|---|---|
| Handle (identity) | Decodable | `IrisBatch.net_ref_handle`. Same actor keeps its handle across packets (e.g. 206308 in two TrailingAttributes packets moved 1.3 m). |
| Position x, y, z | Decodable | Batches of 155 and 158 bits: bits 0..32 fixed state header `0x20E822A8` (identical in all 55 recorded batches of 28 handles, 8 packets); bits 33..39 `FVector_NetQuantize100` header (`64 | comp`); then 3 x `comp` signed raw (cm x 100), the same quantisation as the own outbound move; then 37 trailing bits. Size is exactly `77 + 3*comp` (155 -> 26 bits, 158 -> 27 bits; 27 only when a component exceeds 335 544 cm). All 55 decode canonically to plausible island coordinates, z about 25 000 to 38 000 cm. Code: `decode.decode_actor_location(s)`. |
| Destruction | Partially decodable | The envelope walk already parses destruction records and discarded them. `IrisPacket.destroyed_handles` now exposes the first packed u64 of each record. One recorded record (`DetachedDamagedAttributes`: handle 86078, in the same numeric range as live handles). NOT validated against a recorded despawn (no fixture shows that handle live and then destroyed), so it is applied only to handles already tracked; TTL stays the fallback. Bunch close is channel level (Iris uses one channel), so it says nothing per actor. |
| Name | NOT decodable | No string/ASCII payload in any other-actor batch; names/classes would arrive as object exports or net tokens, which the walk skips (`has_exports` is False in every fixture). |
| Class (player vs AI vs animal) | NOT decodable | Same reason. The 155/158 location state is shared by all such actors, so entries may include AI/animals. They are all shown as `player` named `Actor #<handle>`. |
| Health / growth / stamina / hunger / thirst | NOT decodable | Other batches are 16 to 516 bits. Some 364-516 bit batches contain isolated GAS slots (float, flag, repeated float; e.g. handle 80984 bits 289 and 355) but no fixture ties a slot to a named attribute for a non-own actor, and the heartbeat triple used for self never occurs in them. Left None (`?`). |
| Heading | NOT decodable | The 37 trailing bits are likely rotation/velocity, but nothing in the repo or fixtures confirms a layout. Left None (dot glyph). |

Self exclusion: the own pawn has no 155/158 location batch in any fixture, so self normally never appears; still, exactly as designed in 4.4, a peer whose handle equals `VitalsTracker.self_handle` is dropped and a provisional peer is purged when the lock arrives. Respawn (new handle) replaces the exclusion; server change / pipeline reset clears the store (handles are per connection).

What a new capture would add (needed for name/class/vitals/heading/despawn validation): one pcap of your own session (Wireshark/Npcap on the game's UDP port, the format `--replay FILE.pcap` reads), about 60 s, recorded while (1) standing still next to ONE other known player whose character name and approximate health/growth you note, then (2) that player walking away, dying or logging off while you keep capturing (despawn), then (3) a second capture with that player alone in range of your client but with you not moving. Provide the pcap plus the player's name/species/handles you can read in-game; with that the 37-bit tail (heading), the export section (class/name) and the attribute slots can be matched against ground truth. Without it those fields stay `None`.

Implementation (TRD-051): `decode.py` (additive: `destroyed_handles`, `ActorLocation`, `decode_actor_location(s)`), `tracking.VitalsTracker.self_handle` (read-only property), `capture.TelemetryPipeline.add_observer/remove_observer` (passive taps, exceptions swallowed; own-player decoding unchanged), `peers.GamePeerSource` + `PeerStore.remove/clear`, factory `pipeline=` argument, tests in `tests/test_game_peers.py`.
