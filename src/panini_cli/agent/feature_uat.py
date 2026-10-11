"""Offline, packaged UAT for durable RPC, enforced policy, skills and interactive MXC."""

import asyncio
import io
import json
import os
import tempfile
from pathlib import Path

import httpx

from .server import Server


async def run(project, state):
    report = {}
    (project / "tty.cjs").write_text('console.log("TTY="+process.stdout.isTTY);setTimeout(()=>{process.stdout._refreshSize();console.log("COLS="+process.stdout.columns)},1500)')
    (project / "server.cjs").write_text('require("http").createServer((req,res)=>res.end("SSR OK "+req.url)).listen(5173)')
    (project / "image.png").write_bytes(b"fixture image input plumbing")
    skill = project / ".agents/skills/uat/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: uat\ndescription: Packaged skill fixture\n---\nUAT_SKILL_BODY\n")
    owner = Server(io.BytesIO(), io.BytesIO())
    owner.loop = asyncio.get_running_loop()
    started = await owner.thread_start({"cwd": str(project), "sandbox": "readOnly", "approvalPolicy": "never"})
    identifier = started["thread"]["id"]
    agent = owner.threads[identifier][1]
    try:
        assert started["sandbox"]["filesystemMode"] == "read-only"
        denied = await agent.execution.run("Set-Content readonly-denied.txt 'denied'", 10)
        assert denied.exit_code != 0 and not (project / "readonly-denied.txt").exists()
        report["rpcReadOnlyEnforced"] = True
        assert "UAT_SKILL_BODY" not in agent.skills.instructions()
        assert "UAT_SKILL_BODY" in agent.skills.load("uat")
        report["progressiveSkills"] = True
        turn = await owner.turn_start({"threadId": identifier, "input": [
            {"type": "text", "text": "Remember packaged UAT"}, {"type": "localImage", "path": str(project / "image.png")} ]})
        await asyncio.sleep(.05)
        await owner.turn_steer({"threadId": identifier, "expectedTurnId": turn["turn"]["id"],
                               "input": [{"type": "text", "text": "UAT_STEERING_CORRECTION"}]})
        await owner.turns[identifier][1]
        assert "UAT_STEERING_CORRECTION" in json.dumps(agent.items)
        assert "data:image/png;base64," in json.dumps(agent.items)
        report.update(steering=True, typedImages=True)
        worker = await agent.workers.start("Inspect the workspace", "read-only")
        await agent.workers.get(worker["id"]).task
        assert agent.workers.get(worker["id"]).info()["status"] == "completed"
        report["backgroundWorker"] = True
    finally:
        await owner.thread_close({"threadId": identifier})
    await owner.thread_resume({"threadId": identifier})
    try:
        assert "Remember packaged UAT" in json.dumps(owner.threads[identifier][1].items)
        assert (await owner.thread_list({"cwd": str(project)}))["data"]
        assert (await owner.model_list({}))["data"]
        report["durableRpcResume"] = True
    finally:
        await owner.thread_close({"threadId": identifier})
    input_stream = io.BytesIO(b'{"id":[],"result":{}}\n{"id":1,"method":"initialize"}\n')
    output_stream = io.BytesIO()
    await Server(input_stream, output_stream).serve()
    replies = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert next(r for r in replies if r.get("id") == 1)["result"]["protocolVersion"] == 1
    report["malformedRpcRecovery"] = True
    from .config import Settings
    from .execution import ExecutionBroker
    from .terminals import Terminals
    from .preview import Previews
    from .live_preview import start_live
    settings = Settings(sandbox="mxc", local_preview=True)
    terminals = Terminals(project, settings)
    previews = Previews(ExecutionBroker(project, settings))
    previews.terminals = terminals
    try:
        terminal = await terminals.start("node tty.cjs", tty=True)
        await asyncio.sleep(.3)
        terminals.resize(terminal.id, 120, 40)
        await asyncio.sleep(2)
        output = await terminals.read(terminal.id, 1)
        assert "TTY=true" in output and "COLS=120" in output, output
        report.update(sandboxedConPTY=True, terminalResize=True)
        preview = await start_live(previews, terminals, "server.cjs", [])
        def request():
            base = preview.server.url.split("/_panini/")[0]
            with httpx.Client(follow_redirects=True, trust_env=False, timeout=10) as client:
                assert client.get(base).status_code == 403
                response = client.get(preview.server.url)
                assert response.status_code == 200 and response.text == "SSR OK /"
                assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
                assert client.get(base + "/.env").status_code == 404
        await asyncio.to_thread(request)
        report.update(sandboxedSSR=True, previewTransport="stdio", hostLoopback="deny")
    finally:
        await previews.close()
        await terminals.close()
    return report


def main(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="panini-feature-uat-") as directory:
        state = Path(directory)
        project = state / "project"
        project.mkdir()
        script = state / "model.json"
        script.write_text(json.dumps([{"delay": .2, "text": "UAT response"}, {"text": "Steering accepted"}]))
        values = {"PANINI_CLI_SCRIPTED_MODEL": str(script), "PANINI_CONFIG": str(state / "empty-config.toml"),
                  "PANINI_DATA_DIR": str(state / "data"), "PANINI_CLI_NETWORK_HOSTS": "", "PANINI_CLI_SANDBOX": "mxc",
                  "PANINI_CLI_LOCAL_PREVIEW": "0", "PANINI_CLI_RECOVERABLE": "0"}
        saved = {key: os.environ.get(key) for key in values}
        os.environ.update(values)
        try:
            report = asyncio.run(run(project, state))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    path = output / "feature-sandbox-verification.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"RPC, skills, worker, ConPTY and SSR UAT passed. Report: {path}", flush=True)
    return report
