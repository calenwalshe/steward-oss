import pytest

from steward.control import Controller
from steward.models import ControlRequest, JobState, Judgment
from steward.store import Store

def _walk(store, job_id, states):
    for state in states:
        store.set_state(job_id, state, what="s", evidence={}, rule="r", judgment=Judgment.code)


_TO_RUNNING = (JobState.CONTRACTED, JobState.RUNNING)


@pytest.mark.parametrize(
    "path",
    [
        _TO_RUNNING + (JobState.WORKER_FINISHED,),
        _TO_RUNNING + (JobState.WORKER_FINISHED, JobState.CHECKS_PASSED),
    ],
)
def test_stop_after_worker_finished_goes_to_stopped_and_keeps_branch(steward_home, path):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")
    _walk(store, job.job_id, path)
    before = store.get_job(job.job_id).state

    receipt = controller.submit(ControlRequest(request_id="req-1", job_id=job.job_id, kind="stop"))

    assert receipt.accepted is True
    assert store.get_job(job.job_id).state == JobState.STOPPED
    ev = [e for e in store.events(job.job_id) if e.rule == "operator_stop"][-1]
    assert ev.evidence["previous_state"] == before.value
    # the control plane never touches the job's branch/worktree: no git call is made
    assert controller.may_dispatch(job.job_id) is False


def test_stop_keeps_real_branch(steward_home, tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    run("init", "-q")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i")
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", str(repo))
    run("branch", f"steward/{job.job_id}")
    _walk(store, job.job_id, _TO_RUNNING + (JobState.WORKER_FINISHED,))
    controller.submit(ControlRequest(request_id="req-1", job_id=job.job_id, kind="stop"))
    assert store.get_job(job.job_id).state == JobState.STOPPED
    out = subprocess.run(["git", "-C", str(repo), "branch", "--list", f"steward/{job.job_id}"],
                         capture_output=True, text=True).stdout
    assert f"steward/{job.job_id}" in out


def test_accept_and_incorporate_refused_after_stop(steward_home):
    from steward import closeout, incorporate
    from steward.machine import IllegalTransition

    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")
    _walk(store, job.job_id, _TO_RUNNING + (JobState.WORKER_FINISHED, JobState.CHECKS_PASSED))
    controller.submit(ControlRequest(request_id="req-1", job_id=job.job_id, kind="stop"))

    with pytest.raises(IllegalTransition):
        closeout.accept(store, job.job_id)
    assert store.get_job(job.job_id).state == JobState.STOPPED
    receipt = incorporate._incorporate(store, job.job_id)
    assert receipt["accepted"] is False


def test_stop_on_accepted_is_refused(steward_home):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")
    _walk(store, job.job_id, _TO_RUNNING + (JobState.WORKER_FINISHED, JobState.CHECKS_PASSED, JobState.ACCEPTED))

    receipt = controller.submit(ControlRequest(request_id="req-1", job_id=job.job_id, kind="stop"))

    assert receipt.accepted is False
    assert "cannot stop: job is already ACCEPTED" in receipt.detail
    assert store.get_job(job.job_id).state == JobState.ACCEPTED


def test_stop_on_stopping_stays_idempotent(steward_home):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")
    _walk(store, job.job_id, _TO_RUNNING + (JobState.STOPPING,))
    receipt = controller.submit(ControlRequest(request_id="req-1", job_id=job.job_id, kind="stop"))
    assert receipt.accepted is True
    assert store.get_job(job.job_id).state == JobState.STOPPING
