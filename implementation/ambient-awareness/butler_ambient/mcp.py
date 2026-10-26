"""MCP stdio bridge. No raw media/owner controls; image effects need a real owner request."""
from __future__ import annotations

import json
import socket
import sys

from .display import CARD_SCHEMA


TOOLS = [
    {"name": "butler_observation_status", "description": "Inspect source states and anonymous observation gaps.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "butler_observe", "description": "Fresh camera/screen observation as a locally derived text summary; policy gated.",
     "inputSchema": {"type": "object", "properties": {"source": {"type": "string", "enum": ["screen", "camera"]}},
                     "required": ["source"], "additionalProperties": False}},
    {"name": "butler_activity_summary", "description": "Grounded activity summary for Unix timestamps with coverage and gaps.",
     "inputSchema": {"type": "object", "properties": {"start": {"type": "number"}, "end": {"type": "number"}},
                     "required": ["start", "end"], "additionalProperties": False}},
    {"name": "butler_visual_request", "description": "Fulfill or check the latest actual owner private Telegram image request. No supplied prompt/recipient; service verifies provenance. Returns only status/IDs, never image bytes or private paths.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "butler_display", "description": "Show a card on the Butler display (the main Butler window comes forward). kind text: Markdown (lists, links) in `markdown` plus optional `links`; image: `image` path Butler owns; diagram: Mermaid source in `mermaid`; video: `video` path under ~/.openclaw/media; email: the message `key` from butler-mail only (content is read locally, never sent here). Returns the card id only.",
     "inputSchema": CARD_SCHEMA},
]


def rpc(root, command, args=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(120)
        client.connect(str(root / "ambient.sock"))
        client.sendall(json.dumps({"command": command, "args": args or {}}).encode() + b"\n")
        with client.makefile("rb") as stream:
            result = json.loads(stream.readline(4_000_000))
    if "error" in result:
        raise ValueError(result["error"])
    return result["result"]


def serve(root, reader=None, writer=None, call=None):
    reader, writer = reader or sys.stdin, writer or sys.stdout
    call = call or (lambda command, args: rpc(root, command, args))
    initialized = False
    for line in reader:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                raise ValueError("invalid JSON-RPC request")
            method = request["method"]
            if "id" not in request:  # Notifications do not receive responses.
                continue
            result = None
            error = None
            if method == "initialize":
                initialized = True
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "butler-ambient", "version": "0.1.0"}}
            elif method == "ping":
                result = {}
            elif not initialized:
                error = {"code": -32600, "message": "Initialize first"}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = request.get("params", {})
                name, args = params.get("name"), params.get("arguments", {})
                allowed = {t["name"]: t["inputSchema"] for t in TOOLS}
                if name not in allowed:
                    error = {"code": -32602, "message": "Unknown tool"}
                else:
                    try:
                        schema = allowed[name]
                        if not isinstance(args, dict) or set(args) - set(schema["properties"]) or set(schema.get("required", [])) - set(args):
                            raise ValueError("invalid arguments")
                        if name == "butler_observe" and args["source"] not in {"camera", "screen"}:
                            raise ValueError("invalid source")
                        if name == "butler_activity_summary" and any(type(args[x]) not in (int, float) for x in ("start", "end")):
                            raise ValueError("invalid period")
                        command = {"butler_observation_status": "status", "butler_observe": "observe",
                                   "butler_activity_summary": "summary", "butler_visual_request": "visual-request",
                                   "butler_display": "display"}[name]
                        output = call(command, args)
                        if command == "display":
                            output = {k: output.get(k) for k in ("id", "shown", "kind", "title")}   # never paths or content
                        # Status in MCP omits owner-managed names/agreements entirely.
                        if command == "status":
                            output = {k: v for k, v in output.items() if k != "agreements"}
                        result = {"content": [{"type": "text", "text": json.dumps(output)}]}
                    except Exception:
                        text = "Display unavailable or the card was refused by policy." if name == "butler_display" \
                            else "Observation unavailable or prohibited by policy."
                        result = {"content": [{"type": "text", "text": text}], "isError": True}
            else:
                error = {"code": -32601, "message": "Method not found"}
            response = {"jsonrpc": "2.0", "id": request["id"], "error" if error else "result": error or result}
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        except Exception:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32600, "message": "Invalid request"}}
        writer.write(json.dumps(response) + "\n")
        writer.flush()
