"""Tool surface over steward.api (MCP-04): the exact SP-26 allowlist, each
wired 1:1 onto `StewardAPI`, over the real HTTP JSON-RPC transport."""

from __future__ import annotations

import json
import threading

import pytest

from steward import approvals as approvals_module
from steward import cli, mcp_auth, mcp_server
from steward.api import StewardAPI
from steward.contract import Contract, store_contract
from steward.control_pane import ControlPane
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store


def _contracted_job(store):
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
    return job.job_id


def _fake_git_runner():
    def respond(argv, cwd):
        if argv[:3] == ["git", "diff", "--stat"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="1 file changed", stderr="", duration_s=0.0)
        if argv[:3] == ["git", "diff", "--name-only"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="steward/api.py\n", stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    return FakeRunner(respond)


def _inprocess_control_relay(store):
    """OPX-03: MCP write tools now relay through the control pane instead of
    calling `StewardAPI` directly — this fixture gives the existing suite
    the same in-process `ControlPane` relay `tests/conftest.py`'s CLI
    fixture and `tests/test_mcp_relay.py` use, sharing this test's own
    store, so every assertion below still observes the real dispatch."""
    handlers = cli._build_control_handlers(store)
    pane = ControlPane(store, handlers, print_fn=lambda _line: None)

    def relay(op, args, *, origin, request_id):
        cmd = {"request_id": request_id or "req-mcp-tools", "op": op, "args": args, "origin": origin}
        return pane.execute(cmd)

    return relay


@pytest.fixture
def live_tools_server(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store, runner_exec=_fake_git_runner(), relay=_inprocess_control_relay(store))
    server = mcp_server.MCPServer(mcp_server.build_tools(api), home=steward_home)
    httpd = mcp_server.make_http_server(server, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base_url, store, api
    httpd.shutdown()
    thread.join(timeout=5)
    httpd.server_close()


def _call(base_url, token, name, arguments=None):
    import urllib.error
    import urllib.request

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }
    req = urllib.request.Request(
        f"{base_url}/mcp",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


def _tools_list(base_url, token):
    import urllib.request

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    req = urllib.request.Request(
        f"{base_url}/mcp",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["result"]["tools"]


# -- exact allowlist ----------------------------------------------------------


def test_tools_list_is_exactly_the_sp26_allowlist(steward_home, live_tools_server):
    base_url, _, _ = live_tools_server
    token, _ = mcp_auth.create_token(list(mcp_auth.SCOPES), home=steward_home)

    tools = _tools_list(base_url, token)

    assert {t["name"] for t in tools} == set(mcp_server.TOOL_NAMES)
    assert len(tools) == len(mcp_server.TOOL_NAMES) == 33


def test_read_tools_are_marked_read_only_and_writes_are_not(steward_home, live_tools_server):
    base_url, _, _ = live_tools_server
    token, _ = mcp_auth.create_token(list(mcp_auth.SCOPES), home=steward_home)
    tools = {t["name"]: t for t in _tools_list(base_url, token)}

    read_tools = (
        "list_jobs", "get_job", "get_current_step", "get_recent_events",
        "get_verification", "get_diff_summary", "explain_blocker",
    )
    for name in read_tools:
        assert tools[name]["annotations"]["readOnlyHint"] is True

    for name in ("steer_job", "pause_job", "resume_job", "cancel_job", "approve_action"):
        assert tools[name]["annotations"]["readOnlyHint"] is False

    assert tools["cancel_job"]["annotations"]["destructiveHint"] is True
    assert tools["steer_job"]["annotations"]["destructiveHint"] is False


# -- each tool calls the api --------------------------------------------------


def test_list_jobs_calls_api(steward_home, live_tools_server):
    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)

    body = _call(base_url, token, "list_jobs")

    jobs = body["result"]["structuredContent"]["jobs"]
    assert [j["job_id"] for j in jobs] == [job_id]


def test_get_job_calls_api(steward_home, live_tools_server):
    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)

    body = _call(base_url, token, "get_job", {"job_id": job_id})

    assert body["result"]["structuredContent"]["job"]["job_id"] == job_id


def test_get_current_step_calls_api(steward_home, live_tools_server):
    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)

    body = _call(base_url, token, "get_current_step", {"job_id": job_id})

    assert body["result"]["structuredContent"]["step"]["next_step"] == "dispatch worker"


def test_get_recent_events_calls_api(steward_home, live_tools_server):
    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)

    body = _call(base_url, token, "get_recent_events", {"job_id": job_id, "limit": 5})

    assert isinstance(body["result"]["structuredContent"]["events"], list)


def test_get_verification_calls_api(steward_home, live_tools_server):
    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)

    body = _call(base_url, token, "get_verification", {"job_id": job_id})

    assert body["result"]["structuredContent"]["verification"]["verified"] is False


def test_explain_blocker_calls_api(steward_home, live_tools_server):
    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)

    body = _call(base_url, token, "explain_blocker", {"job_id": job_id})

    assert body["result"]["structuredContent"]["blocker"]["blocker"] is None


def test_get_diff_summary_result_has_no_absolute_worktree_or_env_paths(steward_home, live_tools_server):
    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)

    body = _call(base_url, token, "get_diff_summary", {"job_id": job_id})

    diff = body["result"]["structuredContent"]["diff"]
    dumped = json.dumps(diff)
    assert str(steward_home) not in dumped
    assert "worktrees" not in dumped


def test_steer_job_records_chatgpt_origin_with_both_texts(steward_home, live_tools_server):
    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    current_rev = store.get_revision(job_id)
    token, _ = mcp_auth.create_token(["runner:steer"], home=steward_home)

    body = _call(
        base_url,
        token,
        "steer_job",
        {
            "job_id": job_id,
            "expected_revision": current_rev,
            "instruction": "prioritize the retry loop",
            "human_instruction": "please look at retries first",
        },
    )

    receipt = body["result"]["structuredContent"]
    assert receipt["accepted"] is True
    from steward.steering import Steering

    record = Steering(store).all_instructions(job_id)[-1]
    assert record["origin"] == "chatgpt"
    assert record["human_instruction"] == "please look at retries first"
    assert record["interpreted_instruction"] == "prioritize the retry loop"


def test_steer_job_stale_revision_returns_conflict_passthrough(steward_home, live_tools_server):
    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    token, _ = mcp_auth.create_token(["runner:steer"], home=steward_home)

    body = _call(
        base_url,
        token,
        "steer_job",
        {
            "job_id": job_id,
            "expected_revision": 9999,
            "instruction": "x",
            "human_instruction": "x",
        },
    )

    receipt = body["result"]["structuredContent"]
    assert receipt["accepted"] is False
    assert "CONFLICT" in receipt["detail"]


def test_pause_resume_cancel_and_approve_call_api(steward_home, live_tools_server):
    from steward.models import JobState, Judgment

    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    store.set_state(job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code)
    token, _ = mcp_auth.create_token(list(mcp_auth.SCOPES), home=steward_home)

    pause_body = _call(base_url, token, "pause_job", {"job_id": job_id})
    assert pause_body["result"]["structuredContent"]["accepted"] is True

    resume_body = _call(base_url, token, "resume_job", {"job_id": job_id})
    assert resume_body["result"]["structuredContent"]["accepted"] is True

    approvals_module.open_approval(store, job_id, "may I proceed?", approval_id="ap-1")
    approve_body = _call(
        base_url, token, "approve_action", {"job_id": job_id, "approval_id": "ap-1"}
    )
    assert approve_body["result"]["structuredContent"]["accepted"] is True

    cancel_body = _call(base_url, token, "cancel_job", {"job_id": job_id})
    assert cancel_body["result"]["structuredContent"]["accepted"] is True


# -- scoping -------------------------------------------------------------


def test_read_token_cannot_steer_no_event_appended(steward_home, live_tools_server):
    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    events_before = len(store.events(job_id))
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)

    body = _call(
        base_url,
        token,
        "steer_job",
        {
            "job_id": job_id,
            "expected_revision": store.get_revision(job_id),
            "instruction": "x",
            "human_instruction": "x",
        },
    )

    assert body["error"]["code"] == mcp_server.INSUFFICIENT_SCOPE
    assert len(store.events(job_id)) == events_before


# -- unknown tool ----------------------------------------------------------


def test_unknown_tool_is_invalid_params(steward_home, live_tools_server):
    base_url, _, _ = live_tools_server
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)

    body = _call(base_url, token, "not_a_real_tool")

    assert body["error"]["code"] == mcp_server.INVALID_PARAMS


# -- concurrency: tool-handler dispatch is serialized (SP-1, SP-14) ----------


def test_concurrent_steer_job_calls_never_lose_an_update(steward_home, live_tools_server):
    """Ten threads race the exact same `expected_revision` (a real
    read-check-write race if handler execution weren't serialized): without
    the dispatch lock, more than one could pass the staleness check before
    any of them bumps the revision, or the kv-backed instruction list could
    lose an appended entry to a last-write-wins overwrite. With the lock,
    exactly one is accepted and the revision/event counts are exact — no
    interleaving, no lost update.
    """
    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    starting_revision = store.get_revision(job_id)
    starting_events = len(store.events(job_id))
    token, _ = mcp_auth.create_token(["runner:steer"], home=steward_home)

    results = [None] * 10

    def fire(i):
        results[i] = _call(
            base_url,
            token,
            "steer_job",
            {
                "job_id": job_id,
                "expected_revision": starting_revision,
                "instruction": f"nudge {i}",
                "human_instruction": f"nudge {i}",
                "request_id": f"race-{i}",
            },
        )

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    receipts = [r["result"]["structuredContent"] for r in results]
    accepted = [r for r in receipts if r["accepted"]]
    conflicted = [r for r in receipts if not r["accepted"]]

    assert len(accepted) == 1
    assert len(conflicted) == 9
    assert all("CONFLICT" in r["detail"] for r in conflicted)

    # Exactly one revision bump, exactly one new steering event — no
    # interleaved read-modify-write silently dropped or duplicated a write.
    assert store.get_revision(job_id) == starting_revision + 1
    new_events = [
        e for e in store.events(job_id)[starting_events:] if e.what.endswith("instruction recorded")
    ]
    assert len(new_events) == 1


# -- the ETA on the read tools (phase 18-06, G18) ------------------------------------------------------------


def test_mcp_read_tools_carry_eta(steward_home, live_tools_server):
    import time
    from steward import efficiency_watch as ew
    from steward.models import JobState
    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job_id))
    now = time.time()
    shown = {"p10_end": now + 80 * 60, "p50_end": now + 100 * 60, "p90_end": now + 130 * 60, "source": "units",
             "set_at": now, "units_done_at_set": 3, "band_units_done": 3, "p50_min_at_set": 100.0, "attempt_no": 0,
             "overdue_noted": False}
    from steward import eta
    stored = eta.Eta(at=now, source="units", mid_min=100.0, p10_min=80.0, p90_min=130.0, p50_min=100.0, units_done=3,
                     units_total=7, units_exact=True, unit_label="plan 16-04", shown=shown).to_dict()
    store.kv_set(job_id, ew.STATE_KEY, {"mode": "shadow", "eta": stored})
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)

    step = _call(base_url, token, "get_current_step", {"job_id": job_id})["result"]["structuredContent"]["step"]
    job = _call(base_url, token, "get_job", {"job_id": job_id})["result"]["structuredContent"]["job"]
    rows = _call(base_url, token, "list_jobs")["result"]["structuredContent"]["jobs"]
    assert step["eta"]["shown"] == job["eta"]["shown"] == rows[0]["eta_text"] == "~1h40 left (1h20–2h10)"
    assert step["eta"]["source"] == "units" and step["eta"]["units_total"] == 7 and rows[0]["eta_p50_min"] in (99, 100)

    tools = {t["name"]: t for t in _tools_list(base_url, token)}
    assert set(tools) == set(mcp_server.TOOL_NAMES) and len(tools) == 33          # no new tool
    for name in ("list_jobs", "get_job", "get_current_step"):
        desc = tools[name]["description"]
        assert "the range is the answer" in desc and "p50 is null until the job has finished units" in desc, name
        assert "eta_text" in desc, name
        assert tools[name]["annotations"]["readOnlyHint"] is True
