"""Spec 2026-10-13 §4.6/§9: the pause-review view and the trial numbers, per arm (pause vs off)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pause_support import Spend

from steward import pause_metrics as pm
from steward.models import Judgment
from steward.store import Store

SINCE = "2026-10-01T00:00:00+00:00"


def _job(store, mode, *, minutes_ago=30):
    job = store.create_job("g", "/repo")
    contract = {"goal": "g"} if mode is None else {"goal": "g", "pause": mode}
    store.kv_set(job.job_id, "contract", contract)
    created = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with store._conn:
        store._conn.execute("UPDATE jobs SET created_at = ? WHERE job_id = ?", (created, job.job_id))
    return job.job_id


def _ev(store, jid, what, judgment=Judgment.code, **evidence):
    store.append_event(jid, what=what, evidence=evidence, rule="t", judgment=judgment)


def test_pause_review_metrics_per_arm(steward_home):
    store = Store(steward_home)
    a = _job(store, "unit")
    _job(store, "off")
    _job(store, None)                                            # a contract stored before the field: off arm
    for wait in (10.0, 20.0, 300.0):
        _ev(store, a, "pause_verdict", outcome="continue", wait_s=wait, provisional=False)
    _ev(store, a, "pause_verdict", outcome="send_back", wait_s=400.0, provisional=True)
    _ev(store, a, "pause_review_timeout", unit_id="u1", wait_s=300.0)
    _ev(store, a, "operator released", Judgment.human)          # a mechanical touch
    _ev(store, a, "question_answered", Judgment.human)          # a decision
    _ev(store, a, "completion ruled finished; code guards held", Judgment.model_informed_code_authorized)
    out = pm.compute(store, since_iso=SINCE, report_fn=lambda st, jid: Spend(2.0, 1.0))
    p = out["arms"]["pause"]
    assert (p["jobs"], p["reviews"], p["reviews_per_job"]) == (1, 4, 4.0)
    assert (p["wait_p50_s"], p["wait_p90_s"]) == (20.0, 400.0)
    assert (p["timeouts"], p["provisional_sent_back"]) == (1, 1)
    assert p["outcomes"] == {"continue": 3, "send_back": 1}
    assert p["interventions_per_job"] == 2.0 and p["usd_per_job"] == 3.0
    assert 29.0 <= p["time_to_done_p50_min"] <= 31.0
    o = out["arms"]["off"]
    assert (o["jobs"], o["reviews"], o["wait_p50_s"], o["time_to_done_p50_min"]) == (2, 0, None, None)


def test_jobs_before_since_are_left_out_and_spend_can_be_skipped(steward_home):
    store = Store(steward_home)
    _job(store, "unit", minutes_ago=60 * 24 * 30)
    out = pm.compute(store, since_iso=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat(), spend=False)
    assert out["arms"]["pause"]["jobs"] == 0 and out["arms"]["pause"]["usd_per_job"] is None


def test_render_has_one_line_per_arm(steward_home):
    text = pm.render(pm.compute(Store(steward_home), since_iso=SINCE, spend=False))
    assert "pause arm: 0 jobs" in text and "off arm: 0 jobs" in text
