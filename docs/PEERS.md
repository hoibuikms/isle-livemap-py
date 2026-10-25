# Other players on the minimap (peers)

Other players come only from local data (the game's own traffic, captured passively). There is no server feed, URL, token or HTTP client.
Everything is passive: the app only reads packets, it never injects, sends or reads game memory.

## Sources

There is no source choice for the user. Nearby players always come from the game: the live capture that provides the current player also feeds `GamePeerSource` (decision: TRD rev 6; this is the owner's personal tool).
Start the app with no flags and nearby players appear next to the current player once the server is learned.

| Source | Purpose | Selected by | Status text (examples) |
|---|---|---|---|
| game | the only user-facing source | default; also `--replay FILE.pcap` | `game: waiting for server traffic`, `game: waiting for actor locations (N pkts, own handle not locked yet)`, `game: N actors (K with species; creation C, exports E, destroyed D; heading from movement; growth, hunger and thirst not available; own handle H)`, `game: unavailable (...)` |
| lab | developer/test only | `--lab-peers [PORT]` | `lab: listening 127.0.0.1:7777 (N peers, ...)`, `lab: unavailable (...)` |
| synthetic | developer/test only | bare `--replay` | `synthetic peers (N)` |

A failing or absent pipeline never crashes the app: the status line says why (`game: unavailable (...)`) and the own-player minimap keeps working.
Bare `--replay` shows synthetic demo peers (one entry deliberately carries a self id, to exercise self-exclusion).

## Flags

| Flag | Effect |
|---|---|
| `--replay [FILE.pcap]` | replay a synthetic walker (with synthetic demo peers) or a pcap (peers decoded by the game path) |
| `--no-record` | live capture records a session automatically by default; this turns it off (see "Automatic session logs") |
| `--record FILE.pcap` | manual single-file recording instead of the automatic session files (stops at the size cap) |
| `--record-max-mb MB` | size cap per pcap file (default 200): `--record` stops there, automatic sessions roll to a new file |
| `--lab-peers [PORT]` | dev/test: use lab records on loopback (default port 7777) instead of the game |

`--game-peers` was removed (it is now the default). Passing it is an argparse error rather than a silent no-op, so stale launchers are noticed.

## Settings (`settings.json`)

Tray menu items: "Show other players", "Nearby radius...", "Vitals detail on labels". There is no peers dialog.

| Key | Default | Meaning |
|---|---|---|
| `peer_radius_m` | `1000` | radar radius in metres (clamped 50..20000); tray "Nearby radius...". Old files with `feed_radius_m` are migrated to this on load; other legacy `feed_*` keys and the obsolete `peer_source` key are ignored. |
| `peer_self_ids` | `[]` | settings-file only: extra ids that are "me" and never drawn (for example a second client); read at start |
| `show_players` | `true` | draw other players; tray toggle |
| `show_vitals_detail` | `false` | extra label line with the vitals that are known; tray toggle |
| `show_ai` | `true` | settings-file only: draw server-controlled animals (fish, kind `ai`, orange diamond) from the game source |
| `record_sessions` | `true` | settings-file only: automatic session pcap + log for live capture; `false` = same as `--no-record` |
| `record_dir` | `""` | settings-file only: sessions directory; empty = `<data dir>/logs/sessions` |
| `record_keep_sessions` | `10` | settings-file only: newest sessions kept (1..1000) |
| `record_total_cap_mb` | `1024` | settings-file only: total size cap of the sessions directory (10..) |

## Behaviour common to all sources

- Self is excluded by identity, never by position. Lab: id 0 is the lab's "me". Game: the entity whose Iris handle equals the own handle (learned from the survival heartbeat) is dropped, and a provisional peer with that handle is purged when the lock arrives. `peer_self_ids` adds more.
- Silence timeouts are a property of the source. Lab and synthetic peers are dimmed after 3 s without an update and removed after 5 s. Game peers are dimmed after 8 s (`GAME_PEER_STALE_S`) and, without a destruction record, removed after 180 s for creatures (`GamePeerSource.TTL_S`: captured resting pawns went silent for 100-300 s and came back) or 30 s for server animals (`TTL_AI_S`), because Iris replicates changes only: an actor that stands still sends nothing, so silence is not absence. The game source also removes a tracked handle when the packet carries a destruction record for it (the dot is gone 1.5 s later: `GamePeerSource.GONE_GRACE_S`, applied by `EntitySmoother.prune` every frame; it used to stay dimmed for 30 s) (431 records in the recorded capture, 431 of 431 handles seen in that capture, 430 with a creation record). The 8 s / 30 s values are a judgement call, not a measured number.
- The game server is the destination of the first outbound datagram. A different destination only replaces it after 5 outbound datagrams in a row while the old server has been silent for 1 s (a real server change). A single datagram from the game's second socket to another host (a 1300-byte latency probe was seen once per session) is ignored: it used to count as a server change and wiped the identity tables, the peers and the own-vitals lock, twice within 30 ms (once for the probe, once when the real server was "learned" again).
- Missing fields are `None` and are simply not printed: the minimap label never shows `?`, `HP ?` or `G ?`. The label is the player name with the species in brackets; a creature without a player name is labelled by its species alone (`Deinosuchus`, `Longear`; a `~` marks a species taken from the saved class cache) and only a creature with no class at all shows `Unknown #<handle>`. Second line: the known vitals, distance, compass bearing and height difference, e.g. `Rainbow Fish` / `666 m N` or `<name> (Pteranodon)` / `HP 86% 787 m NNE +60m`. Labels go to players first, then dinosaurs, then server animals (fish: at most 3 labels, none in a crowd of 20+); they avoid the status lines, the dots of players and the edge of the round minimap. The Nearby table keeps `-` / `?` only because its columns need a cell. Nothing is guessed. The heading is the direction of travel measured from successive positions (`GamePeerSource._heading`), not a replicated value: a creature that has not moved yet is a dot, one that has moved is an arrow.
- Peers in range but outside the view get an edge indicator on the minimap circle.

## Lab record (development only)

Little-endian `<IfffH` + UTF-8 name: id (uint32, shown as signed), x, y, z (float32, Y-up metres), status (uint16, read as health 0..100), name. The layout is an assumption used by `labsim`, not the game's protocol. Y-up metres are converted once to geo centimetres (lab z becomes geo y, lab y becomes height).

## What the game backend decodes

Evidence and method: "Identity, species and positions" (section 13) of `docs/trd/other-players-on-minimap.md`, measured on a 193 s capture that started before the server was joined.

| Field | Game source |
|---|---|
| Identity (Iris `net_ref_handle`) | decoded |
| Class | decoded: creation record -> archetype handle -> asset path from the export sections (`/Game/.../BP_X.Default__BP_X_C`) -> `species.describe`. 5419 creation records, 126 classes in the capture. A pawn archetype whose path is never exported (6 pteranodon pawns: archetype 2221) gets its species from the dinosaur assets named by at least two of its creation records, none naming another species; the status then still says `Pteranodon`, the path stays unknown |
| `kind` | `dino` (dinosaur class), `player` (dinosaur whose creation record references a PlayerState), `ai` (fish), `unknown` (no creation record captured). World objects (plants, fruit, gore, items, PlayerStates, level actors; 5307 of 5419 creations) are never drawn |
| Species | `Pteranodon`, `Gallimimus`, `Longear`, `Rainbow Fish` in the capture. Any other class is shown under its cleaned asset name (`BP_Foo_C` -> `Foo`), never invented. The mapping is `species.py` (`CLASS_RULES`, `DISPLAY_NAMES`) |
| Player name | decoded for pawns whose creation record references their PlayerState (name from the PlayerState creation record: 382 of 388 PlayerStates in the capture). A creature whose PlayerState was created before the capture, or an AI animal, has no name |
| Position x, y, z | decoded: creation position (16 x the raw/100 value, see TRD 13.4), two fixed state layouts (BP_Longear, BP_Fish_RainbowFish: x and y only), and layouts learned from creation anchor + continuity (pteranodon pawns). Most pawn state batches carry no position at all |
| Health, stamina | decoded for other creatures from the full attribute block (9 slots: health, maxHealth, stamina, maxStamina, then maxHealth repeated three times and half of it twice; or 8 slots without maxStamina when that did not change), then **followed** between blocks: a pawn that regenerates sends the pair health + stamina (66 bits apart) in small state batches every second or so (`decode.PawnVitalsTracker`). A slot is the health when it is close to the last health (or sits at an offset confirmed twice for that batch header, which then also takes a bite). A percentage needs the maxima of the block, so nothing is shown for a pawn that has not sent one yet (in the hhh capture 26 % of the dino-seconds had HP, 17 % had stamina: the block comes rarely and the 8-slot form has no maxStamina). Label: `HP 86% ST 33% 408 m E -31m` |
| Growth, hunger, thirst | NOT decoded, and probably not replicated to other clients: the own pawn's survival heartbeat (hunger, thirst, hunger repeated) was found 283 times in the own batches of one capture and not once in the batches of other pawns (their repeats were maxHealth blocks); no slot next to the health slot of another pawn holds a growth fraction. maxHealth of a juvenile grows (72.75 -> 73.76 in 20 s) but without the adult maximum of the species that is not a percentage |
| Despawn | destruction records (layout fixed, see TRD 13.3); TTL is the fallback |
| Heading | derived from movement: the bearing from an anchor to the first position at least 1 m away (jumps over 200 m ignored), kept while the creature stands still; the dot becomes an arrow |

A creature whose creation record was missed has no anchor. `ActorLocationTracker.bootstrap` places it from two consecutive state batches: a canonical 22+ bit vector within 1 km of the local player in both, the second continuing one of the first batch's candidates at creature speed. A vector within 15 m (+20 m/s) of the last position is accepted at its first sighting (the layout still needs two sightings to be trusted). In the hhh captures this placed 4 more creatures and raised the fixes of walking dinosaurs from 33 to 42 of 89 batches (a resting creature sends only health and stamina, so it has no position to decode).

The overlay line (`GamePeerSource.headline`) keeps a headline for `HEADLINE_HOLD_S` = 12 s before it changes ("Started late" / "Rejoin server: 1 hidden" used to alternate every second or two). An unclassified actor becomes `ai` after four state batches of the animal size (155/158 bits), not eight.

Not all creatures in view are positioned: the capture shows creatures (Deinosuchus, Triceratops, more pteranodons) whose pawn was never created in the capture and whose state does not use a verified layout; they have vitals and species evidence (animation assets) but no map dot.

## Update rate and lag

The own marker follows the client's own outbound move packets (no network hop). Other actors can only be as fresh as the server's replication of them: one-way latency plus the server's send interval for that actor, which Iris decides per actor (priority, relevance, only when state changed). The recorded fixtures carry no packet timestamps, so that rate cannot be measured in seconds from them. What the code controls:

- Every decoded location is written to the store immediately (no buffering, decode cost 0.04..3 ms per packet) and the overlay reads the store every frame (16 ms timer), the same tick that moves the own marker.
- Between samples the dot is dead-reckoned (`geo.EntitySmoother`, `DR_*`): the target advances along the velocity of the last two distinct samples, for at most one measured interval and never more than 1 s; a position re-sent unchanged, a gap over 3 s, a teleport or an implausible speed (over 60 m/s) switches it off. Position only: it never produces a heading.
- The dot is **feed-forward**: it moves at the measured velocity all the time and the error to the newest sample is blended in over `DR_CORR_TAU_S` (0.2 s). The earlier version chased the target with a 0.09 s lag filter; with samples 0.7-1 s apart it stood still at the end of each horizon and jumped when the next sample came (6-20 visible jumps a minute). A sample that is late by up to `DR_FF_GRACE_S` (0.3 s) no longer stops the dot. The own marker still uses the 0.09 s filter with the lead (its samples come every ~0.1 s).
- A reused handle (`PeerEntity.born` changes) is a different creature: the dot starts at its position instead of gliding there from the old one.

What stays inherent: the first sample after a silence, network latency and the server's send interval.

## Why the own-player vitals do not transfer to other actors

The own decoder never reads an attribute id from the wire. It finds 66-bit slots (float, set flag bit, repeated float; 66 bits apart) and names them by position: the survival heartbeat (hunger, thirst, optionally one slot that changed, hunger repeated), then the last two slots of the same batch (health, stamina), or fixed offsets inside the 1380..1535 bit full frames. It locks onto one handle only after two consistent heartbeats and then checks every value against the maxima it learned.

Other creatures send no heartbeat triple and no full frame, but the same slots appear as a 9-slot block (health, maxHealth, stamina, maxStamina, maxHealth, maxHealth/2, maxHealth/2, maxHealth, maxHealth). The repeated maxima make an accidental match impossible and the values are continuous over time (a pteranodon: health 78.4, 79.2, 79.9, ... 85.8 % over 160 s while its max health grows 14.7 -> 20.7). So health and stamina percentages are decoded for others. Growth is not: its slot (the one before health) is only present when it changed.

The heartbeat of the own pawn changes form during play: from the moment stamina starts changing (gliding), the batch holds hunger, thirst, STAMINA, hunger repeated. A match for the triple only lost the lock 12 s later and could never regain it (the "own vitals froze" of the 21:20 session); `_find_heartbeat` now accepts up to two slots between thirst and the repeat. At 162.2 s (stamina 0) the spacing changed again and the matcher lost it; in the recorded session no heartbeat-sized batch follows and the character is exported as dead 10 s later, so that last freeze is the game.

## When identity is sent (why the capture must start before joining)

Class, name and species are not part of the steady-state updates. The server sends them once, in the actor creation record (plus the object exports that name its class), when an actor first becomes relevant to your client. A capture that starts while the neighbour is already in view cannot contain that data, however long it runs. The app therefore records from launch (next section).

The game status line shows what the capture contains so far:
`game: N actors (K with species; creation C, exports E, destroyed D; ...)`.
- `creation C`: batches whose first bits say "actor creation".
- `exports E`: batches followed by an export section (object paths, net-token strings).
- `destroyed D`: destruction records in the envelope (an actor leaving relevance).

`creation 0, exports 0` means the capture holds no join data and species/name cannot be recovered. The older fixtures show `creation 1, exports 2, destroyed 1` (one creation record in the one packet that used to be cut short by the destruction-record bug).

## Automatic session logs

Every live launch (not `--replay`, not `--lab-peers`) writes two files without any flag, from the moment the app starts, so the join of a server is always on disk:

- `session_YYYYmmdd_HHMMSS.pcap`: the game's UDP datagrams (both directions), format below.
- `session_YYYYmmdd_HHMMSS.log`: a concise plain-text event log: app start/stop, server learned, own handle locked/changed, peer-source status transitions, a counters line every ~10 s (packets in/out, creation / exports / destroyed, actors tracked, distinct handles, and what is on the map by kind with the position/vitals counters), the first creation record (with its handle), every destruction record (with its handle), recorder start / roll / stop / failure, retention deletions, and decoder exceptions with traceback (first 5 per site). No per-packet lines. It rotates at 5 MB (`.log.1`).

Where: `<data dir>/logs/sessions/`. On Windows that is `%APPDATA%\isle-livemap\logs\sessions\` (typically `C:\Users\<you>\AppData\Roaming\isle-livemap\logs\sessions`; paste `%APPDATA%\isle-livemap\logs\sessions` into Explorer). Elsewhere `~/.isle-livemap/logs/sessions/`. `--data-dir D` moves it to `D/logs/sessions`; the `record_dir` setting overrides it completely.

Off switch: `--no-record` or `"record_sessions": false`. An explicit `--record FILE` replaces the automatic files for that run (single file, no session log).

Retention (at startup and at every roll): keep the newest 10 sessions (`record_keep_sessions`), keep the directory under 1 GB (`record_total_cap_mb`), oldest files first. The running session is never deleted. Only files named `session_<stamp>[_partN].pcap|.log|.log.1` are ever removed; anything else in that folder is left alone.

Size cap per pcap file: 200 MB (`--record-max-mb`). At the cap the recorder closes the file and continues in `session_<stamp>_part2.pcap`, `_part3`, ... so a long session keeps its NEWEST data and retention removes the oldest parts first. The join-time creation records are in part 1 (the first ~200 MB of the session), so keep that file if you want to decode names.

The status line under the minimap (and the tray tooltip) shows it working: `recording session_20261022_101500.pcap: N pkts, M MB, creation records seen: K, exports E`. It turns orange on `FAILED` (cannot create or write; the minimap keeps working) or `STOPPED` (manual `--record` hit its cap).

**Privacy:** the pcaps contain your IP address, the server's address and the raw game traffic, and `logs/` is in `.gitignore` so they are never committed by accident. Share them deliberately.

## Starting the app after joining

The server sends the class and player-name tables, and the creation record (with the first position) of every creature in range, once, when you join. An app that starts later cannot recover them from the stream:

- **Nothing is hidden by the animal rule.** Every actor classed `ai` without a creation record repeats a 155/158 bit state in at least 99 % of its batches (checked on five captures: no dinosaur, player or world object matched).
- **Still creatures never get a position.** The game only sends changed fields, so a creature that has not moved since you joined sends no position batch. Three such Deinosuchus were replicated for over 200 s in the `as` captures with no position (no `0xc0000348` / `0xc0100348` batch at all); no bit scan can find a value that is not in the stream. The app counts them (`GamePeerSource.hidden_creatures`: 30+ state batches, at least 80 % of them 60-400 bits, under 10 % animal sized, recent, not an earlier own pawn) and says so.
- **The overlay line is elided to about 30 characters**, so `GamePeerSource.status()` now starts with a short headline and appends the detail: `Started late: rejoin to see all` (tables missed; one constant message, because the old `Rejoin server: N hidden` / `to identify` variants flipped every few seconds with the hidden count), `N creatures: no position yet` (tables seen), `No other players nearby` (tables seen, no dinosaur or player on the map). "Rejoin" means leaving the server and joining again with the app already running.
- **Own handle changes** (`own handle changed: A -> B`) are a death and respawn: the heartbeat lock lets go of the silent pawn and takes the new one. The old pawn keeps streaming as a body for a while; it is remembered (`_past_own`), never counted as another creature and never put on the map (it used to appear as a dot labelled with your own species).
- **Species without tables** come from `class_cache.json` (learned on this machine) and the shipped `isle_livemap/assets/class_seed.json` (51 protocol ids from captures that saw the tables; `python tools/build_class_seed.py PCAP...` rebuilds it; asset paths only). A table that disagrees with a seeded or learned id blocks it for the process. Species from either show with a `~`.

Tests: use the project environment, `.venv/bin/python -m pytest` (Qt and Pillow are installed there; the system `python3` skips the 10 tests that need them).

## Stability budgets (stuck, lag, flicker) and how they are measured

`tools/stability_replay.py` replays recorded sessions through the real path (pcap -> `TelemetryPipeline` -> `GamePeerSource` -> `MapView.tick` at 60 fps -> `MapView.grab` at 5 Hz, offscreen Qt) with `time.monotonic` replaced by a simulated clock that follows the datagram times, so the numbers do not depend on the machine:

    .venv/bin/python -I tools/stability_replay.py DIR/session_*.pcap --check      # exit 1 on a violated budget
    ISLE_CAPTURES=DIR .venv/bin/python -m pytest tests/test_stability_replay.py   # the same, as tests (skipped without ISLE_CAPTURES)

Budgets (`BUDGETS`, `check_budgets`) and the values measured on seven recorded sessions (`sadsad`, `qq`, `as`, `hhh`; 26-562 s each), before -> after the stability work:

| What | Budget | Before | After |
|---|---|---|---|
| Dot of a destroyed creature stays | max 2 s | 30 s (101 of 101 destructions) | 1.6 s (131 of 131) |
| Ghost dot-seconds (shown, not in the store), all sessions | - | 2 600 s | 150 s (= the grace) |
| Dot frozen while the server shows it moving | 1 s | 0 s | 0-0.1 s |
| Dot beyond what dead reckoning allows, longest run | 3 s | n/a | 0.3 s |
| Own marker frozen while it moves | 0.5 s | 0 s | 0 s |
| Visible jumps of a dot per minute | 10 | 0-20 | 0-7 |
| Dot error vs the path, p50 (the dot that stays on the newest sample is 100 %) | 80 % | - | 52-84 % |
| Kind / name changes per actor | no actor twice | up to 37 per minute (unknown -> ai) | up to 0.1 per minute, none twice |
| Headline changes | 1.2 per minute | 2-13 per session | 1-5 per session (the 364 s session with a table dump: 5) |
| Label side changes (A -> B -> A within 2 s counts as a flip-flop) | 0 flip-flops, 3 hops per minute | up to 4.1 hops per minute | 0 flip-flops, up to 1.9 hops |
| Label on/off toggles per minute | 1 | up to 0.7 | up to 0.2 |
| Time to first dot (p50 / worst p95) | 0.5 s / 5 s | 0.03 s / 3.0 s | unchanged |
| Decode time per datagram (worst p99 / max) | 5 ms / 50 ms | 3.4 / 12.8 ms | 3.3 / 15.2 ms |
| Backlog of one decode worker (max wait / max queue) | 250 ms / 200 | 88 ms / 30 | 87 ms / 30 (the 256 KB table dump) |
| Exceptions in tick / decoder / pipeline | 0 | 0 | 0 |
| `tick()` p95 / paint p95 (worst session) | 2 ms / 16 ms | 0.1 / 6.1 ms | 0.12 / 6.0 ms |

Time to first dot is measured from the later of the creature's first record and the first own position fix; creatures whose creation record was missed and that do not move have no position to draw (the "N creatures: no position yet" headline) and are outside the budget.

What was found and fixed:

- **Ghosts.** A creature the server destroyed stayed on the map, dimmed, for the remaining 30 s of the smoother's TTL (the source had already removed it; the smoother kept its own copy). Now `EntitySmoother.missing_grace_s` (1.5 s for the game feed) removes it, and `prune()` runs every frame, because `update()` only runs when the source changes.
- **Resting creatures vanishing.** Dinosaurs and players were dropped after 30 s without a report although captured pawns are silent for 100-300 s while resting. They now wait for the destruction record (180 s safety limit); fish keep 30 s.
- **Stop-and-go and jumps.** See "Update rate and lag".
- **Flicker.** New fish were drawn as `Unknown #id` creatures for 2-3 s before the animal rule turned them into `ai` (with a label and a different colour); the rule now decides from the first batch and keeps its verdict (it is revoked only after 8 batches of which less than 60 % fit). The headline is one constant message while the tables are missing. A label tries the side it had last frame first, with `SLOT_SLACK_PX` of tolerance, and a peer that has a label keeps it over a newcomer at less than 20 % smaller distance.
- **A frozen frame.** `MapView.tick()` ended early when `peers.snapshot()` raised (a "dictionary changed size during iteration" from the UI thread reading decoder-thread tables was possible), so nothing was repainted again. `tick()` now always schedules the repaint, keeps the last good snapshot, and `paintEvent` isolates each layer and always ends the painter; `MapView.errors` / `last_error` count them. The status text iterates copies of the decoder's dictionaries. A datagram that makes the decoder raise is counted (`GamePeerSource.decode_errors`, "Decode errors: see log" headline, session summary) instead of looking like "nobody is nearby".
- **Own pawn after respawn** is excluded for good (see above); after a pipeline reset (new server, game closed) the own marker drops its old fix and says "Waiting for game" instead of staying parked.

### Live capture robustness (cannot be run on macOS; unit-tested with fakes in `tests/test_capture_robustness.py`)

- The supervisor loop (`LiveCapture._supervise` / `_step`) no longer ends for good when a step raises: it logs, shows `capture error, retrying: ...` in the status and tries again after 1-5 s. The decode worker is restarted if its thread dies (`worker restarts` in the session counters).
- No UDP packet at all for 10 s while the game owns a socket (interface switched: Wi-Fi to cable, VPN up or down) reopens the sniffers on every interface (at most every 30 s).
- `PreCaptureBuffer.set_ports` hands the buffered packets over **outside** its lock. Handing over 4 096 real packets takes about 92 ms; it used to hold the sniffer thread for all of it, now `offer()` from the sniffer thread waits under 0.01 ms meanwhile. Arrival order is kept because the new ports stay "unowned" (their packets are still held, and handed over too) until the hand-over has caught up.
- Saving the class cache runs on the decode thread at most every 30 s and only when something was learned: 20-44 ms at the 5 000-entry maximum, 1-3 ms for the few hundred entries of a real cache.
- The session log's counters line now ends its first part with `worker queue max=N slow decodes=N capture errors=N worker restarts=N` (a slow decode is over 50 ms).

### To check live on Windows (what the replays cannot show)

1. **Start order.** Start the app, wait for the status `waiting for server traffic`, then join. The log's first lines should show `sniffing before the game starts` and, after joining, a `first creation record` within the first seconds. Check `worker queue max` stays below ~100 and `slow decodes` stays 0 or tiny in the `counters:` lines.
2. **Dots.** Stand still near a known creature for a minute: the dot must not drift (the dead-reckoned dot stops within ~1.5 s after it stops). Watch a walking creature: motion should look continuous, with no stop-and-go every ~0.8 s.
3. **Destruction.** Walk away until a nearby creature leaves relevance: its dot and label should disappear about 1.5 s after the `destruction record` line in the log, not 30 s later.
4. **Own marker.** Die and respawn: no dot with your own species stays where you died; leave the server to the menu: the minimap shows `Waiting for game`.
5. **Interface change.** With the game running, switch Wi-Fi to cable (or toggle a VPN): within ~10-15 s the log shows `no UDP seen for ... reopening on all interfaces` and the minimap resumes.
6. **Kill the capture.** Nothing to do by hand; a `supervisor step failed` or `decode worker died` line in the log is the signal that a failure was survived. Send the log if you see one.

## Recording steps for the owner

Just play. Start the app before joining a server (it records from launch), join, stay put for the first 30 s, and quit from the tray when done (this flushes the file; it is also flushed about once a second). Optionally note, for one neighbour, the character name, species and roughly when they appeared and left.

Afterwards:

1. Look at the newest session: `python -I tools/inspect_capture.py --latest` (or pass a file or a directory: the newest `session_*.pcap` in it is used). `creation batches` above 0 means the join data was captured.
2. Send the `session_<stamp>.pcap` and `session_<stamp>.log` of that session (plus `_partN.pcap` files if any) from the folder above, and your notes.

If the file cannot be opened or written the status line says `recording FAILED (...)`; the minimap and nearby players keep working.

The pcap is a standard classic pcap (link type 101 = raw IPv4; open it in Wireshark). It holds only the datagrams of the game's own UDP ports, with rebuilt IPv4/UDP headers (no Ethernet layer) and wall-clock arrival times. The same file can be replayed through the full app with `python -m isle_livemap --replay FILE.pcap` (add `--local-port N` if the local port is not auto-detected; it is taken from the private-address side of the busiest flow).

`tools/inspect_capture.py [FILE|DIR] [--latest] [--first N] [--max-bytes B] [--local-port P] [--names] [--top N]` runs offline (no Qt, no network) and prints packet counts, duration, batch kinds, creation / export / destruction counts, distinct handles, the class histogram of the creation records (class path, kind, species), the creatures by species, the number of player names decoded (`--names` lists them: they are other people's names), the creatures with a position and how many destruction records match a creation seen, then a raw hex dump of the first N creation and export-bearing batches.

## What would still help

The join capture settled class, species, names, destruction records and the position of fish and pteranodons. Open: land dinosaurs near you (none came into relevance in the session recorded), growth, hunger and thirst of others (the 9-slot attribute block is only sent at creation: none of the ~1800 state batches of the two players in a recorded session reaches its 593 bits), the replicated heading, and ground truth for names (the PlayerState -> pawn reference is structural, not confirmed by a player telling you who stood next to them). A session in which you stand still next to ONE known player of a known species for 60 s, with the character name and species written down, would confirm the name link and add a land dinosaur's position layout.
