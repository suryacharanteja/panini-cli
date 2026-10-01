"""A tiny MCP server over stdio for tests: one `echo` tool and one failing tool."""

import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method, params = message["method"], message.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "echo", "description": "Echo text back.",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
            {"name": "explode", "description": "Always fails.", "inputSchema": {"type": "object", "properties": {}}},
        ]}
    elif method == "tools/call":
        if params["name"] == "echo":
            result = {"content": [{"type": "text", "text": "echo: " + params["arguments"]["text"]}]}
        else:
            result = {"content": [{"type": "text", "text": "boom"}], "isError": True}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "nope"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
