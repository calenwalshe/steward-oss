"""MCP protocol server: JSON-RPC 2.0 over HTTP, localhost-only (MCP-01).

Starts the real `http.server.ThreadingHTTPServer` on port 0 (OS-assigned,
loopback only) in a background thread and drives it with `urllib` — no
network beyond localhost, and the server is always shut down at the end of
each test.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from steward import mcp_auth, mcp_server


def _echo_tool(name="echo", required_scope="runner:read", *, raises=None):
    calls = []

    def handler(arguments, token_info):
        if raises is not None:
            raise raises
        calls.append(arguments)
        return {"echoed": arguments, "token_id": token_info.id}

    spec = mcp_server.ToolSpec(
        name=name,
        description="echoes its arguments",
        input_schema={"type": "object", "properties": {}},
        annotations={"readOnlyHint": True},
        required_scope=required_scope,
        handler=handler,
    )
    return spec, calls


@pytest.fixture
def live_server(steward_home):
    tool_spec, calls = _echo_tool()
    write_spec, write_calls = _echo_tool(name="write_thing", required_scope="runner:steer")
    server = mcp_server.MCPServer([tool_spec, write_spec], home=steward_home)
    httpd = mcp_server.make_http_server(server, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base_url, calls, write_calls
    httpd.shutdown()
    thread.join(timeout=5)
    httpd.server_close()


def _post(base_url, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{base_url}/mcp", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None)


def _get(base_url, path):
    with urllib.request.urlopen(f"{base_url}{path}") as resp:
        return resp.status, json.loads(resp.read())


# -- healthz: no auth ---------------------------------------------------------


def test_healthz_requires_no_auth_and_returns_ok(live_server):
    base_url, _, _ = live_server
    status, body = _get(base_url, "/healthz")
    assert status == 200
    assert body == {"ok": True}


# -- auth gate ------------------------------------------------------------


def test_mcp_without_bearer_token_gets_401(live_server):
    base_url, _, _ = live_server
    status, body = _post(base_url, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert status == 401
    assert body["error"]["code"] == mcp_server.UNAUTHORIZED


def test_mcp_with_unknown_token_gets_401(live_server):
    base_url, _, _ = live_server
    status, _ = _post(
        base_url, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="not-a-real-token"
    )
    assert status == 401


# -- initialize / ping / tools/list ----------------------------------------


def test_initialize_echoes_protocol_version_and_server_info(steward_home, live_server):
    base_url, _, _ = live_server
    token, _ = mcp_auth.create_token(home=steward_home)

    status, body = _post(
        base_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": mcp_server.PROTOCOL_VERSION},
        },
        token=token,
    )

    assert status == 200
    result = body["result"]
    assert result["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "steward"
    assert result["capabilities"] == {"tools": {}}


def test_notifications_initialized_gets_202_no_body(steward_home, live_server):
    base_url, _, _ = live_server
    token, _ = mcp_auth.create_token(home=steward_home)

    status, body = _post(
        base_url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, token=token
    )

    assert status == 202
    assert body is None


def test_ping_returns_empty_result(steward_home, live_server):
    base_url, _, _ = live_server
    token, _ = mcp_auth.create_token(home=steward_home)

    status, body = _post(base_url, {"jsonrpc": "2.0", "id": 7, "method": "ping"}, token=token)

    assert status == 200
    assert body == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_tools_list_returns_registered_tools(steward_home, live_server):
    base_url, _, _ = live_server
    token, _ = mcp_auth.create_token(home=steward_home)

    status, body = _post(base_url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=token)

    assert status == 200
    names = {t["name"] for t in body["result"]["tools"]}
    assert names == {"echo", "write_thing"}


# -- tools/call -------------------------------------------------------------


def test_tools_call_invokes_handler_and_returns_structured_content(steward_home, live_server):
    base_url, calls, _ = live_server
    token, _ = mcp_auth.create_token(home=steward_home)

    status, body = _post(
        base_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"x": 1}},
        },
        token=token,
    )

    assert status == 200
    result = body["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["echoed"] == {"x": 1}
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert calls == [{"x": 1}]


def test_tools_call_unknown_tool_is_invalid_params(steward_home, live_server):
    base_url, _, _ = live_server
    token, _ = mcp_auth.create_token(home=steward_home)

    status, body = _post(
        base_url,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "no-such-tool"}},
        token=token,
    )

    assert status == 200
    assert body["error"]["code"] == mcp_server.INVALID_PARAMS


def test_tools_call_insufficient_scope_has_no_side_effects(steward_home, live_server):
    base_url, _, write_calls = live_server
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)  # no runner:steer

    status, body = _post(
        base_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "write_thing", "arguments": {}},
        },
        token=token,
    )

    assert status == 200
    assert body["error"]["code"] == mcp_server.INSUFFICIENT_SCOPE
    assert "insufficient_scope" in body["error"]["message"]
    assert write_calls == []  # handler never ran


def test_unknown_method_is_method_not_found(steward_home, live_server):
    base_url, _, _ = live_server
    token, _ = mcp_auth.create_token(home=steward_home)

    status, body = _post(
        base_url, {"jsonrpc": "2.0", "id": 1, "method": "no/such/method"}, token=token
    )

    assert status == 200
    assert body["error"]["code"] == mcp_server.METHOD_NOT_FOUND


def test_batch_request_returns_a_list_of_responses(steward_home, live_server):
    base_url, _, _ = live_server
    token, _ = mcp_auth.create_token(home=steward_home)

    status, body = _post(
        base_url,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ],
        token=token,
    )

    assert status == 200
    assert [r["id"] for r in body] == [1, 2]


# -- host binding safety -----------------------------------------------------


def test_refuses_to_bind_non_loopback_host_without_allow_host(steward_home):
    server = mcp_server.MCPServer([], home=steward_home)
    with pytest.raises(ValueError):
        mcp_server.make_http_server(server, host="0.0.0.0")
