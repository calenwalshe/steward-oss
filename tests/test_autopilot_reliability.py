"""Autopilot reliability: deploy-self retries while a permission answer is in flight; the closeout is idempotent."""

from types import SimpleNamespace

from steward import autopilot
from steward.models import JobState
from steward.runner import Runner
from steward.runner_exec import FakeRunner
from steward.store import Store

REFUSED = {"ok": False, "refused": True, "detail": "a permission answer is still in flight after 5s; try again shortly",
           "summary": "deploy-self refused: a permission answer is still in flight after 5s; try again shortly"}
OK = {"ok": True, "summary": "deploy-self: restarted steward-mcp; health ok", "done": ["steward-mcp"], "health": "ok"}


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _job(store, tmp_path, state=JobState.ACCEPTED):
    job = store.create_job("Fix a thing", str(tmp_path))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))
    return job


def _flag(store, job, deploy, clock, pushes, monkeypatch):
    monkeypatch.setenv("STEWARD_AUTODEPLOY", "1")
    monkeypatch.setattr(autopilot, "_push", lambda s, j, ev, r, **facts: pushes.append((ev, facts.get("reason", ""))))
    autopilot._deploy_or_flag(store, job, "abc1234", FakeRunner(lambda a, c: None), deploy, job.repo,
                              sleep=clock.sleep, clock=clock.now)


def test_deploy_retries_every_30s_until_it_succeeds_and_pushes_one_line(steward_home, tmp_path, monkeypatch):
    store, clock, pushes = Store(steward_home), Clock(), []
    job = _job(store, tmp_path)
    results = [REFUSED, REFUSED, OK]
    _flag(store, job, lambda s, root: results.pop(0), clock, pushes, monkeypatch)
    assert clock.sleeps == [30.0, 30.0] and not results
    assert [p[0] for p in pushes] == ["auto_deployed"]
    assert "auto_deployed" in [e.what for e in store.events(job.job_id)]


def test_deploy_gives_up_after_ten_minutes_with_one_push(steward_home, tmp_path, monkeypatch):
    store, clock, pushes, calls = Store(steward_home), Clock(), [], []

    def deploy(s, root):
        calls.append(1)
        return REFUSED
    job = _job(store, tmp_path)
    _flag(store, job, deploy, clock, pushes, monkeypatch)
    assert clock.t <= 600 and len(calls) == 21
    assert [p[0] for p in pushes] == ["restart_needed"] and "still in flight" in pushes[0][1]
    assert "auto_deploy_failed" in [e.what for e in store.events(job.job_id)]


def test_other_refusals_are_not_retried(steward_home, tmp_path, monkeypatch):
    store, clock, pushes = Store(steward_home), Clock(), []
    job = _job(store, tmp_path)
    _flag(store, job, lambda s, root: {**REFUSED, "detail": "the new code does not import", "summary": "x"},
          clock, pushes, monkeypatch)
    assert clock.sleeps == [] and len(pushes) == 1


def _runner(store, ticks):
    r = Runner(store, lambda ref: None, None, None)
    r.tick = lambda j: ticks.append(j) or SimpleNamespace(job_id=j, state="running", cls=None, action=None, detail="")
    r._alerts = lambda j: None
    r._sleep_between_ticks = lambda j, i, s: None
    return r


def test_accepted_job_gets_exactly_one_autopilot_attempt_and_runner_exits(steward_home, tmp_path, monkeypatch):
    store, ticks, merges, restarts = Store(steward_home), [], [], []
    job = _job(store, tmp_path)
    monkeypatch.setattr(autopilot, "try_merge", lambda s, j, r, **kw: merges.append(j) or {"outcome": "merged"})
    for _ in range(3):  # three runner starts / self-restarts onto the same ACCEPTED job
        rep = _runner(store, ticks).run_forever(job.job_id, self_restart=restarts.append)
        assert rep.state == "ACCEPTED" or rep.state == JobState.ACCEPTED.value
    assert merges == [job.job_id] and not ticks and not restarts
    assert "autopilot_attempt" in [e.what for e in store.events(job.job_id)]


def test_runner_exits_without_restart_when_job_turns_terminal_mid_loop(steward_home, tmp_path, monkeypatch):
    store, ticks, restarts = Store(steward_home), [], []
    job = _job(store, tmp_path, JobState.RUNNING)
    monkeypatch.setattr(autopilot, "try_merge", lambda *a, **k: {"outcome": "skipped"})
    r = _runner(store, ticks)

    def tick(j):  # the job is accepted elsewhere while the runner idles
        ticks.append(j)
        with store._conn:
            store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.ACCEPTED.value, j))
        return SimpleNamespace(job_id=j, state="running", cls=None, action=None, detail="")
    r.tick = tick
    rep = r.run_forever(job.job_id, self_restart=restarts.append)
    assert len(ticks) == 1 and not restarts and rep.state == JobState.ACCEPTED.value


def test_incorporated_or_non_accepted_jobs_are_not_attempted(steward_home, tmp_path, monkeypatch):
    store, merges = Store(steward_home), []
    monkeypatch.setattr(autopilot, "try_merge", lambda s, j, r, **kw: merges.append(j))
    done, stopped = _job(store, tmp_path), _job(store, tmp_path, JobState.STOPPED)
    store.kv_set(done.job_id, "incorporated_commit", "abc")
    for j in (done, stopped):
        assert autopilot.ensure_attempted(store, j.job_id, FakeRunner(lambda a, c: None)) is None
    assert merges == []


def _floor_approval(store, job, decision, relayed):
    from steward import approvals

    a = approvals.open_approval(store, job.job_id, "Bash: cat <<EOF git push EOF", kind="permission")
    store.append_event(job.job_id, what="risk_classified", evidence={"approval_id": a["id"], "tier": "floor"},
                       rule="resolver_classified", judgment=autopilot.Judgment.code)
    if decision:
        approvals.decide(store, job.job_id, a["id"], decision, decided_by="resolver:test")
    if relayed:
        approvals.mark_relayed(store, job.job_id, a["id"])


def test_floor_reason_ignores_denied_stale_and_held_floor_approvals(steward_home, tmp_path):
    store = Store(steward_home)
    for decision, relayed in (("deny", True), (None, False)):  # denied; classified but never decided (held)
        job = _job(store, tmp_path)
        _floor_approval(store, job, decision, relayed)
        assert autopilot.floor_reason(store, job.job_id) is None
    job = _job(store, tmp_path)  # approved but the answer never relayed: not executed
    _floor_approval(store, job, "approve", False)
    assert autopilot.floor_reason(store, job.job_id) is None


def test_floor_reason_counts_approved_and_executed_floor_action(steward_home, tmp_path):
    store = Store(steward_home)
    job = _job(store, tmp_path)
    _floor_approval(store, job, "approve", True)
    assert autopilot.floor_reason(store, job.job_id) == "job did a floor action"
