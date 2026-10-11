"""Real packaged-CLI UAT: npm install, offline Vite build/test, authenticated preview.

No model API key is needed. --serve keeps the verified preview open for browser UAT.
"""

import argparse
import hashlib
import http.cookiejar
import json
import os
import queue
import re
import signal
import socket
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path
import contextlib


def dev_preview_phase(binary, project, state):
    """Exercise Vite's real HTTP and HMR handlers through the packaged pipe bridge."""
    import base64
    import httpx
    process = subprocess.Popen([str(binary), "preview", "-C", str(project), "--json", "--timeout", "120",
        "--dev-entry", "node_modules/vite/bin/vite.js", "--dev-arg=--host", "--dev-arg=127.0.0.1",
        "--dev-arg=--port", "--dev-arg=5173", "--dev-arg=--strictPort"],
        env={**os.environ, "PANINI_DATA_DIR": str(state / "live-preview")}, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    preview = None
    try:
        line = lines.get(timeout=40)
        assert line, "Live preview exited before startup"
        preview = json.loads(line)
        assert preview["mode"] == "sandbox-http" and preview["policy"]["hostLoopback"] == "deny", preview
        base = preview["url"].split("/_panini/")[0]
        address = urllib.parse.urlsplit(base)
        with httpx.Client(trust_env=False, follow_redirects=True, timeout=15) as client:
            assert "@vite/client" in client.get(preview["url"]).text
            content = client.get(base + "/@vite/client")
            assert content.status_code == 200
            token = re.search(r'wsToken\s*=\s*"([^\"]+)"', content.text)
            assert token, "Vite client did not advertise its HMR token"
            assert client.get(base + "/src/style.css").status_code == 200
            cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
            with socket.create_connection((address.hostname, address.port), timeout=15) as connection:
                key = base64.b64encode(os.urandom(16)).decode()
                request = (f"GET /?token={token[1]} HTTP/1.1\r\nHost: {address.netloc}\r\nOrigin: {base}\r\n"
                    f"Cookie: {cookie}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: vite-hmr\r\n\r\n")
                connection.sendall(request.encode())
                data = bytearray()
                while b'connected' not in data:
                    chunk = connection.recv(8192)
                    assert chunk and len(data) < 65536, "HMR handshake did not complete"
                    data.extend(chunk)
                assert b"101 Switching Protocols" in data, data[:1000]
                stylesheet = project / "src/style.css"
                original = stylesheet.read_bytes()
                try:
                    stylesheet.write_bytes(original + b"\n/* PANINI HMR UAT */\n")
                    data.clear()
                    while b'update' not in data and b'full-reload' not in data:
                        chunk = connection.recv(8192)
                        assert chunk and len(data) < 65536, "HMR update did not arrive"
                        data.extend(chunk)
                finally:
                    stylesheet.write_bytes(original)
        print("live-preview: Vite HTTP and HMR passed inside offline MXC (stdio bridge).", flush=True)
        return {"http": True, "hmrHandshake": True, "hmrUpdate": True, "policy": preview["policy"]}
    finally:
        stop_preview(process)
        if preview:
            address = urllib.parse.urlsplit(preview["url"])
            try:
                with socket.create_connection((address.hostname, address.port), timeout=1):
                    raise AssertionError("Live preview port remained open after shutdown")
            except OSError:
                pass


def stop_preview(process):
    """Stop the owned tree: a one-file launcher's child may still own its pipes."""
    if process.poll() is None:
        if os.name == "nt":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
            result = subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                                    capture_output=True, timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode and process.poll() is None:
                raise RuntimeError("Could not stop the UAT preview process tree.")
        else:
            os.killpg(process.pid, signal.SIGTERM)
    process.communicate(timeout=15)


def command_phase(binary, project, state, label, commands, hosts=()):
    script = state / f"{label}-model.json"
    script.write_text(json.dumps([
        *({"calls": [{"name": "run_command", "arguments": {"command": command, "timeout_seconds": 180}}]}
          for command in commands),
        {"text": f"{label} commands finished."},
    ]))
    env = {**os.environ, "PANINI_CLI_SCRIPTED_MODEL": str(script), "PANINI_CLI_SANDBOX": "mxc",
           "PANINI_CLI_APPROVAL_MODE": "ask", "PANINI_CLI_NETWORK_HOSTS": ",".join(hosts),
           "PANINI_CLI_LOCAL_PREVIEW": "0", "PANINI_CLI_RECOVERABLE": "0", "PANINI_DATA_DIR": str(state / label)}
    args = [str(binary), "exec", f"Run the {label} UAT commands", "-C", str(project), "--json", "--sandbox", "mxc"]
    for host in hosts:
        args.extend(["--network-host", host])
    result = subprocess.run(args, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding="utf-8", timeout=300)
    (state / f"{label}-events.jsonl").write_text(result.stdout, encoding="utf-8")
    assert result.returncode == 0, result.stderr + result.stdout
    events = [json.loads(line) for line in result.stdout.splitlines()]
    completed = [event["item"] for event in events if event.get("type") == "item.completed"
                 and event["item"]["type"] == "commandExecution"]
    assert len(completed) == len(commands), result.stdout
    for item in completed:
        assert item.get("exitCode") == 0, item
        assert item["executionScope"] == "workspace" and item["sandbox"] == "mxc", item
        assert item["networkMode"] == ("proxy-only" if hosts else "offline"), item
    receipt_files = list((state / label).rglob("receipts/*.jsonl"))
    policies = [event["policy"] for receipt in receipt_files for line in receipt.read_text().splitlines()
                if (event := json.loads(line)).get("policy", {}).get("engine") == "panini-sandbox"]
    assert len(policies) >= len(commands), "Native execution receipts are missing."
    assert all(policy["networkHosts"] == list(hosts) and policy["runtimeIdentity"] for policy in policies)
    print(f"{label}: passed inside MXC ({'registry only' if hosts else 'offline'}).", flush=True)
    return {"commands": commands, "scope": "workspace", "networkHosts": list(hosts), "policy": policies[-1]}


def main(argv=None, *, binary=None, output=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="keep preview open until Ctrl+C or its one-hour expiry")
    parser.add_argument("--open", action="store_true", help="open a browser; requires --serve")
    args = parser.parse_args(argv)
    if args.open and not args.serve:
        parser.error("--open requires --serve")
    binary = Path(binary) if binary else Path(__file__).resolve().parents[3] / "dist/panini-cli.exe"
    output = Path(output or Path.cwd() / "build").resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not binary.is_file():
        raise SystemExit("Build the Windows CLI with scripts/build-windows.ps1 first.")
    installed = subprocess.run([str(binary), "sandbox", "install"], capture_output=True, text=True, timeout=20)
    assert installed.returncode == 0, installed.stderr + installed.stdout
    state = output / "web-sandbox-uat" / uuid.uuid4().hex[:12]
    project = state / "project"
    (project / "src").mkdir(parents=True)
    (project / "package.json").write_text(json.dumps({
        "name": "panini-sandbox-web-uat", "private": True, "version": "1.0.0", "type": "module",
        "scripts": {"build": "vite build", "test": "node --test src/counter.test.js"},
    }, indent=2))
    (project / "index.html").write_text('<!doctype html><html><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>PANINI Sandbox UAT</title></head><body><main id="app"></main>'
        '<script type="module" src="/src/main.js"></script></body></html>')
    (project / "src/counter.js").write_text("export function increment(value) { return value + 1; }\n")
    (project / "src/counter.test.js").write_text("import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
        "import { increment } from './counter.js';\ntest('counter increments repeatedly', () => {\n"
        "  assert.equal(increment(0), 1); assert.equal(increment(increment(0)), 2);\n});\n")
    (project / "src/main.js").write_text("import { increment } from './counter.js';\nimport './style.css';\n"
        "document.querySelector('#app').innerHTML = `<p class='eyebrow'>PANINI / NATIVE MXC</p>"
        "<h1>Your web app.<br>Inside the boundary.</h1><p>Dependencies installed with registry-only access. "
        "Built and tested offline. This browser receives read-only compiled assets.</p>"
        "<div class='checks'><span>INSTALL PASSED</span><span>BUILD PASSED</span><span>TESTS PASSED</span></div>"
        "<button id='counter'>Count: 0</button><p class='hint'>Click to verify the compiled JavaScript.</p>`;\n"
        "let value = 0; document.querySelector('#counter').addEventListener('click', (event) => {\n"
        "  value = increment(value); event.target.textContent = `Count: ${value}`;\n});\n")
    (project / "src/style.css").write_text("*{box-sizing:border-box}body{margin:0;background:#111827;color:#f3f4f6;"
        "font:18px/1.6 system-ui,sans-serif}main{max-width:850px;margin:10vh auto;padding:40px}"
        "h1{font-size:clamp(38px,6vw,68px);line-height:1.12;letter-spacing:-2px;margin:20px 0}"
        "p{max-width:680px;color:#cbd5e1}.eyebrow{color:#86efac;font-size:14px;letter-spacing:3px}"
        ".checks{display:flex;flex-wrap:wrap;gap:12px;margin:30px 0}.checks span{padding:8px 12px;"
        "border:1px solid #355b49;border-radius:6px;color:#86efac;font-size:12px;letter-spacing:1px}"
        "button{background:#86efac;color:#111827;border:0;border-radius:8px;padding:14px 28px;"
        "font:600 18px system-ui;cursor:pointer}button:focus-visible{outline:3px solid white;outline-offset:4px}"
        ".hint{font-size:14px;color:#94a3b8}")
    report = {"project": str(project), "executableSHA256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    # Vite's supported v7 line; --save-exact records the resolved patch for reproduction.
    report["install"] = command_phase(binary, project, state, "install",
        ["npm.cmd install --save-dev --save-exact vite@7 --no-audit --no-fund"], ("registry.npmjs.org",))
    report["buildAndTest"] = command_phase(binary, project, state, "build-test", ["npm.cmd run build", "npm.cmd test"])
    report["viteVersion"] = json.loads((project / "package-lock.json").read_text())["packages"]["node_modules/vite"]["version"]
    report["livePreview"] = dev_preview_phase(binary, project, state)
    index = (project / "dist/index.html").read_text()
    # Synthetic canaries must not be served, even inside the selected output folder.
    (project / "dist/.env").write_text("SYNTHETIC_PREVIEW_SECRET")
    env = {**os.environ, "PANINI_DATA_DIR": str(state / "preview")}
    process = subprocess.Popen([str(binary), "preview", "dist", "-C", str(project), "--json"], env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        try:
            line = lines.get(timeout=20)
        except queue.Empty:
            raise AssertionError("Preview did not start within 20 seconds.") from None
        assert line, "Preview exited without its URL."
        preview = json.loads(line)
        assert preview["type"] == "preview.started" and preview["mode"] == "built-assets", preview
        assert preview["policy"]["executionScope"] == "workspace" and preview["policy"]["networkMode"] == "offline"
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with opener.open(preview["url"], timeout=5) as response:
            assert response.read().decode() == index
            assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
        base = preview["url"].split("/_panini/")[0]
        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', index)
        assert assets, "Vite did not emit compiled assets."
        for asset in assets:
            with opener.open(base + asset, timeout=5) as response:
                assert response.status == 200 and response.read()
        for path in ("/.env", "/%2e%2e/package.json"):
            try:
                opener.open(base + path, timeout=5)
            except urllib.error.HTTPError as error:
                assert error.code == 404
            else:
                raise AssertionError(f"Protected preview path was served: {path}")
        try:
            urllib.request.urlopen(base, timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 403
        else:
            raise AssertionError("Preview accepted an unauthenticated request.")
        report.update(preview=True, previewPolicy=preview["policy"], compiledAssets=assets,
                      previewSecretDenied=True, previewAuthentication=True, noHostEscalation=True)
        report_path = output / "web-sandbox-verification.json"
        report["previewShutdownVerified"] = False
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Project: {project}", flush=True)
        if args.serve:
            print(f"Preview: {preview['url']}\nPress Ctrl+C to stop; expires after one hour.", flush=True)
            if args.open:
                webbrowser.open(preview["url"])
            with contextlib.suppress(KeyboardInterrupt):
                process.wait(timeout=3700)
    finally:
        stop_preview(process)
        if "preview" in locals():
            address = urllib.parse.urlsplit(preview["url"])
            try:
                with socket.create_connection((address.hostname, address.port), timeout=1):
                    raise AssertionError("Preview port remained open after CLI shutdown.")
            except OSError:
                pass
    report["previewShutdownVerified"] = True
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Web sandbox UAT passed. Vite {report['viteVersion']}. Report: {report_path}", flush=True)


if __name__ == "__main__":
    main()
