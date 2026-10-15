"""Plan 3 Task 9: precise waits use code-only probes and backoff; wakes return blockers to review."""
from __future__ import annotations

import pytest

from ownership_fixtures import CONTROLLER, Clock, park_rule
from steward import approvals as approvals_module
from steward import blockers, waits
from steward.models import JobState, Judgment
from steward.store import Store


def _blocked(store):
    jid = park_rule(store, "recovery_budget_exhausted", "recovery escalated to human")
    b, _ = blockers.raise_blocker(store, jid, kind="recovery_exhausted", subject="worker", claim="worker keeps dying",
                                 evidence=["event:1"], origin_actor=CONTROLLER, now=1.0)
    return jid, b


def test_park_requires_a_supported_precise_condition(steward_home):
    store = Store(steward_home)
    jid, b = _blocked(store)
    with pytest.raises(waits.WaitError, match="unknown wait kind"):
        waits.park(store, jid, blocker_id=b.blocker_id, kind="someday", params={}, reason="r", now=1.0)
    with pytest.raises(waits.WaitError, match="inside the job's worktree"):
        waits.park(store, jid, blocker_id=b.blocker_id, kind="file_exists", params={"path": "../../etc/passwd"},
                   reason="r", now=1.0)


def test_parked_blocker_waits_externally_and_shows_its_condition(steward_home):
    store = Store(steward_home)
    jid, b = _blocked(store)
    w = waits.park(store, jid, blocker_id=b.blocker_id, kind="file_exists", params={"path": "out/report.json"},
                   reason="the vendor drops the report there", now=1.0)
    assert blockers.get(store, b.blocker_id).disposition == "waiting_external"
    assert blockers.get(store, b.blocker_id).wake["kind"] == "file_exists"
    assert waits.park(store, jid, blocker_id=b.blocker_id, kind="file_exists", params={"path": "out/report.json"},
                      reason="dup", now=2.0)["wait_id"] == w["wait_id"]


def test_probes_back_off_and_wake_returns_the_blocker_to_review(steward_home, monkeypatch):
    store = Store(steward_home)
    jid, b = _blocked(store)
    facts = []
    monkeypatch.setattr("steward.ownership.submit_review_fact", lambda store, blk, *, reason: facts.append(reason))
    clock = Clock(1000.0)
    waits.park(store, jid, blocker_id=b.blocker_id, kind="file_exists", params={"path": "out/report.json"},
               reason="r", now=clock())
    assert waits.sweep(store, now=clock.advance(30))["probed"] == 0
    assert waits.sweep(store, now=clock.advance(31))["probed"] == 1
    (row,) = waits.waiting(store, jid)
    assert row["next_probe_epoch"] == pytest.approx(clock() + 120)
    target = store.home / "worktrees" / jid / "out"
    target.mkdir(parents=True)
    (target / "report.json").write_text("{}")
    out = waits.sweep(store, now=clock.advance(121))
    assert out["woken"] == 1 and waits.waiting(store, jid) == []
    assert blockers.get(store, b.blocker_id).disposition == "candidate" and facts


def test_backoff_caps_at_an_hour_and_never_calls_a_model(steward_home, monkeypatch):
    store = Store(steward_home)
    jid, b = _blocked(store)
    monkeypatch.setattr("steward.fleet_tech_lead.invoke_via_runtime",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called")))
    clock = Clock(0.0)
    waits.park(store, jid, blocker_id=b.blocker_id, kind="time", params={"at": 10 ** 9}, reason="r", now=0.0)
    for _ in range(12):
        clock.advance(4000)
        waits.sweep(store, now=clock())
    (row,) = waits.waiting(store, jid)
    assert row["next_probe_epoch"] - clock() <= waits.MAX_PROBE_S


@pytest.mark.parametrize("kind,params,make_true", [
    ("job_state", {"states": ["RUNNING"]},
     lambda store, jid: store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t",
                                        judgment=Judgment.human)),
    ("time", {"at": 5000.0}, lambda store, jid: None),
    ("evidence_change", {"events": ["checks passed at exact commit"]},
     lambda store, jid: store.append_event(jid, what="checks passed at exact commit", evidence={}, rule="t",
                                           judgment=Judgment.code)),
])
def test_each_probe_kind_wakes_on_its_condition(steward_home, kind, params, make_true):
    store = Store(steward_home)
    jid, b = _blocked(store)
    waits.park(store, jid, blocker_id=b.blocker_id, kind=kind, params=params, reason="r", now=1000.0)
    assert waits.sweep(store, now=1100.0)["woken"] == (1 if kind == "time" and 1100.0 >= 5000.0 else 0)
    make_true(store, jid)
    assert waits.sweep(store, now=6000.0)["woken"] == 1


def test_approval_decided_probe(steward_home):
    store = Store(steward_home)
    jid, b = _blocked(store)
    rec = approvals_module.open_approval(store, jid, "allow the vendor call?")
    waits.park(store, jid, blocker_id=b.blocker_id, kind="approval_decided", params={"approval_id": rec["id"]},
               reason="r", now=1.0)
    assert waits.sweep(store, now=100.0)["woken"] == 0
    approvals_module.decide(store, jid, rec["id"], "approve", decided_by="human")
    assert waits.sweep(store, now=400.0)["woken"] == 1


def test_a_broken_probe_backs_off_and_never_breaks_the_sweep(steward_home):
    store = Store(steward_home)
    jid, b = _blocked(store)
    waits.register_probe("explodes", lambda store, row, params: 1 / 0)
    waits.park(store, jid, blocker_id=b.blocker_id, kind="explodes", params={}, reason="r", now=1.0)
    other_jid, ob = _blocked(store)
    waits.park(store, other_jid, blocker_id=ob.blocker_id, kind="time", params={"at": 2.0}, reason="r", now=1.0)
    out = waits.sweep(store, now=100.0)
    assert out["woken"] == 1 and out["errors"] == 1


# -- Phase 42: the `check_run` wait kind (SRD-14) ---------------------------------------------------------------


def _check_run_job(steward_home, tmp_path, *, command="python3 -m pytest -q"):
    import subprocess

    from steward import check_runs

    store = Store(steward_home)
    repo = tmp_path / "wt"
    repo.mkdir()
    for argv in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "a@b"],
                 ["git", "config", "user.name", "a"]):
        subprocess.run(argv, cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "a"], cwd=repo, check=True)
    job = store.create_job("goal", str(repo))
    from steward.runner_exec import SubprocessRunner

    sub = check_runs.submit_worker_run(store, SubprocessRunner(), repo, job.job_id, command, now=1.0,
                                       toolchain_fn=lambda c: "tc")
    return store, job.job_id, sub.row


def _park_check(store, jid, row, now=2.0):
    return waits.park(store, jid, blocker_id=None, kind="check_run", params={"run_id": row.run_id},
                      reason=f"check run {row.run_id}", now=now)


def _finish_run(store, row, state, now=50.0):
    from steward import check_runs
    from steward.baseline import CheckRun

    if state == "done":
        assert check_runs.try_start(store, row.run_id, owner="o", pid=1, now=now)
        check_runs.complete(store, row.run_id, owner="o", run=CheckRun(exit_code=0, tests={"t::a": "passed"},
                                                                       used=row.command, output=""),
                            junit_src=None, now=now + 1)
    elif state == "failed":
        assert check_runs.try_start(store, row.run_id, owner="o", pid=1, now=now)
        check_runs.fail(store, row.run_id, owner="o", error="boom", now=now + 1)
    else:
        store._conn.execute("UPDATE check_runs SET state=?, finished_epoch=?, error='gone' WHERE run_id=?",
                            (state, now, row.run_id))
        store._conn.commit()


def test_check_run_wait_needs_a_run_id(steward_home, tmp_path):
    store, jid, _ = _check_run_job(steward_home, tmp_path)
    with pytest.raises(waits.WaitError, match="run_id"):
        waits.park(store, jid, blocker_id=None, kind="check_run", params={}, reason="r", now=1.0)


@pytest.mark.parametrize("state,met", [("queued", False), ("running", False), ("done", True), ("failed", True),
                                       ("lost", True), ("cancelled", True)])
def test_check_run_probe_is_met_only_when_the_run_is_final(steward_home, tmp_path, state, met):
    store, jid, row = _check_run_job(steward_home, tmp_path)
    w = _park_check(store, jid, row)
    if state == "running":
        from steward import check_runs

        assert check_runs.try_start(store, row.run_id, owner="o", pid=1, now=3.0)
    elif state != "queued":
        _finish_run(store, row, state)
    got, detail = waits.PROBES["check_run"](store, {"job_id": jid, **w, "_now": 99.0}, {"run_id": row.run_id})
    assert got is met and detail["run_id"] == row.run_id


def test_check_run_probe_treats_a_vanished_run_as_met(steward_home, tmp_path):
    store, jid, _ = _check_run_job(steward_home, tmp_path)
    got, detail = waits.PROBES["check_run"](store, {"job_id": jid, "_now": 1.0}, {"run_id": "cr-nope"})
    assert got is True and detail["state"] == "missing"


def test_waking_a_check_run_wait_records_exactly_one_result_steer(steward_home, tmp_path):
    from steward.steering import Steering

    store, jid, row = _check_run_job(steward_home, tmp_path)
    _park_check(store, jid, row)
    _finish_run(store, row, "done")
    assert waits.probe_job(store, jid, now=100.0, kinds=("check_run",))["woken"] == 1
    assert waits.probe_job(store, jid, now=101.0, kinds=("check_run",))["woken"] == 0  # already woken
    assert waits.sweep(store, now=10 ** 6)["woken"] == 0
    assert waits.waiting(store, jid) == []
    (steer,) = Steering(store).pending(jid)
    assert steer["verb"] == "nudge" and steer["origin"] == "check_run"
    assert steer["text"].startswith(f"[steward] check run {row.run_id} finished: ") and "exit 0" in steer["text"]
    assert "code" == next(e for e in store.events(jid) if e.what == "nudge instruction recorded").judgment.value


def test_sweep_backstop_wakes_a_check_run_wait_with_the_same_single_steer(steward_home, tmp_path):
    from steward.steering import Steering

    store, jid, row = _check_run_job(steward_home, tmp_path)
    _park_check(store, jid, row, now=0.0)
    _finish_run(store, row, "failed")
    assert waits.sweep(store, now=10_000.0)["woken"] == 1
    (steer,) = Steering(store).pending(jid)
    assert "failed" in steer["text"] and "boom" in steer["text"]
