"""Plan 3 Task 10: queued independent reviews are bounded, fresh, and only act through the lead gate."""
from __future__ import annotations

import json

import pytest

from ownership_fixtures import ASTRA, CONTROLLER, FABLE, Clock, park_rule
from steward import attempts, blockers, waits
from steward import independent_review as ir
from steward import tech_lead_policy as tpol
from steward.store import Store


class SyncBg:
    def __init__(self, fn):
        try:
            self._r, self._e = fn(), None
        except BaseException as exc:
            self._r, self._e = None, exc

    def done(self): return True
    def result(self):
        if self._e: raise self._e
        return self._r


class Exec:
    def __init__(self): self.done = []
    def execute(self, ctx, action):
        self.done.append((ctx, action))
        return {"outcome": "executed", "action": action.type}
    def record(self, ctx, action, outcome, detail, **kw): return {"outcome": outcome, "detail": detail}
    def refuse(self, ctx, action, why, klass): return {"outcome": "refused", "detail": why}


class Answers:
    def __init__(self, plan): self.plan, self.calls = {k: list(v) for k, v in plan.items()}, []
    def __call__(self, entry, prompt, timeout_s):
        self.calls.append((entry.id, prompt))
        nxt = self.plan[entry.id].pop(0)
        if isinstance(nxt, Exception): raise nxt
        return nxt


def NEW(conf=0.9, change="regenerate fixtures before the suite"):
    return (json.dumps({"verdict": "new_approach", "hypothesis": "fixtures are stale", "evidence_refs": ["event:1"],
                        "confidence": conf, "mechanism": "regenerate", "change": change,
                        "success_observation": "pytest -q exits 0", "instructions": "make fixtures; pytest -q"}), 0.12)


def WAIT(at=10 ** 9):
    return (json.dumps({"verdict": "no_credible_action", "hypothesis": "vendor API is down", "evidence_refs": [],
                        "confidence": 0.8, "wait": {"kind": "time", "params": {"at": at}}}), 0.10)


def _setup(steward_home, plan, *, leads=(ASTRA, FABLE), worker_model=None):
    store = Store(steward_home)
    jid = park_rule(store, "recovery_budget_exhausted", "recovery escalated to human")
    (store.home / "worktrees" / jid).mkdir(parents=True, exist_ok=True)
    b, _ = blockers.raise_blocker(store, jid, kind="recovery_exhausted", subject="worker", claim="worker keeps dying",
                                  evidence=["event:1"], origin_actor=CONTROLLER, now=1.0)
    clock, answers, ex = Clock(1000.0), Answers(plan), Exec()
    rev = ir.IndependentReviewer(store, list(leads), ex, invoke=answers, clock=clock, start_bg=SyncBg,
                                 policy_fn=lambda: tpol.Policy(), worker_model_fn=lambda j: worker_model,
                                 available=lambda e: None)
    return store, jid, b, clock, answers, ex, rev


def _enqueue(store, jid, b, now, prefer_other_than="astra-tech-lead"):
    return ir.enqueue(store, b.blocker_id, job_id=jid, issue_key="recovery:worker", reason="2 unproductive rounds",
                      prefer_other_than=prefer_other_than, now=now)


def test_unchanged_evidence_never_buys_a_second_paid_diagnosis_continuous_ownership_recovery_acceptance(steward_home):
    store, jid, b, clock, answers, ex, rev = _setup(steward_home, {"fable-tech-lead": [WAIT(), WAIT()]})
    assert _enqueue(store, jid, b, clock())
    rev.tick(); rev.tick()
    assert len(answers.calls) == 1
    assert _enqueue(store, jid, b, clock.advance(3600))
    rev.tick()
    assert len(answers.calls) == 1 and [e for e in store.events(jid) if e.what == "independent_review_reused"]
    attempt = attempts.record(store, jid, issue_key="recovery:worker", actor=CONTROLLER,
                             hypothesis="h", change="c", success_observation="s", now=clock())
    attempts.resolve(store, attempt.attempt_id, "failed", detail={}, now=clock())
    _enqueue(store, jid, b, clock.advance(60))
    rev.tick(); rev.tick()
    assert len(answers.calls) == 2


def test_new_approach_runs_through_the_gate_and_records_an_attempt_continuous_ownership_recovery_acceptance(steward_home):
    store, jid, b, clock, answers, ex, rev = _setup(steward_home, {"fable-tech-lead": [NEW()]})
    _enqueue(store, jid, b, clock()); rev.tick(); rev.tick()
    assert [a.type for _, a in ex.done] == ["reopen"]
    ctx = ex.done[0][0]
    assert ctx.lead_id == "independent:fable-tech-lead" and ctx.proposal.judgment is True
    (att,) = attempts.for_issue(store, jid, "recovery:worker")
    assert att.actor.startswith("independent_reviewer") and att.success_observation == "pytest -q exits 0"
    assert blockers.get(store, b.blocker_id).diagnosis["by"] == "independent"
    spend = store._conn.execute("SELECT source, usd FROM spend_ledger WHERE job_id = ?", (jid,)).fetchall()
    assert [tuple(r) for r in spend] == [("independent_review", 0.12)]


def test_low_confidence_records_the_diagnosis_without_acting(steward_home):
    store, jid, b, clock, answers, ex, rev = _setup(steward_home, {"fable-tech-lead": [NEW(conf=0.6)]})
    _enqueue(store, jid, b, clock()); rev.tick(); rev.tick()
    assert ex.done == [] and blockers.get(store, b.blocker_id).diagnosis["verdict"] == "new_approach"
    assert [e for e in store.events(jid) if e.what == "independent_review_below_threshold"]


def test_no_credible_action_parks_on_a_wake_condition_continuous_ownership_recovery_acceptance(steward_home):
    store, jid, b, clock, answers, ex, rev = _setup(steward_home, {"fable-tech-lead": [WAIT(at=5000.0)]})
    _enqueue(store, jid, b, clock()); rev.tick(); rev.tick()
    (w,) = waits.waiting(store, jid)
    assert w["kind"] == "time" and blockers.get(store, b.blocker_id).disposition == "waiting_external"


def test_same_model_fallback_is_disclosed(steward_home):
    store, jid, b, clock, answers, ex, rev = _setup(steward_home, {"astra-tech-lead": [WAIT()]}, leads=(ASTRA,))
    _enqueue(store, jid, b, clock()); rev.tick(); rev.tick()
    started = [e for e in store.events(jid) if e.what == "independent_review_started"][0]
    assert started.evidence["disclosure"] == "same_model_fresh_context"
    assert blockers.get(store, b.blocker_id).diagnosis["disclosure"] == "same_model_fresh_context"


def test_reviewer_failures_fall_through_and_never_ask_the_owner(steward_home):
    store, jid, b, clock, answers, ex, rev = _setup(
        steward_home, {"fable-tech-lead": [TimeoutError("slow")], "astra-tech-lead": [("not json", None)]})
    _enqueue(store, jid, b, clock())
    for _ in range(4): rev.tick()
    assert len(answers.calls) == 2
    assert [e for e in store.events(jid) if e.what == "independent_review_unavailable"]
    assert blockers.get(store, b.blocker_id).disposition != "confirmed_human"
    assert ir.queued(store, jid)
    spend = store._conn.execute("SELECT usd, price_basis FROM spend_ledger WHERE job_id = ?", (jid,)).fetchall()
    assert all(r["usd"] is None and r["price_basis"] == "unknown" for r in spend) and len(spend) == 2


def test_answer_against_a_newer_authority_revision_is_discarded(steward_home, monkeypatch):
    store, jid, b, clock, answers, ex, rev = _setup(steward_home, {"fable-tech-lead": [NEW()]})
    cur = {"rev": 1}
    monkeypatch.setattr("steward.ownership.authority_rev", lambda store, job_id: cur["rev"])
    def bg(fn):
        cur["rev"] = 2
        return SyncBg(fn)
    rev.start_bg = bg
    _enqueue(store, jid, b, clock()); rev.tick(); rev.tick()
    assert ex.done == [] and [e for e in store.events(jid) if e.what == "independent_review_discarded"]
    assert ir.queued(store, jid)


def test_restart_requeues_a_lost_review_without_counting_a_failure(steward_home):
    store, jid, b, clock, answers, ex, rev = _setup(steward_home, {"fable-tech-lead": [WAIT()]})
    rev.start_bg = lambda fn: type("Never", (), {"done": lambda self: False})()
    _enqueue(store, jid, b, clock()); rev.tick()
    fresh = ir.IndependentReviewer(store, [ASTRA, FABLE], ex, invoke=answers, clock=clock, start_bg=SyncBg,
                                  policy_fn=lambda: tpol.Policy(), available=lambda e: None)
    fresh.tick()
    clock.advance(ir.TIMEOUT_S + 61)
    fresh.tick(); fresh.tick()
    assert not [e for e in store.events(jid) if e.what == "independent_review_unavailable"]
    assert waits.waiting(store, jid)
