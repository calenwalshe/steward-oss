import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from steward.coordinator import db, episodes, triggers
from steward.coordinator.config import Settings
from steward.models import JobState, Judgment
from steward.store import Store

S = Settings()
ALIVE = lambda _j: True  # noqa: E731
DEAD = lambda _j: False  # noqa: E731


def _ro(home):
    return triggers.RoStore(home / "steward.db", home)


def _cdb(tmp_path):
    return db.CoordDB(tmp_path / "c.db")


def _to(store, job, state, what, rule):
    store.set_state(job.job_id, state, what=what, evidence={}, rule=rule, judgment=Judgment.code)


def _scan(ro, cdb, **kw):
    kw.setdefault("now", time.time())
    kw.setdefault("health", None)
    kw.setdefault("runner_alive", ALIVE)
    return triggers.scan(ro, cdb, settings=S, **kw)


def _park(store, rule="recovery_budget_exhausted", what="recovery escalated to human"):
    j = store.create_job("g", "/tmp/r")
    _to(store, j, JobState.CONTRACTED, "contracted", "contract")
    _to(store, j, JobState.RUNNING, "launched", "launch")
    return j


def test_ro_store_is_read_only(steward_home):
    Store().create_job("g", "/tmp/r")
    ro = _ro(steward_home)
    with pytest.raises(sqlite3.OperationalError):
        ro._conn.execute("INSERT INTO kv (job_id, key, value_json) VALUES ('x','y','1')")


def test_ro_store_reads_and_duck_types(steward_home):
    store = Store()
    j = store.create_job("g", "/tmp/r")
    _to(store, j, JobState.CONTRACTED, "contracted", "contract")
    _to(store, j, JobState.RUNNING, "launched", "launch")
    store.kv_set(j.job_id, "k", {"a": 1})
    store.append_event(j.job_id, what="x", evidence={"e": 1}, rule="r", judgment=Judgment.code)
    ro = _ro(steward_home)
    assert ro.home == steward_home
    assert ro.get_job(j.job_id).state is JobState.RUNNING
    assert ro.get_job("nope") is None
    assert [x.job_id for x in ro.list_jobs({JobState.RUNNING})] == [j.job_id]
    assert ro.list_jobs({JobState.STOPPED}) == []
    assert ro.kv_get(j.job_id, "k") == {"a": 1} and ro.kv(j.job_id, "zz", 5) == 5
    assert ro.kv_rows("k") == [(j.job_id, {"a": 1})]
    assert [e.what for e in ro.events_of(j.job_id, ["x"])] == ["x"]
    assert ro.events_of(j.job_id, [], rules=["r"])[0].evidence == {"e": 1}
    assert ro.max_seq() == ro.last_event_seq(j.job_id) == len(ro.events(j.job_id))
    assert ro.approvals(j.job_id) == []
    assert ro.checks(j.job_id, "l") == []


def test_recovery_park_triggers_needs_human(steward_home, tmp_path):
    store = Store(); j = _park(store); cdb = _cdb(tmp_path)
    ro = _ro(steward_home)
    cdb.cursor_set("events", 0)
    _to(store, j, JobState.NEEDS_HUMAN, "recovery escalated to human", "recovery_budget_exhausted")
    trigs, cur = _scan(ro, cdb)
    assert [t.kind for t in trigs] == ["needs_human:recovery_budget_exhausted"]
    assert trigs[0].job_id == j.job_id and trigs[0].detail["what"] == "recovery escalated to human"
    assert {"rule", "evidence"} <= set(trigs[0].detail)
    assert cur == ro.max_seq()
    cdb.cursor_set("events", cur)
    assert _scan(ro, cdb)[0] == []


def test_acceptance_unresolved_ignored(steward_home, tmp_path):
    store = Store(); j = _park(store); cdb = _cdb(tmp_path)
    cdb.cursor_set("events", 0)
    _to(store, j, JobState.NEEDS_HUMAN, "acceptance unresolved", "acceptance_unresolved")
    assert _scan(_ro(steward_home), cdb)[0] == []


def test_park_requires_needs_human_now(steward_home, tmp_path):
    store = Store(); j = _park(store); cdb = _cdb(tmp_path)
    cdb.cursor_set("events", 0)
    _to(store, j, JobState.NEEDS_HUMAN, "recovery escalated to human", "recovery_budget_exhausted")
    _to(store, j, JobState.RUNNING, "resumed", "resume")
    assert _scan(_ro(steward_home), cdb)[0] == []


def test_one_trigger_per_job_per_scan(steward_home, tmp_path):
    store = Store(); j = _park(store); cdb = _cdb(tmp_path)
    cdb.cursor_set("events", 0)
    _to(store, j, JobState.NEEDS_HUMAN, "recovery escalated to human", "recovery_budget_exhausted")
    store.append_event(j.job_id, what="liveness_ask", evidence={}, rule="liveness", judgment=Judgment.code)
    trigs, _ = _scan(_ro(steward_home), cdb)
    assert len(trigs) == 1 and trigs[0].kind == "liveness_ask"


def test_first_run_cursor_is_max_seq_no_replay(steward_home, tmp_path):
    store = Store(); j = _park(store); cdb = _cdb(tmp_path)
    _to(store, j, JobState.NEEDS_HUMAN, "recovery escalated to human", "recovery_budget_exhausted")
    ro = _ro(steward_home)
    trigs, cur = _scan(ro, cdb)
    assert trigs == [] and cur == ro.max_seq()
    cdb.cursor_set("events", cur)
    store.append_event(j.job_id, what="liveness_ask", evidence={}, rule="liveness", judgment=Judgment.code)
    trigs, cur2 = _scan(ro, cdb)
    assert [t.kind for t in trigs] == ["liveness_ask"] and cur2 == ro.max_seq() > cur


def test_liveness_orphan_flips_zt_p3(steward_home, tmp_path):
    store = Store(); j = _park(store); cdb = _cdb(tmp_path)
    cdb.cursor_set("events", 0)
    store.append_event(j.job_id, what="liveness_orphan", evidence={}, rule="liveness", judgment=Judgment.code)
    _scan(_ro(steward_home), cdb)
    assert cdb.cursor_get("zt_p3", 0) == 1


def test_post_p3_orphan_without_actor(steward_home, tmp_path):
    store = Store(); j = _park(store); cdb = _cdb(tmp_path)
    cdb.cursor_set("events", 0)
    store.append_event(j.job_id, what="liveness_orphan", evidence={}, rule="liveness", judgment=Judgment.code)
    ro = _ro(steward_home)
    assert not any(t.kind.startswith("orphan") for t in _scan(ro, cdb)[0])
    trigs, _ = _scan(ro, cdb, now=time.time() + 4000)
    assert [t.kind for t in trigs] == ["orphan:RUNNING"]
    store.append_event(j.job_id, what="liveness_actor", evidence={}, rule="liveness", judgment=Judgment.code)
    assert _scan(ro, cdb, now=time.time() + 4000)[0] == []


def test_liveness_ask_trigger(steward_home, tmp_path):
    store = Store(); j = _park(store); cdb = _cdb(tmp_path)
    cdb.cursor_set("events", 0)
    store.append_event(j.job_id, what="liveness_ask", evidence={"blocker_id": "b"}, rule="liveness",
                       judgment=Judgment.code)
    trigs, _ = _scan(_ro(steward_home), cdb)
    assert [(t.kind, t.job_id) for t in trigs] == [("liveness_ask", j.job_id)]


def test_health_dict_shape_control_red(steward_home, tmp_path):
    Store(); cdb = _cdb(tmp_path); now = time.time()
    h = {"components": {"control": {"ok": False, "red_since": now - 300}, "mcp": {"ok": False, "red_since": now - 900},
                        "x": {"ok": True}}}
    trigs, _ = _scan(_ro(steward_home), cdb, now=now, health=h)
    assert [(t.kind, t.job_id) for t in trigs] == [("health:control", None)]
    h["components"]["control"]["red_since"] = now - 30
    assert _scan(_ro(steward_home), cdb, now=now, health=h)[0] == []


def test_health_list_shape_uses_cursor_red_since_and_iso(steward_home, tmp_path):
    Store(); cdb = _cdb(tmp_path); ro = _ro(steward_home); now = 1_000_000.0
    h = {"components": [{"name": "control", "ok": False, "detail": "d", "problems": []}]}
    assert _scan(ro, cdb, now=now, health=h)[0] == []
    assert cdb.cursor_get("red_since:control", None) == now
    assert [t.kind for t in _scan(ro, cdb, now=now + 130, health=h)[0]] == ["health:control"]
    h["components"][0]["ok"] = True
    _scan(ro, cdb, now=now + 140, health=h)
    assert cdb.cursor_get("red_since:control", None) in (None, 0, "")
    iso = datetime.fromtimestamp(now, timezone.utc).isoformat()
    h2 = {"components": [{"name": "control", "ok": False, "red_since": iso}]}
    assert [t.kind for t in _scan(ro, cdb, now=now + 500, health=h2)[0]] == ["health:control"]


def test_health_runners_problem_orphans_job(steward_home, tmp_path):
    store = Store(); j = _park(store); cdb = _cdb(tmp_path); now = time.time()
    pb = f"runner {j.job_id[:8]}: runner process 123 is gone"
    h = {"components": {"runners": {"ok": False, "red_since": now - 500, "problems": [pb, "runner deadbeef: runner process 9 is gone"]}}}
    trigs, _ = _scan(_ro(steward_home), cdb, now=now, health=h)
    assert [(t.kind, t.job_id) for t in trigs] == [("orphan:RUNNING", j.job_id)]
    assert pb in trigs[0].detail["problem"]


def test_pre_p3_orphan_needs_dead_runner_and_age(steward_home, tmp_path):
    store = Store(); j = _park(store); ro = _ro(steward_home)
    now = time.time() + 4000
    assert any(t.kind == "orphan:RUNNING" for t in _scan(ro, _cdb(tmp_path / "a"), now=now, runner_alive=DEAD)[0])
    assert not any(t.kind.startswith("orphan") for t in _scan(ro, _cdb(tmp_path / "b"), now=now, runner_alive=ALIVE)[0])
    assert _scan(ro, _cdb(tmp_path / "c"), now=time.time(), runner_alive=DEAD)[0] == []
    store.kv_set(j.job_id, "bg_verify", {"status": "running"})
    assert _scan(ro, _cdb(tmp_path / "d"), now=now, runner_alive=DEAD)[0] == []
    store.kv_set(j.job_id, "bg_verify", {"status": "done"})
    cdb = _cdb(tmp_path / "e")
    assert _scan(ro, cdb, now=now, runner_alive=DEAD)[0]
    assert _scan(ro, cdb, now=now + 10, runner_alive=DEAD)[0] == []  # throttled by orphan_scan_s


def test_pre_p3_orphan_excludes_other_states(steward_home, tmp_path):
    store = Store(); j = _park(store)
    _to(store, j, JobState.NEEDS_HUMAN, "x", "y")
    assert _scan(_ro(steward_home), _cdb(tmp_path), now=time.time() + 4000, runner_alive=DEAD)[0] == []


def test_last_non_noise_event_ts_iso(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    ro = _ro(steward_home)
    assert ro.last_non_noise_event_ts(j.job_id) is None
    _to(store, j, JobState.CONTRACTED, "contracted", "contract")
    _to(store, j, JobState.RUNNING, "launched", "launch")
    real = ro.last_non_noise_event_ts(j.job_id)
    assert abs(real - time.time()) < 60
    for w in ("heartbeat_x", "jev_call", "runner_self_restart", "ownership_status", "efficiency_x",
              "tech_lead_review_deferred"):
        store.append_event(j.job_id, what=w, evidence={}, rule="r", judgment=Judgment.code)
    assert ro.last_non_noise_event_ts(j.job_id) == real
    assert real == datetime.fromisoformat(ro.events(j.job_id)[-7].ts).timestamp()


def test_retrigger_attaches_then_cooldown_then_ask_only(tmp_path):
    cdb = _cdb(tmp_path); now = 10_000.0
    t = triggers.Trigger("needs_human:recovery_budget_exhausted", "j1", 5, {})
    eid, how = episodes.open_or_attach(cdb, t, now=now, settings=S)
    assert how == "opened" and not episodes.is_ask_only(cdb.open_for("j1"))
    assert episodes.open_or_attach(cdb, triggers.Trigger(t.kind, "j1", 6, {}), now=now + 1, settings=S) == (eid, "attached")
    assert cdb.open_for("j1")["last_seq"] == 6
    cdb.close(eid, "resolved", "d", [], now=now + 2)
    # a park during the cooldown still owes the owner a line: it opens ask-only (final-review fix 2)
    ec, how = episodes.open_or_attach(cdb, triggers.Trigger(t.kind, "j1", 7, {}), now=now + 3, settings=S)
    assert how == "ask_only" and ec and episodes.ask_only_why(cdb.open_for("j1")) == "cooldown"
    assert cdb.open_for("j1")["reopened"] == 0
    cdb.close(ec, "asked", "d", [], now=now + 4)
    # other kinds (orphan, health) are still dropped during the cooldown
    assert episodes.open_or_attach(cdb, triggers.Trigger("orphan:RUNNING", "j1", 0, {}), now=now + 5,
                                   settings=S) == (None, "cooldown")
    # a cooldown ask-only episode neither restarts the cooldown nor counts toward the daily cap or the reopen check
    e2, how = episodes.open_or_attach(cdb, triggers.Trigger(t.kind, "j1", 8, {}), now=now + 2000, settings=S)
    assert how == "opened"
    assert cdb.open_for("j1")["reopened"] == 0
    cdb.close(e2, "resolved", "d", [], now=now + 2001)
    e3, how = episodes.open_or_attach(cdb, triggers.Trigger(t.kind, "j1", 9, {}), now=now + 4000, settings=S)
    assert how == "opened"
    cdb.close(e3, "asked", "d", [], now=now + 4001)
    e4, how = episodes.open_or_attach(cdb, triggers.Trigger(t.kind, "j1", 10, {}), now=now + 6000, settings=S)
    assert how == "ask_only" and e4
    ep = cdb.open_for("j1")
    assert episodes.is_ask_only(ep) and ep["reopened"] == 1 and episodes.ask_only_why(ep) == "cap"


def test_reopened_after_overturn_and_not_after_old(tmp_path):
    cdb = _cdb(tmp_path); now = 10_000.0
    t = triggers.Trigger("needs_human:x", "j2", 1, {})
    e, _ = episodes.open_or_attach(cdb, t, now=now, settings=S)
    cdb.close(e, "resolved", "d", [], now=now + 1); cdb.overturn(e, "wrong", now + 2)
    e2, how = episodes.open_or_attach(cdb, t, now=now + 2000, settings=S)
    assert how == "opened" and cdb.open_for("j2")["reopened"] == 1
    cdb.close(e2, "asked", "d", [], now=now + 2001)
    e3, how = episodes.open_or_attach(cdb, t, now=now + 2001 + 90_000, settings=S)
    assert how == "opened" and cdb.open_for("j2")["reopened"] == 0


def test_jobless_trigger_keys_on_health_component(tmp_path):
    cdb = _cdb(tmp_path)
    t = triggers.Trigger("health:control", None, 0, {})
    eid, how = episodes.open_or_attach(cdb, t, now=100.0, settings=S)
    assert how == "opened" and cdb.open_for("health:control")["episode_id"] == eid
    assert episodes.open_or_attach(cdb, t, now=101.0, settings=S) == (eid, "attached")


def test_trajectory_events_do_not_reset_orphan(steward_home):
    store = Store(); j = store.create_job("g", "/tmp/r")
    ro = _ro(steward_home)
    _to(store, j, JobState.CONTRACTED, "contracted", "contract")
    _to(store, j, JobState.RUNNING, "launched", "launch")
    real = ro.last_non_noise_event_ts(j.job_id)
    for w in ("trajectory_review_requested", "trajectory_concern_recorded", "tech_lead_idle_paused"):
        store.append_event(j.job_id, what=w, evidence={}, rule="r", judgment=Judgment.code)
    assert ro.last_non_noise_event_ts(j.job_id) == real
