# Other players on the minimap (peers)

Other players come only from local data (the game's own traffic, captured passively). There is no server feed, URL, token or HTTP client.
Everything is passive: the app only reads packets, it never injects, sends or reads game memory.

## Sources

There is no source choice for the user. Nearby players always come from the game: the live capture that provides the current player also feeds `GamePeerSource` (decision: TRD rev 6; this is the owner's personal tool).
Start the app with no flags and nearby players appear next to the current player once the server is learned.

| Source | Purpose | Selected by | Status text (examples) |
|---|---|---|---|
| game | the only user-facing source | default; also `--replay FILE.pcap` | `game: waiting for server traffic`, `game: waiting for actor locations (N pkts, own handle not locked yet)`, `game: N actors (position only; ...)`, `game: unavailable (...)` |
| lab | developer/test only | `--lab-peers [PORT]` | `lab: listening 127.0.0.1:7777 (N peers, ...)`, `lab: unavailable (...)` |
| synthetic | developer/test only | bare `--replay` | `synthetic peers (N)` |

A failing or absent pipeline never crashes the app: the status line says why (`game: unavailable (...)`) and the own-player minimap keeps working.
Bare `--replay` shows synthetic demo peers (one entry deliberately carries a self id, to exercise self-exclusion).

## Flags

| Flag | Effect |
|---|---|
| `--replay [FILE.pcap]` | replay a synthetic walker (with synthetic demo peers) or a pcap (peers decoded by the game path) |
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

## Behaviour common to all sources

- Self is excluded by identity, never by position. Lab: id 0 is the lab's "me". Game: the entity whose Iris handle equals the own handle (learned from the survival heartbeat) is dropped, and a provisional peer with that handle is purged when the lock arrives. `peer_self_ids` adds more.
- A peer is dimmed after 3 s without an update and removed after 5 s. This is the fallback despawn rule. The game source also removes a tracked handle when the packet carries a destruction record for it (see "Not decoded").
- Missing fields are `None` and shown as `?`. Nothing is guessed. There is no heading inferred from movement: peers without a decoded heading are drawn as a dot, and only a source-supplied heading draws an arrow.
- Peers in range but outside the view get an edge indicator on the minimap circle.

## Lab record (development only)

Little-endian `<IfffH` + UTF-8 name: id (uint32, shown as signed), x, y, z (float32, Y-up metres), status (uint16, read as health 0..100), name. The layout is an assumption used by `labsim`, not the game's protocol. Y-up metres are converted once to geo centimetres (lab z becomes geo y, lab y becomes height).

## What the game backend decodes

| Field | Game source |
|---|---|
| Identity (Iris `net_ref_handle`) | decoded, shown as `Actor #<handle>` |
| Position x, y, z | decoded, same quantisation and cm units as the own movement |
| Despawn | partial: destruction records remove an already tracked handle; not validated against a recorded despawn, TTL is the fallback |
| Name | not decoded |
| Class (player, AI, animal) | not decoded; every actor is shown as `player`, so AI/animals with the same state layout can appear |
| Health, growth, stamina, hunger, thirst | not decoded (`?`) |
| Heading | not decoded (dot glyph) |
| Reliable despawn | not validated |

Evidence and method are in section 11 of `docs/trd/other-players-on-minimap.md`.

## Capture that would unlock the rest

Record a pcap of your own session (Wireshark or Npcap on the game's UDP port; the format `--replay FILE.pcap` reads), about 60 s each:

1. Stand still next to exactly ONE other known player. Write down their character name and roughly their health and growth.
2. Keep capturing while that player walks away, dies or logs off (despawn).
3. A second capture with that player alone in range of your client while you do not move.

Also note the player's name, species and any handle you can read in game. With that ground truth the 37 trailing bits of the location state (likely heading), the export section (class and name) and the attribute slots (vitals) can be matched, and the despawn record validated. Until then those fields stay `None`.
