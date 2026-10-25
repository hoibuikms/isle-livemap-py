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
- Silence timeouts are a property of the source. Lab and synthetic peers are dimmed after 3 s without an update and removed after 5 s. Game peers are dimmed after 8 s and removed after 30 s (`GAME_PEER_STALE_S` / `GAME_PEER_TTL_S`), because Iris replicates changes only: an actor that stands still sends nothing, so silence is not absence. The game source also removes a tracked handle at once when the packet carries a destruction record for it (431 records in the recorded capture, 431 of 431 handles seen in that capture, 430 with a creation record). The 8 s / 30 s values are a judgement call, not a measured number.
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
| Health, stamina | decoded for other creatures from the 9-slot attribute block (health, maxHealth, stamina, maxStamina, then maxHealth repeated three times and half of it twice): shown as percentages when the block has been seen |
| Growth, hunger, thirst | NOT decoded: growth is only sent when it changes (never alongside the block in the capture); hunger and thirst of others were not found |
| Despawn | destruction records (layout fixed, see TRD 13.3); TTL is the fallback |
| Heading | derived from movement: the bearing from an anchor to the first position at least 1 m away (jumps over 200 m ignored), kept while the creature stands still; the dot becomes an arrow |

Not all creatures in view are positioned: the capture shows creatures (Deinosuchus, Triceratops, more pteranodons) whose pawn was never created in the capture and whose state does not use a verified layout; they have vitals and species evidence (animation assets) but no map dot.

## Update rate and lag

The own marker follows the client's own outbound move packets (no network hop). Other actors can only be as fresh as the server's replication of them: one-way latency plus the server's send interval for that actor, which Iris decides per actor (priority, relevance, only when state changed). The recorded fixtures carry no packet timestamps, so that rate cannot be measured in seconds from them. What the code controls:

- Every decoded location is written to the store immediately (no buffering, decode cost 0.04..3 ms per packet) and the overlay reads the store every frame (16 ms timer), the same tick that moves the own marker.
- Between samples the dot is dead-reckoned (`geo.EntitySmoother`, `DR_*`): the target advances along the velocity of the last two distinct samples, for at most one measured interval and never more than 1 s; a position re-sent unchanged, a gap over 3 s, a teleport or an implausible speed (over 60 m/s) switches it off. Position only: it never produces a heading.
- The smoothing time constant of peers equals the own marker's (0.09 s).

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
