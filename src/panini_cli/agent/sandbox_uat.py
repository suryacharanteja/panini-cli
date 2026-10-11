"""Verify the packaged Windows CLI's native boundary and recovery without a model API."""

import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path


def main(*, binary=None, output=None):
    binary = Path(binary) if binary else Path(__file__).resolve().parents[3] / "dist/panini-cli.exe"
    output = Path(output or Path.cwd() / "build").resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not binary.is_file():
        raise SystemExit("Build the Windows CLI with scripts/build-windows.ps1 first.")
    report = {}
    wheels = sorted((output / "wheels").glob("panini_cli-*.whl"))
    if wheels:
        with zipfile.ZipFile(wheels[-1]) as archive:
            assets = ["panini_cli/panini-sandbox/runtime/win-x64/" + name
                      for name in ("wxc-exec.exe", "mxc_ffi.dll", "LICENSE.md", "build.json")]
            assert all(name in archive.namelist() for name in assets), "Wheel is missing the native sandbox."
            assert not any("/panini-sandbox/src/" in name for name in archive.namelist()), "Wheel includes unused Rust source."
            report["wheelNativeAssets"] = assets
    installed = subprocess.run([str(binary), "sandbox", "install"], capture_output=True, text=True, timeout=15)
    assert installed.returncode == 0, installed.stderr + installed.stdout
    with tempfile.TemporaryDirectory(dir=output, prefix="sandbox-packaged-smoke-") as directory:
        test_root = Path(directory)
        workspace = test_root / "project"
        workspace.mkdir()
        (workspace / "notes.txt").write_text("original")
        (workspace / ".env").write_text("SYNTHETIC_FILE_SECRET")
        script = test_root / "model.json"
        command = ('Write-Output 123; Set-Content result.txt OK; Remove-Item notes.txt; '
                   'try {Get-Content .env -ErrorAction Stop} catch {Write-Output SECRET_DENIED}; '
                   "try {Set-Content '../escape.txt' bad -ErrorAction Stop} catch {Write-Output OUTSIDE_DENIED}; "
                   "Write-Output $env:DATABASE_URL")
        script.write_text(json.dumps([
            {"calls": [{"name": "run_command", "arguments": {"command": command}}]},
            {"text": "Verified command execution."},
        ]))
        env = {**os.environ, "PANINI_CLI_SCRIPTED_MODEL": str(script), "PANINI_CLI_SANDBOX": "mxc",
               "PANINI_CLI_APPROVAL_MODE": "ask", "PANINI_CLI_NETWORK_HOSTS": "", "PANINI_CLI_LOCAL_PREVIEW": "0",
               "PANINI_CLI_RECOVERABLE": "1", "PANINI_DATA_DIR": str(test_root / "data"),
               "DATABASE_URL": "SYNTHETIC_ENV_SECRET"}
        result = subprocess.run([str(binary), "exec", "Run the command", "-C", str(workspace), "--json"], env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=40)
        assert result.returncode == 0, result.stderr
        assert (workspace / "result.txt").read_text().strip() == "OK" and not (workspace / "notes.txt").exists()
        assert not (test_root / "escape.txt").exists()
        assert "SYNTHETIC_FILE_SECRET" not in result.stdout and "SYNTHETIC_ENV_SECRET" not in result.stdout
        assert "SECRET_DENIED" in result.stdout and "OUTSIDE_DENIED" in result.stdout and "123" in result.stdout
        events = [json.loads(line) for line in result.stdout.splitlines()]
        started = next(event["item"] for event in events
                       if event.get("type") == "item.started" and event["item"]["type"] == "commandExecution")
        assert started["executionScope"] == "workspace" and started["sandbox"] == "mxc" and started["networkMode"] == "offline"
        receipt_path = next((test_root / "data").rglob("receipts/*.jsonl"))
        receipts = [json.loads(line) for line in receipt_path.read_text().splitlines()]
        policy = next(event["policy"] for event in receipts if event.get("policy", {}).get("engine") == "panini-sandbox")
        assert policy["runtimeIdentity"] and policy["tier"] == "base-container"
        snapshot = next((test_root / "data" / "recovery").glob("*/manifest.json")).parent.name
        restored = subprocess.run([str(binary), "sandbox", "restore", snapshot, "-C", str(workspace)], env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=15)
        assert restored.returncode == 0, restored.stderr + restored.stdout
        assert (workspace / "notes.txt").read_text() == "original" and not (workspace / "result.txt").exists()
        report.update(packagedCommand=True, unattendedWorkspaceCommand=True, workspaceWrite=True,
                      outsideWriteDenied=True, secretFileDenied=True, secretEnvironmentStripped=True,
                      recoveryRestored=True, policy=policy,
                      executableSHA256=hashlib.sha256(binary.read_bytes()).hexdigest())
    report_path = output / "sandbox-verification.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"Packaged sandbox, secret protection, workspace boundary and recovery passed. Report: {report_path}")


if __name__ == "__main__":
    main()
