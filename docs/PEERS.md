# Other players on the minimap (peers)

Other players come only from local data (the game's own traffic, captured passively). There is no server feed, URL, token or HTTP client.
Everything is passive: the app only reads packets, it never injects, sends or reads game memory.

## Sources

There is no source choice for the user. Nearby players always come from the game: the live capture that provides the current player also feeds `GamePeerSource` (decision: TRD rev 6; this is the owner's personal tool).
Start the app with no flags and nearby players appear next to the current player once the server is learned.

| Source | Purpose | Selected by | Status text (examples) |
|---|---|---|---|
| game | the only user-facing source | default; also `--replay FILE.pcap` | `game: waiting for server traffic`, `game: waiting for actor locations (N pkts, own handle not locked yet)`, `game: N actors (creation 0, exports 0, destroyed 1; position only, ...)`, `game: unavailable (...)` |
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
| `show_ai` | `true` | inert: packet sources never report an AI kind |
| `record_sessions` | `true` | settings-file only: automatic session pcap + log for live capture; `false` = same as `--no-record` |
| `record_dir` | `""` | settings-file only: sessions directory; empty = `<data dir>/logs/sessions` |
| `record_keep_sessions` | `10` | settings-file only: newest sessions kept (1..1000) |
| `record_total_cap_mb` | `1024` | settings-file only: total size cap of the sessions directory (10..) |

## Behaviour common to all sources

- Self is excluded by identity, never by position. Lab: id 0 is the lab's "me". Game: the entity whose Iris handle equals the own handle (learned from the survival heartbeat) is dropped, and a provisional peer with that handle is purged when the lock arrives. `peer_self_ids` adds more.
- Silence timeouts are a property of the source. Lab and synthetic peers are dimmed after 3 s without an update and removed after 5 s. Game peers are dimmed after 8 s and removed after 30 s (`GAME_PEER_STALE_S` / `GAME_PEER_TTL_S`), because Iris replicates changes only: an actor that stands still sends nothing, so silence is not absence. The game source also removes a tracked handle at once when the packet carries a destruction record for it (see "Not decoded"). The 8 s / 30 s values are a judgement call, not a measured number (no recorded idle actor exists).
- Missing fields are `None` and are simply not printed: the minimap label never shows `?`, `HP ?` or `G ?`. The label is name (or `Actor #<handle>`), species if known, then the known vitals, distance, compass bearing and height difference, e.g. `Actor #84466` / `757 m N +116m`. The Nearby table keeps `-` / `?` only because its columns need a cell. Nothing is guessed. There is no heading inferred from movement: peers without a decoded heading are drawn as a dot, and only a source-supplied heading draws an arrow.
- Peers in range but outside the view get an edge indicator on the minimap circle.

## Lab record (development only)

Little-endian `<IfffH` + UTF-8 name: id (uint32, shown as signed), x, y, z (float32, Y-up metres), status (uint16, read as health 0..100), name. The layout is an assumption used by `labsim`, not the game's protocol. Y-up metres are converted once to geo centimetres (lab z becomes geo y, lab y becomes height).

## What the game backend decodes

| Field | Game source |
|---|---|
| Identity (Iris `net_ref_handle`) | decoded, shown as `Actor #<handle>` |
| Position x, y, z | decoded, same quantisation and cm units as the own movement |
| Despawn | partial: destruction records remove an already tracked handle; not validated against a recorded despawn, TTL is the fallback |
| Name | not decoded (label shows `Actor #<handle>`) |
| Class / species (player, AI, animal, trice, t-rex, ...) | not decoded; every actor is shown as `player`, so AI/animals with the same state layout can appear |
| Health, growth, stamina, hunger, thirst | not decoded, not shown (see "Why the own-player vitals do not transfer") |
| Heading | not decoded (dot glyph) |
| Reliable despawn | not validated |

Evidence and method are in section 11 of `docs/trd/other-players-on-minimap.md`.

## Update rate and lag

The own marker follows the client's own outbound move packets (no network hop). Other actors can only be as fresh as the server's replication of them: one-way latency plus the server's send interval for that actor, which Iris decides per actor (priority, relevance, only when state changed). The recorded fixtures carry no packet timestamps, so that rate cannot be measured in seconds from them. What the code controls:

- Every decoded location is written to the store immediately (no buffering, decode cost 0.04..3 ms per packet) and the overlay reads the store every frame (16 ms timer), the same tick that moves the own marker.
- Between samples the dot is dead-reckoned (`geo.EntitySmoother`, `DR_*`): the target advances along the velocity of the last two distinct samples, for at most one measured interval and never more than 1 s; a position re-sent unchanged, a gap over 3 s, a teleport or an implausible speed (over 60 m/s) switches it off. Position only: it never produces a heading.
- The smoothing time constant of peers equals the own marker's (0.09 s).

What stays inherent: the first sample after a silence, network latency and the server's send interval.

## Why the own-player vitals do not transfer to other actors

The own decoder never reads an attribute id from the wire. It finds 66-bit slots (float, set flag bit, repeated float) and names them by position: the survival heartbeat (hunger, thirst, hunger repeated), then the last two slots of the same batch (health, stamina), or fixed offsets inside the 1380..1535 bit full frames. It locks onto one handle only after two consistent heartbeats and then checks every value against the maxima it learned. Other actors' batches (16..516 bits) show lone slots or a pair 66 bits apart (handle 80984 at bits 289 and 355: 620.877 and 483.054; handle 61016 at 295 and 361: 470.133 and 541.388), never the heartbeat triple and never a full frame, so no slot position can be named. Values would also need a maximum to become a percentage and none is sent. Nothing is decoded for others: not health, growth, stamina, hunger, thirst, species or name.

## When identity is sent (why the capture must start before joining)

Name, species and player-versus-AI are not part of the steady-state position updates. The game sends them once, in the actor creation record (plus the object exports that name its class), when an actor first becomes relevant to your client. A capture that starts while the neighbour is already in view cannot contain that data, however long it runs. Vitals of other players are not replicated in any form that has been decoded.

The game status line shows what the capture contains so far:
`game: N actors (creation C, exports E, destroyed D; ...)`.
- `creation C`: batches whose first bits say "actor creation" (the record itself is not decoded yet).
- `exports E`: batches followed by an export section (object paths, net-token strings).
- `destroyed D`: destruction records in the envelope (an actor leaving relevance).

`creation 0, exports 0` means the capture holds no join data and species/name cannot be recovered from it. All current fixtures show `creation 0, exports 0, destroyed 1`.

## Automatic session logs

Every live launch (not `--replay`, not `--lab-peers`) writes two files without any flag, from the moment the app starts, so the join of a server is always on disk:

- `session_YYYYmmdd_HHMMSS.pcap`: the game's UDP datagrams (both directions), format below.
- `session_YYYYmmdd_HHMMSS.log`: a concise plain-text event log: app start/stop, server learned, own handle locked/changed, peer-source status transitions, a counters line every ~10 s (packets in/out, creation / exports / destroyed, actors tracked, distinct handles), the first creation record (with its handle), every destruction record (with its handle), recorder start / roll / stop / failure, retention deletions, and decoder exceptions with traceback (first 5 per site). No per-packet lines. It rotates at 5 MB (`.log.1`).

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

`tools/inspect_capture.py [FILE|DIR] [--latest] [--first N] [--max-bytes B] [--local-port P]` runs offline (no Qt, no network) and prints packet counts, duration, batch kinds, creation / export / destruction counts, distinct handles and a raw hex dump of the first N creation and export-bearing batches. It does not decode species or names.

## Capture that would unlock the rest

Record a pcap of your own session (Wireshark or Npcap on the game's UDP port; the format `--replay FILE.pcap` reads), about 60 s each:

1. Stand still next to exactly ONE other known player. Write down their character name and roughly their health and growth.
2. Keep capturing while that player walks away, dies or logs off (despawn).
3. A second capture with that player alone in range of your client while you do not move.

Also note the player's name, species and any handle you can read in game. With that ground truth the 37 trailing bits of the location state (likely heading), the export section (class and name) and the attribute slots (vitals) can be matched, and the despawn record validated. Until then those fields stay `None`.
