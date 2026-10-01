"""A real pipe peer for transport tests; deliberately interleaves RPC directions."""

import json
import subprocess
import sys


def send(data):
    print(json.dumps(data), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        send({"id": request["id"], "result": {"userAgent": "fixture"}})
    elif method == "interleave":
        send({"id": "approval", "method": "approve", "params": {"command": "fixture"}})
        send({"method": "progress", "params": {"step": 1}})
        send({"id": request["id"], "result": {"done": True}})
    elif request.get("id") == "approval":
        send({"method": "decision", "params": request["result"]})
    elif method == "bad-frame":
        print("not-json", flush=True)
    elif method == "disconnect":
        sys.exit(0)
    elif method == "child":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
        send({"id": request["id"], "result": {"pid": child.pid}})
    elif method == "never":
        pass
