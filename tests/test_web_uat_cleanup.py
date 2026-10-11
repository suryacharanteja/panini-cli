import os
from types import SimpleNamespace

import pytest

from panini_cli.agent import web_uat


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree shutdown")
def test_preview_cleanup_stops_tree_before_draining_pipes(monkeypatch):
    operations = []
    process = SimpleNamespace(pid=123, poll=lambda: None,
                              communicate=lambda timeout: operations.append("drain"))
    def stop(argv, **kwargs):
        assert argv[-4:] == ["/PID", "123", "/T", "/F"]
        operations.append("tree")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(web_uat.subprocess, "run", stop)
    web_uat.stop_preview(process)
    assert operations == ["tree", "drain"]


def test_already_exited_preview_only_drains_pipes(monkeypatch):
    drained = []
    process = SimpleNamespace(poll=lambda: 0, communicate=lambda timeout: drained.append(timeout))
    def unexpected(*args, **kwargs):
        pytest.fail("An exited preview must not trigger process-tree termination")
    monkeypatch.setattr(web_uat.subprocess, "run", unexpected)
    web_uat.stop_preview(process)
    assert drained == [15]
