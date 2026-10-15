"""Carried-forward owner decisions: accepting (or releasing) a job never answers its owner questions.

Regression for 84bc6350 / 58b8065c (2026-10-06): `_clear` resolved every open blocker with `job ACCEPTED`, so the
unanswered owner questions vanished from every view that keys off open confirmed_human blockers, while `steward
questions` still listed them as needing the owner."""
from __future__ import annotations

import json

from ownership_fixtures import CONTROLLER, park_done
from questions_helpers import make_record, seed

from steward import alerts, blockers, cli, jobview, ownership, phone, questions, session
from steward.models import JobState, Judgment
from steward.store import Store

QID = "gsd_context:CONTEXT.md:G1"


def _accepted(store, state=JobState.ACCEPTED) -> str:
    """A job parked with one technical blocker and one unanswered owner question, then finished in `state`."""
    jid = park_done(store, ["test-weakening detected: ['x']"])
    seed(store, jid, [make_record(QID, starred=False, title="Which carrier?")])
    ownership.ensure_classified(store, jid, now=100.0)
    kinds = {b.kind: b.disposition for b in blockers.open_blockers(store, jid)}
    assert kinds == {"test_weakening": "candidate", "owner_question": "confirmed_human"}
    path = {JobState.RELEASED: (JobState.ACCEPTED, JobState.RELEASED),
            JobState.FAILED: (JobState.RUNNING, JobState.FAILED)}.get(state, (state,))
    for st in path:
        store.set_state(jid, st, what=f"owner {st.value.lower()}", evidence={}, rule="owner_decision",
                        judgment=Judgment.human)
        ownership.reconcile_job(store, jid, now=200.0)
    return jid


def _by_kind(store, jid):
    return {b.kind: b for b in blockers.for_job(store, jid)}


def test_accept_keeps_an_unanswered_owner_question_open_and_listed(steward_home):
    store = Store(steward_home)
    jid = _accepted(store)
    (q,) = blockers.open_blockers(store, jid)
    assert (q.kind, q.disposition) == ("owner_question", "confirmed_human")
    assert [c["qid"] for c in questions.view(store, jid)["questions"]] == [QID]
    view = ownership.owner_view(store, jid)
    assert view.status == "needs_owner"
    assert view.text.startswith(f"decision carried forward from accepted job {jid[:8]}: ")
    assert ownership.projection(store, jid)["owner_kinds"] == ["owner_question"]


def test_non_owner_blockers_still_resolve_on_accept(steward_home):
    store = Store(steward_home)
    jid = _accepted(store)
    tw = _by_kind(store, jid)["test_weakening"]
    assert tw.disposition == "resolved" and tw.resolution["reason"] == "job ACCEPTED"


def test_answering_a_carried_question_after_accept_resolves_it(steward_home):
    store = Store(steward_home)
    jid = _accepted(store)
    rc = questions.apply(store, jid, answers=[{"qid": QID, "option": "b"}], origin="cli")
    assert rc["accepted"] is True
    res = ownership.reconcile_job(store, jid, now=300.0)
    q = _by_kind(store, jid)["owner_question"]
    assert q.blocker_id in res.cleared and q.disposition == "resolved"
    assert q.resolution["predicate"] == "question_answered"
    assert ownership.carried_decisions(store) == {}
    assert ownership.owner_view(store, jid).status == "none"


def test_release_carries_forward_too(steward_home):
    store = Store(steward_home)
    jid = _accepted(store, JobState.RELEASED)
    assert [b.kind for b in blockers.open_blockers(store, jid)] == ["owner_question"]
    (d,) = ownership.carried_decisions(store)[jid]
    assert d["qid"] == QID and d["label"] == f"decision carried forward from released job {jid[:8]}"


def test_stopped_and_failed_jobs_keep_resolving_their_questions(steward_home):
    store = Store(steward_home)
    for state in (JobState.STOPPED, JobState.FAILED):
        jid = _accepted(store, state)
        assert blockers.open_blockers(store, jid) == []
        assert _by_kind(store, jid)["owner_question"].resolution["reason"] == f"job {state.value}"
    assert ownership.carried_decisions(store) == {}


def test_a_question_closed_only_by_the_old_accept_sweep_reopens(steward_home):
    """The live shape of 84bc6350: the blocker says `resolved: job ACCEPTED`, the question is still waiting."""
    store = Store(steward_home)
    jid = _accepted(store)
    (q,) = blockers.open_blockers(store, jid)
    blockers.transition(store, q.blocker_id, "resolved", actor=CONTROLLER, reason="job ACCEPTED", now=210.0)
    store.append_event(jid, what="note", evidence={}, rule="test", judgment=Judgment.code)  # the log moved
    ownership.ensure_classified(store, jid, now=220.0)
    (again,) = blockers.open_blockers(store, jid)
    assert again.kind == "owner_question" and again.episode == q.episode + 1
    assert again.disposition == "confirmed_human"
    ownership.ensure_classified(store, jid, now=230.0)  # idempotent: no third episode
    assert len([b for b in blockers.for_job(store, jid) if b.kind == "owner_question"]) == 2


def test_an_answered_question_closed_by_accept_stays_closed(steward_home):
    store = Store(steward_home)
    jid = _accepted(store)
    (q,) = blockers.open_blockers(store, jid)
    blockers.transition(store, q.blocker_id, "resolved", actor=CONTROLLER, reason="job ACCEPTED", now=210.0)
    questions.apply(store, jid, answers=[{"qid": QID, "option": "a"}], origin="cli")
    ownership.reconcile_job(store, jid, now=220.0)
    assert blockers.open_blockers(store, jid) == []


def test_attention_sources_include_a_carried_decision(steward_home, capsys):
    store = Store(steward_home)
    acc = _accepted(store)
    rel = _accepted(store, JobState.RELEASED)
    carried = ownership.carried_decisions(store)
    assert set(carried) == {acc, rel}

    # alerts: a `decision` reason, not misfiled as delivery, no duplicate needs_you
    keys = dict(alerts.reasons(store, acc))
    assert keys["decision"].startswith(f"decision carried forward from accepted job {acc[:8]}")
    assert "delivery" not in keys and "needs_you" not in keys

    # the dash's source (`steward status --json --recent N`): both jobs, stamped with their decisions
    assert cli.main(["status", "--json", "--recent", "0"]) == 0
    rows = {r["job_id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows[acc]["owner_decisions"][0]["qid"] == QID
    assert rows[rel]["owner_decisions"][0]["text"].startswith(f"decision carried forward from released job {rel[:8]}")

    # the phone: the RELEASED job is back on the list, waiting on the owner
    snap = {j.job_id: j for j in phone.gather(store).jobs}
    assert snap[rel].kind == "waiting" and snap[acc].kind == "waiting"
    assert snap[acc].ask.startswith("decision carried forward from accepted job")

    # `steward jobs`: a finished job with an open decision stays listed
    listed = {r["job_id"]: r for r in session.list_rows(store, me={})}
    assert rel in listed and listed[rel]["owner_decisions"]
    assert "◆ needs you: decision carried forward" in session.render_rows([listed[rel]])

    # jobview (dash / phone rows): the projection reads as a decision, not as done
    proj = ownership.projection(store, acc)
    assert jobview.liveness("ACCEPTED", [], ownership=proj)[0] == "waiting"


def test_no_carried_decision_means_done_as_before(steward_home):
    store = Store(steward_home)
    jid = park_done(store, ["test-weakening detected: ['x']"])
    ownership.ensure_classified(store, jid, now=100.0)
    store.set_state(jid, JobState.ACCEPTED, what="owner accepted", evidence={}, rule="owner_decision",
                    judgment=Judgment.human)
    ownership.reconcile_job(store, jid, now=200.0)
    assert blockers.open_blockers(store, jid) == [] and ownership.carried_decisions(store) == {}
    assert "decision" not in dict(alerts.reasons(store, jid))
    assert jobview.liveness("ACCEPTED", [], ownership=ownership.projection(store, jid))[0] == "done"
