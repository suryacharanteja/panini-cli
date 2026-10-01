from panini_cli.agent import jev_selector


def test_jev_selector_names_each_candidate_and_accounts_for_usage(monkeypatch):
    from panini_cli.jev import client as jev_client

    def ask(state, questions, timeout):
        assert timeout == 15
        assert state["locations"][0]["path"] == "a.py"
        assert "locations[0]" in questions["location_0"]["instructions"]
        assert "locations[1]" in questions["location_1"]["instructions"]
        return {
            "answers": {"location_0": {"probability": 0.2}, "location_1": {"probability": 0.9}},
            "usage": {"inputTokens": 42},
            "cost": 0.01,
            "latencyMs": 30,
        }

    monkeypatch.setattr(jev_client, "ask", ask)
    candidates = [(10, "a.py", 1, "foo", "a"), (9, "b.py", 2, "bar", "b")]
    order, info = jev_selector.select("find bar", candidates)
    assert order == [1, 0]
    assert info["input_tokens"] == 42
    assert info["cost_usd"] == 0.01
