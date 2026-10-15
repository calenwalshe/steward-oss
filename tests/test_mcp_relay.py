"""MCP write tools relay into the control pane (OPX-03).

`StewardAPI(store, relay=...)` is how `steward.mcp_server`'s write tools
reach the control pane instead of touching the store directly (see
`steward/api.py`'s module docstring). These tests drive `MCPServer` directly
via `handle_authenticated` (no real HTTP, no real Herdr socket) with a
`relay` wired to an in-process `ControlPane` sharing the same store — the
same "fake HerdrApi and an in-process control loop" shape
`tests/conftest.py`'s CLI fixture and `tests/test_cli_relay.py` already use.
"""

from __future__ import annotations

import json

from steward import cli, mcp_auth, mcp_server
from steward.api import StewardAPI
from steward.contract import Contract, store_contract
from steward.control_pane import ControlPane, NoControlPane
from steward.models import JobState, Judgment
from steward.steering import Steering
from steward.store import Store


def _contracted_running_job(store: Store) -> str:
    job = store.create_job("do the thing", "/repo")
    contract = Contract(
        goal="do the thing",
        boundaries=[],
        done_when=["checks pass"],
        next_step="dispatch worker",
        authority=[],
        assumptions=[],
    )
    store_contract(store, job.job_id, contract)
    store.set_state(job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code)
    return job.job_id


def _inprocess_control_relay(store: Store):
    """The real `_build_control_handlers` dispatch table, run synchronously —
    the production shape (`steward.control_pane.relay` over a real Herdr
    socket) minus the socket, exactly like `tests/conftest.py`'s CLI fixture."""
    handlers = cli._build_control_handlers(store)
    pane = ControlPane(store, handlers, print_fn=lambda _line: None)

    def relay(op, args, *, origin, request_id):
        cmd = {"request_id": request_id or "req-mcp", "op": op, "args": args, "origin": origin}
        return pane.execute(cmd)

    return relay


def _relay_raising_no_control_pane(op, args, *, origin, request_id):
    raise NoControlPane("no control pane — start one: `steward control`")


def _call(server: mcp_server.MCPServer, home, token: str, name: str, arguments: dict | None = None) -> dict:
    token_info = mcp_auth.authorize(f"Bearer {token}", home=home)
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    ).encode()
    status, raw = server.handle_authenticated(body, token_info)
    assert status == 200
    return json.loads(raw)


def test_steer_job_relays_with_chatgpt_origin_and_human_instruction(steward_home):
    store = Store(steward_home)
    job_id = _contracted_running_job(store)
    api = StewardAPI(store, relay=_inprocess_control_relay(store))
    server = mcp_server.MCPServer(mcp_server.build_tools(api), home=steward_home)
    token, _ = mcp_auth.create_token(["runner:steer"], home=steward_home)

    body = _call(
        server, steward_home, token, "steer_job",
        {
            "job_id": job_id, "expected_revision": store.get_revision(job_id),
            "instruction": "prioritize the retry loop", "human_instruction": "please look at retries first",
        },
    )

    receipt = body["result"]["structuredContent"]
    assert receipt["accepted"] is True
    record = Steering(store).all_instructions(job_id)[-1]
    assert record["origin"] == "chatgpt"
    assert record["human_instruction"] == "please look at retries first"
    assert record["interpreted_instruction"] == "prioritize the retry loop"


def test_steer_job_conflict_passes_through_the_relay(steward_home):
    store = Store(steward_home)
    job_id = _contracted_running_job(store)
    api = StewardAPI(store, relay=_inprocess_control_relay(store))
    server = mcp_server.MCPServer(mcp_server.build_tools(api), home=steward_home)
    token, _ = mcp_auth.create_token(["runner:steer"], home=steward_home)

    body = _call(
        server, steward_home, token, "steer_job",
        {"job_id": job_id, "expected_revision": 9999, "instruction": "x", "human_instruction": "x"},
    )

    receipt = body["result"]["structuredContent"]
    assert receipt["accepted"] is False
    assert "CONFLICT" in receipt["detail"]


def test_pause_job_relays_and_records_chatgpt_mcp_exception_use(steward_home):
    store = Store(steward_home)
    job_id = _contracted_running_job(store)
    api = StewardAPI(store, relay=_inprocess_control_relay(store))
    server = mcp_server.MCPServer(mcp_server.build_tools(api), home=steward_home)
    token, _ = mcp_auth.create_token(["runner:pause"], home=steward_home)

    body = _call(server, steward_home, token, "pause_job", {"job_id": job_id})

    receipt = body["result"]["structuredContent"]
    assert receipt["accepted"] is True
    assert store.get_job(job_id).state.value == "PAUSED"
    used = [e for e in store.events(job_id) if e.what == "exception_used"]
    assert used and used[-1].evidence["name"] == "chatgpt-mcp"


def test_no_control_pane_is_a_tool_error_not_a_transport_fault(steward_home):
    store = Store(steward_home)
    job_id = _contracted_running_job(store)
    api = StewardAPI(store, relay=_relay_raising_no_control_pane)
    server = mcp_server.MCPServer(mcp_server.build_tools(api), home=steward_home)
    token, _ = mcp_auth.create_token(["runner:pause"], home=steward_home)

    body = _call(server, steward_home, token, "pause_job", {"job_id": job_id})

    result = body["result"]
    assert result["isError"] is True
    assert "Steward control pane is not running" in result["structuredContent"]["error"]


def test_scope_check_happens_before_the_relay_is_ever_called(steward_home):
    store = Store(steward_home)
    job_id = _contracted_running_job(store)
    calls: list[str] = []

    def relay(op, args, *, origin, request_id):
        calls.append(op)
        return {"accepted": True, "detail": "ok"}

    api = StewardAPI(store, relay=relay)
    server = mcp_server.MCPServer(mcp_server.build_tools(api), home=steward_home)
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)

    body = _call(server, steward_home, token, "pause_job", {"job_id": job_id})

    assert body["error"]["code"] == mcp_server.INSUFFICIENT_SCOPE
    assert calls == []


def test_read_tools_never_relay(steward_home):
    store = Store(steward_home)
    job_id = _contracted_running_job(store)
    api = StewardAPI(store, relay=_relay_raising_no_control_pane)
    server = mcp_server.MCPServer(mcp_server.build_tools(api), home=steward_home)
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)

    body = _call(server, steward_home, token, "get_job", {"job_id": job_id})

    assert body["result"]["structuredContent"]["job"]["job_id"] == job_id


def test_tools_list_is_unchanged_by_relay_mode(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store, relay=_inprocess_control_relay(store))
    server = mcp_server.MCPServer(mcp_server.build_tools(api), home=steward_home)
    token, _ = mcp_auth.create_token(list(mcp_auth.SCOPES), home=steward_home)
    token_info = mcp_auth.authorize(f"Bearer {token}", home=steward_home)

    status, raw = server.handle_authenticated(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(), token_info
    )

    tools = json.loads(raw)["result"]["tools"]
    assert {t["name"] for t in tools} == set(mcp_server.TOOL_NAMES)
    assert len(tools) == len(mcp_server.TOOL_NAMES) == 33
