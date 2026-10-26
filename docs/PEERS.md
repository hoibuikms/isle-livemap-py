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

Tray menu items: "Show other players", "Nearby radius...", "Health / stamina bars". There is no peers dialog.

| Key | Default | Meaning |
|---|---|---|
| `peer_radius_m` | `1000` | radar radius in metres (clamped 50..20000); tray "Nearby radius...". Old files with `feed_radius_m` are migrated to this on load; other legacy `feed_*` keys and the obsolete `peer_source` key are ignored. |
| `peer_self_ids` | `[]` | settings-file only: extra ids that are "me" and never drawn (for example a second client); read at start |
| `show_players` | `true` | draw other players; tray toggle |
| `show_vitals_bars` | `true` | thin health and stamina bars under the arrow of every player and dinosaur (the 60 most important), labelled or not; a value never read is an empty grey track; tray toggle "Health / stamina bars" |
| `show_ai` | `true` | settings-file only: draw server-controlled animals (fish, kind `ai`, muted grey-blue diamond) from the game source |
| `record_sessions` | `true` | settings-file only: automatic session pcap + log for live capture; `false` = same as `--no-record` |
| `record_dir` | `""` | settings-file only: sessions directory; empty = `<data dir>/logs/sessions` |
| `record_keep_sessions` | `10` | settings-file only: newest sessions kept (1..1000) |
| `record_total_cap_mb` | `1024` | settings-file only: total size cap of the sessions directory (10..) |

## Behaviour common to all sources

- Self is excluded by identity, never by position. Lab: id 0 is the lab's "me". Game: the entity whose Iris handle equals the own handle (learned from the survival heartbeat) is dropped, and a provisional peer with that handle is purged when the lock arrives. `peer_self_ids` adds more.
- Silence timeouts are a property of the source. Lab and synthetic peers are dimmed after 3 s without an update and removed after 5 s. Game peers are dimmed after 8 s (`GAME_PEER_STALE_S`) and, without a destruction record, removed after 180 s for creatures (`GamePeerSource.TTL_S`: captured resting pawns went silent for 100-300 s and came back) or 30 s for server animals (`TTL_AI_S`), because Iris replicates changes only: an actor that stands still sends nothing, so silence is not absence. The game source also removes a tracked handle when the packet carries a destruction record for it (the dot is gone 1.5 s later: `GamePeerSource.GONE_GRACE_S`, applied by `EntitySmoother.prune` every frame; it used to stay dimmed for 30 s) (431 records in the recorded capture, 431 of 431 handles seen in that capture, 430 with a creation record). The 8 s / 30 s values are a judgement call, not a measured number.
- The game server is the destination of the first outbound datagram. A different destination only replaces it after 5 outbound datagrams in a row while the old server has been silent for 1 s (a real server change). A single datagram from the game's second socket to another host (a 1300-byte latency probe was seen once per session) is ignored: it used to count as a server change and wiped the identity tables, the peers and the own-vitals lock, twice within 30 ms (once for the probe, once when the real server was "learned" again).
- A label never states a guess as a fact; see "Label confidence" below. A value that was never read is an en dash (`–`), or the line is left out; a species remembered from an earlier session carries `~` and is drawn softer, and growth, hunger and thirst of other creatures are never printed (not even as `?`): they are not replicated to us. The format is the same for everybody (`geo.entity_label_lines`, `geo.vitals_line`):
  * a player: `<name> - <species> - <distance>` / `HP <current>/<max>  ST <current>/<max>` (points, as the game sent them), e.g. `P1 - Deinosuchus - 724 m` / `HP 1419/1650  ST 330/500`
  * a creature whose player name was never sent (the player joined before the app started): `Player ? - <species> - <distance>` / the same second line (`Dino` while the species is not known; every creature that is not a server animal is a player's dinosaur). The placeholder is `geo.NO_NAME`; the Nearby table and the tooltip say `Player ?` too, never `Actor #id`
  * a server animal: `AI - <name> - <distance>`, one line (`AI - Catfish - 120 m`, `AI - Forktail - 1.5 km`, `AI - Animal - 120 m` without a class)
  * a pack of nameless creatures of one species standing together beyond 250 m: `Carnotaurus ×5 - <distance of the nearest>`, one line. A creature with a known name is never part of a pack: its name is not swallowed by a `×N` label.
  The second line holds only what was read (`geo.vitals_line`, with `hp_text` / `st_text` shared by the label, the Nearby table cells and the tooltip text): **the raw values the game sent, in points**, not a percentage (the bars under the arrow show the ratio). Health is `<current>/<max>`; the maximum is `~1650` while it is only the creation record's (a creature grows after it is created: `Entity.max_src == "creation"`; a full attribute block gives the real one: `"block"`), and `–` when no maximum is known (`HP 1500/–`). Stamina is `<current>/<max>`; the maximum is `–` when the block did not carry it (the 8-slot block leaves it out and no creation record has it), so a stamina value can stand without a maximum (`ST 624/–`), and then its bar stays an empty track because there is no ratio. Values below 10 keep one decimal (a hatchling is `HP 1.6/1.6`, not `2/2`). Without a raw value (the lab feed) the percentage is printed instead (`HP 86%`). Unread values are `–`, a creature nothing was read for has no second line, and an old reading gets a coarse age tag (`[2m+]`). Where the numbers come from: current health, health maximum (block), current stamina and stamina maximum (9-slot block only) are float32 attribute values in points (own pawn, same slots: a hatchling `1.6/1.6`, `79.7/107.9`; recorded adults `2275/2620`, `134/163`); the pair health + stamina that follows between blocks is absolute too. In the recorded sessions the stamina value was known for every pawn-second with a health reading, its maximum for 27-100 % of them, and the health maximum came from a full block for 42-100 % (the rest from the creation record, hence the `~`). Compass direction and height are not on the minimap label; they are in the Nearby table and the tooltip text (`geo.entity_summary`). The label keeps the side of the dot it had (even when a dot has moved under it) while it still fits. The Nearby table keeps `-` / `?` because its columns need a cell. The heading is the direction of travel measured from successive positions (`GamePeerSource._heading`), not a replicated value: a creature that has not moved yet points north, one that has moved points the way it went (and keeps that direction while it rests).
- **Priority** (`geo.priority_key`, one number used for the dot budget, the label slots and collisions): creatures before server animals; then the distance tier **within 250 m, within 500 m, within 1000 m, further**; inside a tier players before dinosaurs before unidentified creatures; then the nearest. A peer that had a label last frame ranks as 15 % nearer, so two creatures at almost the same distance do not swap labels. Beyond 500 m the labels per tier on the minimap are 4 (1000 m; a crowd of more than 20 creatures cuts it to 2); fish get at most 3 labels and none in a shoal of 20+. **Inside 500 m there is no cap**: see "Every creature within 500 m" below. Beyond the 1000 m view radius players and dinosaurs up to 2.5 km are pointers on the circle edge.
- **Your own stats** (growth, hunger, thirst, health) are not drawn anywhere on the minimap: there is no panel and no text for the red arrow.
- Peers in range but outside the view get an edge indicator on the minimap circle.

## Label confidence (what the minimap claims, and how sure it is)

`isle_livemap/confidence.py` holds the rules; the minimap, the Nearby table, the session log and `tools/label_report.py` use the same ones.

| Field | Tier | Shown as | Meaning |
|---|---|---|---|
| species | confirmed | `Allosaurus` | this connection named it: the server's export tables (source `table`), or the dinosaur assets its own creation records reference (source `asset`) |
| species | inferred | `Allosaurus~`, head drawn at 75 % opacity | remembered from an earlier session: the saved class cache (source `cache`) or the seed shipped with the app (source `seed`), matched by protocol id |
| species | unknown | `Dino` (`Animal` for a server animal), drawn at 60 % opacity | nothing is known |
| player name | confirmed | `<name> - ...` | the creature references its PlayerState; there is no other source, so a creature without one reads `Player ?` (never `Actor #id`). A name that is known stays: see "Player names" |
| health, stamina | known | `HP 1419/1650  ST 330/500` | a full attribute block was seen (so the maxima are real) and the reading is at most `VITALS_FRESH_S` = 120 s old (the server sends only what changed, so an unchanged value is still true) |
| health, stamina | stale | `HP 1419/1650  ST 330/500 [1m+]` | the same reading, older; the age is coarse (`2m+`, `5m+`, `10m+`) so the text changes at most three times in a creature's life |
| health | none | no second line (`–` for a part that is missing) | never read for this creature (dinosaurs and players only; server animals have no health line) |
| growth, hunger, thirst | not replicated | nothing | never printed, not even `?`; the status line says "growth, hunger and thirst not available" |

Hysteresis (`confidence.TierTracker`, `LabelConfidence`, one per map view): a label that becomes MORE sure (unknown to remembered to confirmed, a first or a fresh health reading) changes at once; one that would say LESS (a species that vanishes, a reading that goes stale) changes only after the weaker evidence has lasted `DWELL_S` = 3 s. A species or a reading that comes and goes inside that time never reaches the label. State is dropped when the creature leaves the map.

Corrections: when a species remembered from the cache is later contradicted by the server's tables (or the cache entry is dropped because the tables disagreed), the label flips to the confirmed species at once and one line is written to the session log, once per creature: `label corrected: handle 123: Allosaurus~ (remembered class) -> Carnotaurus (server tables)`; dropped cache entries are reported as `class cache: N remembered class(es) dropped ...`.

### Checking the labels against the game

The session log has a compact `labels:` line, written every 30 s while creatures are on the map and whenever what the labels claim changes (a species is confirmed, a name is linked, a first health reading arrives; at most one per 5 s):

    labels: creatures=3 fish=12 | 186850 player Deinosuchus[table] "P1" HP86 ST33 724mN | 171102 dino Deinosuchus~[cache] HP? 408mE | 255202 dino ? HP? 1531mW

`<handle> <kind> <species>[source] "<player name>" HP<cur/max or ?> ST<cur/max> (<age>s old, when stale) <distance>m<bearing>`; creatures are nearest first, at most 12 are spelled out, fish are only counted. Names are the game's player names (the minimap shows them too); the log stays on your machine.

`tools/label_report.py` replays a recorded session through the same code:

    .venv/bin/python -I tools/label_report.py session_X.pcap [--all] [--csv out.csv] [--hide-names]
    .venv/bin/python -I tools/label_report.py session_X.pcap --at 14:10:30 [--png at.png]

* The table has one row per creature seen: handle, kind, species with its source, name, first and last time on the map (seconds into the capture and the recording machine's clock, read from the `session_X.log` beside the pcap), whether it was ever placed on the map (`no` = replicated to us, position never received), the last health and stamina and how old that reading was, and the distance and bearing at first sight. Compare it with what you saw in the game: species, name, the HP you remember, how far away it was.
* `--at HH:MM:SS` (or `+SECONDS`) draws the real `MapView` offscreen at that moment and prints, for every creature on the map, what the label said, or why it had none: outside the minimap circle (an edge arrow only), or no room / over the label limit. It also prints the status line. `--png` saves the picture. Without PySide6 the labels are built by the same functions but not placed.
* `--cache class_cache.json` uses your saved class cache (copy it from the data folder); by default only the shipped seed is used. `--no-seed` uses neither. `--hide-names` replaces player names before you share a report.

## Marker look (2D, one arrow for every dinosaur)

There are no species, diet or size categories on the marker: every dinosaur of another person (a creature that
references a PlayerState, a dinosaur whose controller is not known, and a creature whose class is not known at all) is
the **same purple arrow** as your own red one: the same polygon at 75 % of its size (`geo.PEER_ARROW_SCALE`), purple
`#a24dff` (`geo.PEER_COLOR`) with a dark outline for the 40 most important peers. It points along the direction of travel
(measured from successive positions; kept while the creature rests) and points north, slightly softer, until the creature
has moved. The species, the name and the numbers are in the label text only. Your own arrow stays red.

| Peer | Look |
|---|---|
| you | red arrow |
| another player's dinosaur, any species or none | purple arrow, the same shape as yours |
| server animal (fish) | small muted grey-blue diamond: background, not a target |

Labels sit on a soft dark pill, so the second line reads on any background. A thin **health bar** (green to red) and a
**stamina bar** (blue) hang under the arrow of every player and dinosaur, the 60 most important (`BAR_MAX`), whether it has
a label or not (`show_vitals_bars`, tray "Health / stamina bars"; not for fish). A value that was never read is an empty
grey track, so every creature has the same two bars. The bars show the ratio of the label's second line on purpose: the
label gives the figures in points, the bar the reading at a glance. They are painted under the labels, and a label avoids the bar
of its own creature (the slots under the arrow start below the bars: `place_label(..., below=)`). Tray "Marker colours..." shows the legend
(`geo.LEGEND_ENTRIES`). Beyond the 150 most important peers of a huge crowd the rest are tiny unrotated purple triangles
(500 anti-aliased glyphs do not fit a 16 ms frame; the 500-peer paint test takes ~3.6 ms: the arrows are cached polygons
moved by the painter, and only the 40 most important have an outline).

## Distance, direction, height and why a dot can be missing

Every place that describes where a creature is uses one wording (`geo.where_text`): `724 m NE ▲40 m` (tooltip text and
log lines) and, in separate columns, in the Nearby table (Distance, Direction, Height Δ); the minimap label itself carries the
distance only. The direction is a
compass bearing from map-north (north = -Y), not relative to where you look; the minimap does not rotate. `▲` is above you,
`▼` below you, `level` within half a metre, and the height is left out when it is not known. Health and stamina are printed
once in the label, as `HP` / `ST` (table headers use the same two words); the bars under the arrows show the same
readings as a picture.

A creature leaves the minimap only when the server destroys it. Everything else that used to hide one:

| Cause (found in a recorded session) | What the map does now |
|---|---|
| Beyond the view radius (default 1000 m): the player and most dinosaurs of that session were 1.0-1.2 km away and were never drawn | players and dinosaurs up to 2.5 km are a pointer on the circle edge; the nearest six also read `Name 1.1 km` |
| The server destroys an actor that leaves its relevancy range and sends a new creation record 6-8 s later (same handle, same spot) | players, dinosaurs and unknown creatures stay as dimmed ghosts for 12 s (`GamePeerSource.GHOST_GRACE_S`); fish still leave after 1.5 s |
| A creature whose class could not be named (join tables missed, id not in the cache) and that stands still gave no position at all, so it stayed off the map for as long as it did not move: in the vv capture 3-6 such creatures were "replicated but not placeable" all the time | its creation record carries a spawn position and a maxHealth (plants, fruit and items have none): it is drawn at the spawn position at once, as an unknown creature, and is upgraded when its class is known (hidden creatures 3-6 -> 0-2; time to the first dot, 95th percentile, 334 s -> 13 s). Creatures that existed before the app started and never move still cannot be placed |
| A pack of one species standing together beyond 250 m had no room for labels | one label `Carnotaurus ×5` (nearest one's position) instead of none; inside 250 m every creature keeps its own label |
| No room for a two-line label | a one-line label (who / what it is) for at least 4 s instead of none |
| A creature drawn as `unknown` whose creation record arrived later (or that linked to its PlayerState) stayed `unknown` until it moved | kind and name are upgraded on any state update; never downgraded |
| Resting creatures were dimmed after 8 s although Iris sends nothing for a creature at rest | dimmed after 45 s |

What stays the server's: a destroyed creature leaves after its 12 s ghost, and a creature that rests without ever having
had a position (its creation record predates the app) is not on the map until it moves.

**Player names.** A name exists only in the creation record of the PlayerState, which the server sends when that player
joins. A pawn references its PlayerState by handle; if that handle is below the first one the app saw, the PlayerState was
created before the app started and its name was never sent to us: the pawn is a dinosaur without a name until you rejoin
with the app running. In a recorded session 2 of 24 dinosaurs/players had a seen PlayerState; both were named.

A name that is known is kept, and always printed, for as long as its creature is shown:

* The server destroys a PlayerState when its player leaves relevancy and may create it again; the tracker keeps the names of
  destroyed PlayerStates for the rest of the connection (`IdentityTracker._retired_names`, 512 at most), so a pawn that is
  created again, or still linked to one that was destroyed, keeps its name. A PlayerState created again on the same handle
  replaces the old name.
* A batch that does not carry the link never takes a name or the `player` kind away from a creature on the map
  (`GamePeerSource._emit`). A creature the server destroyed and created again on the same handle, within 60 m + 25 m/s
  of where it was, is the same player when its new record does not resolve a name (`GamePeerSource._revive`).
* A label cap never hides a name: when the distance tier of a player with a known name is full, he or she still gets a
  one-line label, up to 8 of them (3 in a crowd of more than 20); a label that finds no free spot next to a dot near the
  edge of the round view is pulled in towards the centre, clear of the other labels if possible, over them if not.
  Beyond the view radius, the pointers on the circle edge caption players with a known name first.
* Recorded sessions m, jj, k and vv, share of the frames in which a named player inside the view radius had a label
  (before -> after): 94.4 -> 100 %, 91.2 -> 99.7 %, 87.2 -> 99.2 %, 92.0 -> 99.5 %; label on/off toggles 6, 9, 4, 6 -> 0, 4, 2, 4.
  The name itself never fell back to a placeholder in those sessions; the rest of the gap was labels dropped by the caps.

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
| Health, stamina | decoded for other creatures from the full attribute block (9 slots: health, maxHealth, stamina, maxStamina, then maxHealth repeated three times and half of it twice; or 8 slots without maxStamina when that did not change), then **followed** between blocks: a pawn that regenerates sends the pair health + stamina (66 bits apart) in small state batches every second or so (`decode.PawnVitalsTracker`). A slot is the health when it is close to the last health (or sits at an offset confirmed twice for that batch header, which then also takes a bite). A percentage needs the maxima. maxHealth is also in the **creation record** (see below), so a creature created while the app runs gets its health percentage from the first confirmed health + stamina pair, without waiting for a block (`PawnVitalsTracker.note_creation`, `_bootstrap`: the pair must be seen twice at the same place of the same batch layout, health inside 2-105 % of the maximum). maxStamina is in no creation record: such a pawn has HP but no stamina percentage. A creature created before the app started still waits for its first block (in the hhh capture 26 % of the dino-seconds had HP, 17 % had stamina: the block comes rarely and the 8-slot form has no maxStamina). Label: `HP 2275/2620  ST 626/–` (the points the game sent; `~` before a maximum that is only the creation record's) |
| Growth, hunger, thirst | NOT replicated to other clients as far as the data shows (see "Why other players' data is less complete than yours"): at every growth tick of the own pawn the growth slot travels with the new maxHealth (57 of 60 full blocks of growing own pawns), at the same event of a growing other pawn it never does (0 of 16 blocks, 5 of them growth ticks); the own pawn's hunger / thirst heartbeat reaches it about once a second, other pawns' batches never hold it. Not printed, not even `?` |
| Despawn | destruction records (layout fixed, see TRD 13.3); TTL is the fallback |
| Heading | derived from movement: the bearing from an anchor to the first position at least 1 m away (jumps over 200 m ignored), kept while the creature stands still; the dot becomes an arrow |

**Positions of other creatures** (`tools/position_audit.py`, no ground truth exists for another player, so the audit checks what must hold for any real creature): over the recorded sessions no dinosaur / player step is faster than 16 m/s (136 steps, budget 40 m/s); six silences of 4 to 146 s ended 0.0-3.8 m from where the creature had been, i.e. **the server sends a position only when it changed, and no position means "has not moved"** (the same delta rule as for health: a missing update is an unchanged value, not stale data); every fix is within 6.4 km of the own player. A search for a position around an anchor older than `ANCHOR_LIFETIME` (a creature that rested for minutes) was tried and dropped: it found vectors 2.5-4.9 km from the old anchor with 60-97 m/s between consecutive fixes, which are false locks (the same search on 10,795 batches of non-creature streams with the normal 1 km bootstrap radius found none). What a creature without a creation record in the capture and without a position since the app started cannot be given: a position.

A creature whose creation record was missed has no anchor. `ActorLocationTracker.bootstrap` places it from two consecutive state batches: a canonical 22+ bit vector within 1 km of the local player in both, the second continuing one of the first batch's candidates at creature speed. A match inside the tight radius (15 m + 20 m/s) stands on those two samples; a looser one (up to 20 m + 100 m/s) needs a **third** consistent sample in a row (`BOOT_SAMPLES`, the short-gap rule only: the long-silence rules are strict already). Candidates within 400 m of the world origin below 200 m height are dropped while the local player is more than 1 km from the origin (`ORIGIN_EXCLUDE_*`), and creatures are only searched for around the local player **after the own pawn is locked** (`GamePeerSource._bootstrap_origin`): before the lock the own position is a spawn transition (near the world origin, then a jump of kilometres), and in the vv capture handle 223284 was drawn on a false track of 10 fixes within 400 m of the origin, with steps of 63-91 m/s, while the player stood 2.8 km away. A vector within 15 m (+20 m/s) of the last position is accepted at its first sighting (the layout still needs two sightings to be trusted). In the hhh captures this placed 4 more creatures and raised the fixes of walking dinosaurs from 33 to 42 of 89 batches (a resting creature sends only health and stamina, so it has no position to decode).

The overlay line (`GamePeerSource.headline`) keeps a headline for `HEADLINE_HOLD_S` = 12 s before it changes ("Started late" / "Rejoin server: 1 hidden" used to alternate every second or two). An unclassified actor becomes `ai` after four state batches of the animal size (155/158 bits), not eight.

Not all creatures in view are positioned: the capture shows creatures (Deinosuchus, Triceratops, more pteranodons) whose pawn was never created in the capture and whose state does not use a verified layout; they have vitals and species evidence (animation assets) but no map dot.

## Update rate and lag

The own marker follows the client's own outbound move packets (no network hop). Other actors can only be as fresh as the server's replication of them: one-way latency plus the server's send interval for that actor, which Iris decides per actor (priority, relevance, only when state changed). The recorded fixtures carry no packet timestamps, so that rate cannot be measured in seconds from them. What the code controls:

- Every decoded location is written to the store immediately (no buffering, decode cost 0.04..3 ms per packet) and the overlay reads the store every frame (16 ms timer), the same tick that moves the own marker.
- Between samples the dot is dead-reckoned (`geo.EntitySmoother`, `DR_*`): the target advances along the velocity of the last two distinct samples, for at most twice the measured interval and never more than 3 s (`DR_PEER_HORIZON_FACTOR`, `DR_PEER_MAX_EXTRAP_S`; the own marker keeps one interval / 1 s), then holds and is dimmed by its age (`PEER_STALE_S`); a position re-sent unchanged, a gap over 3 s, a teleport or an implausible speed (over 60 m/s) switches it off. Position only: it never produces a heading.
- The dot is **feed-forward**: it moves at the measured velocity all the time and the error to the newest sample is blended in over `DR_CORR_TAU_S` (0.2 s). The earlier version chased the target with a 0.09 s lag filter; with samples 0.7-1 s apart it stood still at the end of each horizon and jumped when the next sample came (6-20 visible jumps a minute). A sample that is late by up to `DR_FF_GRACE_S` (0.3 s) no longer stops the dot. The own marker still uses the 0.09 s filter with the lead (its samples come every ~0.1 s).
- A reused handle (`PeerEntity.born` changes) is a different creature: the dot starts at its position instead of gliding there from the old one.

What stays inherent: the first sample after a silence, network latency and the server's send interval.

## One position path for every player (own marker and others)

| Step | Own marker | Other creatures |
|---|---|---|
| Source | outbound move packets (client-predicted) | inbound server state batches (sent when a position changed) |
| Quantised integers -> world cm | `decode.vector_to_cm` | the same function (`_vector_at`, and `decode_creation` with `CREATION_LOCATION_SCALE` x16) |
| Axes, origin, north | world cm, north = -Y | identical |
| Sample time | `Datagram.observed_at` -> `Interpolator.set_target(at=...)` | the same packet time: `PeerEntity.sample_at` -> `EntitySmoother.update` (a refresh without a new position keeps the old sample time and no longer reads as "stopped") |
| Smoothing / dead reckoning | `Interpolator`, `DR_*` | `EntitySmoother`, the same constants, peer horizon twice as long (see above) |
| Map projection | `project_unclamped`, `to_screen` | the same maths (the entity loop in `overlay._draw_entities` is the same linear map written out per frame) |

What cannot be the same: the lock criteria (8 hits over 0.6 s of monotonic client timestamps for the own pawn; a continuation test for others), and the ground-truth check. The server never replicates the own position back to the client (a bit scan of every state batch of the own handle for a vector within 100 m of the outbound position found 2 chance hits and no consistent offset), so "own replication against outbound packets" cannot test the decoder for other creatures. The indirect checks: creation positions x16 match the same actor's first state fix to 0-12 m (median), own heading against the bearing of own motion has a median error of 0 degrees, and the fastest dinosaur step is 15.7 m/s.

**Latency match (not applied).** The own marker is the client-predicted position (ahead of the server by about half a round trip); others are server positions (behind by a one-way trip plus the send interval, minus what dead reckoning claws back). A round-trip offset would need an RTT measurement: the app records on the client and measures no round trip (the protocol's acknowledgements are not parsed), so there is none. Shifting the own marker back would make it trail the player's own movement, which is worse than the few metres of relative offset. Timing others by the packet (and the longer horizon) lowered the dot lag p50 by 1-11 ms in all eight recorded sessions; p95 is unchanged (it is the server's send interval).

## Own position frame (recorded session `m`: bird, then crocodile)

The user reported that as a bird the positions were fairly right, as a crocodile ("Deinosuchus", swimming) players standing next to them were drawn far away. An earlier reading of the capture was that the own position was in a frame of its own, shifted by about (+1.6 km, -0.4 km). **That reading is withdrawn**: the shift was found by counting creature fixes near the shifted own track, and a lake full of fish gives a strong peak at any offset towards the nearest school (the same method also gives a non-zero "best shift" on the bird connection, which the user says is accurate).

**The own position is in the server's frame, in every recorded connection.** From time to time the server sends the owner's location as three IEEE doubles (cm) at any bit offset of an inbound packet, repeated in the next few packets (`decode.find_server_locations`; a second packet must repeat it). It is the server's own copy of the own location, with no client prediction in it. Compared with the position decoded from the client's moves (`tools`-style replay of all ten connections: flying, walking, swimming):

| connection | confirmed server locations | difference to the decoded own position (m) |
|---|---|---|
| m, crocodile (swimming, z 202) | 5 | p50 0.3, max 1.9 (and 1232 m at the jump, see below) |
| m, crocodile, second app run | 2 | 0.8 |
| jj, vv, k, hhh, as, sadsad | 9, 32, 23, 4, 3, 5 | p50 0.0-2.5, p90 up to 6.1, max 9.5 |
| m, bird (flying) | 2 | 29 (a 15 m/s mover about 2 s ahead of the server) |

So there is no origin shift, no rotation or mirroring (heading against the bearing of motion: median error 0 degrees) and no scale error between the own marker and what the server holds. `TelemetryPipeline` counts the confirmations (`server own-location confirmations=N (far=M)` in the session log's counters line) and logs a line when one is more than 60 m from the decoded position; it never moves the marker for it.

**What the croc capture does show**
- **A frozen client position, then a jump.** For 94 s (flow time 477-571 s) the decoded position stays at one point while the yaw changes and the move record carries `CompressedMoveFlags` 0x20 (`MOVE_Swimming`, tail `1 00000100 0 0 1 00100000`). Then, between two consecutive moves 0.12 s apart (`ts` 50.47 -> 50.59), it jumps 1232 m. The server's double location arrives in the same packet burst (571.10 s; also the large own-pawn state batches and several new players) and is exactly the new position. Either the client's prediction was stale for those 94 s and the server corrected it (the crocodile then really swam about 13 m/s) or the server relocated it. Nothing in the stream tells which; the marker follows the server's truth from the jump on.
- **The players around are not next to the decoded position.** After the jump the three Deinosuchus players are 700-960 m away and 55-58 m higher (z 255-263 against 202). The server confirms the own z (203.3), so unless the user was somewhere else than the capture says, they were not in the same water. Two flat water levels (202 and 258 m) are two different lakes.
- **A creature with only a spawn position is a guess** (see below).

So the app does not shift anything. What it does:

1. **Junk is no longer locked as the own position.** A vector with client timestamp 0 (or a constant one) reached the 8 hits the lock needs and was the own position for the first 20-60 s of a join (`(-527, -477)` in `C8` and `C3`). The initial lock now needs a positive timestamp that advances with the wall clock (`tracking._MIN_TS_RATE`).
2. **A jump is followed within seconds, not after 13 s.** After a jump of more than 1 km the old lock waited for 24 hits of one bit *layout*; the layout of a move changes from datagram to datagram, so none repeated. `recover_distant` now follows a chain of moves that continue each other (close in place, clock strictly forward) and needs 16 hits: 4 s in `m` instead of 13.
3. **A jump is recorded** (`TelemetryPipeline.jumps`, `position_jumps` counter, a line in the session log): more than 300 m at more than 100 m/s while the client clock advanced by the wall time between the two moves. A respawn restarts the clock and is not a jump.
4. **The server's own location is checked** (above), so a future log shows whether the two ever disagree.
5. **Manual alignment** (Nearby list: "I am standing next to the selected one", "Clear alignment"; `TelemetryPipeline.align_to`): the own marker is moved to the selected creature's position, every later own position is shifted the same way. Since the frames agree it is only a workaround for a position that is wrong for another reason (it is cleared by a new server and by `reset()`).

**What would settle the rest:** stand next to another player, note the clock time and, from the in-game map, both coordinates. The only thing left that can differ is the position of the *other* creature (decoded from the replicated state, anchored on the creation record); there is no server copy of it to compare with.

## The own marker standing still while the player moves (two decoder bugs, found by replaying every connection)

Replaying all 11 connections of the recorded sessions and looking for stretches where the decoded own position does not change for more than 3 s *while move-sized client packets keep flowing* found 28 of them (3-30 s each; the marker then jumped 12 m to 1.4 km). Two bugs in `decode.decode_movement` caused most:

1. **Acceleration cap.** A move record carries the pawn's acceleration; `_MAX_ACCEL` (100 000 cm/s²) rejected any record above it. A flyer at 25 m/s sends 105 000-110 000, so all its moves were dropped for as long as it accelerated that hard (`C1`: 14.9 s and 15.5 s, `C8` bird: 7.2 s and 30.8 s, `C7`, `C3`, ...). The cap is 400 000 now; 2 000 000 decodes exactly the same on every capture.
2. **A false short prefix hid the true one.** `_read_move_prefix` takes the first acceleration width whose 7-bit header matches. In `C2` (166-179 s) a false 4-bit reading matched first and returned a "timestamp" of 5.6e-17; the true 20-bit reading, with the real timestamp 187.76 rising, was never looked at, and the tracker ignored the record (its clock went backwards). A client clock below 1 ms is now not a clock (`_MIN_CLIENT_TS`) and the reader goes on to the next width.

Stretches without a decoded move (over 3 s), before -> after, per connection: `C1` 4 -> 0 and 1 -> 0, `C7` 3 -> 0, `C8` bird 3 -> 0, `C2` 9 -> 1, `C3` 10 -> 4, `C5` 2 -> 2, `C4` 1 -> 0, `C8` crocodile 1 -> 1, `C6` 0 -> 0. What is left is not a decoder problem: five **respawns** (the client clock restarts at 0-3 s; 6-24 s without moves; 297 m to 1.4 km from the place of death), two moments where **the client clock itself stood still** (5-7 s, 3-12 m: the game hitched) and one **teleport** with a continuous clock (`C8` crocodile, 4.1 s, 1232 m; the server's own location confirms the new place 0.3 s after the last old move).

Effect on the own marker (`tools/stability_replay.py --local-port N`, per connection, before -> after): jumps per minute `C2` 5.92 -> 1.76, `C7` 5.43 -> 1.75, `C3` 6.13 -> 2.57, `C8` bird 5.23 -> 2.99, `C4` 3.10 -> 1.07, `C1` 2.68 -> 1.19 and 3.47 -> 3.03, `C5` 2.59 -> 1.44 (budget 5; the three that failed it pass); own-marker lag p95 104 -> 46 ms (`C2`), 87 -> 46 (`C7`), 88 -> 47 (`C3`); p50 unchanged at 24-33 ms; the lag of the other creatures' dots and the decode time per datagram are unchanged.

**The server's own copy of the own location cannot be the authority.** It is sent rarely: 0-16 confirmed copies per connection, intervals p50 9-171 s (max 327 s), so for most of a session there is none. Wherever one exists it agrees with the decoded track: after the fixes p50 1.7 m, p90 4.6 m, max 9.1 m over 46 confirmations (the 27 m of the `C8` bird connection was the 7 s decode gap; it is 3.2 m now), plus the one teleport. Adopting it would have moved the marker only in that single teleport, and a chance triple of doubles could move it wrongly, so it still only verifies.

**The marker says when it cannot know.** After `OWN_STALE_S` (3 s) without an accepted move the own arrow is drawn hollow and dim with `? no moves 12 s` next to it (`geo.own_marker_stale`, `MapView.own_age_s`), and the session log gets `own moves resumed after N s without one, M m from where they stopped` (`TelemetryPipeline.move_gaps`, counter `own_move_gaps`). Distances to other players measured from a held marker are as old as the marker.

## Every creature within 500 m: arrow, bars and its own label (name + species)

Guarantee (minimap only; `geo.is_guaranteed`): every player, dinosaur or unclassified creature whose displayed distance is
at most **500 m** has (1) a full arrow and its health / stamina bars, (2) its own label whose first line is
`<name or Player ?> - <species or Dino> - <distance>` and (3) a spot for that label inside the round view that does not cover
another creature's arrow, your own arrow, the status lines or the ring captions. A creature that was guaranteed a moment ago
stays so until 520 m (`GUARANTEED_STICKY_FACTOR`), so one walking along the 500 m ring does not flip between label modes.

* **No tier cap and no pack label inside 500 m.** The nearest `GUARANTEED_FULL_LABELS` = 30 have two lines (name - species - distance, then HP / ST);
  the next ones one line, up to `GUARANTEED_MAX_LABELS` = 45. More than `GUARANTEED_CROWD` = 60 creatures within 500 m (a stress test, never seen in
  a recording: the busiest second had 10) keeps every arrow but labels only the nearest `GUARANTEED_CROWD_LABELS` = 20, so a frame stays cheap
  (`tests/test_peers_ui.py::test_500_entities_selection_and_paint_budget`, 3.7 ms of 4.0).
* **Between 250 and 500 m the label uses a 7 pt font** (8 pt inside 250 m), so more of them fit. A label may become one line there when the two-line one finds no room (for 4 s, no flipping); inside 250 m it stays two lines.
* **Placement** (`overlay.MapView._draw_entities`, `place_guaranteed`): the side it had (kept while it still fits; it gives way only to an arrow under its text, not to a ring caption) -> the usual ten spots and five rings of twelve directions (`geo.spread_slots`, 20 - 104 px from the arrow; an index means the same place from frame to frame) clear of other labels and arrows -> clear of other labels only -> one line (250 - 500 m) -> the spot that covers the least of the others (`geo.least_overlap`; a few per frame, then the spot it had) -> never none. A label that is more than 14 px (`LEADER_MIN_PX`) from its arrow gets a thin leader line. At most 10 whole-list searches per frame (`SEARCH_BUDGET`); the rest of a crowd reuses its spot, reviewed after 1 s (`FORCED_REVIEW_S`).
* **Nothing is hidden**: labels avoid your own arrow, the status lines, the ring captions (a label that already sits on one keeps its side) and the edge captions; arrows are painted after all pills, players topmost.
* Label contrast: the pill is 69 % opaque (55 % for a stale creature), a stale or spawn-only creature's label keeps 73 % text opacity (its arrow is the dimmed part), a remembered / unknown species 90 % / 80 %.

Checking it on a capture: `.venv/bin/python -I tools/tier_check.py --labels SESSION.pcap [--png DIR] [--check]` replays the capture through the real `MapView`
(offscreen, simulated clock) and each second asks, for every creature within 500 m: inside the view, full arrow, own label with name and species, label inside the round view, not covering an arrow.
`--png` saves the busiest second at 3x. Result on the recorded sessions (creature-seconds within 500 m; before = commit d861cce):

| session | creature-seconds | before | after |
|---|---|---|---|
| session A, connection 1 | 142 | 73.9 % | 100 % |
| session A, connection 2 | 16 | 100 % | 100 % |
| session B | 604 | 93.5 % | 100 % |
| session C | 277 | 49.1 % | 100 % |
| session D, flying connection | 888 | 64.6 % | 100 % |
| session E | 961 | 74.7 % | 100 % |
| four more sessions (and D's swimming connection) | 0 | nobody within 500 m | |

The names behind the placeholders: "Player ?" stood for every label in B, E, C and A-1 (the app started after the join, so the server's player names were never seen:
the server sends a name once, at join) and for 200 of 888 in D; "Dino" (species unknown: no table seen, id neither in the shipped seed nor in the user's class cache, which these runs did not use) for 604 / 604 in B, 793 / 961 in E, 527 / 888 in D, 121 / 277 in C, 24 / 142 in A-1. Both upgrade as soon as the name or class is known (`tests/test_label_guarantee.py`).
In a dense cluster two labels can still share a few pixels (informational; 6 - 13 % of the creature-seconds in the recordings); none covers an arrow and none is clipped.
`tools/stability_replay.py --check` still reports only the known budgets (time to first dot, dot jumps in session C, headline changes in session D); the label hop budget (3 per minute) holds on every capture (worst 2.7), because a kept side tolerates `SLOT_SLACK_PX` = 6 px.

## Painting order, tiers and low-confidence positions

- **Nothing covers an arrow.** Label pills and text are drawn first and the glyphs after them (the most important peer last: players before dinosaurs before fish, the 250 m tier before 500 m); before, the pill of a nearer creature (or a fish) could be painted over an arrow of a lower tier. The ring captions, edge captions and status lines are under the glyphs as before; `tests/test_zorder.py`.
- **Tiers are inclusive**: `priority_tier(250) == 0`, `(250.01) == 1`, `(500) == 1`, `(1000) == 2` (`bisect_left`).
- **Checking the tiers on a capture**: `.venv/bin/python -I tools/tier_check.py SESSION.pcap [--port N] [--check]` replays it and lists every dinosaur / player / unclassified creature that was within 500 m of the own position (tier 250 m / 500 m, position source `fix` or `spawn`) and fails with `--check` when a spawn-only position there would be drawn without the `~`. In `C8` (bird connection) 432 of 862 creature-seconds within 500 m were spawn-only positions, i.e. creatures the server had sent no position for since they were created; the crocodile connection has no dinosaur or player within 500 m of the (server-confirmed) own position.
- **A spawn position is a guess.** A creature that has only its creation record's spawn position (`Entity.pos_source == "spawn"`) is dimmed and its distance reads `~724 m` until a decoded fix confirms it; a spawn position never replaces a followed one (`GamePeerSource._emit`). In `C8` 10 of 60 handles in the bird connection never left their spawn position; the ones that could be checked sent 7-28 state batches in 150 s, i.e. they were resting.
- **Players whose positions stop arriving** keep their last position; whether such a player has really stopped or its position is not decoded cannot be told from the stream (one player in `m` sent 449 state batches in 150 s without any vector at plausible height).

## Fluid motion of other creatures

Fish and players are sent a few times a second and the velocity of the last two quantised samples jitters (and is sometimes ~0 for a steady swim), which made the dot stop and start. `EntitySmoother._note_sample` now blends a new velocity with the old one when it points the same way (within 60 degrees, speed within a factor of 3; `DR_VEL_KEEP` 0.5); a turn or a stop replaces it at once. `tools/stability_replay.py` measures it at the rendered frame rate (`choppiness`): for every interval between two server positions over which the creature really moves, the share of frames that move less than 20 % of the interval's mean ("stall") or more than 3 x ("spike"). On the real pipeline, bird connection of `C8` (330-500 s): fish stall 13.2 % -> 4.7 %, spike 3.4 % -> 1.7 %; players stall 2.3 % -> 1.2 %, spike 1.1 % -> 0.85 %; crocodile connection (250-460 s): fish stall 11.6 % -> 7.6 %, spike 2.3 % -> 1.6 %. The dot lag p50 rises by 10-15 ms (`C9`: 108 -> 121 ms). A slower correction (`DR_CORR_TAU_S` 0.3) and a tail that slows down after the horizon reduced it a little more but cost 30-60 ms of lag and broke the "dot error below 80 % of the hold dot" budget on `C9`, so the correction stays 0.2 and the tail stops at the horizon as before. The budgets in `BUDGETS` (`choppy.*`) are regression guards set above the worst recorded session, not targets. The blend costs a little error at stops and turns: the dot error p95 rises by 20-35 cm (`C6`: 347 -> 381 cm against 359 cm for the dot that stays on the newest sample), so that budget now allows 10 % over the hold dot. What stays: a creature that starts moving after a silence of a second or more cannot be drawn moving before the server says so.

## Why other players' data is less complete than yours

Your own pawn is the ground truth the app reads: the server sends the **owner** the whole attribute set (growth, hunger,
thirst, health and maxima) in a full frame about every 16 s and the hunger / thirst heartbeat about once a second. To
everybody else it sends what changed, and only members that are not owner-only. `tools/attribute_census.py` counts the
signatures per capture, own pawn against all others (5 captures):

| Field | Own pawn | Other pawns |
|---|---|---|
| health, maxHealth | in every full block | yes: full blocks (16 seen) and the health + stamina pair in small batches |
| stamina | yes | yes (the pair); maxStamina only in some blocks |
| growth | slot in 57 of the 60 full blocks of growing own pawns; none for an adult (1.0 is probably the default and is not sent) | **never**, in 16 full blocks, 5 of them growth ticks of growing pawns (maxHealth +0.5..1.4 %), the event that always carries the growth slot to the owner |
| hunger, thirst | heartbeat in 95-100 % of the attribute batches (~1 per second) | **never**: 5 look-alikes in ~250,000 batches, all maxHealth blocks |
| maxHealth in the creation record | n/a (created before the app) | yes: 10 of the 15 creation records of 1500+ bits (the rest are classes with another layout) |

So growth, hunger and thirst of others are not an unfinished decoder: the server does not send them to us. A plain float32
copy of the own growth is in no batch of the own pawn either, so an unknown encoding for others cannot be excluded, but
nothing in the data points to one. maxHealth grows with growth (own pawn: 1.57 at growth 0.038, 6.64 at 0.150), but the
curve differs per species (a pawn of 1650 maxHealth had growth 0.24, one of 2.1 growth 0.049), so no percentage of growth can be
derived from it without a per-species calibration; none is shown.

**A missing update is an unchanged value.** Iris sends the members that changed, so a health that has stopped changing (full
health), a creature that stands still, and a creature that stopped moving are all silent. The last health and the last
position stay true until the server sends the next one or destroys the creature, which is why `VITALS_FRESH_S` is 120 s and a
dot is not dimmed for being quiet.

The creation record holds maxHealth, not health: floats 34 bits apart (value + two flag bits), maxHealth twice and maxHealth / 2
four times (402662: 2606.108, 2606.108, 1303.054 x4 at bits 3304..3474; its first block 50 s later said 2620.49, it had grown;
`decode.creation_max_health`; 0.9 ms per record; creation vs first block: +0.6 %, +0.9 %, +1.4 % for three pawns).

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
- **Still creatures never get a position.** The game only sends changed fields, so a creature that has not moved since you joined sends no position batch. Three such Deinosuchus were replicated for over 200 s in the `C5` captures with no position (no `0xc0000348` / `0xc0100348` batch at all); no bit scan can find a value that is not in the stream. The app counts them (`GamePeerSource.hidden_creatures`: 30+ state batches, at least 80 % of them 60-400 bits, under 10 % animal sized, recent, not an earlier own pawn) and says so.
- **The overlay line is elided to about 30 characters**, so `GamePeerSource.status()` now starts with a short headline and appends the detail: `Started late: rejoin to see all` (tables missed; one constant message, because the old `Rejoin server: N hidden` / `to identify` variants flipped every few seconds with the hidden count), `N creatures: no position yet` (tables seen), `No other players nearby` (tables seen, no dinosaur or player on the map). "Rejoin" means leaving the server and joining again with the app already running.
- **Own handle changes** (`own handle changed: A -> B`) are a death and respawn: the heartbeat lock lets go of the silent pawn and takes the new one. The old pawn keeps streaming as a body for a while; it is remembered (`_past_own`), never counted as another creature and never put on the map (it used to appear as a dot labelled with your own species).
- **Species without tables** come from `class_cache.json` (learned on this machine) and the shipped `isle_livemap/assets/class_seed.json` (51 protocol ids from captures that saw the tables; `python tools/build_class_seed.py PCAP...` rebuilds it; asset paths only). A table that disagrees with a seeded or learned id blocks it for the process. Species from either show with a `~`.

Tests: use the project environment, `.venv/bin/python -m pytest` (Qt and Pillow are installed there; the system `python3` skips the 10 tests that need them).

## Stability budgets (stuck, lag, flicker) and how they are measured

`tools/stability_replay.py` replays recorded sessions through the real path (pcap -> `TelemetryPipeline` -> `GamePeerSource` -> `MapView.tick` at 60 fps -> `MapView.grab` at 5 Hz, offscreen Qt) with `time.monotonic` replaced by a simulated clock that follows the datagram times, so the numbers do not depend on the machine:

    .venv/bin/python -I tools/stability_replay.py DIR/session_*.pcap --check      # exit 1 on a violated budget
    ISLE_CAPTURES=DIR .venv/bin/python -m pytest tests/test_stability_replay.py   # the same, as tests (skipped without ISLE_CAPTURES)

Budgets (`BUDGETS`, `check_budgets`) and the values measured on seven recorded sessions (`C6`, `C4`, `C5`, `C1`; 26-562 s each), before -> after the stability work:

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


## Nearby creatures that were missing (jj capture)

In the jj capture most creatures had no class (the app started after the join) and nothing else was wrong with the
tables, but many nearby creatures were drawn once and then stood still on the map, or never drawn:

| Cause | Fix |
|---|---|
| A creature without a class never learned its position layout (`learn` was only for dinosaurs, players and animals): it was drawn at its spawn point or first bootstrap and then frozen (1-3 different positions in 12 minutes) | a creature that behaves like a creature (`GamePeerSource._creature_like`: its creation record carried a maxHealth, or its state batches have the size profile of a pawn and never the animal size, handle at least 1000) learns its layout like a classified one: positions that changed per near creature-second 64 -> 131, on the map within 500 m 318 -> 450 s |
| An anchor older than `ANCHOR_LIFETIME` (120 s) was never replaced: a creature that rested for two minutes and then walked was stuck at its old position until the server destroyed it | `ActorLocationTracker.bootstrap` takes over from a stale anchor, also for a creature that is on the map |
| The bootstrap needed two batches within 3 s | a longer silence (up to `BOOT_LONG_GAP` 45 s) is bridged when the same vector offset of the same state header continues twice in a row, or when each batch has at most two candidates and one of each pair continues the other at walking speed (16 m/s) |
| The game's static singletons (handles under 1000, e.g. the game state with 8000 batches) looked like "creatures without a position" and kept the headline at "1 creature: no position yet" | `MIN_DYNAMIC_HANDLE` |
| Species `?` on 83 % of the creature-seconds | the seed now holds 61 protocol ids: dinosaur ids that no capture had the tables for but whose own creation assets name one species (Stegosaurus, Dilophosaurus, Tenontosaurus, Omniraptor, Pteranodon) are derived by `tools/build_class_seed.py` (the tables win over a derived path). 44 % remain: 1042 creature-seconds belong to creatures created before the app started (no creation record, so no id) and 963 to ids no capture has seen with tables. The class cache learns them over sessions in which the app was started before the join |
| A remembered class that this connection's own assets agree with was marked `~` | `species_source` reports `asset` (confirmed) when the cache path and the assets name the same species |

Still impossible: a creature that existed before the app started, has no class and never moves sends state batches but
no position the decoder can use; its first candidate vectors change by hundreds of metres between batches.
