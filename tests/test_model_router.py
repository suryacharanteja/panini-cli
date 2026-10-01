from panini_cli.agent import model_router


def options():
    return [
        model_router.ModelOption("gpt-cheap", "cheap", "cheap"),
        model_router.ModelOption("gpt-balanced", "balanced", "balanced"),
        model_router.ModelOption("gpt-strong", "strong", "strong"),
    ]


def test_model_router_picks_cheapest_confident_sufficient_model():
    decision = model_router.decide(
        prompt="format this file",
        current_model="gpt-balanced",
        answers={
            "model_0": {"probability": 0.91},
            "model_1": {"probability": 0.60},
            "model_2": {"probability": 0.20},
        },
        options=options(),
    )
    assert decision.model == "gpt-cheap"
    assert decision.reason == "jev"
    assert decision.changed


def test_model_router_does_not_downgrade_on_low_confidence():
    decision = model_router.decide(
        prompt="fix the bug",
        current_model="gpt-strong",
        answers={
            "model_0": {"probability": 0.29},
            "model_1": {"probability": 0.20},
            "model_2": {"probability": 0.10},
        },
        options=options(),
    )
    assert decision.model == "gpt-strong"
    assert decision.reason == "low-confidence-no-downgrade/no-change"


def test_model_router_does_not_use_cheapest_model_for_local_machine_tasks():
    decision = model_router.decide(
        prompt="check downloads folder",
        current_model="gpt-cheap",
        answers={
            "model_0": {"probability": 0.91},
            "model_1": {"probability": 0.50},
            "model_2": {"probability": 0.20},
        },
        options=options(),
    )
    assert decision.model == "gpt-balanced"
    assert decision.reason == "local-machine-floor"


def test_model_router_respects_explicit_human_override():
    decision = model_router.decide(
        prompt="use gpt-strong and audit the sandbox",
        current_model="gpt-balanced",
        answers={"model_0": {"probability": 0.99}},
        options=options(),
    )
    assert decision.model == "gpt-strong"
    assert decision.reason == "override"


def test_model_router_models_are_configurable(monkeypatch):
    monkeypatch.setenv(
        "PANINI_CLI_ROUTER_MODELS",
        "gpt-x:nano is invalid tier,gpt-y:frontier:deep release work",
    )
    parsed = model_router.model_options()
    assert [option.id for option in parsed] == ["gpt-x", "gpt-y"]
    assert parsed[0].tier == "strong"
    assert parsed[1].tier == "frontier"


LOCAL = "ollama/qwen2.5:3b-instruct"


def local_options():
    return [model_router.ModelOption(LOCAL, "local", "local"), *options()]


def test_local_models_are_their_own_tier():
    assert model_router._infer_tier(LOCAL) == "local"
    assert model_router.actual_model("local").startswith("ollama/")


def test_jev_can_pick_the_local_model_for_a_simple_task():
    decision = model_router.decide(
        prompt="what is in the README?",
        current_model="gpt-balanced",
        answers={"model_0": {"probability": 0.9}, "model_1": {"probability": 0.9}, "model_2": {"probability": 0.5}},
        options=local_options(),
    )
    assert decision.model == LOCAL


def test_offline_estimate_vetoes_the_local_model_for_a_hard_task():
    decision = model_router.decide(
        prompt="refactor the auth module across all files and migrate the database",
        current_model="gpt-balanced",
        answers={"model_0": {"probability": 0.95}, "model_1": {"probability": 0.4}, "model_2": {"probability": 0.4}},
        options=local_options(),
    )
    assert decision.model != LOCAL
    assert decision.reason.startswith("complexity-veto")


def test_local_machine_tasks_never_go_local():
    decision = model_router.decide_offline(
        prompt="how much RAM do I have", current_model="gpt-balanced", options=local_options()
    )
    assert decision.model == "gpt-balanced"


def test_a_cloud_session_with_history_does_not_drop_to_the_local_model():
    decision = model_router.decide_offline(
        prompt="what is a mutex?", current_model="gpt-strong", options=local_options(), context_tokens=5_000
    )
    assert decision.model == "gpt-strong"
    assert decision.reason.startswith("no-local-after-cloud-context")


def test_offline_routing_picks_the_cheapest_sufficient_model():
    simple = model_router.decide_offline(prompt="git status", current_model="gpt-strong", options=local_options())
    medium = model_router.decide_offline(
        prompt="fix the failing test", current_model="gpt-strong", options=local_options()
    )
    assert simple.model == LOCAL
    assert medium.model == "gpt-cheap"


def test_route_falls_back_offline_when_jev_is_not_configured(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr(model_router, "routing_options", lambda allow_local=True: local_options())
    decision = model_router.route("git status", "gpt-balanced")
    assert decision.model == LOCAL
    assert "jev off" in decision.reason


def test_route_uses_only_the_local_model_without_a_cloud_key(monkeypatch):
    from panini_cli.agent import ollama

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(ollama, "has_model", lambda name, host=None: True)
    decision = model_router.route("refactor everything", LOCAL)
    assert decision.model == LOCAL
    assert decision.reason.startswith("only-model")


def test_local_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("PANINI_CLI_LOCAL", "off")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert all(option.tier != "local" for option in model_router.routing_options())


def test_you_can_ask_for_the_local_model_by_name():
    assert model_router.detect_override("use qwen for this", local_options()) == LOCAL
