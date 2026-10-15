"""Failure-mode attribution: the resolver is the actual actor, not the `human` judgment label."""
import pytest

from steward import failure_modes as fm
from steward.models import Judgment
from steward.store import Store

HOLD = "checks passed; acceptance unresolved"


class Clock:
    def __init__(self, store):
        self.store = store
        self.t = 0
        self.job = store.create_job("goal", "/tmp/nowhere")

    def ev(self, what, judgment=Judgment.code, rule="t", **evidence):
        self.t += 1
        ts = f"2026-10-02T10:{self.t:02d}:00+00:00"
        self.store.append_event(self.job.job_id, what=what, evidence=evidence, rule=rule, judgment=judgment)
        with self.store._conn:
            self.store._conn.execute("DROP TRIGGER events_no_update")
            self.store._conn.execute("UPDATE events SET ts=? WHERE seq=(SELECT MAX(seq) FROM events)", (ts,))
            self.store._conn.execute("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN "
                                     "SELECT RAISE(ABORT, 'events are append-only'); END;")


@pytest.fixture
def c(steward_home):
    return Clock(Store())


def f6_resolver(store):
    out = fm.compute(store, since_iso="2026-10-01T00:00:00+00:00")
    return out["modes"]["F6"]["resolved_by"]


def _hold(c):
    c.ev(HOLD, reasons=["hard constraint violation blocks closeout"])


def _denied_sent_back(c, by, **extra):
    c.ev("approval denied", Judgment.human, "operator_approval_decision", reason="sent back", **extra)
    c.ev("sent_back", Judgment.code, "tech_lead_send_back" if by.startswith("tech_lead") else "operator_send_back",
         by=by)


def test_tl_send_back_is_tl(c):
    _hold(c)
    _denied_sent_back(c, "tech_lead:assess")
    assert f6_resolver(c.store) == {"TL": 1}


def test_operator_agent_accept_is_agent(c):
    _hold(c)
    c.ev("operator accepted", Judgment.human, "operator_accept", actor="operator_agent:claude")
    assert f6_resolver(c.store) == {"agent": 1}


def test_unblock_agent_send_back_is_agent(c):
    _hold(c)
    _denied_sent_back(c, "unblock-agent")
    assert f6_resolver(c.store) == {"agent": 1}


def test_owner_cli_deny_is_human(c):
    _hold(c)
    _denied_sent_back(c, "cli")
    assert f6_resolver(c.store) == {"human": 1}


def test_human_judgment_without_actor_is_human(c):
    _hold(c)
    c.ev("operator accepted", Judgment.human, "operator_accept")
    assert f6_resolver(c.store) == {"human": 1}


def test_agent_in_resolvers_render_and_summary(c):
    assert "agent" in fm.RESOLVERS
    _hold(c)
    c.ev("operator accepted", Judgment.human, "operator_accept", actor="operator_agent:claude")
    out = fm.compute(c.store, since_iso="2026-10-01T00:00:00+00:00")
    assert out["modes"]["F6"]["resolved_by"] == {"agent": 1}
    line = [ln for ln in fm.render(out).splitlines() if ln.startswith("F6")][0]
    assert "agent 1" in line
    assert "F6 1/1j" in fm.summary_line(out)


def test_sent_back_uses_the_matching_following_event(c):
    c.ev("sent_back", Judgment.code, "operator_send_back", by="tech_lead:early")  # before the denial: ignored
    _hold(c)
    c.ev("approval denied", Judgment.human, "operator_approval_decision", reason="sent back")
    c.ev("sent_back", Judgment.human, "operator_send_back", by="cli")
    assert f6_resolver(c.store) == {"human": 1}


def test_agent_actor_overrides_human_judgment(c):
    _hold(c)
    c.ev("approval approved", Judgment.human, "operator_approval_decision", actor="operator_agent:claude")
    assert f6_resolver(c.store) == {"agent": 1}


CASES = __import__("pathlib").Path(__file__).resolve().parents[1] / "docs/diagnostics/2026-10-14-approver-backtest/data/cases.json"


@pytest.mark.skipif(not CASES.exists(), reason="backtest data not shipped")
def test_112_case_fixture_counts(c):
    import json
    cases = json.loads(CASES.read_text())
    assert len(cases) == 112
    store = c.store
    for n, case in enumerate(cases):
        job = store.create_job("goal", "/tmp/nowhere")
        rows = [(HOLD, Judgment.code, "t", {"reasons": case["reasons"]})]
        for r in case["resolution"]:
            rows.append((r["what"], Judgment(r["judgment"]), r["rule"], r["ev"]))
        by = case["sent_back_by"]
        if by:
            rows.append(("sent_back", Judgment.code if by.startswith("tech_lead") else Judgment.human,
                         "tech_lead_send_back" if by.startswith("tech_lead") else "operator_send_back", {"by": by}))
        for i, (what, j, rule, ev) in enumerate(rows):
            store.append_event(job.job_id, what=what, evidence=ev, rule=rule, judgment=j)
            with store._conn:
                store._conn.execute("DROP TRIGGER events_no_update")
                store._conn.execute("UPDATE events SET ts=? WHERE seq=(SELECT MAX(seq) FROM events)",
                                    (f"2026-10-02T11:{n // 60:02d}:{n % 60:02d}.{i:03d}000+00:00",))
                store._conn.execute("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN "
                                    "SELECT RAISE(ABORT, 'events are append-only'); END;")
    # the c.job from the fixture holds no events; only the 112 synthetic jobs count
    assert f6_resolver(store) == {"TL": 26, "agent": 11, "human": 75}
