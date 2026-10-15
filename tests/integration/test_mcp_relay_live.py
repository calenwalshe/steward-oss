"""OPX-03 gate: an MCP write tool relays into a real control pane over a
real Herdr session (`steward-test-<pid>`), never touching `StewardAPI`
directly. Marked `integration`; skipped by default.

Mirrors `tests/integration/test_control_live.py`'s shape: the control pane
runs as a real subprocess inside the test session's own pane; the MCP
server here runs in-process (an ordinary `make_http_server` on an ephemeral
port, exactly `steward mcp serve`'s own wiring —
`steward.cli._mcp_relay`/`StewardAPI(..., relay=...)` — pointed at the test
session's socket instead of the live one) and is driven over real loopback
HTTP with a static bearer token. The cmd's arrival is confirmed by reading
the control pane's own redirected log file (SP-31: never a screen scrape),
and the receipt itself carries `origin=chatgpt`."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from steward import mcp_auth, mcp_server
from steward.api import StewardAPI
from steward.cli import _mcp_relay
from steward.contract import Contract, store_contract
from steward.models import JobState, Judgment
from steward.steering import Steering
from steward.store import Store

pytestmark = pytest.mark.integration

REPO_ROOT = str(Path(__file__).resolve().parents[2])
PYTHON = sys.executable


def _wait_for(predicate, *, timeout: float, poll: float = 0.3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(poll)
    pytest.fail(f"condition not met within {timeout}s; last={last!r}")


def _running_job(store: Store, repo: str = "/repo"):
    job = store.create_job("do the thing", repo)
    contract = Contract(
        goal="do the thing", boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[],
    )
    store_contract(store, job.job_id, contract)  # CREATED -> CONTRACTED
    store.set_state(job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code)
    return job


def _call_mcp(base_url: str, token: str, name: str, arguments: dict) -> dict:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    ).encode()
    req = urllib.request.Request(
        f"{base_url}/mcp",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def test_steer_job_relays_over_a_real_herdr_session_into_the_control_pane(herdr_test_session, tmp_path):
    session = herdr_test_session
    home = tmp_path / "steward_home"
    home.mkdir()
    exceptions_path = tmp_path / "exceptions.toml"

    store = Store(home)
    job = _running_job(store)

    # -- a real `steward control` pane, in the test session -----------------
    control_pane_id = session.create_pane(cwd=REPO_ROOT)
    control_log = tmp_path / "control.log"
    control_command = (
        f"export STEWARD_HOME={home} && export STEWARD_EXCEPTIONS={exceptions_path} && "
        f"export PYTHONPATH={REPO_ROOT} && cd {REPO_ROOT} && "
        f"{PYTHON} -u -m steward.cli control --sock {session.socket_path} --pane {control_pane_id} "
        f">> {control_log} 2>&1"
    )
    session.run_in_pane(control_pane_id, control_command)

    def control_pane_registered():
        result = session.api.call("pane.list", {})
        for pane in result.get("panes", []):
            if pane.get("tokens", {}).get("steward_role") == "control":
                return pane["pane_id"]
        return None

    _wait_for(control_pane_registered, timeout=15.0)

    # -- a local MCP server, wired exactly like `steward mcp serve` --------
    relay = _mcp_relay(store, session.socket_path)
    api = StewardAPI(store, relay=relay)
    server = mcp_server.MCPServer(mcp_server.build_tools(api), home=home)
    httpd = mcp_server.make_http_server(server, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    token, _ = mcp_auth.create_token(["runner:steer"], home=home)

    try:
        body = _call_mcp(
            base_url, token, "steer_job",
            {
                "job_id": job.job_id, "expected_revision": store.get_revision(job.job_id),
                "instruction": "prioritize the retry loop", "human_instruction": "please look at retries first",
            },
        )
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()

    receipt = body["result"]["structuredContent"]
    assert receipt["accepted"] is True, receipt

    # -- the receipt's own provenance ---------------------------------------
    record = Steering(store).all_instructions(job.job_id)[-1]
    assert record["origin"] == "chatgpt"
    assert record["human_instruction"] == "please look at retries first"

    # -- and it really traveled through Herdr into the control pane's own
    # pane (SP-31): its redirected log shows the cmd and result, never a
    # screen scrape.
    # `steer_job`'s `verb` defaults to "nudge" (steward/mcp_server.py); the
    # control op is that verb, not the literal string "steer".
    def log_has_steer_result():
        if not control_log.exists():
            return None
        text = control_log.read_text()
        return text if "result nudge" in text else None

    log_text = _wait_for(log_has_steer_result, timeout=15.0)
    assert "cmd nudge" in log_text
    assert "origin=chatgpt" in log_text

    used = [e for e in store.events(job.job_id) if e.what == "exception_used"]
    assert used and used[-1].evidence["name"] == "chatgpt-mcp"
