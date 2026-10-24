import base64
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def recorded() -> dict[str, str]:
    return json.loads((FIXTURES / "recorded_payloads.json").read_text())


@pytest.fixture(scope="session")
def b64(recorded):
    return lambda name: base64.b64decode(recorded[name])


@pytest.fixture(scope="session")
def hexb(recorded):
    return lambda name: bytes.fromhex(recorded[name])


@pytest.fixture(scope="session")
def join_packets() -> list[dict]:
    """Inbound datagrams of the live join capture (payload bytes only), in capture order. Each entry: group,
    cap_packet (its number in the capture), t (seconds since the capture's first packet), payload (bytes)."""
    data = json.loads((FIXTURES / "recorded_join_payloads.json").read_text())
    return [{**r, "payload": base64.b64decode(r["payload"])} for r in data["packets"]]
