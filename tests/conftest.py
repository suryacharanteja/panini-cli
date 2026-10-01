import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def no_personal_sandbox(monkeypatch):
    # A developer's .env may turn on the Docker sandbox; these tests run commands on the host.
    monkeypatch.setenv("PANINI_CLI_SANDBOX", "none")  # .env loading never overrides a set value.
