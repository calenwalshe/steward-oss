from steward import zero_touch_metrics as m
from steward.models import Judgment
from steward.store import Store


def _ev(store, jid, what, judgment=Judgment.human, rule="t", **ev):
    store.append_event(jid, what=what, evidence=ev, rule=rule, judgment=judgment)


def test_coordinator_events_are_not_touches(steward_home):
    store = Store()
    j = store.create_job("g", "/tmp/r")
    _ev(store, j.job_id, "incorporated", Judgment.code, merge_commit="abc")
    _ev(store, j.job_id, "nudge instruction recorded", actor="operator_agent:coordinator", urgent=True)
    _ev(store, j.job_id, "operator stop")
    _ev(store, j.job_id, "coordinator_episode", Judgment.code, outcome="asked", episode_id="e")
    _ev(store, j.job_id, "coordinator_episode", Judgment.code, outcome="gave_up", episode_id="f")
    _ev(store, j.job_id, "coordinator_episode", Judgment.code, outcome="resolved", episode_id="g")
    out = m.compute(store, since_iso="2000-01-01T00:00:00+00:00")
    assert out["coordinator_actions"] == 1 and out["asks"] == 2
    assert out["touches"] == 1  # the operator stop only


def test_non_touch_coordinator_event_not_counted_and_owner_nudge_still_touch(steward_home):
    store = Store()
    j = store.create_job("g", "/tmp/r")
    _ev(store, j.job_id, "nudge instruction recorded", actor="operator_agent:coordinator", urgent=False)
    _ev(store, j.job_id, "nudge instruction recorded", urgent=True)
    out = m.compute(store, since_iso="2000-01-01T00:00:00+00:00")
    assert out["coordinator_actions"] == 0 and out["touches"] == 1
    assert "coordinator 0 actions, 0 asks" in m.render(out)
