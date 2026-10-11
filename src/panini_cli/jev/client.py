"""Concurrent, correlated JSON-line RPC to one warm Node evaluation sidecar.

The sidecar lives in ``~/.panini/jev`` (or PANINI_JEV_HOME) after ``panini-cli jev setup``;
PANINI_JEV_SIDECAR points at a different script, for example a PANINI source checkout's.
Every failure raises RuntimeError so callers can fall back instead of blocking the task.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import Future, TimeoutError
from pathlib import Path

logger = logging.getLogger(__name__)

PROVIDER_TIMEOUT = 45.0
BUNDLED = Path(__file__).resolve().parent / "sidecar"


def home() -> Path:
    return Path(os.getenv("PANINI_JEV_HOME") or Path.home() / ".panini" / "jev")


def sidecar_script() -> Path:
    return Path(os.getenv("PANINI_JEV_SIDECAR") or home() / "jev_sidecar.mjs")


def configured() -> bool:
    value = os.getenv("AI_GATEWAY_API_KEY", "").strip()
    return bool(value) and not value.startswith("your_")


def available() -> tuple[bool, str]:
    """Whether Jev can run here, and if not, the one thing to fix."""
    if not configured():
        return False, "Jev key missing; run `panini-cli auth login jev` or set AI_GATEWAY_API_KEY"
    if not shutil.which(os.getenv("PANINI_NODE", "node")):
        return False, "Node.js is not on PATH"
    script = sidecar_script()
    if not script.is_file():
        return False, "sidecar not installed; run `panini-cli jev setup`"
    if not (script.parent / "node_modules" / "ai").is_dir():
        return False, "sidecar dependencies missing; run `panini-cli jev setup`"
    return True, str(script)


def setup() -> Path:
    """Copy the bundled sidecar into the Jev home and install its one dependency."""
    target = home()
    target.mkdir(parents=True, exist_ok=True)
    for name in ("jev_sidecar.mjs", "package.json"):
        shutil.copyfile(BUNDLED / name, target / name)
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm was not found; install Node.js 20+ first")
    subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=target, check=True, timeout=180)
    return target


def check() -> tuple[bool, str]:
    """Verify installation, process startup, and gateway access with a tiny request."""
    ready, detail = available()
    if not ready:
        return False, detail
    try:
        result = ask(
            "PANINI connection check.",
            {"connected": {"type": "boolean", "instructions": "Return true for this connection check."}},
            timeout=20,
        )
        if not result.get("ok") or not result.get("answers"):
            return False, "Gateway returned no evaluation answers; run `jev check` again."
        return True, "gateway connection verified; Jev routing is enabled"
    except RuntimeError:
        # Provider exception strings may contain URLs or request headers. Never display credentials.
        return False, "Gateway connection failed. Check your AI Gateway key, network, and gateway quota; run `auth login jev` to replace the key."


class JevClient:
    def __init__(self, command=None):
        self.command = command
        self._process = None
        self._lock = threading.RLock()
        self._pending = {}
        self._ready = None

    def _command(self):
        script = sidecar_script()
        return self.command or [os.getenv("PANINI_NODE", "node"), str(script)], script.parent

    def _read(self, process, ready):
        try:
            for line in process.stdout:
                try:
                    response = json.loads(line)
                except ValueError:
                    logger.warning("Ignoring non-protocol sidecar output")
                    continue
                if not isinstance(response, dict):
                    continue
                if response.get("id") == "ready":
                    ready.set_result(True)
                    continue
                with self._lock:
                    future = self._pending.pop((process.pid, response.get("id")), None)
                if future is not None:  # Late replies belong to expired requests.
                    future.set_result(response)
        except (OSError, ValueError) as exc:
            logger.warning("Jev sidecar protocol failed: %s", exc)
        finally:
            if not ready.done():
                ready.set_exception(RuntimeError("Jev sidecar failed to start"))
            with self._lock:
                for key in [key for key in self._pending if key[0] == process.pid]:
                    self._pending.pop(key).set_exception(RuntimeError("Jev sidecar disconnected"))
                if self._process is process:
                    self._process = None
            if process.poll() is None:
                process.kill()

    @staticmethod
    def _drain_stderr(process):
        for line in process.stderr:
            logger.debug("Jev sidecar: %s", line.rstrip()[:500])

    def start(self):
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                command, cwd = self._command()
                self._process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                    env=os.environ.copy(),
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                self._ready = Future()
                threading.Thread(target=self._read, args=(self._process, self._ready), daemon=True).start()
                threading.Thread(target=self._drain_stderr, args=(self._process,), daemon=True).start()
            process, ready = self._process, self._ready
        try:
            ready.result(timeout=10)
        except Exception:
            self.stop()
            raise RuntimeError("Jev sidecar did not become ready; run `panini-cli jev setup`") from None
        return process

    def ask(self, state, questions, timeout=PROVIDER_TIMEOUT):
        process = self.start()
        request_id = uuid.uuid4().hex
        key = (process.pid, request_id)
        future = Future()
        try:
            with self._lock:
                self._pending[key] = future
                process.stdin.write(
                    json.dumps(
                        {"id": request_id, "state": state, "questions": questions, "timeoutMs": int(timeout * 1000)}
                    )
                    + "\n"
                )
                process.stdin.flush()
            response = future.result(timeout=timeout + 1)
            if not response.get("ok"):
                raise RuntimeError(response.get("error", "Jev evaluation failed"))
            return response
        except TimeoutError:
            raise RuntimeError(f"Jev evaluation timed out after {timeout}s") from None
        except (BrokenPipeError, OSError):
            raise RuntimeError("Jev sidecar disconnected during request") from None
        finally:
            with self._lock:
                self._pending.pop(key, None)

    def stop(self):
        with self._lock:
            process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if process.stdin:
            process.stdin.close()


_client = JevClient()


def ask(state, questions, timeout=PROVIDER_TIMEOUT):
    ok, reason = available()
    if not ok:
        raise RuntimeError(f"Jev unavailable: {reason}")
    return _client.ask(state, questions, timeout)


def stop():
    _client.stop()
