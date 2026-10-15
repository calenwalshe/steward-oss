from datetime import datetime

from steward import approvals, train_report
from steward.models import Judgment
from steward.store import Store


def _now():
    return datetime(2026, 10, 8, 8, 5).timestamp()


def _job(store):
    return store.create_job("goal", "/tmp/repo")


def _send_body(store):
    sent = []
    assert train_report.maybe_send_daily(store, now=_now(), report_hour=7, send=lambda t: sent.append(t) or True)
    return sent[0]


def test_report_has_metrics_and_pending_carried_decisions():
    store = Store()
    job = _job(store)
    rec = approvals.open_approval(store, job.job_id, "Which license?", kind="owner_question")
    store.append_event(job.job_id, what="owner_decision_carried", evidence={"ids": [rec["id"]], "cause": "x"},
                       rule="cleanup", judgment=Judgment.code)
    body = _send_body(store)
    assert "touches/job" in body
    assert "still waiting on you:" in body and rec["id"] in body


def test_answered_carried_decision_is_not_listed():
    store = Store()
    job = _job(store)
    rec = approvals.open_approval(store, job.job_id, "Which license?", kind="owner_question")
    store.append_event(job.job_id, what="owner_decision_carried", evidence={"ids": [rec["id"]], "cause": "x"},
                       rule="cleanup", judgment=Judgment.code)
    approvals.mark_stale(store, job.job_id, rec["id"], reason="answered")
    body = _send_body(store)
    assert "touches/job" in body and "still waiting on you" not in body
