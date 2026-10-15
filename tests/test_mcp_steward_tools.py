"""MCP tools added for supervisors/watchers/tokens/decisions/closeout/create+launch.

Driven through `MCPServer.handle_authenticated` with either a recording relay
(to assert the exact op/args/origin) or the in-process control pane (to assert
real effects on a scratch store / scratch git repo)."""

from __future__ import annotations

import json
import os
import subprocess
import uuid

import pytest

from steward import approvals, cli, mcp_auth, mcp_server
from steward.api import StewardAPI
from steward.contract import Contract, store_contract
from steward.control_pane import ControlPane
from steward.models import JobState, Judgment
from steward.store import Store

NEW = (
    "get_supervisors", "get_watchers_feed", "get_tokens", "get_decision", "preview_incorporate",
    "accept_job", "reject_job", "release_job", "incorporate_job", "toggle_supervisor",
    "add_watcher", "remove_watcher", "create_job", "launch_job", "play_job",
)
READ = NEW[:5]
_ENV = {"GIT_AUTHOR_NAME": "C", "GIT_AUTHOR_EMAIL": "c@x", "GIT_COMMITTER_NAME": "C", "GIT_COMMITTER_EMAIL": "c@x"}


def _git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, env={**os.environ, **_ENV})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _job(store, state=JobState.RUNNING, repo="/repo"):
    job = store.create_job("do the thing", repo)
    store_contract(store, job.job_id, Contract(goal="do the thing", boundaries=[], done_when=["checks pass"],
                                               next_step="go", authority=[], assumptions=[]))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))
    return job.job_id


def _inprocess(store):
    pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _l: None)
    return lambda op, args, *, origin, request_id: pane.execute(
        {"request_id": request_id or str(uuid.uuid4()), "op": op, "args": args, "origin": origin})


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, op, args, *, origin, request_id):
        self.calls.append((op, args, origin))
        return {"accepted": True, "detail": "ok", "job_id": args.get("job_id"), "contract_text": "GOAL: x"}


@pytest.fixture
def env(steward_home):
    store = Store(steward_home)
    rec = Recorder()

    def make(relay):
        api = StewardAPI(store, relay=relay)
        return mcp_server.MCPServer(mcp_server.build_tools(api), home=steward_home)

    token, _ = mcp_auth.create_token(list(mcp_auth.SCOPES), home=steward_home)
    info = mcp_auth.authorize(f"Bearer {token}", home=steward_home)

    def call(server, name, arguments=None, info_=info):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments or {}}}).encode()
        _, raw = server.handle_authenticated(body, info_)
        return json.loads(raw)

    return store, rec, make, call, info


def _res(body):
    assert "error" not in body, body
    assert not body["result"]["isError"], body
    return body["result"]["structuredContent"]


def test_tools_list_scopes_and_hints(env):
    store, rec, make, call, info = env
    server = make(rec)
    _, raw = server.handle_authenticated(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(), info)
    tools = {t["name"]: t for t in json.loads(raw)["result"]["tools"]}
    assert set(NEW) <= set(tools)
    for n in READ:
        assert tools[n]["annotations"]["readOnlyHint"] is True
        assert server.tools[n].required_scope == "runner:read"
    for n in NEW[5:]:
        assert tools[n]["annotations"]["readOnlyHint"] is False
        assert server.tools[n].required_scope in ("runner:steer", "runner:approve")
    for n in ("accept_job", "reject_job", "release_job", "incorporate_job"):
        assert tools[n]["annotations"]["destructiveHint"] is True
    assert "explicit" in tools["launch_job"]["description"]
    assert "read" in tools["create_job"]["description"].lower()
    assert "urgent" in tools["steer_job"]["inputSchema"]["properties"]


def test_read_token_cannot_call_new_write_tools(env, steward_home):
    store, rec, make, call, _ = env
    token, _ = mcp_auth.create_token(["runner:read"], home=steward_home)
    ro = mcp_auth.authorize(f"Bearer {token}", home=steward_home)
    body = call(make(rec), "accept_job", {"job_id": "x"}, ro)
    assert body["error"]["code"] == mcp_server.INSUFFICIENT_SCOPE
    assert rec.calls == []


def test_get_supervisors_labels_and_tokens(env):
    store, rec, make, call, _ = env
    jid = _job(store)
    out = _res(call(make(rec), "get_supervisors", {"job_id": jid}))
    labels = [r["label"] for r in out["supervisors"]]
    assert {"advisor", "doctor", "observer"} <= set(labels)
    assert all("{SPIN}" not in r["display"] for r in out["supervisors"])
    adv = next(r for r in out["supervisors"] if r["label"] == "advisor")
    assert adv["switch"] and adv["model"]


def test_get_watchers_feed_is_plain(env):
    store, rec, make, call, _ = env
    jid = _job(store)
    store.append_event(jid, what="supervisor_toggled", evidence={"role": "sol", "state": "off", "previous": "on"},
                       rule="operator_supervisor_switch", judgment=Judgment.human)
    out = _res(call(make(rec), "get_watchers_feed", {"job_id": jid, "last": 5}))
    assert isinstance(out["lines"], list)
    assert all("\x1b" not in ln for ln in out["lines"])


def test_get_tokens_shapes(env):
    store, rec, make, call, _ = env
    a, b = _job(store), _job(store)
    one = _res(call(make(rec), "get_tokens", {"job_id": a}))
    assert [j["job_id"] for j in one["jobs"]] == [a]
    assert {"total", "cached", "fresh", "output", "effective", "usd"} <= set(one["grand_total"])
    allj = _res(call(make(rec), "get_tokens"))
    assert {j["job_id"] for j in allj["jobs"]} == {a, b}


def test_get_decision_acceptance_and_none(env):
    store, rec, make, call, _ = env
    quiet = _job(store)
    waiting = _job(store, JobState.NEEDS_HUMAN)
    ap = approvals.open_approval(store, waiting, approvals.ACCEPTANCE_QUESTION, kind="acceptance")
    server = make(rec)
    out = _res(call(server, "get_decision"))
    assert out["waiting_on_sam"] == 1
    d = out["decisions"][0]
    assert d["job_id"] == waiting and d["kind"] == "acceptance" and d["approval_id"] == ap["id"]
    assert any(ln.startswith("verified") for ln in d["evidence"])
    assert {a["tool"] for a in d["actions"]} >= {"accept_job", "reject_job"}
    assert _res(call(server, "get_decision", {"job_id": quiet}))["decisions"] == []


def test_reject_denies_acceptance_approval(env):
    store, rec, make, call, _ = env
    jid = _job(store, JobState.NEEDS_HUMAN)
    ap = approvals.open_approval(store, jid, approvals.ACCEPTANCE_QUESTION, kind="acceptance")
    rec_calls = Recorder()
    server = make(_inprocess(store))
    out = _res(call(server, "reject_job", {"job_id": jid}))
    assert out["accepted"] and "stopped" in out["detail"]
    assert approvals.get_approval(store, jid, ap["id"])["status"] == "denied"
    assert store.get_job(jid).state == JobState.STOPPED
    # nothing pending now
    body = call(server, "reject_job", {"job_id": jid})
    assert body["error"]["code"] == mcp_server.INVALID_PARAMS
    assert rec_calls.calls == []


def test_accept_and_release_relay_ops(env):
    store, rec, make, call, _ = env
    server = make(rec)
    j1 = _job(store)
    _res(call(server, "accept_job", {"job_id": j1}))
    _res(call(server, "release_job", {"job_id": j1}))
    _res(call(server, "incorporate_job", {"job_id": j1, "release": True}))
    assert rec.calls == [
        ("accept", {"job_id": j1}, "chatgpt"),
        ("release", {"job_id": j1}, "chatgpt"),
        ("incorporate", {"job_id": j1, "release": True}, "chatgpt"),
    ]


def test_accept_really_accepts_in_process(env):
    store, rec, make, call, _ = env
    jid = _job(store, JobState.NEEDS_HUMAN)
    out = _res(call(make(_inprocess(store)), "accept_job", {"job_id": jid}))
    assert out["accepted"] and store.get_job(jid).state == JobState.ACCEPTED


def test_preview_and_incorporate_scratch_repo(env, tmp_path):
    store, rec, make, call, _ = env
    repo = tmp_path / "src"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    jid = _job(store, JobState.ACCEPTED, repo=str(repo))
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", f"steward/{jid}", str(wt), "HEAD")
    (wt / "n.txt").write_text("n\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "job edit")
    head = _git(repo, "rev-parse", "HEAD")

    server = make(_inprocess(store))
    pre = _res(call(server, "preview_incorporate", {"job_id": jid}))
    assert pre["commit_count"] == 1 and "n.txt" in pre["files"] and pre["dry_run"]
    assert _git(repo, "rev-parse", "HEAD") == head  # preview changed nothing

    out = _res(call(server, "incorporate_job", {"job_id": jid, "release": True}))
    assert out["accepted"] and out["released"] is True
    assert (repo / "n.txt").exists()
    assert store.get_job(jid).state == JobState.RELEASED


def test_preview_rejects_wrong_state(env):
    store, rec, make, call, _ = env
    jid = _job(store)
    assert call(make(rec), "preview_incorporate", {"job_id": jid})["error"]["code"] == mcp_server.INVALID_PARAMS


def test_toggle_supervisor_alias_and_watcher(env):
    store, rec, make, call, _ = env
    server = make(rec)
    j = _job(store)
    _res(call(server, "toggle_supervisor", {"job_id": j, "role": "advisor"}))
    _res(call(server, "toggle_supervisor", {"job_id": j, "role": "Doctor", "state": "off"}))
    _res(call(server, "toggle_supervisor", {"job_id": j, "role": "sol", "state": "on"}))
    _res(call(server, "toggle_supervisor", {"job_id": j, "role": "mywatch", "state": "off"}))
    assert rec.calls == [
        ("supervisor", {"job_id": j, "role": "astra", "state": "toggle"}, "chatgpt"),
        ("supervisor", {"job_id": j, "role": "sol", "state": "off"}, "chatgpt"),
        ("supervisor", {"job_id": j, "role": "sol", "state": "on"}, "chatgpt"),
        ("watcher", {"action": "toggle", "job_id": j, "name": "mywatch", "state": "off"}, "chatgpt"),
    ]
    body = call(server, "toggle_supervisor", {"job_id": j, "role": "mywatch", "state": "shadow"})
    assert body["error"]["code"] == mcp_server.INVALID_PARAMS


def test_toggle_supervisor_effect_in_process(env):
    store, rec, make, call, _ = env
    jid = _job(store)
    out = _res(call(make(_inprocess(store)), "toggle_supervisor", {"job_id": jid, "role": "doctor", "state": "off"}))
    assert out["accepted"] and "sol -> off" in out["detail"]
    rows = _res(call(make(rec), "get_supervisors", {"job_id": jid}))["supervisors"]
    assert next(r for r in rows if r["label"] == "doctor")["switch"] == "off"


def test_add_and_remove_watcher(env):
    store, rec, make, call, _ = env
    jid = _job(store)
    server = make(_inprocess(store))
    out = _res(call(server, "add_watcher", {"job_id": jid, "name": "tests", "ask": "are tests still green?",
                                            "may": ["nudge"]}))
    assert out["accepted"] and "haiku" in out["detail"] and "600s" in out["detail"]
    rows = _res(call(server, "get_supervisors", {"job_id": jid}))["supervisors"]
    assert any(r["label"] == "tests" for r in rows)
    assert _res(call(server, "remove_watcher", {"job_id": jid, "name": "tests"}))["accepted"]
    bad = _res(call(server, "add_watcher", {"job_id": jid, "name": "Bad!", "ask": "x"}))
    assert bad["accepted"] is False


def test_steer_urgent_and_verb_pass_through(env):
    store, rec, make, call, _ = env
    j = _job(store)
    _res(call(make(rec), "steer_job", {"job_id": j, "expected_revision": 3, "instruction": "stop now",
                                       "human_instruction": "stop now!", "verb": "avoid", "urgent": True}))
    op, args, origin = rec.calls[0]
    assert op == "avoid" and origin == "chatgpt"
    assert args["urgent"] is True and args["verb"] == "avoid" and args["expected_revision"] == 3


def test_create_and_launch_relay_ops(env):
    store, rec, make, call, _ = env
    server = make(rec)
    j9 = _job(store)
    out = _res(call(server, "create_job", {
        "goal": "Fix the bug", "repo": "/some/repo", "finish_line": "tests pass",
        "watchers": [{"name": "w1", "ask": "still on track?", "every": "5m", "may": ["escalate"]}]}))
    assert out["contract_text"] == "GOAL: x" and "launch_job" in out["next"]
    op, args, origin = rec.calls[0]
    assert op == "new" and origin == "chatgpt" and args["repo"] == "/some/repo"
    assert args["goal"] == "Fix the bug. Done when: tests pass"
    assert args["watchers"] == [{"name": "w1", "ask": "still on track?", "model": "haiku", "every_s": 300,
                                 "may": ["escalate"]}]
    _res(call(server, "launch_job", {"job_id": j9}))
    _res(call(server, "launch_job", {"job_id": j9, "model": "opus"}))
    (op1, a1, o1), (_, a2, _) = rec.calls[1], rec.calls[2]
    assert op1 == "launch" and o1 == "chatgpt" and a1["agent_args"] == ["--model", "sonnet"]
    assert a2["agent_args"] == ["--model", "opus"] and a1["worker"] == "fabric"
    assert call(server, "launch_job", {"job_id": j9, "model": "x; rm"})["error"]["code"] == mcp_server.INVALID_PARAMS


def test_play_job_launches_only_contracted_with_recorded_args(env):
    store, rec, make, call, _ = env
    server = make(rec)
    ready, running = _job(store, JobState.CONTRACTED), _job(store)
    _res(call(server, "play_job", {"job_id": ready}))
    store.kv_set(ready, "launch_args", {"agent_args": ["--model", "opus"], "worker": "fabric"})
    _res(call(server, "play_job", {"job_id": ready}))
    out = _res(call(server, "play_job", {"job_id": running}))
    (op1, a1, o1), (_, a2, _) = rec.calls
    assert op1 == "launch" and o1 == "chatgpt" and a1["agent_args"] == ["--model", "sonnet"]
    assert a2["agent_args"] == ["--model", "opus"] and "already RUNNING" in out["detail"]
    assert len(rec.calls) == 2


def test_relay_only_tools_fail_cleanly_without_control_pane(env):
    store, rec, make, call, _ = env
    body = call(make(None), "accept_job", {"job_id": "j"})
    assert body["result"]["isError"] is True and "control pane" in body["result"]["structuredContent"]["error"]


def test_serve_passes_configured_grant_scopes_to_oauth(monkeypatch, tmp_path):
    from steward import mcp_server as ms
    seen = {}

    class FakeOAuth:
        def __init__(self, store, **kw):
            seen.update(kw)

    class FakeHttpd:
        def serve_forever(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(ms, "OAuthServer", FakeOAuth)
    monkeypatch.setattr(ms, "make_http_server", lambda *a, **k: FakeHttpd())
    server = ms.MCPServer([], home=tmp_path)
    ms.serve(server, public_base_url="https://x.example", passphrase_record={"r": 1}, oauth_home=tmp_path,
             grant_scopes=("runner:read", "runner:steer", "runner:pause", "runner:approve"))
    assert seen["grant_scopes"] == ("runner:read", "runner:steer", "runner:pause", "runner:approve")
    seen.clear()
    ms.serve(server, public_base_url="https://x.example", passphrase_record={"r": 1}, oauth_home=tmp_path)
    assert "grant_scopes" not in seen  # default grant unchanged
