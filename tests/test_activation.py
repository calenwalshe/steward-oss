"""Plan 4 Task 10: activation names the running revision and required live checks; a deploy or a merge alone is
not activation (spec §8, §9; P4-22)."""
from __future__ import annotations

from steward import activation, approvals, blockers, cli, delivery, incorporate, stability
from steward.contract import Contract, store_contract
from steward.store import Store
from steward.models import JobState
from test_incorporate import git, mkjob, scratch  # noqa: F401 - scratch is a fixture

NO_SLEEP = lambda s: None  # noqa: E731


def merged_job(store, scratch, tmp_path, monkeypatch, targets=("control",)):
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    merge = incorporate.incorporate(store, job.job_id)["merge_commit"]
    monkeypatch.setattr(delivery, "activation_targets", lambda st, j: tuple(targets))
    return job.job_id, merge


def control_at(head, ok=True):
    return {"control": lambda: [{"target": "control", "ok": ok, "head": head, "pid": 1, "detail": "pong"}]}


GREEN = lambda: (True, "ok")  # noqa: E731


def test_deploy_ok_without_running_revision_is_not_activation(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    jid, merge = merged_job(store, scratch, tmp_path, monkeypatch)
    old = git(scratch, "rev-parse", f"{merge}^1")
    res = activation.activate(store, jid, merge, deploy=lambda st, **kw: {"ok": True, "summary": "restarted"},
                              probes=control_at(old), health_fn=GREEN, now=100.0, sleep=NO_SLEEP, authorized=True)
    assert res["activated"] is False and delivery.receipts(store, jid, "activation") == []
    (b,) = [b for b in blockers.open_blockers(store, jid) if b.kind == activation.UNPROVEN_KIND]
    assert b.disposition == "candidate" and old[:12] in b.claim
    assert not delivery.summary(store, jid)["delivered"]


def test_running_revision_with_the_merge_and_green_checks_is_activation(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    jid, merge = merged_job(store, scratch, tmp_path, monkeypatch)
    v = activation.verify_activation(store, jid, merge, probes=control_at(merge), health_fn=GREEN, now=100.0,
                                     sleep=NO_SLEEP)
    assert v["proven"] is True
    (r,) = delivery.receipts(store, jid, "activation")
    assert r["revision"] == merge and r["revision_contains"] == merge and r["evidence"]["live_checks"]["ok"]
    assert r["evidence"]["unprobed"] == list(activation.UNPROBED)
    assert next(m for m in delivery.milestones(store, jid) if m.name == "activation").status == "done"


def test_red_live_checks_block_activation_even_at_the_right_revision(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    jid, merge = merged_job(store, scratch, tmp_path, monkeypatch)
    v = activation.verify_activation(store, jid, merge, probes=control_at(merge), health_fn=lambda: (False, "RED: mcp"),
                                     now=100.0, sleep=NO_SLEEP, tries=2)
    assert v["proven"] is False and delivery.receipts(store, jid, "activation") == []


def test_runners_on_old_code_keep_activation_pending_until_they_restart(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    jid, merge = merged_job(store, scratch, tmp_path, monkeypatch, targets=("control", "runners"))
    old = git(scratch, "rev-parse", f"{merge}^1")
    runners = {"head": old}
    probes = {**control_at(merge),
              "runners": lambda: [{"target": "runner:abcd1234", "ok": True, "head": runners["head"], "pid": 2}]}
    assert not activation.verify_activation(store, jid, merge, probes=probes, health_fn=GREEN, now=100.0,
                                            sleep=NO_SLEEP, tries=1)["proven"]
    runners["head"] = merge  # the runner self-restarted onto the new code
    assert activation.verify_activation(store, jid, merge, probes=probes, health_fn=GREEN, now=500.0,
                                        sleep=NO_SLEEP, tries=1)["proven"]
    assert not [b for b in blockers.open_blockers(store, jid) if b.kind == activation.UNPROVEN_KIND]


def test_without_activation_authority_the_owner_gets_the_exact_restart_decision(steward_home, scratch, tmp_path,
                                                                                monkeypatch):
    store = Store(steward_home)  # conftest: STEWARD_AUTODEPLOY=0
    jid, merge = merged_job(store, scratch, tmp_path, monkeypatch)
    delivery.record_post_merge(store, jid, merge, None, now=90.0)
    out = delivery.plan(store, jid, now=100.0)
    delivery.plan(store, jid, now=200.0)
    assert out["step"]["kind"] == "owner_decision" and out["step"]["milestone"] == "activation"
    (b,) = [b for b in blockers.open_blockers(store, jid) if b.kind == delivery.DECISION_KIND]
    assert b.human_request["decision"] == f"Restart Steward to activate {merge[:12]} (job {jid[:8]})?"


def test_deploy_runs_at_most_once_per_merge_commit(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    jid, merge = merged_job(store, scratch, tmp_path, monkeypatch)
    deploys = []
    old = git(scratch, "rev-parse", f"{merge}^1")
    for t in (100.0, 500.0):
        activation.activate(store, jid, merge, deploy=lambda st, **kw: deploys.append(1) or {"ok": True},
                            probes=control_at(old), health_fn=GREEN, now=t, sleep=NO_SLEEP, authorized=True)
    assert len(deploys) == 1
    assert activation.state(store, jid, merge, now=500.0 + activation.VERIFY_EVERY_S) == "verify"


def test_ping_reports_the_loaded_revision(steward_home):
    store = Store(steward_home)
    rec = cli._build_control_handlers(store)["ping"]({"op": "ping", "args": {}})
    assert rec["accepted"] and rec["head"] == stability.PROCESS_HEAD and rec["started_at"] == stability.PROCESS_STARTED_AT


def test_declared_activation_target_is_owner_attested(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}, state=JobState.CREATED)
    store_contract(store, job.job_id, Contract(goal="g", done_when=["the service is deployed and serving the new page"]))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.ACCEPTED.value, job.job_id))
    merge = incorporate.incorporate(store, job.job_id)["merge_commit"]
    delivery.record_post_merge(store, job.job_id, merge, None, now=90.0)
    assert delivery.activation_targets(store, job.job_id) == ("declared",)
    out = delivery.plan(store, job.job_id, now=100.0)
    assert out["step"]["kind"] == "owner_decision" and "confirm" in out["step"]["decision"]
    aid = next(a["id"] for a in approvals.list_approvals(store, job.job_id, status="pending")
               if a.get("kind") == delivery.APPROVAL_KIND)
    approvals.decide(store, job.job_id, aid, "approve", decided_by="human")
    assert delivery.next_step(store, job.job_id, now=200.0)["kind"] == "attest_activation"
    activation.attest(store, job.job_id, merge, approval_id=aid, now=200.0)
    (r,) = delivery.receipts(store, job.job_id, "activation")
    assert r["actor"].startswith("owner") and r["evidence"]["attested"] and r["revision_contains"] == merge
