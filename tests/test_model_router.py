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


def test_low_confidence_uses_workload_fallback_without_incumbent_bias():
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
    assert decision.model == "gpt-cheap"
    assert "below capability thresholds" in decision.reason


def test_machine_topic_does_not_impose_model_floor():
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
    assert decision.model == "gpt-cheap"
    assert decision.reason == "jev/no-change"


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


def test_jev_capability_assessment_is_not_overridden_by_keyword_veto():
    decision = model_router.decide(
        prompt="refactor the auth module across all files and migrate the database",
        current_model="gpt-balanced",
        answers={"model_0": {"probability": 0.95}, "model_1": {"probability": 0.4}, "model_2": {"probability": 0.4}},
        options=local_options(),
    )
    assert decision.model == LOCAL
    assert decision.reason == "jev"


def test_local_machine_tasks_can_go_local():
    decision = model_router.decide_offline(
        prompt="how much RAM do I have", current_model="gpt-balanced", options=local_options()
    )
    assert decision.model == LOCAL


def test_independent_question_can_return_to_local_after_cloud_history():
    decision = model_router.decide_offline(
        prompt="what is a mutex?", current_model="gpt-strong", options=local_options(), context_tokens=5_000
    )
    assert decision.model == LOCAL
    assert decision.context_policy == "independent"


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
    assert "jev unavailable" in decision.reason


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


def test_cheapest_capable_wins_even_when_stronger_scores_higher():
    decision = model_router.decide(prompt="explain the README", current_model="gpt-strong", options=local_options(),
        answers={"model_0": {"probability": .8}, "model_1": {"probability": .9}, "model_3": {"probability": .99}})
    assert decision.model == LOCAL


def test_greeting_never_calls_paid_router(monkeypatch):
    from panini_cli.jev import client
    monkeypatch.setattr(model_router, "routing_options", lambda **kwargs: local_options())
    monkeypatch.setattr(client, "ask", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("paid routing")))
    decision = model_router.route("hi", "gpt-strong", 50000)
    assert decision.model == LOCAL
    assert decision.context_policy == "independent"
    assert decision.cost_usd == 0


def test_followup_keeps_cloud_history():
    decision = model_router.decide_offline(prompt="explain that again", current_model="gpt-strong",
                                          options=local_options(), context_tokens=50000)
    assert decision.model == "gpt-strong"
    assert decision.context_policy == "full"


def test_jev_assesses_capability_instead_of_subject_floor():
    decision = model_router.decide(prompt="refactor auth and migrate database", current_model="gpt-balanced",
        options=local_options(), answers={"model_1": {"probability": .99}, "model_3": {"probability": .85}})
    assert decision.model == "gpt-cheap"


def test_malformed_score_is_not_confidence():
    assert model_router._answer_probability({"probability": float("nan")}) == 0
    assert model_router._answer_probability({"probability": True}) == 0


def test_cache_saves_router_cost_and_respects_local_availability(monkeypatch):
    from panini_cli.jev import client
    model_router._cache.clear()
    calls = []
    monkeypatch.setattr(model_router, "routing_options", lambda allow_local=True: local_options() if allow_local else options())
    monkeypatch.setattr(client, "available", lambda: (True, "ready"))
    def ask(*args, **kwargs):
        calls.append(1)
        return {"answers": {"model_1": {"probability": .9}}, "cost": .00004, "usage": {"inputTokens": 900}}
    monkeypatch.setattr(client, "ask", ask)
    first = model_router.route("read the README", "gpt-balanced")
    second = model_router.route("read the README", "gpt-balanced")
    assert len(calls) == 1
    assert first.cost_usd > 0 and second.cost_usd == 0 and second.input_tokens == 0
    model_router.route("read the README", "gpt-balanced", allow_local=False)
    assert len(calls) == 2
    model_router._cache.clear()


def test_router_failure_falls_back_without_exposing_error(monkeypatch):
    from panini_cli.jev import client
    monkeypatch.setattr(model_router, "routing_options", lambda **kwargs: local_options())
    monkeypatch.setattr(client, "available", lambda: (True, "ready"))
    def fail(*args, **kwargs):
        raise OSError("private credential")
    monkeypatch.setattr(client, "ask", fail)
    decision = model_router.route("git log", "gpt-balanced")
    assert decision.model == LOCAL
    assert "private credential" not in decision.reason


def test_simple_definition_bypasses_jev(monkeypatch):
    from panini_cli.jev import client
    monkeypatch.setattr(model_router, "routing_options", lambda **kwargs: local_options())
    monkeypatch.setattr(client, "ask", lambda *a, **k: (_ for _ in ()).throw(AssertionError("paid call")))
    decision = model_router.route("What is a mutex?", "gpt-strong", 5000)
    assert decision.model == LOCAL and decision.cost_usd == 0
    assert decision.context_policy == "independent"


def test_routing_retains_the_cheapest_capable_cloud_alternative():
    decision = model_router.decide(prompt="transform the data", current_model="gpt-balanced",
        options=local_options(), answers={"model_0": {"probability": .9}, "model_1": {"probability": .8},
                                         "model_2": {"probability": .95}})
    assert decision.model == LOCAL and decision.escalation_model == "gpt-cheap"


def test_jev_receives_all_topics_and_shared_tool_capabilities(monkeypatch):
    from panini_cli.jev import client
    model_router._cache.clear()
    monkeypatch.setattr(model_router, "routing_options", lambda **kwargs: local_options())
    monkeypatch.setattr(client, "available", lambda: (True, "ready"))
    captured = []
    def ask(state, questions, **kwargs):
        captured.append(state)
        return {"answers": {"model_0": {"probability": .9}, "model_1": {"probability": .8}}}
    monkeypatch.setattr(client, "ask", ask)
    for prompt in ("inspect memory usage", "write a tiny script", "convert JSON to CSV", "query the connected inventory"):
        assert model_router.route(prompt, "gpt-balanced").model == LOCAL
    assert all(len(state["candidates"]) == 4 for state in captured)
    assert all("All candidates share approved" in state["policy"] for state in captured)
    assert all("cannot edit" not in state["policy"] for state in captured)
    model_router._cache.clear()
