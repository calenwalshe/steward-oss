"""Plan 2 Task 6: the resolver brief carries the owner's grants untruncated; stale authority never acts (spec §6)."""
from __future__ import annotations

import pytest

from steward import approvals as approvals_module
from steward import authority, blockers, grants
from steward import resolver as rs
from steward.contract import Contract, store_contract
from steward.models import JobState
from steward.store import Store
from authority_fixtures import TURING_GOAL, grant
from ownership_fixtures import OWNER
from test_resolver import Clock, FakePopen, _hold, _run


@pytest.fixture
def popen():
    FakePopen.instances, FakePopen.finish, FakePopen.cost = [], True, 0.03
    FakePopen.reply = {"decision": "approve", "reason": "in scope", "risk_notes": ""}
    return FakePopen


def _goal_rig(steward_home):
    store = Store(steward_home)
    job = store.create_job(TURING_GOAL, "/tmp/somerepo")
    store_contract(store, job.job_id, Contract(goal=TURING_GOAL, boundaries=["b1", "b2", "b3", "b4", "b5-hidden"],
                                               done_when=["tests pass"]))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    eng = rs.ResolverEngine(store, popen=FakePopen, clock=Clock(), config=rs.ResolverConfig(),
                            activity_fn=lambda s, j: {"turns": []})
    return store, job.job_id, eng


def test_grant_beyond_goal_prefix_reaches_resolver_brief_continuous_ownership_recovery_acceptance(steward_home, popen):
    store, jid, eng = _goal_rig(steward_home)
    assert TURING_GOAL.index("/etc/msi-guard.nft") > 400
    grant(store, jid, "write_path:/etc/msi-guard.nft")
    _hold(store, jid, eng, "nft -f /etc/msi-guard.nft.new", "remote transfer (ssh/scp/rsync)")
    eng.step(jid)
    prompt = next((store.home / "resolver" / jid).glob("*.prompt.txt")).read_text()
    assert "Owner authority (revision" in prompt and "/etc/msi-guard.nft" in prompt
    assert "b5-hidden" in prompt  # boundaries are no longer cut at four


def test_many_grants_state_the_overflow_instead_of_dropping_silently(steward_home, popen, monkeypatch):
    store, jid, eng = _goal_rig(steward_home)
    monkeypatch.setattr(authority, "RESOLVER_GRANT_LINES_MAX", 3)
    for i in range(6):
        grant(store, jid, f"write_path:/srv/app{i}.conf")
    _hold(store, jid, eng, "rm -rf /home/user/scratch-test-dir", "rm targets a path outside the worktree: x")
    eng.step(jid)
    prompt = next((store.home / "resolver" / jid).glob("*.prompt.txt")).read_text()
    assert "3 more grant(s) not shown; Steward validates every action against all of them" in prompt


def test_resolver_approve_is_discarded_when_authority_changed_in_flight(steward_home, popen):
    store, jid, eng = _goal_rig(steward_home)
    FakePopen.finish = False  # the resolver is still thinking
    g = grant(store, jid, "run_command:sudo tee /etc/caddy/Caddyfile")
    rec, _ = _hold(store, jid, eng, "sudo tee /etc/caddy/Caddyfile < new.txt", "sudo")
    eng.step(jid)
    grants.revoke(store, g.grant_id, by=OWNER, reason="pause caddy work", now=5.0)
    proc = FakePopen.instances[-1]
    out = store.kv_get(jid, f"resolver_inflight:{rec['id']}")["out"]
    with open(out, "w") as fh:
        import json
        fh.write(json.dumps({"result": json.dumps({"decision": "approve", "reason": "granted", "risk_notes": ""}),
                             "total_cost_usd": 0.01}))
    proc.rc = 0
    _run(eng, jid)
    a = approvals_module.get_approval(store, jid, rec["id"])
    assert a["status"] == "pending"
    d = [e for e in store.events(jid) if e.what == "resolver_decision"][-1]
    assert d.evidence["applied"] == "discarded" and "authority_changed" in d.evidence["reason"]


def test_prohibited_hold_is_denied_by_code_without_a_model(steward_home, popen):
    store, jid, eng = _goal_rig(steward_home)
    grant(store, jid, "run_command:git push", kind="prohibit")
    rec, _ = _hold(store, jid, eng, "git push origin main", "git push")
    eng.step(jid)
    assert FakePopen.instances == []
    assert approvals_module.get_approval(store, jid, rec["id"])["status"] == "denied"
    assert any(e.rule == "owner_prohibition" for e in store.events(jid))


def test_escalation_raises_one_authorization_blocker_that_a_later_grant_clears(steward_home, popen):
    from steward import ownership

    store, jid, eng = _goal_rig(steward_home)
    FakePopen.reply = {"decision": "escalate_human", "reason": "live write needs the owner", "risk_notes": ""}
    rec, _ = _hold(store, jid, eng, "sudo tee /srv/x.conf < y", "sudo")
    _run(eng, jid)
    bl = [b for b in blockers.open_blockers(store, jid) if b.kind == "authorization_missing"]
    assert len(bl) == 1 and bl[0].evidence == ("approval:" + rec["id"],) or bl[0].evidence == ["approval:" + rec["id"]]
    grant(store, jid, "run_command:sudo tee /srv/x.conf")
    grant(store, jid, "write_path:/srv/x.conf")
    assert ownership.CHEAP_CHECKS["grant_covers"](store, jid, bl[0]) is not None
