"""Plan 4 Task 6: a unit-green suite never closes an outcome that needs a live flow (spec §8, §11 s13)."""
from __future__ import annotations

import pytest

from steward import approvals, blockers, closeout, ownership, outcome_scope as osc
from steward.baseline import CheckRun
from steward import check_runs as cr
from steward.contract import Contract, store_contract
from steward.models import JobState, Judgment
from steward.store import Store
from steward.verify import Verification
from ownership_fixtures import CONTROLLER, OWNER
from ruling_support import answer, green

LIVE = "`scripts/live_transfer.py` passes on the device with an active transfer"


def finished(store, done_when):
    job = store.create_job("transfer recovery", "/repo")
    store_contract(store, job.job_id, Contract(goal="transfer recovery", done_when=done_when))
    store.set_state(job.job_id, JobState.RUNNING, what="worker dispatch started", evidence={}, rule="worker_dispatch",
                    judgment=Judgment.code)
    store.set_state(job.job_id, JobState.WORKER_FINISHED, what="worker reported done", evidence={"marker": "STEWARD-DONE"},
                    rule="steward_done_marker", judgment=Judgment.code)
    return job.job_id


def close(store, jid, done_when, sha="sha-1"):
    return closeout.closeout(store, jid, green(sha), [], Contract(goal="g", done_when=done_when))


@pytest.mark.parametrize("text,scope", [
    (LIVE, "live"), ("end-to-end upload works against the real account", "live"),
    ("the new code is running in the control pane after restart", "activation"),
    ("Sam physically unplugs the unit and it recovers", "manual"), ("unit tests cover the parser", "unit"),
    ("the parser handles empty input (live)", "live"), ("smoke test is green (unit)", "unit")])
def test_outcomes_get_a_required_scope(text, scope):
    (o,) = osc.outcomes(Contract(goal="g", done_when=[text]))
    assert o.requires == scope and o.oid == "o1"
    assert (o.command == "scripts/live_transfer.py") is (text == LIVE)


def test_unit_green_cannot_auto_accept_a_live_outcome(steward_home, monkeypatch):
    monkeypatch.setattr(closeout, "acceptance_mode", lambda: "auto")
    store = Store(steward_home)
    jid = finished(store, [LIVE, "unit tests cover the parser"])
    assert close(store, jid, [LIVE, "unit tests cover the parser"]) == JobState.NEEDS_HUMAN
    reasons = store.events(jid)[-1].evidence["reasons"]
    assert reasons == [f"outcome unproven (live): {LIVE}"]
    ownership.reconcile_job(store, jid, now=100.0)
    assert [(b.kind, b.disposition) for b in blockers.open_blockers(store, jid)] == [("outcome_unproven", "candidate")]


def test_unit_and_activation_outcomes_do_not_block_acceptance(steward_home, monkeypatch):
    monkeypatch.setattr(closeout, "acceptance_mode", lambda: "auto")
    store = Store(steward_home)
    dw = ["unit tests cover the parser", "the new code is running in the control pane after restart"]
    jid = finished(store, dw)
    assert close(store, jid, dw) == JobState.CHECKS_PASSED  # nothing blocks: the one completion ruling decides
    answer(store, jid)
    assert closeout.decide_after_ruling(store, jid, Contract(goal="g", done_when=dw)) == JobState.ACCEPTED


def test_finish_line_met_with_the_outcome_command_since_the_last_run_proves_it(steward_home):
    store = Store(steward_home)
    jid = finished(store, [LIVE])
    store.append_event(jid, what="finish_line_met", evidence={"command": "python3 scripts/live_transfer.py"},
                       rule="finish_line_gate", judgment=Judgment.code)
    contract = Contract(goal="g", done_when=[LIVE])
    assert osc.unproven(store, jid, contract, commit_sha="sha-1") == []


def test_a_finish_line_pass_before_the_latest_worker_run_is_stale(steward_home):
    store = Store(steward_home)
    jid = finished(store, [LIVE])
    store.append_event(jid, what="finish_line_met", evidence={"command": "scripts/live_transfer.py"},
                       rule="finish_line_gate", judgment=Judgment.code)
    store.append_event(jid, what="worker attempt started", evidence={}, rule="worker_dispatch", judgment=Judgment.code)
    assert [o.oid for o in osc.unproven(store, jid, Contract(goal="g", done_when=[LIVE]), commit_sha="sha-1")] == ["o1"]


def test_record_evidence_accepts_only_a_green_receipt_of_the_outcome_command(steward_home):
    store = Store(steward_home)
    jid = finished(store, [LIVE])
    contract = Contract(goal="g", done_when=[LIVE])
    store_contract(store, jid, contract)

    def receipt(command, exit_code):
        row, _ = cr.request(store, cr.RunSpec(f"T-{command}-{exit_code}", command, "e", "tc", False, 5.0), job_id=jid,
                            purpose="live_check", now=1.0)
        cr.try_start(store, row.run_id, owner="me", pid=None, now=1.0)
        cr.complete(store, row.run_id, owner="me", run=CheckRun(exit_code=exit_code, tests={}), junit_src=None, now=2.0)
        return f"check_run:{row.run_id}"

    for bad in (receipt("scripts/other.py", 0), receipt("scripts/live_transfer.py", 1), "check_run:cr-missing",
                "worker-said-so"):
        with pytest.raises(ValueError):
            osc.record_evidence(store, jid, oid="o1", scope="live", ref=bad, commit_sha="sha-1", actor=CONTROLLER, now=3.0)
    osc.record_evidence(store, jid, oid="o1", scope="live", ref=receipt("scripts/live_transfer.py", 0),
                        commit_sha="sha-1", actor=CONTROLLER, now=3.0)
    assert osc.unproven(store, jid, contract, commit_sha="sha-1") == []
    assert [o.oid for o in osc.unproven(store, jid, contract, commit_sha="sha-2")] == ["o1"]  # changed code


def test_a_manual_outcome_is_proven_only_by_an_owner_approved_decision(steward_home):
    store = Store(steward_home)
    manual = "Sam physically unplugs the unit and it recovers"
    jid = finished(store, [manual])
    contract = Contract(goal="g", done_when=[manual])
    rec = approvals.open_approval(store, jid, "Unplug the unit and confirm it recovers", kind="general")
    with pytest.raises(ValueError):
        osc.record_evidence(store, jid, oid="o1", scope="manual", ref=f"approval:{rec['id']}", commit_sha="sha-1",
                            actor=OWNER, now=3.0)  # still pending
    approvals.decide(store, jid, rec["id"], "approve", decided_by="human")
    osc.record_evidence(store, jid, oid="o1", scope="manual", ref=f"approval:{rec['id']}", commit_sha="sha-1",
                        actor=OWNER, now=4.0)
    assert osc.unproven(store, jid, contract, commit_sha="sha-1") == []


def test_outcome_blocker_clears_when_the_outcome_is_proven(steward_home, monkeypatch):
    monkeypatch.setattr(closeout, "acceptance_mode", lambda: "auto")
    store = Store(steward_home)
    jid = finished(store, [LIVE])
    store_contract(store, jid, Contract(goal="g", done_when=[LIVE]))
    close(store, jid, [LIVE])
    ownership.reconcile_job(store, jid, now=100.0)
    (b,) = [b for b in blockers.open_blockers(store, jid) if b.kind == "outcome_unproven"]
    store.append_event(jid, what="finish_line_met", evidence={"command": "scripts/live_transfer.py"},
                       rule="finish_line_gate", judgment=Judgment.code)
    res = ownership.reconcile_job(store, jid, now=200.0)
    assert b.blocker_id in res.cleared
