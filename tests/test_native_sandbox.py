import asyncio
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from panini_cli.agent import native_sandbox


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_native_environment_does_not_inherit_unknown_host_variables(tmp_path):
    values = native_sandbox.native_environment(
        tmp_path,
        {
            "SystemRoot": "C:\\Windows",
            "PATH": "C:\\Windows\\System32",
            "PANINI_SECRET_VALUE": "must-not-pass",
            "OPENAI_API_KEY": "must-not-pass",
        },
    )
    assert values["TEMP"] == str(tmp_path)
    assert values["USERPROFILE"] == str(tmp_path)
    assert "PANINI_SECRET_VALUE" not in values
    assert "OPENAI_API_KEY" not in values


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_output_and_workspace_write(tmp_path):
    ready, reason = native_sandbox.available()
    if not ready:
        pytest.skip(reason)
    target = tmp_path / "result.txt"
    streamed = []

    async def on_output(value):
        streamed.append(value)

    command = f"Set-Content -LiteralPath '{target}' -Value 'inside'; Write-Output PANINI_NATIVE_OK"
    result = await native_sandbox.run_command(command, tmp_path, 10, on_output)
    assert result.exit_code == 0 and not result.timed_out
    assert target.read_text().strip() == "inside"
    assert "PANINI_NATIVE_OK" in result.output
    assert "PANINI_NATIVE_OK" in "".join(streamed)


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_denies_outside_write(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.txt")
    assert not outside.exists()
    command = f"Set-Content -LiteralPath '{outside}' -Value 'escaped'"
    await native_sandbox.run_command(command, tmp_path, 10)
    assert not outside.exists()


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_denies_symlink_escape(tmp_path):
    with tempfile.TemporaryDirectory(prefix="panini-native-outside-") as outside_dir:
        outside = Path(outside_dir)
        link = tmp_path / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Windows symlink creation unavailable: {exc}")
        target = link / "escaped.txt"
        with pytest.raises(OSError, match="reparse point"):
            await native_sandbox.run_command(
                f"Set-Content -LiteralPath '{target}' -Value 'escaped'",
                tmp_path,
                10,
            )
        assert not (outside / "escaped.txt").exists()


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_denies_junction_escape(tmp_path):
    with tempfile.TemporaryDirectory(prefix="panini-native-outside-") as outside_dir:
        outside = Path(outside_dir)
        link = tmp_path / "junction"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode:
            pytest.skip(f"Windows junction creation unavailable: {created.stderr}")
        target = link / "escaped.txt"
        with pytest.raises(OSError, match="reparse point"):
            await native_sandbox.run_command(
                f"Set-Content -LiteralPath '{target}' -Value 'escaped'",
                tmp_path,
                10,
            )
        assert not (outside / "escaped.txt").exists()


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_appcontainer_denies_preexisting_junction_without_preflight(tmp_path, monkeypatch):
    """Prove the OS boundary, independently of PANINI's preflight link scan."""
    with tempfile.TemporaryDirectory(prefix="panini-native-outside-") as outside_dir:
        outside = Path(outside_dir)
        (outside / "secret.txt").write_text("PRIVATE_SENTINEL", encoding="utf-8")
        link = tmp_path / "junction"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True, text=True, check=False,
        )
        if created.returncode:
            pytest.skip(f"Windows junction creation unavailable: {created.stderr}")
        monkeypatch.setattr(native_sandbox, "_reject_unsafe_workspace_links", lambda _: None)
        result = await native_sandbox.run_command(
            "Get-Content -LiteralPath '.\\junction\\secret.txt'; "
            "Set-Content -LiteralPath '.\\junction\\escaped.txt' -Value escaped",
            tmp_path, 15,
        )
        assert "PRIVATE_SENTINEL" not in result.output
        assert not (outside / "escaped.txt").exists()


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_allows_junction_within_workspace(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "alias"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, check=False,
    )
    if created.returncode:
        pytest.skip(f"Windows junction creation unavailable: {created.stderr}")
    result = await native_sandbox.run_command(
        "Set-Content -LiteralPath '.\\alias\\inside.txt' -Value inside", tmp_path, 15,
    )
    assert result.exit_code == 0, result.output
    assert (target / "inside.txt").read_text().strip() == "inside"


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_ignores_broken_package_junction(tmp_path):
    missing = tmp_path / "package-that-was-moved"
    link = tmp_path / "package-link"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(missing)],
        capture_output=True, text=True, check=False,
    )
    if created.returncode:
        pytest.skip(f"Windows junction creation unavailable: {created.stderr}")
    result = await native_sandbox.run_command("Write-Output PANINI_LINK_OK", tmp_path, 15)
    assert result.exit_code == 0 and "PANINI_LINK_OK" in result.output


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_denies_hardlink_escape(tmp_path):
    with tempfile.TemporaryDirectory(prefix="panini-native-outside-") as outside_dir:
        outside = Path(outside_dir) / "private.txt"
        outside.write_text("PANINI_PRIVATE_SENTINEL", encoding="utf-8")
        link = tmp_path / "hardlink.txt"
        try:
            os.link(outside, link)
        except OSError as exc:
            pytest.skip(f"Windows hard-link creation unavailable: {exc}")
        with pytest.raises(OSError, match="hard link"):
            await native_sandbox.run_command(
                f"Set-Content -LiteralPath '{link}' -Value 'escaped'",
                tmp_path,
                10,
            )
        assert outside.read_text(encoding="utf-8") == "PANINI_PRIVATE_SENTINEL"


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_cannot_create_hardlink_to_outside_file(tmp_path):
    with tempfile.TemporaryDirectory(prefix="panini-native-outside-") as outside_dir:
        outside = Path(outside_dir) / "private.txt"
        outside.write_text("PANINI_PRIVATE_SENTINEL", encoding="utf-8")
        link = tmp_path / "created-hardlink.txt"
        await native_sandbox.run_command(
            f'cmd.exe /d /c mklink /H "{link}" "{outside}"',
            tmp_path,
            10,
        )
        assert not link.exists()
        assert outside.read_text(encoding="utf-8") == "PANINI_PRIVATE_SENTINEL"


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_cannot_escape_through_new_junction(tmp_path):
    with tempfile.TemporaryDirectory(prefix="panini-native-outside-") as outside_dir:
        outside = Path(outside_dir)
        private = outside / "private.txt"
        private.write_text("PANINI_PRIVATE_SENTINEL", encoding="utf-8")
        link = tmp_path / "new-junction"
        command = (
            f'cmd.exe /d /c mklink /J "{link}" "{outside}"; '
            f'Set-Content -LiteralPath "{link / "escaped.txt"}" -Value escaped; '
            f'Get-Content -LiteralPath "{link / "private.txt"}"'
        )
        result = await native_sandbox.run_command(command, tmp_path, 10)
        assert not (outside / "escaped.txt").exists()
        assert private.read_text(encoding="utf-8") == "PANINI_PRIVATE_SENTINEL"
        assert "PANINI_PRIVATE_SENTINEL" not in result.output


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_denies_device_path_outside_workspace(tmp_path):
    with tempfile.TemporaryDirectory(prefix="panini-native-outside-") as outside_dir:
        outside = Path(outside_dir) / "private.txt"
        outside.write_text("PANINI_PRIVATE_SENTINEL", encoding="utf-8")
        device_path = "\\\\?\\" + str(outside)
        await native_sandbox.run_command(
            f"Set-Content -LiteralPath '{device_path}' -Value 'escaped'",
            tmp_path,
            10,
        )
        assert outside.read_text(encoding="utf-8") == "PANINI_PRIVATE_SENTINEL"


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_child_process_keeps_workspace_boundary(tmp_path):
    inside = tmp_path / "child-inside.txt"
    outside = tmp_path.parent / (tmp_path.name + "-child-outside.txt")
    assert not outside.exists()
    command = f"cmd.exe /d /c 'echo inside > \"{inside}\"'; cmd.exe /d /c 'echo outside > \"{outside}\"'"
    await native_sandbox.run_command(command, tmp_path, 10)
    assert inside.exists()
    assert not outside.exists()


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_bounds_large_output(tmp_path):
    with pytest.raises(OSError, match="output exceeded"):
        await native_sandbox.run_command("Write-Output ('x' * 5000000)", tmp_path, 10)


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_timeout(tmp_path):
    started = time.monotonic()
    result = await native_sandbox.run_command("Start-Sleep -Seconds 30", tmp_path, 1)
    assert result.timed_out and result.exit_code is None
    assert time.monotonic() - started < 10


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
async def test_native_command_cancellation(tmp_path):
    task = asyncio.create_task(native_sandbox.run_command("Start-Sleep -Seconds 30", tmp_path, 30))
    await asyncio.sleep(1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
