import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402


@pytest.fixture
def windows_sandbox_api():
    from panini_cli.agent import native_sandbox

    ready, reason = native_sandbox.available()
    if not ready:
        pytest.skip(f"Native integration requires the Windows sandbox API: {reason}")


@pytest.fixture(autouse=True)
def no_personal_sandbox(monkeypatch):
    # A developer's .env may turn on the Docker sandbox; these tests run commands on the host.
    monkeypatch.setenv("PANINI_CLI_SANDBOX", "none")  # .env loading never overrides a set value.


@pytest.fixture(autouse=True)
def no_personal_config(monkeypatch, tmp_path_factory):
    # Tests never read the developer's ~/.panini/config.toml.
    monkeypatch.setenv("PANINI_CONFIG", str(tmp_path_factory.getbasetemp() / "no-config.toml"))
    monkeypatch.delenv("PANINI_CLI_PROFILE", raising=False)


@pytest.fixture(autouse=True)
def private_data_dir(monkeypatch, tmp_path):
    # Sessions, receipts and the usage ledger go to a per-test folder.
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path / "panini-data"))
