"""End to end with the live join capture: recorded datagrams -> real ``python -m isle_livemap`` wiring (offscreen Qt)
-> rendered MapView. Creatures decoded from creation records appear as dots, plants and fruit do not.

The pcap is built from ``fixtures/recorded_join_payloads.json`` (46 inbound datagrams, payload bytes only) plus an
outbound own-position stream so the local fix sits among the creatures."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("scapy")

from isle_livemap.geo import peer_style  # noqa: E402
from isle_livemap.replay import encode_move_payload  # noqa: E402
from test_e2e_peers import near_color, run_main, screen_of, settings_dir  # noqa: E402

OWN = (141_000.0, -312_000.0, 38_000.0)  # among the creatures of the capture (the Gallimimus sits ~360 m away)
CYAN = (77, 208, 255)
ORANGE = (143, 166, 179)  # geo.ENTITY_STYLE["ai"].color


def style_rgb(entity) -> tuple[int, int, int]:
    """The colour the minimap paints this creature with (geo.peer_style: players cyan, dinosaurs by diet, animals muted)."""
    from PySide6.QtGui import QColor

    c = QColor(peer_style(entity).color)
    return c.red(), c.green(), c.blue()


def build_join_pcap(path: Path, packets: list[dict]) -> Path:
    from scapy.layers.inet import IP, UDP
    from scapy.layers.l2 import Ether
    from scapy.utils import wrpcap

    client, server, cport, sport = "192.168.0.10", "203.0.113.5", 50_000, 7777
    pkts = []
    t = 1_700_000_000.0
    for i in range(10):  # learn the server and the own position before the first creature
        p = Ether() / IP(src=client, dst=server) / UDP(sport=cport, dport=sport) / encode_move_payload(*OWN, 10.0, 41.0 + i * 0.05)
        p.time = t + i * 0.05
        pkts.append(p)
    t += 0.5
    for k, r in enumerate(packets):  # the capture's order, compressed to 50 ms per datagram (ample for continuity)
        p = Ether() / IP(src=server, dst=client) / UDP(sport=sport, dport=cport) / r["payload"]
        p.time = t + k * 0.05
        pkts.append(p)
        if k % 2 == 0:
            # the client's clock runs with the wall clock: a constant timestamp is not a move (tracking._MIN_TS_RATE)
            move = encode_move_payload(*OWN, 10.0, 41.0 + 0.5 + k * 0.05 + 0.02)
            q = Ether() / IP(src=client, dst=server) / UDP(sport=cport, dport=sport) / move
            q.time = t + k * 0.05 + 0.02
            pkts.append(q)
    wrpcap(str(path), pkts)
    return path


def test_creatures_with_species_and_names_are_drawn_and_world_objects_are_not(tmp_path, monkeypatch, join_packets):
    pk = [r for r in join_packets if r["cap_packet"] <= 3018 or r["cap_packet"] in (5030, 6643)]
    pcap = build_join_pcap(tmp_path / "join.pcap", pk)
    data = settings_dir(tmp_path)

    def ready(app):
        return (app.pipeline.latest.position is not None and app.view.interp.has_fix
                and app.peers.identity.creations >= 100 and len(app.peers.store.entities()) >= 5)

    app, img = run_main(monkeypatch, ["--replay", str(pcap), "--speed", "2", "--data-dir", str(data)], ready)
    assert type(app.peers).__name__ == "GamePeerSource" and app.peers.identity.creations >= 100
    ents = {p.entity.id: p.entity for p in app.peers.store.entities()}
    # only creatures: no plants, fruit, gore, player states, controllers
    assert ents and {e.kind for e in ents.values()} <= {"player", "dino", "ai", "unknown"}
    assert app.peers.filtered_world_objects > 20
    species = {e.species for e in ents.values()}
    assert "Pteranodon" in species and "Rainbow Fish" in species

    near = {n.entity.id: n for n in app.view.nearby()}
    assert near and all(n.distance_m <= 1000 for n in near.values())
    pos = app.pipeline.latest.position
    cx, cy = screen_of(app.view, pos.x, pos.y)
    drawn = 0
    for n in near.values():
        sx, sy = screen_of(app.view, n.x, n.y)
        if math.hypot(sx - cx, sy - cy) < app.view.width() / 2 - 8:
            drawn += near_color(img, sx, sy, style_rgb(n.entity), r=5)
    assert drawn >= 2, "creatures inside the circle must be drawn at their projected positions"
    # the label of a named creature: name, then species in brackets, then distance and bearing
    named = [n for n in near.values() if n.entity.species and not n.entity.name.startswith("Actor #")]
    if named:
        from isle_livemap.geo import peer_label_lines

        head, tail = peer_label_lines(named[0])
        assert head.startswith(named[0].entity.name[:10]) and f" - {named[0].entity.species} - " in head and "HP" in tail
