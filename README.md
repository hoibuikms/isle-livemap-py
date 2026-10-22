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

## Controls (global hotkeys on Windows; also tray menu)

| Key | Action |
|---|---|
| Alt+M | Toggle full map |
| Ctrl+Shift+O | Edit mode (window becomes interactive: drag to move, wheel = zoom, Ctrl+wheel = size 65-175 %, drag = pan on full map, right-click = drop marker). Otherwise the overlay is click-through. |
| Ctrl+Shift+P | Marker at your position (name / type / notes prompt) |
| Ctrl+Shift+L | Marker list (distance + bearing, delete) |
| Ctrl+Shift+= / - | Zoom |

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

## Anti-cheat safety

* Passive packet capture only (Npcap, read-only sniffing of your own UDP flow).
* No DLL injection, no process-memory access (no OpenProcess / ReadProcessMemory on
  the game), no hooks. The game is found with a Toolhelp32 process-list snapshot and
  its UDP ports with `GetExtendedUdpTable`, neither of which opens the game process.
* Nothing is sent or modified; no evasion or obfuscation.
* Separate window. Global hotkeys use `RegisterHotKey` (not a keyboard hook).
* Out of scope by design: other players / AI entities, nearby lists, remote radar.

**The author cannot guarantee that this complies with any anti-cheat or server rules.
Check your server's rules; use at your own risk.**

## Tests

    pip install -r requirements-dev.txt
    python -m pytest

Includes ports of the upstream decoder / tracker / vitals / projection tests using the
upstream recorded packets (`tests/fixtures`), plus rings, markers, waypoints, settings,
a synthetic-packet pipeline test and a headless Qt smoke test. Not testable off
Windows: live Npcap capture, process / port lookup against a real game, global
hotkeys, click-through on Windows, real-game accuracy.

## Credits and assets

Decoding logic ported from Undo-IsleLiveMap / IsleLiveMap (MIT). The map texture
(downscaled) and zone data derive from myislemap.com snapshots bundled by that project;
their redistribution terms are not verified here, so check before redistributing.
`scripts/build_assets.py` regenerates the assets from the upstream files.
