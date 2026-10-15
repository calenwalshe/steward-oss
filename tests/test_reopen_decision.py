"""`steward reopen-decision`: reopen owner questions the pre-carry-forward sweep closed with `job ACCEPTED` /
`job RELEASED` while they were still unanswered (84bc6350 / 58b8065c), as open carried-forward owner decisions.

The legacy shape is reproduced by resolving the carried blocker with the old sweep's reason and NOT moving the event
log, so the automatic repair in `ownership._question_specs` has not run yet: only the verb reopens it."""
from __future__ import annotations

import json

from ownership_fixtures import CONTROLLER
from test_ownership_carry_forward import QID, _accepted

from steward import alerts, blockers, cli, ownership, phone, questions
from steward.actor import Actor, ActorRef
from steward.models import JobState
from steward.store import Store

OWNER = ActorRef(Actor.OWNER, "cli")


def _legacy(store, state=JobState.ACCEPTED) -> str:
    """A finished job whose unanswered question's blocker the old sweep closed with `job <STATE>`."""
    jid = _accepted(store, state)
    (q,) = blockers.open_blockers(store, jid)
    blockers.transition(store, q.blocker_id, "resolved", actor=CONTROLLER, reason=f"job {state.value}", now=210.0)
    assert blockers.open_blockers(store, jid) == [] and ownership.carried_decisions(store) == {}
    return jid


def _episodes(store, jid):
    return [b for b in blockers.for_job(store, jid) if b.kind == "owner_question"]


def _reopened_events(store, jid):
    return [e for e in store.events(jid) if e.what == ownership.REOPEN_EVENT]


def test_scan_lists_only_unanswered_questions_closed_by_the_job_end(steward_home, capsys):
    store = Store(steward_home)
    acc, rel = _legacy(store), _legacy(store, JobState.RELEASED)
    answered = _legacy(store)
    questions.apply(store, answered, answers=[{"qid": QID, "option": "a"}], origin="cli")
    still_open = _accepted(store)  # carried forward correctly: nothing to reopen

    found = ownership.closed_by_job_end(store)
    assert {(f["job_id"], f["qid"], f["resolved_reason"]) for f in found} == {
        (acc, QID, "job ACCEPTED"), (rel, QID, "job RELEASED")}
    assert ownership.closed_by_job_end(store, [still_open]) == []

    before = {j: len(_episodes(store, j)) for j in (acc, rel)}
    assert cli.main(["reopen-decision", "--scan-all", "--json"]) == 0
    assert {f["job_id"] for f in json.loads(capsys.readouterr().out)} == {acc, rel}
    assert cli.main(["reopen-decision", "--scan", acc]) == 0
    out = capsys.readouterr().out
    assert acc in out and QID in out and "job ACCEPTED" in out and rel not in out
    # a dry run writes nothing
    assert {j: len(_episodes(store, j)) for j in (acc, rel)} == before
    assert blockers.open_blockers(store, acc) == [] and _reopened_events(store, acc) == []


def test_reopen_opens_a_new_audited_episode_without_answering(steward_home, capsys):
    store = Store(steward_home)
    jid = _legacy(store)
    (old,) = _episodes(store, jid)
    assert cli.main(["reopen-decision", jid, QID, "--reason", "closed by the old accept sweep"]) == 0
    assert "reopened" in capsys.readouterr().out

    (b,) = blockers.open_blockers(store, jid)
    assert (b.kind, b.disposition, b.episode) == ("owner_question", "confirmed_human", old.episode + 1)
    assert b.human_request is not None
    (ev,) = _reopened_events(store, jid)
    assert ev.evidence["qid"] == QID and ev.evidence["blocker_id"] == b.blocker_id
    assert ev.evidence["previous_blocker_id"] == old.blocker_id and ev.evidence["previous_reason"] == "job ACCEPTED"
    assert ev.evidence["reason"] == "closed by the old accept sweep" and ev.evidence["by"]
    assert blockers.history(store, b.blocker_id)[0]["actor"] == ev.evidence["by"]
    # the question itself is untouched: still waiting, no answer recorded
    (rec,) = questions.load(store, jid)
    assert rec["status"] in questions.WAITING and not rec.get("answer")
    # a later reconcile keeps it open (carried forward, not re-swept)
    store.append_event(jid, what="note", evidence={}, rule="test", judgment=ev.judgment)
    ownership.reconcile_job(store, jid, now=400.0)
    assert [x.blocker_id for x in blockers.open_blockers(store, jid)] == [b.blocker_id]


def test_reopen_refuses_an_answered_question(steward_home, capsys):
    store = Store(steward_home)
    jid = _legacy(store)
    questions.apply(store, jid, answers=[{"qid": QID, "option": "b"}], origin="cli")
    n = len(_episodes(store, jid))
    assert cli.main(["reopen-decision", jid, QID]) == 1
    assert "answered" in capsys.readouterr().out
    assert len(_episodes(store, jid)) == n and blockers.open_blockers(store, jid) == []
    assert _reopened_events(store, jid) == []


def test_reopen_is_idempotent(steward_home, capsys):
    store = Store(steward_home)
    jid = _legacy(store)
    assert cli.main(["reopen-decision", jid, QID]) == 0
    assert cli.main(["reopen-decision", jid, QID]) == 0
    assert "already an open owner decision" in capsys.readouterr().out
    assert len(_episodes(store, jid)) == 2 and len(blockers.open_blockers(store, jid)) == 1
    assert len(_reopened_events(store, jid)) == 1
    assert ownership.closed_by_job_end(store) == []
    # the automatic repair on the next classification does not add a third episode either
    ownership.ensure_classified(store, jid, now=500.0)
    assert len(_episodes(store, jid)) == 2


def test_reopen_refusals(steward_home):
    store = Store(steward_home)
    stopped = _accepted(store, JobState.STOPPED)  # abandoned work: its questions resolve with the job
    try:
        ownership.reopen_decision(store, stopped, QID, actor=OWNER, reason="x", now=600.0)
        raise AssertionError("reopened a STOPPED job's question")
    except ownership.ReopenRefused as exc:
        assert exc.kind == "refused"
    jid = _legacy(store)
    for qid, kind in (("nope", "unknown_question"),):
        try:
            ownership.reopen_decision(store, jid, qid, actor=OWNER, reason="x", now=600.0)
            raise AssertionError
        except ownership.ReopenRefused as exc:
            assert exc.kind == kind
    try:
        ownership.reopen_decision(store, "no-such-job", QID, actor=OWNER, reason="x", now=600.0)
        raise AssertionError
    except ownership.ReopenRefused as exc:
        assert exc.kind == "unknown_job"
    # a blocker some other actor resolved is not the sweep's doing: not reopened
    other = _accepted(store)
    (q,) = blockers.open_blockers(store, other)
    blockers.transition(store, q.blocker_id, "resolved", actor=OWNER, reason="owner withdrew it", now=210.0)
    try:
        ownership.reopen_decision(store, other, QID, actor=OWNER, reason="x", now=600.0)
        raise AssertionError
    except ownership.ReopenRefused as exc:
        assert exc.kind == "refused" and "owner withdrew it" in str(exc)


def test_reopen_needs_a_human_origin(steward_home):
    store = Store(steward_home)
    jid = _legacy(store)
    handler = cli._build_control_handlers(store)["reopen_decision"]
    res = handler({"args": {"job_id": jid, "qid": QID, "reason": ""}, "origin": "steer-agent"})
    assert res["accepted"] is False and res["error_kind"] == "refused"
    assert blockers.open_blockers(store, jid) == []


def test_a_reopened_decision_reaches_the_attention_sources(steward_home, capsys):
    store = Store(steward_home)
    jid = _legacy(store)
    assert cli.main(["reopen-decision", jid, QID, "--reason", "repair"]) == 0
    capsys.readouterr()

    (d,) = ownership.carried_decisions(store)[jid]
    assert d["qid"] == QID
    assert cli.main(["status", "--json", "--recent", "0"]) == 0  # the dash / ops source
    rows = {r["job_id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows[jid]["owner_decisions"][0]["text"].startswith(f"decision carried forward from accepted job {jid[:8]}")
    assert dict(alerts.reasons(store, jid))["decision"].startswith("decision carried forward")
    snap = {j.job_id: j for j in phone.gather(store).jobs}
    assert snap[jid].kind == "waiting"
    assert [c["qid"] for c in questions.view(store, jid)["questions"]] == [QID]
