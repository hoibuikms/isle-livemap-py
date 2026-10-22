# Reference project findings: how Undo-IsleLiveMap gets other players' data

Date: 2026-10-22. Scope: research only, no source in this repo was changed. Method: shallow clone of
`https://github.com/blackundo/Undo-IsleLiveMap` (HEAD `2cccd84`, "release: 2.4.3", 493 tracked files) into
`/tmp/ref_undo_isle`, read only. Nothing from it was built, run or imported. Validation scripts live in
`/tmp/scratch` (outside both repos) and import only our `isle_livemap` package, run with `python -I`.
All reference paths below are relative to that repo.

## 0. Bottom line

The public reference repo does NOT contain a decoder for other players' name, species, vitals, rotation or
player-vs-AI. That logic is in the closed-source "Pro Agent" (`PRO_LOCAL_KEY.md:3`: "It contains the IPC client
and key-entry UI, not the private Agent, tracking algorithms"). README line 14 confirms Free = own player only;
species/player/AI/weight are Pro.

What the public repo does give us is (a) the exact packet envelope we already ported, (b) a probe-level reader
for Iris object exports and actor-creation records (`tools/TheIsleOverlay.IrisStatsProbe/ProbeIrisMetadataReaders.cs`),
which is the mechanism by which species and identity reach the client, (c) the shape of the Pro Agent's output
(which fields exist, which identities count as proof), and (d) strong documented evidence that identity is only
sent once, at actor creation. Our recorded fixtures contain no creation record, no export and no net-token stream,
so none of (b) can be validated or applied until we capture a join/reconnect. Nothing for name, vitals or
heading of OTHER actors is available from the reference.

Stack: C# / .NET 8, WPF, SharpPcap on Npcap (`NpcapLocalMovementSource.cs`, `PacketCapturePipeline.cs`,
`WindowsUdpPortOwnerResolver.cs`). Passive UDP capture for the game process's ports only.

## 1. License

- README.md:3 and :130 say MIT ("[MIT](LICENSE)", "giu nguyen ... giay phep MIT cua phan public").
- There is NO `LICENSE` file in the clone (`git ls-files | grep -i licen` is empty), no license metadata in any
  project file, and the repo is a fork of `klong-dev/IsleLiveMap` (README:3), so the upstream author holds the copyright.
- Our `isle_livemap/decode.py` docstring already states it is a port of the "open-source (MIT)" decoders. The
  claim rests on the README only. Recommendation: keep the attribution line, and if we want it airtight, check
  the upstream `klong-dev/IsleLiveMap` for the actual LICENSE text and copy its copyright notice into our repo's
  notices. The Pro Agent is proprietary and absent, so nothing from it is reusable.
- Reusable: protocol facts, bit layouts, field order, thresholds (facts are not copyrightable). Do NOT copy code
  wholesale; re-implement in our style (as decode.py already does). Pro-only data (species tables, assets, the
  signed Agent, backend URLs) is out of scope; the repo has no species-id table anyway (see 3.4).

## 2. Non-passive or not applicable (do not use, do not propose)

| Item | Where | Status |
|---|---|---|
| Pro Agent (signed exe, device lease, named-pipe IPC) | `src/TheIsleOverlay.ProClient/*`, `PRO_LOCAL_KEY.md` | NOT applicable: closed, licensed, network-activated |
| Server-side data: EraGaming, IslePilot, Gacha, Origin, SDVN providers, Steam login, cookies | `src/TheIsleOverlay.EraGaming`, `.IslePilot`, `.Gacha`, `HomeWindow.*` | NOT applicable: we explicitly have no server feed |
| Team relay (SignalR, invite codes) | `src/TheIsleOverlay.TeamRelay` is referenced in README; teammates' HP/hunger/water arrive over the relay from each teammate's own app | NOT applicable: not packet decoding; this is how the reference shows "other players' vitals" |
| Low-level mouse hook, DPAPI, auto-update | `GlobalMouseShortcutHook.cs` etc. | Irrelevant UI plumbing |
| Memory reading / DLL injection / sending packets | README:67, :73 state none is used | Not present; nothing to exclude |

The reference is itself passive for the telemetry path. The only non-passive pieces are web/backend features.

## 3. Protocol facts (reference location, then our verdict)

### 3.1 Envelope (UE 5.5 Iris), `UnrealIrisPacketParser.cs`
- :12-:122 packet walk: last-one-bit sentinels, `SerializedInt(4)`, `SerializedInt(8)`, handshake bit, skip 8,
  second sentinel, 32-bit packet header (`seq = (h>>18)&0x3FFF`, `ack words = (h&0xF)+1`), optional frame-time.
- :124-:182 bunch header; channel index 2 is the Iris data stream (:86).
- :184-:300 data stream: 5-bit stream count-1, 5-bit stream mask (bit 0 = NetToken stream, bit 1 = replication
  stream, :210); then 16-bit batch count, 16-bit destruction count; per destruction: packed u64 handle, 1 bit
  has-info (+packed u64), 1 bit; per batch: 1 bit inline-destruction (aborts as incomplete), packed u64 handle,
  16-bit batch bit count, then first bit = has owner data, second bit = has exports, payload starts 2 bits later.
- Iris packed u64 = 3-bit (bytes-1) + bytes*8 bits (:580); packed u32 = 2-bit + bytes*8 (:587).

Verdict: CONFIRMED. `decode.parse_iris_packet` is the same algorithm. All 11 Iris fixture payloads parse; every
data stream has mask 0b10 (no NetToken stream) and `has_exports` is false for all 126 batches; the one
destruction record (handle 86078) parses and the packet ends with `inline-destruction`, exactly as the reference
treats it. No contradiction with our 25/24 batches per packet or `data_bit_offset = start + 2`.

### 3.2 Own movement, `UnrealMovementPacketDecoder.cs`
FVector_NetQuantize100 = 7-bit header (bit 6 scale flag, low 6 bits component count) + 3 signed components, raw/100
cm; compressed rotation = 3 axes of (1 flag + 16 bits), degrees = raw*360/65536 (:152-:192); map heading = yaw+90
(`MapHeading.cs:5`). Verdict: CONFIRMED (our decode.py is a port; fixtures `MovingPayload`/`StationaryPayload` pass in the existing tests).
This is client-to-server only, so it says nothing about other actors' rotation.

### 3.3 Own vitals, `UnrealDinosaurVitalsTracker.cs`
66-bit GAS slot = float32, 1 separator bit that must be set, float32 repeat (:715-:747). Survival heartbeat = three
slots Hunger, Thirst, Hunger(dup) (:420-:430). Full frames: slot offsets 831 growth, 897 health, 963 maxHealth, 1029
stamina, 1095 maxStamina, 237 maxHunger, shift -100 for the shorter form (:283-:289); sparse form offsets
229/361/823/889/955/1021 with 3 repeated maxHealth at 1285/1318/1351 (+100 prefix variant) (:335-:352). Self handle
needs 2 consecutive heartbeat hits, timeout 12 s (:16-:17).
Verdict: CONFIRMED as already implemented in `tracking.py` (same offsets, lines 461-466, 671, 695). These offsets
describe the OWNER's full attribute-set batch only. The reference never applies them to other actors.

### 3.4 Identity, species, name, class (what exists, all mechanism-level)
- Export lists follow a batch payload (`ProbeIrisMetadataReaders.cs:17-:52`): (1) net-token string exports
  (token = packed u32 index + authority bit + 3-bit type id, then UTF-8 string = 1 bit + 16-bit byte length + bytes),
  (2) object-reference exports (handle + optional path string + optional outer reference, recursive up to depth 32),
  (3) must-be-mapped handle list. Full path is built as `outerPath.relativePath` (:234-:238). This is how an
  object handle (for example an archetype or a class) resolves to a path string such as an asset name.
- Actor-creation record (`ProbeUnrealIrisActorCreationScanner`, :427-:499), read from the batch payload start:
  3 bits skipped, 1 bit has-state, 1 bit is-creation, 1 bit has-baseline (+2 bits), 3-bit factory id (actor factory = 0),
  32-bit protocol id, 1 bit has-archetype, archetype as an imported object reference (client-ref bit 0, exported
  bit 0, valid bit 1, packed u64 handle), 1 bit persistent-level, optional level reference, 1 bit has-location,
  then (valid bit, 7-bit quantized vector header, 3 signed components). The probe divides scaled spawn locations by 8
  (:532), which is NOT the /100 used for state updates; unexplained, treat as unverified.
- Therefore: species is derived by resolving the creation record's ARCHETYPE handle through exported object paths
  (`BP_<Species>_C` or `Default__...` style names; the normalisation the reference applies is in
  `CreatureSpeciesIdentity.cs:15-:47`: strip `Default__`, `BP_` or `TI`, strip `_C`, lowercase alphanumerics, aliases
  trex->tyrannosaurus, utahraptor->omniraptor, trice/trike->triceratops). Evidence in
  `docs/pro-tracking-validation-20260912.md:44-:48`: handle 578656 "references Allo, Dryo, Herrera and Trice assets: an
  asset name alone cannot establish actor species/control". So an export path match is necessary but not sufficient.
- Player vs AI: the Agent output separates `Kind` Player/AI (`RemotePlayerTelemetry.cs:115-:120`). Player proof =
  structural Iris identity: an actor handle, a PlayerState handle or a Pawn handle (`LocalPositionSnapshotMerger.cs:320-:324,
  :415-:424, :509-:512`); AI is accepted only when the Agent classified an exact non-player fauna archetype. Names are
  explicitly "optional presentation metadata and never proof" (:320). The reference calls this a "name-free protocol"
  (`RemotePlayerTelemetry.cs:62`). So a PlayerState object exists per player in the stream (separate handle from the pawn).
- Name: the Agent field `PlayerProofName`/`ingameName` exists (`RemotePlayerTelemetry.cs:133`,
  `scripts/Analyze-ReferenceActors.ps1:37`), but the public repo contains no code that reads it from packets, and its
  validation docs say the pipeline has since become name-free. No bit layout for names is published anywhere.
- Mass (kg), diet: Agent output (`RemotePlayerTelemetry.cs:136-:137`), used for labels like "T-Rex 12T"
  (`CreatureMarkerLabelFormatter.cs`). Source of mass is not in the public repo; there is no species-id table (the
  reference has a name normaliser, not an id table). No numeric "class id" exists anywhere in it.

Verdict: UNVERIFIABLE with our data (no creation record, export, or token stream in any fixture, see 5). Mechanism is
plausible and consistent with ours: all 126 recorded batches have first five payload bits `01000` (3 skipped bits
zero, has-state=1, is-creation=0), i.e. every recorded batch is a state delta and none is a creation, which is why
no strings are present.

### 3.5 When identity is sent
`src/TheIsleOverlay.ProClient/PrewarmedRemotePlayerTelemetrySource.cs:6-:10`: "Iris sends actor creation and
identity proof only once, so delaying the Agent until the map window opens permanently loses bootstrap evidence for
already-rendered players". `RELEASE_NOTES.md:286`: "bootstrap reconnect so a creation packet is not missed".
Validation doc: 201 creation records and 17 export records in a 180 s capture (21,344 batches, 228 destroy records)
(`docs/pro-tracking-validation-20260912.md:20-:23`). Also: the 16 identity records/201 creations "are NOT counts of
nearby players" (:24).
Consequence for us: start capturing BEFORE joining/relevance (or force a reconnect), otherwise already-visible
actors will never reveal name/species; steady-state captures (all our fixtures) cannot contain them. This
matches the empty "Name"/"Class" rows in `docs/PEERS.md`.

### 3.6 Vitals for OTHER actors
Not decoded by the reference. README:26 "Growth, Health, Stamina, Hunger and Water when the source provides the
field" and :31 "each teammate has a row of HP, Hunger, Water" refer to the own dino and to teammates via the relay
(their own apps send them). Pro output for others carries species, kind, diet, mass only. We should not expect
GAS percentages for strangers from this approach.

### 3.7 Heading for OTHER actors
Not decoded by the reference. The only heading logic: own yaw from outbound moves (3.2), and a movement-derived
heading for the local marker (`MovementHeading.cs`: atan2(dx, -dy), min 100 cm, smoothing weight 0.72,
used only in `MainWindow.xaml.cs:768-:782` for the local player). Remote markers carry no heading field
(`VerifiedRemoteEntityTelemetry`, `RemotePlayerTelemetry.cs:130-:149`). Our decision (no heading inference) is unchanged.

### 3.8 Despawn / lifecycle
- Destruction records are parsed but the public parser discards the handles (`UnrealIrisPacketParser.cs:236-:245`).
  The Agent clearly uses them: validation checklist item "destroy removal", "destroy/session generation
  invalidation" (`pro-tracking-validation-20260912.md:67-:70`).
- Public time-based lifecycle (`RemoteEntityLifecycleTracker.cs:33-:35`): stale after 2 s, removed after 6 s without a
  frame; session/endpoint change resets everything (:51-:56, :128-:135). Ours: dim 3 s, remove 5 s.
- Pro Agent retention rules (`RemotePlayerTelemetry.cs:151-:158`): presence 90 s, max position retention 6 min,
  location freshness 2 s, initial admission 15 s. Display-only; stale markers are drawn dimmed, never refreshed.
- Range gate: 1 km, "retain 1 km gate, local-player exclusion" (validation doc :67); aquatic allowlist Coel/Turtle (same line).
- Local exclusion by handle, never by coordinate overlap (`RemotePlayerMapMarkerResolver.cs`, comment at ~:130:
  "Never infer local identity from coordinate overlap").

Verdict: our destroyed_handles design CONFIRMED in structure; the claim that destruction is the reliable despawn
signal is plausible (228 records in 180 s) but remains UNVERIFIED against our data (one record, no matching live handle).

### 3.9 Other-actor location (155/158-bit batches)
The reference documents none of: the 33-bit fixed header `0x20E822A8`, the 155/158 sizes, or the 37 trailing bits.
It documents the quantised-vector header and component layout generically (3.2) and the creation-time location (3.4).

Verdict on our findings: NOT CONTRADICTED, NOT INDEPENDENTLY CONFIRMED by the reference. The reference's own
description of a state batch prefix (3 bits, has-state, is-creation) is consistent with the low 5 bits of our 33-bit
header (0x...A8 -> bits 0..2 zero, bit 3 = 1, bit 4 = 0). Hence the header is probably (prefix + change mask) rather
than a magic constant, so decoding by exact header equality is brittle: a different dirty-property mask would change it
(see plan 6.4).

## 4. Contradictions with our current findings

None found. Points of tension worth noting:
1. Creation-time vector scaling: the probe uses /8 (scaled) where state updates use /100. Different serializer or a
   probe bug. Do not reuse that number.
2. Handle semantics: reference tracks three kinds of handle per player (actor/pawn, PlayerState, and `TrackId` =
   actor handle). Our "handle = the replicated object in a 155/158 batch" is one of those; we do not know which.
   Our own-handle lock uses the owner pawn's survival heartbeat, so it is the pawn/ASC owner object. Mapping other
   actors' PlayerState handles to pawns needs a creation/export capture (an object reference between them).
3. Their "player vs AI" is not inferable from state batches either; they require a classifier on the archetype.

## 5. Validation against `tests/fixtures/recorded_payloads.json` (scripts in /tmp/scratch, `python -I`)

| Claim | Result |
|---|---|
| Envelope walk, destruction record layout, batch offsets | CONFIRMED (11/11 Iris payloads parse; 1 destruction record, ends `inline-destruction`) |
| Stream mask has NetToken bit only on first contact | CONSISTENT: mask is 0b10 in all 11, so no token strings in fixtures |
| Exports (`has_exports`) | 0 of 126 batches; creation records (has-state & is-creation) 0 of 126; all first-5-bit prefixes are `01000` |
| Strings (names / class paths) in other-actor batches | none (only random ASCII-looking runs of 5 chars in 8 payloads, noise) |
| 66-bit GAS slot layout on other actors | UNVERIFIABLE: non-own batches show lone slots or 2 slots 66 bits apart (e.g. handle 80984 at 289/355: 620.877, 483.054; handle 61016 at 295/361: 470.133, 541.388); the Hunger/Thirst/Hunger heartbeat triple appears only on the three own handles (103940, 54816, 208310). No ground truth ties any slot to Health/Stamina for others |
| 55 location batches / 53 distinct handles / z in 257..378 m | CONFIRMED (our decoder) |
| 37 trailing bits = rotation | UNVERIFIABLE. Structure of the tail across 55 batches: bits 0,1 always 1; bits 2, 35, 36 always 0; bits 14..18 move together (23/55 set, looks like sign extension of a short signed field). A 7-bit NetQuantize header + 3 comps fits the 37 bits at only 4/55 batches (chance level). No compressed rotation (flag+16-bit) structure fits (bit 3 is not constant). Needs ground truth (known player turning) or a delta capture |
| Reference movement-heading derivation applies to others | NOT applicable (local only) |

## 6. Implementation plan for our repo (no source edited in this pass)

Only items resting on confirmed facts are scheduled; the rest is gated on a new capture.

### 6.1 Confirmed, implement now (small, safe)
1. `decode.py`: expose destruction handles via a documented `IrisPacket.destroyed_handles` (already present). Add
   a pure decoder for the creation record prefix so we can at least CLASSIFY batches: `batch_kind(bits, batch) ->
   "creation" | "state" | "unknown"` using the 3-skip/has-state/is-creation bits. Test: all 126 fixture batches are
   `state`; a synthetic bit string with creation bits set returns `creation`.
2. `tracking.py` / capture pipeline: count and expose diagnostics (not features): number of creation batches, export
   batches, net-token streams, destruction records seen per session. Surface in `GamePeerSource.status()` e.g.
   `game: N actors (position only; 0 creation records seen: start capture before joining)`. Tests with fixtures
   (expects 0 creation, 0 exports, 1 destruction).
3. `peers.py`: keep time-based despawn as the primary rule (reference does the same publicly), keep destroyed_handles
   removal. Optionally adopt "stale at 2 s, remove at 6 s" semantics only if the owner wants parity; our 3 s/5 s is fine.
4. `docs/PEERS.md`: add a line that identity is sent once at creation, so a capture must start before the actor
   becomes relevant, and that vitals of strangers are not replicated in a form anyone has decoded.

### 6.2 Gated on a connect-time capture (CANNOT be confirmed today)
When a pcap that spans a join/reconnect exists (60 s as already specified in PEERS.md, but the start point matters:
begin capture, THEN launch/rejoin):
1. `decode.py`: implement `read_net_token_stream` (strings by token, same layout as `_skip_net_token_stream` which
   already knows the framing: packed u32 token, authority bit, 3-bit type, 1 bit, 16-bit byte count, bytes) and
   an `ExportTracker` (net-token strings + object-reference exports + must-be-mapped handles, recursive, depth 32,
   atomic commit of pending dicts so a malformed export never poisons state). Re-implement from the format
   description in 3.4, not by copying. Key output: `handle -> full path`.
2. `decode.py`: `decode_creation(bits, batch) -> Creation(handle, factory_id, protocol_id, archetype_handle,
   persistent_level, spawn_location|None)` per 3.4.
3. New `isle_livemap/identity.py` (or in `tracking.py`): `IdentityTracker` keyed by net_ref_handle, combines creation
   archetype -> export path -> `species` via a normaliser equivalent to 3.4 (strip `Default__`, `BP_`, `_C`, lowercase,
   aliases), and a diet/short-name table that WE build from observed paths (no ids exist in the reference).
   Confidence rule from the reference: a path match alone is not proof (the 578656 example), require creation +
   path on the same actor handle.
4. `peers.py`: add `species`, `kind` fields (`PeerEntity`/`Entity` already carries optional fields: fill `class`/`name`
   only when proven, else `None` so the UI keeps `?`). Do not guess names; there is no published layout. Name
   decoding stays out until a capture shows a string in a PlayerState batch (look at batches with `has_exports` or
   a net-token stream, and at FString-sized UTF-8 runs).
5. Tests: add the new capture as `tests/fixtures/recorded_join_payloads.json` (hex per packet), assert
   creation count > 0, at least one archetype path resolves, and species of a known nearby player (note ground truth).

### 6.3 Do NOT build
- Vitals percentages for other actors from packets: no evidence anyone decodes them; the reference gets teammate
  vitals via a network relay. Keep `?`.
- Heading from the 37 tail bits: unverified. Keep dot glyph. (Movement-derived heading remains rejected per Q4.)
- Player-vs-AI: only possible via archetype classification (6.2.3); until then keep everyone as `player` and the
  inert `show_ai`.

### 6.4 Robustness of the current location decoder (small hardening, optional)
`decode_actor_location` requires the exact 33-bit header `0x20E822A8` and a size equation. If the header is really
"prefix + change mask" the mask for other actors with different dirty properties will not match, silently dropping
them. Suggest: relax to prefix bits (`state, not creation`) + the 7-bit vector header + size equation consistency, and log
(count only) batches that match the size equation but not the exact header. Needs a decision since it can raise
false positives; verify on fixtures that the 55 locations are unchanged and that no extra batch (126-55 = 71) decodes.

## 7. Questions for the owner / next capture
- Capture that starts before joining and runs through at least one neighbour spawning in range and one leaving
  (reference saw 201 creation records and 228 destroy records in 180 s on a populated server).
- Write down: that neighbour's name, species and (if available) in-game handle, to match against the first creation records.
- We should also decide whether to confirm the MIT claim with upstream `klong-dev/IsleLiveMap`, since the Undo fork has no LICENSE file.
