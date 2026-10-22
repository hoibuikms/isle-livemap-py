# Other players on the minimap (peers)

Other players come only from local data. There is no server feed, URL, token or HTTP client.
Everything is passive: the app only reads packets, it never injects, sends or reads game memory.

## Sources

`peer_source` is one of `off`, `lab`, `game`. The default is `off`.

| Source | Purpose | Input | Status text (examples) |
|---|---|---|---|
| `off` | no other players | none | `peers: off` |
| `lab` | development and testing | loopback UDP `127.0.0.1:PORT` (default 7777), 18-byte lab records from `labsim` | `lab: listening 127.0.0.1:7777 (N peers, ...)`, `lab: unavailable (...)` |
| `game` | owner-only, opt-in | the game's inbound replication, through the same capture path as the local player | `game: waiting for actor locations`, `game: N actors (position only; ...)` |

`game` is a personal tool for the repo owner. It is never the default and is not a distributed feature.
`--replay` on its own shows synthetic demo peers (one entry deliberately carries a self id, to exercise self-exclusion).

## Flags

| Flag | Effect |
|---|---|
| `--replay [FILE.pcap]` | replay a synthetic walker or a pcap; peers default to the synthetic demo source |
| `--lab-peers [PORT]` | use the `lab` source on loopback (default port 7777) |
| `--game-peers` | use the `game` source (owner-only, opt-in) |

Flags win over the saved setting. `--lab-peers` and `--game-peers` are mutually exclusive.
`--game-peers` also works together with `--replay FILE.pcap`: the pcap's inbound datagrams go through the same pipeline.

## Settings (`settings.json`, editable in "Peers settings...")

| Key | Default | Meaning |
|---|---|---|
| `peer_source` | `"off"` | `off`, `lab` or `game` |
| `peer_radius_m` | `1000` | radar radius in metres (clamped 50..20000). Old files with `feed_radius_m` are migrated to this on load; other legacy `feed_*` keys are ignored. |
| `peer_self_ids` | `[]` | extra ids that are "me" and never drawn (for example a second client) |
| `show_players` | `true` | draw other players |
| `show_vitals_detail` | `false` | extra label line with the vitals that are known |
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
