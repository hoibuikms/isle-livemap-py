# isle-livemap (Python port, free tier)

A small always-on-top minimap overlay for **The Isle: Evrima** (Windows). It shows
**only your own** position, heading and vitals, decoded passively from your own
game traffic. Python 3.11+, PySide6. Port of the open-source (MIT) free tier of
Undo-IsleLiveMap. No account, login, key, license check, server or telemetry upload.

## Install and run

    pip install -r requirements.txt
    python -m isle_livemap                 # live (Windows + Npcap, game running)
    python -m isle_livemap --replay        # demo with a synthetic walker, no game needed
    python -m isle_livemap --replay my.pcap [--local-port 54321] [--speed 2]

Live capture needs [Npcap](https://npcap.com) (install with "WinPcap API-compatible mode").
Run in **Borderless/Windowed**; true exclusive fullscreen hides overlays.

## Windows exe build (PyInstaller)

On Windows, run `build_exe.bat`. It creates/uses `.venv`, installs `requirements.txt` and
PyInstaller, and builds a one-folder, windowed app from `IsleLiveMap.spec`:
`dist\IsleLiveMap\IsleLiveMap.exe` (distribute the whole `IsleLiveMap` folder).

* [Npcap](https://npcap.com) must still be installed separately (WinPcap API-compatible mode).
* The exe is unsigned, so Windows SmartScreen may warn; choose "More info" then "Run anyway".
* The game must still run in **Borderless/Windowed** mode. Exclusive fullscreen hides overlays.
* Pass options (e.g. `--replay`) by running the exe from a terminal or a shortcut.

## Controls (global hotkeys on Windows; also tray menu)

| Key              | Action                                                                                                                                                                                     |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Alt+M            | Toggle full map                                                                                                                                                                            |
| Ctrl+Shift+O     | Edit mode (window becomes interactive: drag to move, wheel = zoom, Ctrl+wheel = size 65-175 %, drag = pan on full map, right-click = drop marker). Otherwise the overlay is click-through. |
| Ctrl+Shift+P     | Marker at your position (name / type / notes prompt)                                                                                                                                       |
| Ctrl+Shift+L     | Marker list (distance + bearing, delete)                                                                                                                                                   |
| Ctrl+Shift+= / - | Zoom                                                                                                                                                                                       |

Tray menu: size, range-ring radii (default 250 m / 500 m / 1 km), species label,
zone / food-source / ring toggles.

Features: player-centred circular minimap at 60 fps (position and heading
interpolated); concentric range rings with distance labels; growth / health /
stamina / hunger / water percentages; migration + patrol zones; the 13 food-source
animal layers (as point markers); user-placed markers (waypoint, nest, water,
dinosaur, other) with live distance and bearing, waypoints draw a line from the
player. Markers are purely manual and stored in `%APPDATA%\isle-livemap\markers.json`.

Notes: the own-species name cannot be read from the traffic by this free-tier code,
so it is a label you set yourself. "13 food-source zones": the upstream asset in
this snapshot has no food polygons; the 13 default animal food categories are
bundled as point layers instead. World units are assumed to be Unreal centimetres.

## Nearby players and AI (server feed, optional)

The overlay can show nearby players and AI (dots with name, species, health %, growth %,
distance; minimap radius default 1 km; list panel with Ctrl+Shift+N; tray toggles for
players / AI and "Feed settings...") **only** from a JSON feed published by a server you
own and can mod. It cannot work on servers you do not control; nothing about other
entities is taken from the game's network packets or memory.

    python -m isle_livemap --feed-url https://my-server/feed      # token: ISLE_FEED_TOKEN env or --feed-token
    python tools/mock_feed.py                                      # local demo feed (127.0.0.1:8765)
    python -m isle_livemap --replay --feed-url http://127.0.0.1:8765/feed
    python -m isle_livemap --replay                                # internal synthetic feed

Schema, units, security notes and how to expose the feed from your mod: `docs/SERVER_FEED.md`.

## Troubleshooting (overlay stuck on "waiting" / "capturing")

The overlay and tray tooltip show the stage: waiting for game process, game running but no
UDP socket, capturing with 0 packets (wrong interface?), UDP seen but none on the game's
ports, game packets arriving but no position decoded, tracking.

Without `--iface` the app picks the interface that owns the default outbound route (found
with a UDP `connect`, nothing is sent) and, if that fails or sees no UDP for 5 s, sniffs all
usable interfaces. "capture failed: ..." in the status is the real sniffer error; details and
the interfaces tried are in `%LOCALAPPDATA%\IsleLiveMap\capture.log`. The built exe asks for
administrator rights (UAC) because Npcap capture often needs them.

Run the passive diagnostic (no UI) while playing, ideally from an elevated terminal:

    python -m isle_livemap --diagnose [--iface NAME] [--seconds 10]

It reports Npcap and administrator status, the game process (exact name, falling back to any
`TheIsleClient*` exe; otherwise lists running exes containing "isle"), its UDP ports, all
capture interfaces, then sniffs each interface and prints UDP packets, packets on the game's
ports, decoded positions and bytes, ending with a verdict ("Best interface: ..." or the likely
cause). Then run normally with `python -m isle_livemap --iface "NAME"`; the choice is
remembered in settings.json.

## Tests

    pip install -r requirements-dev.txt
    python -m pytest

Includes ports of the upstream decoder / tracker / vitals / projection tests using the
upstream recorded packets (`tests/fixtures`), plus rings, markers, waypoints, settings,
a synthetic-packet pipeline test, feed parsing / polling tests against a local mock
server, and headless Qt smoke tests. Not testable off
Windows: live Npcap capture, process / port lookup against a real game, global
hotkeys, click-through on Windows, real-game accuracy.

## Credits and assets

Decoding logic ported from Undo-IsleLiveMap / IsleLiveMap (MIT). The map texture
(downscaled) and zone data derive from myislemap.com snapshots bundled by that project;
their redistribution terms are not verified here, so check before redistributing.
`scripts/build_assets.py` regenerates the assets from the upstream files.
