from steward import approvals
from steward.models import JobState, Judgment
from steward.store import Store


def _needs_human_job(store: Store, job_id_goal="goal") -> str:
    job = store.create_job(job_id_goal, "/repo")
    # Drive CREATED -> ... -> NEEDS_HUMAN the same way closeout does, so the
    # acceptance-approval test can exercise a real accept() transition.
    store.set_state(
        job.job_id, JobState.CONTRACTED, what="c", evidence={}, rule="r", judgment=Judgment.code
    )
    store.set_state(
        job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code
    )
    store.set_state(
        job.job_id,
        JobState.WORKER_FINISHED,
        what="w",
        evidence={},
        rule="r",
        judgment=Judgment.code,
    )
    store.set_state(
        job.job_id,
        JobState.CHECKS_PASSED,
        what="c",
        evidence={},
        rule="r",
        judgment=Judgment.code,
    )
    store.set_state(
        job.job_id,
        JobState.NEEDS_HUMAN,
        what="checks passed; acceptance unresolved",
        evidence={"reasons": ["contract has no done_when criteria"]},
        rule="acceptance_unresolved",
        judgment=Judgment.code,
    )
    return job.job_id


def test_open_approval_records_pending_and_event(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    record = approvals.open_approval(store, job.job_id, "may I proceed?", approval_id="ap-1")

    assert record["status"] == "pending"
    assert record["kind"] == "general"
    assert record["relayed_at"] is None
    assert approvals.list_approvals(store, job.job_id) == [record]
    events = [e for e in store.events(job.job_id) if e.what == "approval opened"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.code


def test_open_approval_accepts_explicit_kind(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    record = approvals.open_approval(
        store, job.job_id, "worker permission: rm foo", approval_id="ap-1", kind="permission"
    )

    assert record["kind"] == "permission"


def test_mark_relayed_sets_timestamp_and_records_event(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    approvals.open_approval(store, job.job_id, "q", approval_id="ap-1", kind="permission")
    approvals.decide(store, job.job_id, "ap-1", "approve", request_id="req-1")

    record = approvals.mark_relayed(store, job.job_id, "ap-1")

    assert record["relayed_at"] is not None
    events = [e for e in store.events(job.job_id) if e.rule == "permission_decision_relayed"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.human


def test_mark_stale_sets_status_and_records_event(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    approvals.open_approval(store, job.job_id, "q", approval_id="ap-1", kind="permission")

    record = approvals.mark_stale(store, job.job_id, "ap-1", reason="worker no longer blocked")

    assert record["status"] == "stale"
    events = [e for e in store.events(job.job_id) if e.rule == "approval_stale"]
    assert len(events) == 1
    assert events[0].evidence["reason"] == "worker no longer blocked"


def test_decide_approve_marks_approved_with_human_judgment(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    approvals.open_approval(store, job.job_id, "may I proceed?", approval_id="ap-1")

    receipt = approvals.decide(store, job.job_id, "ap-1", "approve", request_id="req-1")

    assert receipt.accepted is True
    record = approvals.get_approval(store, job.job_id, "ap-1")
    assert record["status"] == "approved"
    assert record["decided_at"] is not None
    events = [e for e in store.events(job.job_id) if e.what == "approval approved"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.human


def test_decide_deny_marks_denied(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    approvals.open_approval(store, job.job_id, "may I proceed?", approval_id="ap-1")

    receipt = approvals.decide(store, job.job_id, "ap-1", "deny", request_id="req-1")

    assert receipt.accepted is True
    assert approvals.get_approval(store, job.job_id, "ap-1")["status"] == "denied"


def test_decide_unknown_approval_is_not_accepted(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    receipt = approvals.decide(store, job.job_id, "nope", "approve", request_id="req-1")

    assert receipt.accepted is False
    assert "unknown approval" in receipt.detail


def test_decide_already_decided_approval_is_not_accepted_again(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    approvals.open_approval(store, job.job_id, "may I proceed?", approval_id="ap-1")
    approvals.decide(store, job.job_id, "ap-1", "approve", request_id="req-1")

    receipt = approvals.decide(store, job.job_id, "ap-1", "deny", request_id="req-2")

    assert receipt.accepted is False
    assert "already approved" in receipt.detail


def test_decide_dedupes_on_request_id(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    approvals.open_approval(store, job.job_id, "may I proceed?", approval_id="ap-1")

    first = approvals.decide(store, job.job_id, "ap-1", "approve", request_id="req-dup")
    second = approvals.decide(store, job.job_id, "ap-1", "deny", request_id="req-dup")

    assert second.duplicate is True
    assert second.detail == first.detail
    assert approvals.get_approval(store, job.job_id, "ap-1")["status"] == "approved"


def test_approving_acceptance_question_drives_needs_human_to_accepted(steward_home):
    store = Store(steward_home)
    job_id = _needs_human_job(store)
    approvals.open_approval(
        store, job_id, approvals.ACCEPTANCE_QUESTION, approval_id="ap-1"
    )

    receipt = approvals.decide(store, job_id, "ap-1", "approve", request_id="req-1")

    assert receipt.accepted is True
    assert "job accepted" in receipt.detail
    assert store.get_job(job_id).state == JobState.ACCEPTED


def test_approving_acceptance_question_from_wrong_state_reports_failure_without_raising(
    steward_home,
):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")  # still CREATED, not NEEDS_HUMAN
    approvals.open_approval(
        store, job.job_id, approvals.ACCEPTANCE_QUESTION, approval_id="ap-1"
    )

    receipt = approvals.decide(store, job.job_id, "ap-1", "approve", request_id="req-1")

    assert receipt.accepted is True  # the approval itself still resolved
    assert "accept not applied" in receipt.detail
    assert store.get_job(job.job_id).state == JobState.CREATED


def test_denying_acceptance_question_stops_needs_human_job(steward_home):
    store = Store(steward_home)
    job_id = _needs_human_job(store)
    approvals.open_approval(
        store, job_id, approvals.ACCEPTANCE_QUESTION, approval_id="ap-1"
    )

    receipt = approvals.decide(store, job_id, "ap-1", "deny", request_id="req-1")

    assert "job stopped" in receipt.detail
    assert store.get_job(job_id).state == JobState.STOPPED
