"""Plan 2 Task 5: every worker-side decider reads the same effective authority (spec §6; audit RC-2/F-2)."""
from __future__ import annotations

import argparse

from steward import approvals as approvals_module
from steward import authority, cli, evidence, fleet_live, grants, watcher_authority
from steward.brief import render_brief
from steward.contract import load_contract
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.recovery import RecoveryPolicy
from steward.runner import Runner
from steward.runner_exec import FakeRunner
from steward.store import Store
from steward.worker import FakeWorker, WorkerObservation
from authority_fixtures import grant, owner_job
from ownership_fixtures import OWNER
from test_resolver import FakePopen, _setup


def _runner(store, eng):
    worker = FakeWorker([WorkerObservation(lifecycle="running", output_tail="", pid_alive=True, ts="t")])
    r = Runner(store, lambda *_: worker, FakeRunner(), FakeDiagnoser(Diagnosis("needs_human", "", "")),
               policy=RecoveryPolicy(), resolver=eng)
    notes = []
    r._notify = lambda *a: notes.append(a)
    return r, notes


def _frame(rid, command):
    return type("F", (), {"seq": 3, "body": {"request_id": rid, "tool": "Bash", "input": {"command": command}}})()


def test_granted_live_write_auto_approves_with_grant_id_and_ungranted_stays_held(steward_home):
    FakePopen.instances, FakePopen.reply = [], None
    store, jid, eng = _setup(steward_home)
    grant(store, jid, "write_path:/etc/caddy/Caddyfile")
    g = grant(store, jid, "run_command:sudo tee /etc/caddy/Caddyfile")
    runner, notes = _runner(store, eng)
    runner._open_fabric_permission(jid, _frame("r1", "sudo tee /etc/caddy/Caddyfile < Caddyfile.new"))
    runner._open_fabric_permission(jid, _frame("r2", "sudo tee /etc/passwd < x"))
    a1, a2 = approvals_module.list_approvals(store, jid)
    assert a1["status"] == "approved" and a1["decided_by"] == "code"
    approvals_module.mark_relayed(store, jid, a1["id"])  # TUI speed plan 10b: its one event is written on relay
    ev = next(e for e in store.events(jid) if e.rule == "owner_grant_auto_approve")
    assert ev.evidence["grant_id"] == g.grant_id and ev.evidence["authority_rev"] == authority.authority_rev(store, jid)
    assert a2["status"] == "pending"  # not granted: the resolver path, exactly as before


def test_prohibited_call_is_denied_by_code_not_escalated(steward_home):
    FakePopen.instances, FakePopen.reply = [], None
    store, jid, eng = _setup(steward_home)
    grant(store, jid, "run_command:git push", kind="prohibit")
    runner, notes = _runner(store, eng)
    runner._open_fabric_permission(jid, _frame("r1", "git push origin main"))
    a = approvals_module.list_approvals(store, jid)[0]
    assert a["status"] == "denied" and a["decided_by"] == "code" and not notes
    assert any(e.rule == "owner_prohibition_denied" for e in store.events(jid))


def test_preauthorization_drops_prohibited_checks_and_never_preauthorizes_grants(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "run_command:sudo systemctl reload caddy")
    ns = argparse.Namespace(agent_arg=[], kind="claude", job=jid)
    args = cli._worker_agent_args(s, ns)
    assert "Bash(pytest -q:*)" in args and not any("systemctl" in a for a in args)
    grant(s, jid, "run_command:pytest", kind="prohibit")
    assert not any("pytest -q" in a for a in cli._worker_agent_args(s, ns))


def test_fleet_lane_uses_the_same_effective_authority(steward_home, tmp_path):
    from test_fleet_wiring import DeliveryResult, Frame, seeded

    store, _ = seeded(tmp_path, steward_home)
    grant(store, "J1", "run_command:ssh-keygen -l", by=OWNER)
    answers = []

    class A:
        def answer_prompt(self, approve, request_id=None, message=None):
            answers.append((approve, request_id))
            return DeliveryResult(ok=True, detail="ok")

    relay = fleet_live.permission_relay(store, store.home / "worktrees", lambda c, auth, wt: False)
    relay("J1", "lane-a", Frame(v=2, seq=1, job="J1", type="permission_request",
                                body={"request_id": "r1", "tool": "Bash", "input": {"command": "ssh-keygen -l -f k.pub"}},
                                task="a", lane="lane-a", attempt=1, sender="s", epoch=1), A())
    relay("J1", "lane-a", Frame(v=2, seq=2, job="J1", type="permission_request",
                                body={"request_id": "r2", "tool": "Bash", "input": {"command": "ssh host ls"}},
                                task="a", lane="lane-a", attempt=1, sender="s", epoch=1), A())
    assert answers == [(True, "r1"), (False, "r2")]


def test_watcher_and_held_reason_agree_with_the_seam(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "write_path:/etc/caddy/Caddyfile")
    wt = s.home / "worktrees" / jid
    call = watcher_authority.ToolCall(id="c1", tool="Write", input={"file_path": "/etc/caddy/Caddyfile"}, ts=100.0)
    out = watcher_authority.runner_predicates(s, jid, now=100.0, calls=[call], samples=[], worktree=wt)
    assert not any(p["id"] == "needs_human_call" for p in out)
    q = "worker permission: Write {'file_path': '/etc/caddy/Caddyfile'}"
    assert "held" not in evidence.held_reason(s, jid, q) or "grant" in evidence.held_reason(s, jid, q)


def test_settle_pending_asks_after_a_late_grant(steward_home):
    FakePopen.instances, FakePopen.reply = [], None
    store, jid, eng = _setup(steward_home)
    runner, _ = _runner(store, eng)
    runner._open_fabric_permission(jid, _frame("r1", "sudo tee /etc/caddy/Caddyfile < new"))
    aid = approvals_module.list_approvals(store, jid)[0]["id"]
    assert authority.settle_pending_asks(store, jid, now=5.0) == []
    grant(store, jid, "run_command:sudo tee /etc/caddy/Caddyfile")
    grant(store, jid, "write_path:/etc/caddy/Caddyfile")
    assert authority.settle_pending_asks(store, jid, now=6.0) == [aid]
    assert approvals_module.get_approval(store, jid, aid)["status"] == "approved"
    assert authority.settle_pending_asks(store, jid, now=7.0) == []


def test_worker_brief_lists_owner_grants_and_prohibitions(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "write_path:/etc/caddy/Caddyfile")
    grant(s, jid, "run_command:git push", kind="prohibit")
    lines = authority.render_lines(authority.effective(s, jid))
    text = render_brief(load_contract(s, jid), [], None, None, grants=lines)
    assert "OWNER GRANTS" in text and "/etc/caddy/Caddyfile" in text and "PROHIBITED" in text
    assert "OWNER GRANTS" not in render_brief(load_contract(s, jid), [], None, None)
