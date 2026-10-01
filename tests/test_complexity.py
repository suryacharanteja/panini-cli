from panini_cli.agent import complexity


def test_tiny_questions_stay_local():
    assert complexity.estimate("what is in the README?").tier == "local"
    assert complexity.estimate("git status").simple


def test_vague_engineering_work_is_not_trusted_to_the_small_model():
    assert complexity.estimate("fix the bug in parse()").tier == "cheap"


def test_hard_words_raise_the_tier():
    text = "refactor the auth module and migrate the database schema"
    assert complexity.estimate(text).tier in {"strong", "frontier"}


def test_easy_words_never_cancel_a_hard_signal():
    assert not complexity.estimate("explain how to redesign the whole architecture").simple


def test_long_context_and_long_prompts_add_weight():
    assert complexity.estimate("hello", context_tokens=20_000).tier != "local"
    assert complexity.estimate("word " * 400).tier != "local"


def test_multi_step_lists_add_weight():
    prompt = "Please do:\n1. one\n2. two\n3. three"
    assert "3-steps" in complexity.estimate(prompt).signals
