import hashlib

import pytest
from panini_cli.progress import EventJournal, LoopGuard
from panini_cli.verification import FileCondition, file_receipt, verify_files


def test_repeats_stop_and_agent_text_cannot_reset_progress():
    guard = LoopGuard()
    assert [guard.observe({"command": "status"}, {"output": "unchanged"}) for _ in range(4)] == [
        None,
        None,
        None,
        "repeated_action_and_result",
    ]
    guard.verified_progress("host-file-hash")
    assert guard.observe("status", "unchanged") is None
    guard.verified_progress("host-file-hash")
    assert len(guard.history) == 1
    assert "status" not in str(guard.history)


def test_alternating_cycle_and_polling_are_detected():
    guard = LoopGuard(repeat_limit=5)
    results = [guard.observe(str(i % 2), str(i % 2)) for i in range(8)]
    assert results[-1] == "alternating_action_cycle"
    guard = LoopGuard()
    results = [guard.observe({"timeout": i}, "pending", polling_key="job") for i in range(4)]
    assert results[-1] == "polling_repeat"


def test_changing_commands_with_same_failure_still_stop():
    guard = LoopGuard()
    results = [guard.observe(str(i), str(i), error_family="permission-denied") for i in range(4)]
    assert results[-1] == "same_error_family"


def test_action_budget_cannot_be_reset_by_progress():
    guard = LoopGuard(action_limit=2)
    for i in range(2):
        guard.verified_progress(str(i))
        assert guard.observe(i, i) is None
    guard.verified_progress("new")
    assert guard.observe(3, 3) == "action_budget"


def test_replay_survives_restart_and_deduplicates_per_run(tmp_path):
    path = tmp_path / "events.db"
    journal = EventJournal(path)
    assert journal.append("run", "one", {"type": "completed"})
    assert not journal.append("run", "one", {"type": "duplicate"})
    assert journal.append("other", "one", {"type": "other"})
    cursor = journal.replay("run")[0]["cursor"]
    journal.close()
    journal = EventJournal(path)
    assert journal.replay("run") == [{"cursor": cursor, "type": "completed"}]
    assert journal.replay("run", cursor) == []
    journal.close()


def test_postcondition_requires_exact_readback_and_blocks_path_escape(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "answer.txt").write_bytes(b"verified")
    expected = FileCondition("answer.txt", hashlib.sha256(b"verified").hexdigest())
    assert verify_files(workspace, [expected]) == [file_receipt(workspace, "answer.txt")]
    assert verify_files(workspace, [FileCondition("answer.txt", "wrong")]) == []
    (tmp_path / "outside.txt").write_text("outside")
    with pytest.raises(ValueError):
        file_receipt(workspace, "../outside.txt")
