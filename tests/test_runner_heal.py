"""Control-side runner heal: recheck right before launch, duplicates are no-ops, a missing pane is recreated, the
worker is never touched (issue W, spec §4)."""
from steward import cli, launch, proclock
from steward.models import Judgment, JobState
from steward.store import Store

from herdr_fake import FakeHerdr


def _set(store, job_id, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, job_id))


def _running(store, tmp_path, *, runner_pane="w4Q:p2", worker_pane="w4Q:p3"):
    job = store.create_job("goal", str(tmp_path))
    _set(store, job.job_id, JobState.RUNNING)
    store.kv_set(job.job_id, "runner_pane", runner_pane)
    store.kv_set(job.job_id, "herdr_pane", worker_pane)
    store.kv_set(job.job_id, "herdr_tab", "w4Q:t2")
    store.kv_set(job.job_id, "launch_args", {"worker": "fabric", "interval": 15.0})
    return store.get_job(job.job_id)


def _herdr(job, *, runner_pane=True):
    panes = [{"pane_id": "w4Q:p3", "tab_id": "w4Q:t2", "label": "worker"}]
    if runner_pane:
        panes.insert(0, {"pane_id": "w4Q:p2", "tab_id": "w4Q:t2", "label": f"runner {job.job_id[:8]}"})
    return FakeHerdr([{"tab_id": "w4Q:t2", "label": "job"}], panes)


def _runs(h):
    return [c for c in h.calls if c[:3] == ["herdr", "pane", "run"]]


def test_alive_runner_is_a_noop(tmp_path):
    store = Store(tmp_path / "home")
    job = _running(store, tmp_path)
    h = _herdr(job)
    held = proclock.acquire(proclock.runner_lock(store.home, job.job_id))
    try:
        r = launch.heal_runner(h, store, job)
    finally:
        held.release()
    assert r["accepted"] and r["noop"] and _runs(h) == []


def test_dead_runner_restarts_in_its_pane_once(tmp_path):
    store = Store(tmp_path / "home")
    job = _running(store, tmp_path)
    h = _herdr(job)
    r = launch.heal_runner(h, store, job)
    assert r["accepted"] and not r.get("noop") and r["pane"] == "w4Q:p2"
    (run,) = _runs(h)
    assert run[3] == "w4Q:p2" and f"steward.cli run {job.job_id}" in run[4]
    # a duplicate request inside the start grace is a no-op: never a second `steward run`
    again = launch.heal_runner(h, store, job)
    assert again["noop"] and len(_runs(h)) == 1
    assert store.kv_get(job.job_id, "herdr_pane") == "w4Q:p3"  # worker untouched


def test_missing_runner_pane_gets_a_new_tab(tmp_path):
    store = Store(tmp_path / "home")
    job = _running(store, tmp_path)
    h = _herdr(job, runner_pane=False)
    r = launch.heal_runner(h, store, job)
    assert r["accepted"] and r["created_pane"]
    assert any(c[:3] == ["herdr", "tab", "create"] for c in h.calls)
    new = store.kv_get(job.job_id, "runner_pane")
    assert new != "w4Q:p2" and r["pane"] == new
    (run,) = [c for c in _runs(h) if "steward.cli run" in c[4]]
    assert run[3] == new
    assert store.kv_get(job.job_id, "herdr_pane") == "w4Q:p3"
    assert not any(c[:3] in (["herdr", "pane", "close"], ["herdr", "pane", "send-keys"]) and "w4Q:p3" in c for c in h.calls)


def test_paused_job_is_not_restored(tmp_path):
    store = Store(tmp_path / "home")
    job = _running(store, tmp_path)
    _set(store, job.job_id, JobState.PAUSED)
    h = _herdr(job)
    r = launch.heal_runner(h, store, store.get_job(job.job_id))
    assert not r["accepted"] and _runs(h) == []


def test_deliberate_restart_refuses_when_the_old_runner_does_not_stop(tmp_path):
    store = Store(tmp_path / "home")
    job = _running(store, tmp_path)
    h = _herdr(job)
    held = proclock.acquire(proclock.runner_lock(store.home, job.job_id))
    try:
        r = launch.restart_runner(h, store, job, sleep=lambda s: None, settle_s=0)
    finally:
        held.release()
    assert not r["accepted"] and "not starting a second" in r["detail"]
    assert _runs(h) == []


def test_handler_heal_mode_is_code_judgment(tmp_path, monkeypatch):
    store = Store(tmp_path / "home")
    job = _running(store, tmp_path)
    h = _herdr(job)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: h)
    handlers = cli._build_control_handlers(store)
    r = handlers["runner_restart"]({"args": {"job_id": job.job_id, "mode": "heal"}, "origin": "watchdog",
                                    "request_id": "x"})
    assert r["accepted"], r
    ev = [e for e in store.events(job.job_id) if e.what == "runner_restarted"][-1]
    assert ev.judgment == Judgment.code and ev.evidence["mode"] == "heal"
    dup = handlers["runner_restart"]({"args": {"job_id": job.job_id, "mode": "heal"}, "origin": "watchdog",
                                      "request_id": "y"})
    assert dup["accepted"] and dup["noop"]
    assert len([e for e in store.events(job.job_id) if e.what == "runner_restarted"]) == 1
