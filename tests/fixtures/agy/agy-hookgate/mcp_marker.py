#!/usr/bin/env python3
"""Minimal stdio MCP server: one tool `touch_marker(path)` that creates a file. Logs every call to $MARKER_LOG."""
import json, os, sys

LOG = os.environ.get("MARKER_LOG", "/dev/null")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    mid, method = m.get("id"), m.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": (m.get("params") or {}).get("protocolVersion", "2025-03-26"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "marker", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
            "name": "touch_marker", "description": "Create an empty file at the given absolute path.",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]}})
    elif method == "tools/call":
        path = ((m.get("params") or {}).get("arguments") or {}).get("path", "")
        with open(LOG, "a") as f:
            f.write(f"called touch_marker {path}\n")
        open(path, "a").close()
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"created {path}"}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
