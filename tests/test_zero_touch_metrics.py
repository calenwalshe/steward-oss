import json, subprocess
from steward import zero_touch_metrics as m
from steward.models import JobState, Judgment
from steward.store import Store


def _job(store, state=JobState.RELEASED):
    j = store.create_job("goal", "/tmp/nowhere")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, j.job_id))
    return j


def _ev(store, jid, what, judgment=Judgment.human, **ev):
    store.append_event(jid, what=what, evidence=ev, rule="t", judgment=judgment)


def test_touches_count_mechanical_only(steward_home):
    store = Store()
    j = _job(store)
    _ev(store, j.job_id, "incorporated", Judgment.code, merge_commit="abc")
    _ev(store, j.job_id, "operator released")
    _ev(store, j.job_id, "runner_restarted", origin="cli")
    _ev(store, j.job_id, "runner_restarted", origin="watchdog")      # not a touch
    _ev(store, j.job_id, "question_answered")                       # a decision, not a touch
    _ev(store, j.job_id, "approval denied", kind="delivery")        # stale cleanup = touch
    _ev(store, j.job_id, "approval approved", kind="acceptance")    # product call = decision
    out = m.compute(store, since_iso="2000-01-01T00:00:00+00:00")
    assert out["touches"] == 3 and out["decisions"] == 2 and out["incorporated_jobs"] == 1
    assert out["touches_per_job"] == 3.0


def test_orphan_hours_from_liveness_events(steward_home):
    store = Store()
    j = _job(store, JobState.RUNNING)
    _ev(store, j.job_id, "liveness_orphan", Judgment.code, state="RUNNING", since="2026-10-08T00:00:00+00:00")
    _ev(store, j.job_id, "liveness_actor", Judgment.code, at="2026-10-08T02:30:00+00:00")
    out = m.compute(store, since_iso="2000-01-01T00:00:00+00:00")
    assert out["orphan"]["total_h"] == 2.5 and out["orphan"]["max_h"] == 2.5


def test_landing_ratio_from_git_subjects(steward_home, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    for s in ("Land Steward job x (train 1)", "Manual merge Steward job y", "Incorporate Steward job z: goal"):
        (repo / "f").write_text(s); g("add", "f"); g("commit", "-qm", s)
    store = Store()
    out = m.compute(store, since_iso="2000-01-01T00:00:00+00:00", targets=[(str(repo), "main")])
    assert out["landings"] == {"auto": 2, "manual": 1, "ratio": 2 / 3}
