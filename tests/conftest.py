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
