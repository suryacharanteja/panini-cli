import asyncio
import os
import sys
import time

import pytest
from panini_cli.agent import codex_sandbox
from panini_cli.agent.config import Settings
from panini_cli.agent.shell import run_command, safe_environment
from panini_cli.agent.terminals import Terminals


@pytest.fixture
def anyio_backend():
    return "asyncio"


def python(*arguments):
    """A command line running this Python in the platform's shell."""
    quoted = " ".join(f"'{argument}'" for argument in (sys.executable, *arguments))
    return f"& {quoted}" if os.name == "nt" else quoted


def test_credentials_are_removed_from_the_command_environment():
    env = safe_environment(
        {
            "PATH": "p",
            "OPENAI_API_KEY": "sk",
            "AWS_SECRET_ACCESS_KEY": "s",
            "GITHUB_TOKEN": "t",
            "DB_PASSWORD": "x",
            "TOKENIZERS_PARALLELISM": "false",
            "SSH_AUTH_SOCK": "sock",
        }
    )
    assert {"PATH", "TOKENIZERS_PARALLELISM"} <= set(env)
    assert not {"OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "SSH_AUTH_SOCK"} & set(env)


@pytest.mark.anyio
async def test_output_exit_code_and_scrubbed_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak")
    script = tmp_path / "probe.py"
    script.write_text(
        "import os, sys\nprint('hello', os.environ.get('OPENAI_API_KEY', 'no key'))\nsys.exit(3)\n",
        encoding="utf-8",
    )
    streamed = []

    async def on_output(text):
        streamed.append(text)

    result = await run_command(python(str(script)), tmp_path, 60, on_output)
    assert result.exit_code == 3 and not result.timed_out
    assert "hello no key" in result.output and "sk-must-not-leak" not in result.output
    assert "hello no key" in "".join(streamed)


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="PowerShell semantics")
async def test_failed_powershell_cmdlet_is_a_failure(tmp_path):
    result = await run_command("Get-Item ./does-not-exist", tmp_path, 60)
    assert result.exit_code == 1


def tree(tmp_path):
    """A command whose grandchild writes a marker after a delay unless the tree is killed."""
    marker = tmp_path / "survived.txt"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import pathlib, sys, time\ntime.sleep(5)\npathlib.Path(sys.argv[1]).write_text('x')\n"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "print('started', flush=True)\n"
        "time.sleep(60)\n"
    )
    return python(str(parent), str(grandchild), str(marker)), marker


@pytest.mark.anyio
async def test_timeout_stops_the_whole_process_tree(tmp_path):
    command, marker = tree(tmp_path)
    started = time.monotonic()
    result = await run_command(command, tmp_path, 3)
    assert result.timed_out and result.exit_code is None and "started" in result.output
    assert time.monotonic() - started < 15
    await asyncio.sleep(6)
    assert not marker.exists(), "a grandchild survived the timeout"


@pytest.mark.anyio
async def test_interrupt_stops_the_whole_process_tree(tmp_path):
    command, marker = tree(tmp_path)
    task = asyncio.create_task(run_command(command, tmp_path, 60))
    await asyncio.sleep(2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(6)
    assert not marker.exists(), "a grandchild survived the interrupt"


@pytest.mark.anyio
async def test_terminal_stop_ends_descendants(tmp_path):
    command, marker = tree(tmp_path)
    terminals = Terminals(tmp_path, Settings(sandbox="none"))
    terminal = await terminals.start(command)
    try:
        await asyncio.sleep(2)
        await terminals.stop(terminal.id)
        await asyncio.sleep(6)
        assert not marker.exists(), "a grandchild survived terminal stop"
    finally:
        await terminals.close()


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Codex native Windows sandbox")
async def test_native_sandbox_runs_without_docker(tmp_path):
    ready, reason = codex_sandbox.available(tmp_path)
    if not ready:
        pytest.skip(reason)
    command = "Write-Output PANINI_NATIVE_OK"
    result = await run_command(command, tmp_path, 20, argv=codex_sandbox.argv(command, tmp_path))
    assert result.exit_code == 0 and "PANINI_NATIVE_OK" in result.output
    terminals = Terminals(tmp_path, Settings(sandbox="codex"))
    try:
        terminal = await terminals.start(command)
        output = await terminals.read(terminal.id, 3)
        assert "PANINI_NATIVE_OK" in output
    finally:
        await terminals.close()
