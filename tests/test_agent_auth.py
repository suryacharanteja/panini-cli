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


def test_jev_loaded_when_openai_is_already_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "existing-openai")
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("PANINI_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "PANINI_ROOT", tmp_path)
    monkeypatch.setattr(auth, "read_key", lambda **kw: "saved-jev" if kw["target"] == auth.PROVIDERS["jev"][2] else None)
    (tmp_path / ".env").write_text("AI_GATEWAY_API_KEY=runtime-jev\n", encoding="utf-8")
    config.load_panini_env()
    assert os.environ["OPENAI_API_KEY"] == "existing-openai"
    assert os.environ["AI_GATEWAY_API_KEY"] == "runtime-jev"


@pytest.mark.skipif(os.name != "nt", reason="Windows Credential Manager only")
def test_saved_jev_loaded_independently(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "existing-openai")
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("PANINI_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "PANINI_ROOT", tmp_path)
    monkeypatch.setattr(auth, "read_key", lambda **kw: "saved-jev" if kw["target"] == auth.PROVIDERS["jev"][2] else None)
    config.load_panini_env()
    assert os.environ["AI_GATEWAY_API_KEY"] == "saved-jev"


def test_jev_validation_does_not_call_openai(monkeypatch):
    from panini_cli.agent import cli
    monkeypatch.setattr(cli, "_validate_login_key", lambda key: pytest.fail("OpenAI called for Jev"))
    assert cli._validate_provider_key("gateway-secret", "jev")[0]
    assert not cli._validate_provider_key("your_key", "jev")[0]


def test_jev_check_requires_gateway_response(monkeypatch):
    from panini_cli.jev import client
    monkeypatch.setattr(client, "available", lambda: (True, "installed"))
    monkeypatch.setattr(client, "ask", lambda *a, **kw: {"ok": True, "answers": {"connected": {"probability": .62}}})
    assert client.check()[0]
    monkeypatch.setattr(client, "ask", lambda *a, **kw: {"ok": True, "answers": {}})
    assert not client.check()[0]


def test_jev_check_does_not_display_provider_secrets(monkeypatch):
    from panini_cli.jev import client
    monkeypatch.setattr(client, "available", lambda: (True, "installed"))
    def fail(*a, **kw):
        raise RuntimeError("Authorization: Bearer private-test-secret")
    monkeypatch.setattr(client, "ask", fail)
    ok, detail = client.check()
    assert not ok
    assert "private-test-secret" not in detail
