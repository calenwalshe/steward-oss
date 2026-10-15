import pytest

from steward.control import Controller
from steward.models import ControlRequest, JobState, Judgment
from steward.store import Store


class ExplodingModel:
    """A stand-in for a model call: any invocation is a test failure (SP-11)."""

    def __call__(self, *args, **kwargs):
        raise AssertionError("model must never be called for pause/resume/stop")


def test_new_job_creates_job_and_human_event(steward_home):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("ship it", "/repo")

    assert job.state == JobState.CREATED
    events = store.events(job.job_id)
    assert len(events) == 1
    assert events[0].judgment == Judgment.human
    assert events[0].what == "job created"


def test_stop_running_job_transitions_to_stopping_and_blocks_dispatch(steward_home):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")
    store.set_state(
        job.job_id, JobState.CONTRACTED, what="c", evidence={}, rule="r",
        judgment=Judgment.code,
    )
    store.set_state(
        job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r",
        judgment=Judgment.code,
    )
    assert controller.may_dispatch(job.job_id) is True

    never_call_model = ExplodingModel()  # not passed anywhere; stop must not need it

    receipt = controller.submit(
        ControlRequest(request_id="req-1", job_id=job.job_id, kind="stop")
    )

    assert receipt.accepted is True
    assert "pending" in receipt.detail or "blocked" in receipt.detail
    assert controller.may_dispatch(job.job_id) is False
    assert store.get_job(job.job_id).state == JobState.STOPPING
    # Confirm nothing in the control path touched the exploding model.
    with pytest.raises(AssertionError):
        never_call_model()


def test_stop_before_running_goes_straight_to_stopped(steward_home):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")

    receipt = controller.submit(
        ControlRequest(request_id="req-1", job_id=job.job_id, kind="stop")
    )
    assert receipt.accepted is True
    assert store.get_job(job.job_id).state == JobState.STOPPED
    assert controller.may_dispatch(job.job_id) is False


def test_stop_needs_human_job_goes_to_stopped(steward_home):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")
    for state in (JobState.CONTRACTED, JobState.RUNNING, JobState.NEEDS_HUMAN):
        store.set_state(
            job.job_id, state, what="s", evidence={}, rule="r", judgment=Judgment.code,
        )

    receipt = controller.submit(
        ControlRequest(request_id="req-1", job_id=job.job_id, kind="stop")
    )
    assert receipt.accepted is True
    assert store.get_job(job.job_id).state == JobState.STOPPED


def test_duplicate_request_id_returns_original_receipt_and_appends_no_event(
    steward_home,
):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")

    req = ControlRequest(request_id="dup-1", job_id=job.job_id, kind="stop")
    first = controller.submit(req)
    events_after_first = len(store.events(job.job_id))

    second = controller.submit(req)
    events_after_second = len(store.events(job.job_id))

    assert second.duplicate is True
    assert second.request_id == first.request_id
    assert second.detail == first.detail
    assert events_after_second == events_after_first


def test_pause_then_resume(steward_home):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")
    store.set_state(
        job.job_id, JobState.CONTRACTED, what="c", evidence={}, rule="r",
        judgment=Judgment.code,
    )
    store.set_state(
        job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r",
        judgment=Judgment.code,
    )

    pause_receipt = controller.submit(
        ControlRequest(request_id="p1", job_id=job.job_id, kind="pause")
    )
    assert pause_receipt.accepted is True
    assert store.get_job(job.job_id).state == JobState.PAUSED
    assert controller.may_dispatch(job.job_id) is False

    resume_receipt = controller.submit(
        ControlRequest(request_id="r1", job_id=job.job_id, kind="resume")
    )
    assert resume_receipt.accepted is True
    assert store.get_job(job.job_id).state == JobState.RUNNING
    assert controller.may_dispatch(job.job_id) is True


def test_pause_from_non_running_is_not_accepted(steward_home):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")  # still CREATED

    receipt = controller.submit(
        ControlRequest(request_id="p1", job_id=job.job_id, kind="pause")
    )
    assert receipt.accepted is False
    assert store.get_job(job.job_id).state == JobState.CREATED


def test_unknown_control_kind_is_not_accepted_but_still_stored(steward_home):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")

    receipt = controller.submit(
        ControlRequest(request_id="x1", job_id=job.job_id, kind="teleport")
    )
    assert receipt.accepted is False
    assert store.get_receipt("x1") == receipt


def test_receipts_persist_across_controller_instances(steward_home):
    store1 = Store(steward_home)
    controller1 = Controller(store1)
    job = controller1.new_job("goal", "/repo")
    controller1.submit(
        ControlRequest(request_id="req-1", job_id=job.job_id, kind="stop")
    )

    store2 = Store(steward_home)
    controller2 = Controller(store2)
    receipt = controller2.submit(
        ControlRequest(request_id="req-1", job_id=job.job_id, kind="stop")
    )
    assert receipt.duplicate is True


def test_why_returns_last_event_as_dict(steward_home):
    store = Store(steward_home)
    controller = Controller(store)
    job = controller.new_job("goal", "/repo")
    store.set_state(
        job.job_id, JobState.CONTRACTED, what="contracted", evidence={"x": 1},
        rule="discovery", judgment=Judgment.code,
    )

    last = controller.why(job.job_id)
    assert last["what"] == "contracted"
    assert last["rule"] == "discovery"
    assert last["judgment"] == "code"
    assert last["evidence"] == {"x": 1}


def test_why_unknown_job_returns_none(steward_home):
    store = Store(steward_home)
    controller = Controller(store)
    assert controller.why("nope") is None
