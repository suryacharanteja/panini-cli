import os
import uuid

import pytest
from panini_cli.agent import auth, config


@pytest.mark.skipif(os.name != "nt", reason="Windows Credential Manager only")
def test_credential_manager_round_trip():
    target = f"PANINI-CLI/test/{uuid.uuid4()}"
    try:
        assert auth.read_key(target=target) is None
        auth.save_key("sk-test-panini", target=target)
        assert auth.read_key(target=target) == "sk-test-panini"
        auth.save_key("sk-test-replaced", target=target)
        assert auth.read_key(target=target) == "sk-test-replaced"
        assert auth.delete_key(target=target)
        assert auth.read_key(target=target) is None
    finally:
        auth.delete_key(target=target)


def test_rejects_placeholder_without_writing():
    with pytest.raises(ValueError):
        auth.save_key("your_openai_key")


def test_runtime_env_key_is_loaded_without_workspace_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-panini-env-test\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PANINI_RUNTIME_ROOT", str(tmp_path))
    config.load_panini_env()
    assert os.environ["OPENAI_API_KEY"] == "sk-panini-env-test"
