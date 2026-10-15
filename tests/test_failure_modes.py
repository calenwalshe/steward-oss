import json

import pytest

from steward import failure_modes as fm
from steward.models import JobState, Judgment
from steward.store import Store


class Clock:
    """Appends events with chosen timestamps (the store stamps `now`; this rewrites ts only on a scratch fixture DB)."""

    def __init__(self, store):
        self.store = store
        self.job = store.create_job("goal", "/tmp/nowhere")

    def ev(self, ts, what, judgment=Judgment.code, rule="t", **evidence):
        self.store.append_event(self.job.job_id, what=what, evidence=evidence, rule=rule, judgment=judgment)
        with self.store._conn:
            self.store._conn.execute("DROP TRIGGER events_no_update")
            self.store._conn.execute("UPDATE events SET ts=? WHERE seq=(SELECT MAX(seq) FROM events)", (ts,))
            self.store._conn.execute("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN "
                                     "SELECT RAISE(ABORT, 'events are append-only'); END;")


@pytest.fixture
def c(steward_home):
    return Clock(Store())


def modes(store, **kw):
    out = fm.compute(store, since_iso="2026-10-01T00:00:00+00:00", **kw)
    return {m: s["count"] for m, s in out["modes"].items() if s["count"]}


def test_f2_same_as_last_ruled(c):
    c.ev("2026-10-02T10:00:00+00:00", "yield_classified", failed_preconditions=["same_as_last_ruled"])
    c.ev("2026-10-02T10:01:00+00:00", "yield_classified", failed_preconditions=["no_new_commit"])  # negative
    assert modes(c.store) == {"F2": 1}


def test_f3_f10_and_f6_hold(c):
    h = "checks passed; acceptance unresolved"
    c.ev("2026-10-02T10:00:00+00:00", h, reasons=["test-weakening detected: ['test_config_changed']"])
    c.ev("2026-10-02T11:00:00+00:00", h, reasons=["test-weakening detected: ['added_skip_marker']"])  # not config
    c.ev("2026-10-02T12:00:00+00:00", h, reasons=["no checks ran: the verification has no check results"])
    got = modes(c.store)
    assert got["F3"] == 1 and got["F10"] == 1 and got["F6"] == 3


def test_f6_needs_no_coordinator_episode_after_park(c):
    c.ev("2026-10-02T10:00:00+00:00", "checks passed; acceptance unresolved", reasons=["x"])
    c.ev("2026-10-02T10:05:00+00:00", "coordinator_episode", outcome="asked")
    assert "F6" not in modes(c.store)


def test_f4_ordered_answer_then_ask(c):
    c.ev("2026-10-02T10:00:00+00:00", "tech_lead_escalated", kind="tech_lead_exhausted")  # ask first: negative
    c.ev("2026-10-02T10:10:00+00:00", "tech_lead_answered")
    c.ev("2026-10-02T10:20:00+00:00", "tech_lead_escalated")
    c.ev("2026-10-02T10:50:00+00:00", "approval approved", Judgment.human, approval_id="a", question="q")
    out = fm.compute(c.store, since_iso="2026-10-01T00:00:00+00:00")
    f4 = out["modes"]["F4"]
    assert f4["count"] == 1 and f4["resolved_by"] == {"human": 1} and f4["median_ttr_s"] == 1800


def test_f5_needs_human_after_approve_with_nothing_pending(c):
    c.ev("2026-10-02T10:00:00+00:00", "approval opened", approval_id="a")
    c.ev("2026-10-02T10:01:00+00:00", "approval approved", Judgment.human, approval_id="a", question="q")
    c.ev("2026-10-02T10:02:00+00:00", "recovery escalated to human")
    assert modes(c.store)["F5"] == 1


def test_f5_not_when_an_approval_is_still_pending(c):
    c.ev("2026-10-02T10:00:00+00:00", "approval opened", approval_id="a")
    c.ev("2026-10-02T10:00:30+00:00", "approval opened", approval_id="b")
    c.ev("2026-10-02T10:01:00+00:00", "approval approved", Judgment.human, approval_id="a", question="q")
    c.ev("2026-10-02T10:02:00+00:00", "recovery escalated to human")
    assert "F5" not in modes(c.store)


def test_f7_health_not_green(c):
    c.ev("2026-10-02T10:00:00+00:00", "wait_parked", reason="health not green")
    c.ev("2026-10-02T10:01:00+00:00", "wait_parked", reason="baseline pending")
    assert modes(c.store) == {"F7": 1}


def test_f8_operator_agent_approval_and_coordinator_excluded(c):
    c.ev("2026-10-02T10:00:00+00:00", "approval opened", approval_id="a")
    c.ev("2026-10-02T10:10:00+00:00", "approval approved", Judgment.human, approval_id="a", question="q",
         actor="operator_agent:claude-code")
    c.ev("2026-10-02T10:20:00+00:00", "approval approved", Judgment.human, approval_id="b", question="q",
         actor="operator_agent:coordinator")
    out = fm.compute(c.store, since_iso="2026-10-01T00:00:00+00:00")
    assert out["modes"]["F8"]["count"] == 1 and out["modes"]["F8"]["median_ttr_s"] == 600
    assert out["modes"]["OTHER"]["count"] == 0  # claimed by F8 / coordinator is not a human touch


def test_f9_answer_after_merge_for_carried_question(c):
    c.ev("2026-10-02T10:00:00+00:00", "owner_decision_carried", ids=["q1"])
    c.ev("2026-10-02T10:30:00+00:00", "question_answered", Judgment.human, qid="q1")  # not merged yet: negative
    c.ev("2026-10-02T11:00:00+00:00", "landed_by_train")
    c.ev("2026-10-02T12:00:00+00:00", "question_answered", Judgment.human, qid="q1")
    c.ev("2026-10-02T12:01:00+00:00", "question_answered", Judgment.human, qid="other")
    assert modes(c.store)["F9"] == 1


def test_f1_human_restart_without_recent_orphan(c):
    c.ev("2026-10-02T10:00:00+00:00", "runner_restarted", Judgment.human, origin="cli")
    c.ev("2026-10-02T12:00:00+00:00", "liveness_orphan", state="RUNNING")
    c.ev("2026-10-02T12:10:00+00:00", "runner_restarted", Judgment.human, origin="cli")  # liveness had seen it
    c.ev("2026-10-02T13:00:00+00:00", "runner_restarted", Judgment.human, origin="watchdog")
    got = modes(c.store)
    assert got["F1"] == 1 and got["OTHER"] == 1


def test_f11_index_lock(c):
    c.ev("2026-10-02T10:00:00+00:00", "index_lock_healed", age_s=144, quarantine="/r/.git/index.lock.stale-1")
    out = fm.compute(c.store, since_iso="2026-10-01T00:00:00+00:00")
    assert out["modes"]["F11"]["resolved_by"] == {"code": 1} and out["modes"]["F11"]["median_ttr_s"] == 144


def test_other_human_touches(c):
    c.ev("2026-10-02T10:00:00+00:00", "operator accepted", Judgment.human)
    c.ev("2026-10-02T10:01:00+00:00", "operator stop", Judgment.human)
    c.ev("2026-10-02T10:02:00+00:00", "approval denied", Judgment.human, approval_id="z", question="q")
    c.ev("2026-10-02T10:03:00+00:00", "approval approved", Judgment.human, approval_id="p",
         question="worker permission: Bash")  # permission relay: not a touch
    assert modes(c.store) == {"OTHER": 3}


def test_since_filter_and_daily_boundaries(c):
    c.ev("2026-09-30T23:59:59+00:00", "yield_classified", failed_preconditions=["same_as_last_ruled"])
    c.ev("2026-10-01T00:00:00+00:00", "yield_classified", failed_preconditions=["same_as_last_ruled"])
    c.ev("2026-10-01T23:59:59+00:00", "yield_classified", failed_preconditions=["same_as_last_ruled"])
    c.ev("2026-10-02T00:00:00+00:00", "yield_classified", failed_preconditions=["same_as_last_ruled"])
    out = fm.compute(c.store, since_iso="2026-10-01T00:00:00+00:00", by_day=True)
    assert out["by_day"] == {"2026-10-01": {"F2": 2}, "2026-10-02": {"F2": 1}}
    assert out["modes"]["F2"]["count"] == 3 and out["modes"]["F2"]["jobs"] == 1


def test_empty_results_and_json_and_render(c):
    out = fm.compute(c.store, since_iso="2026-10-01T00:00:00+00:00", by_day=True)
    assert out["by_day"] == {} and all(s["count"] == 0 and s["median_ttr_s"] is None for s in out["modes"].values())
    assert json.loads(json.dumps(out))["modes"]["F1"]["unresolved"] == 0
    assert "F11" in fm.render(out) and fm.summary_line(out) == "failure modes (24h): none"


def test_summary_line_lists_only_fired_modes(c):
    c.ev("2026-10-02T10:00:00+00:00", "yield_classified", failed_preconditions=["same_as_last_ruled"])
    assert fm.summary_line(fm.compute(c.store, since_iso="2026-10-01T00:00:00+00:00")) == "failure modes (24h): F2 1/1j"


def test_cli_failure_modes_json(steward_home, capsys):
    from steward import cli
    Clock(Store()).ev("2026-10-02T10:00:00+00:00", "yield_classified", failed_preconditions=["same_as_last_ruled"])
    assert cli.main(["metrics", "failure-modes", "--since", "2026-10-01", "--by-day", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["modes"]["F2"]["count"] == 1 and got["by_day"] == {"2026-10-02": {"F2": 1}}
