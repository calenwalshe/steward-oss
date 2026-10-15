"""Plan 3 Task 11: strategy requests, wait sweeps, independent review, and owner status are wired end-to-end."""
from __future__ import annotations

import json
import time

from test_diagnosis_loop import WAIT, Answers, Exec, SyncBg

from ownership_fixtures import ASTRA, FABLE, CONTROLLER, Clock, contracted, park_rule, proposal
from steward import blockers, ownership, questions_delivery, waits
from steward import independent_review as ir
from steward import tech_lead_actions as tla
from steward import tech_lead_notebook as nb
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store


def _reviewer(store, plan, clock):
    return ir.IndependentReviewer(store, [ASTRA, FABLE], Exec(), invoke=Answers(plan), clock=clock, start_bg=SyncBg,
                                  policy_fn=lambda: tpol.Policy(), available=lambda e: None)


def test_third_sighting_gets_independent_diagnosis_from_another_model(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    facts = [{"id": "f1", "kind": "new_failures_repeated", "data": {"test": "t::a"}}]
    sig = nb.signature(facts)
    for trg in ("trg-1", "trg-2"):
        nb.append(store, jid, signature=sig, action="send_back", strategy="send_back", outcome="pending",
                  model="gpt-6-astra", lead="astra-tech-lead", trigger_id=trg, kind="new_failures_repeated")
    prop = tla.parse_job_proposal(json.loads(proposal({"type": "send_back", "params": {"text": "x", "evidence": "t"}})))
    ctx = tla.ActionContext(jid, "astra-tech-lead", "gpt-6-astra", prop, store.home / "worktrees" / jid,
                            tpol.Policy(), facts=facts, trigger_id="trg-3")
    nb.Memory(store).before_act(ctx, executor=None)
    (item,) = ir.queued(store, jid)
    assert item["prefer_other_than"] == "astra-tech-lead"
    clock = Clock(time.time() + 1)
    rev = _reviewer(store, {"fable-tech-lead": [WAIT()]}, clock)
    rev.tick()
    started = [e for e in store.events(jid) if e.what == "independent_review_started"]
    assert started[0].evidence["reviewer"] == "fable-tech-lead"
    assert started[0].evidence["disclosure"] == "different_model"


def test_strategy_blockers_are_not_also_reviewed_by_the_normal_lead(steward_home):
    store = Store(steward_home)
    jid = park_rule(store, "recovery_budget_exhausted", "recovery escalated to human")
    b = ownership.request_strategy_review(store, jid, blocker_id=None, reason="2 unproductive rounds",
                                          signature="recovery:worker", now=1.0)
    due = ownership.due_reviews(store, now=10_000.0)
    assert b.blocker_id not in {getattr(r, "blocker_id", None) for r in due}
    assert ir.queued(store, jid)[0]["blocker_id"] == b.blocker_id


def test_delivery_tick_probes_waits_and_drives_the_reviewer(steward_home):
    store = Store(steward_home)
    jid = park_rule(store, "recovery_budget_exhausted", "recovery escalated to human")
    b, _ = blockers.raise_blocker(store, jid, kind="recovery_exhausted", subject="w", claim="c", evidence=["e:1"],
                                  origin_actor=CONTROLLER, now=1.0)
    waits.park(store, jid, blocker_id=b.blocker_id, kind="time", params={"at": 5.0}, reason="r", now=1.0)

    class Rev:
        calls = 0
        def tick(self):
            Rev.calls += 1
            return {"started": 0}

    ok = FakeRunner(lambda argv, cwd: CommandResult(argv, str(cwd), 0, "", "", 0.0))
    out = questions_delivery.tick(store, ok, now=1000.0, independent=Rev())
    assert out["waits"]["woken"] == 1 and Rev.calls == 1 and out["independent"] == {"started": 0}


def test_owner_view_says_what_steward_is_doing_and_waiting_for(steward_home):
    store = Store(steward_home)
    jid = park_rule(store, "recovery_budget_exhausted", "recovery escalated to human")
    b = ownership.request_strategy_review(store, jid, blocker_id=None, reason="2 unproductive rounds",
                                          signature="recovery:worker", now=1.0)
    assert "independent diagnosis queued" in ownership.owner_view(store, jid, now=2.0).text
    waits.park(store, jid, blocker_id=b.blocker_id, kind="time", params={"at": 7200.0},
               reason="the vendor reopens at 02:00", now=3.0)
    text = ownership.owner_view(store, jid, now=4.0).text
    assert "waiting: time" in text and "vendor reopens" in text and "next check" in text
