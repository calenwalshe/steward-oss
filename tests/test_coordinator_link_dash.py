import time

from steward import coordinator_link as cl
from steward.models import Judgment
from steward.store import Store


def _episode(store, jid, outcome="resolved", diagnosis="10 ids env-dependent", actions=("approve", "steer")):
    store.append_event(jid, what="coordinator_episode", rule="coordinator", judgment=Judgment.code,
                       evidence={"episode_id": "e", "trigger": "t", "outcome": outcome, "diagnosis": diagnosis,
                                 "actions": list(actions)})


def test_empty_when_nothing(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    assert cl.dash_line(store, j.job_id) == "" and cl.dash_line(store, "nope") == ""


def test_open_hold_line(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    cl.open_hold(store, j.job_id, episode_id="e", trigger="t", now=1000.0)
    line = cl.dash_line(store, j.job_id, now=1000.0 + 300)
    assert line.startswith("⚙ coordinator: working on it (5m; tells you by ") and line.endswith("if unsettled)")


def test_latest_episode_line_and_24h_window(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    _episode(store, j.job_id, diagnosis="x" * 200)
    _episode(store, j.job_id)
    assert cl.dash_line(store, j.job_id) == "⚙ coordinator: resolved — 10 ids env-dependent (approve, steer)"
    assert cl.dash_line(store, j.job_id, now=time.time() + 90000) == ""


def test_ask_not_delivered_suffix(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    cl.open_hold(store, j.job_id, episode_id="e", trigger="t", now=1.0)
    cl.close_hold(store, j.job_id, "asked", now=2.0, delivery="skipped:pane_gone")
    _episode(store, j.job_id, "asked")
    assert cl.dash_line(store, j.job_id).endswith("· ask not delivered (skipped:pane_gone)")
    cl.open_hold(store, j.job_id, episode_id="e2", trigger="t", now=1.0)
    cl.close_hold(store, j.job_id, "asked", now=2.0, delivery="delivered")
    assert "not delivered" not in cl.dash_line(store, j.job_id)


def test_close_hold_is_noop_without_open_hold(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    cl.close_hold(store, j.job_id, "resolved", now=1.0)
    assert store.kv_get(j.job_id, "coordinator_hold") is None and cl.hold(store, j.job_id) is None
