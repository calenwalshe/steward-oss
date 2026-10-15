from pathlib import Path

from steward import approvals
from steward.api import StewardAPI
from steward.contract import Contract, store_contract
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.steering import Steering
from steward.store import Store


def _contracted_job(store, goal="do the thing", repo="/repo", *, next_step="dispatch worker"):
    job = store.create_job(goal, repo)
    contract = Contract(
        goal=goal,
        boundaries=["don't touch the db"],
        done_when=["checks pass"],
        next_step=next_step,
        authority=["edit files in job worktree"],
        assumptions=["assumed pytest"],
    )
    store_contract(store, job.job_id, contract)
    return job


# -- read -------------------------------------------------------------------


def test_list_jobs_returns_state_and_revision(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)

    jobs = api.list_jobs()

    assert len(jobs) == 1
    assert jobs[0]["job_id"] == job.job_id
    assert jobs[0]["state"] == JobState.CONTRACTED.value
    # store_contract seeds the one goal-derived boundary as an instruction,
    # bumping job_revision to 1 before any explicit steering happens.
    assert jobs[0]["revision"] == 1


def test_list_jobs_filters_by_state(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    _contracted_job(store, goal="a")
    store.create_job("b", "/repo")  # stays CREATED

    contracted = api.list_jobs(states={JobState.CONTRACTED})

    assert len(contracted) == 1
    assert contracted[0]["goal"] == "a"


def test_get_job_returns_none_for_unknown_job(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    assert api.get_job("nope") is None


def test_get_job_includes_contract_and_active_constraints(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)

    result = api.get_job(job.job_id)

    assert result["state"] == JobState.CONTRACTED.value
    assert result["contract"]["goal"] == "do the thing"
    assert result["contract"]["contract_revision"] == 1
    assert any("touch the db" in c["text"] for c in result["active_constraints"])
    assert result["max_recoveries"] == 2
    assert result["recoveries_used"] == 0


def test_get_current_step_reports_next_and_pending(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store, next_step="fix the reconnect loop")
    Steering(store).record(job.job_id, "focus", "look at retries", "req-1")

    step = api.get_current_step(job.job_id)

    assert step["next_step"] == "fix the reconnect loop"
    # 1 seeded boundary instruction (from store_contract) + this steer.
    assert len(step["pending_receipts"]) == 2
    assert any(p["text"] == "look at retries" for p in step["pending_receipts"])


def test_get_recent_events_caps_at_limit(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    for i in range(5):
        store.append_event(
            job.job_id, what=f"event-{i}", evidence={}, rule="r", judgment=Judgment.code
        )

    events = api.get_recent_events(job.job_id, limit=3)

    assert len(events) == 3
    assert events[-1]["what"] == "event-4"


def test_get_verification_reports_no_verification_yet(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)

    result = api.get_verification(job.job_id)

    assert result["verified"] is False


def test_get_verification_reports_new_and_preexisting_failures(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    store.append_event(
        job.job_id,
        what="verification recorded",
        evidence={
            "commit_sha": "abc123",
            "new_failures": ["tests/test_x.py::test_a"],
            "fixed": [],
            "preexisting_failures": ["tests/test_y.py::test_b"],
        },
        rule="verify_at_exact_commit",
        judgment=Judgment.code,
    )

    result = api.get_verification(job.job_id)

    assert result["verified"] is True
    assert result["dirty"] is False
    assert result["commit_sha"] == "abc123"
    assert result["new_failures"] == ["tests/test_x.py::test_a"]
    assert result["preexisting_failures"] == ["tests/test_y.py::test_b"]


def test_get_verification_reports_dirty_worktree(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    store.append_event(
        job.job_id,
        what="verify_worktree_dirty",
        evidence={"commit_sha": "abc123"},
        rule="verify_requires_clean_worktree",
        judgment=Judgment.code,
    )

    result = api.get_verification(job.job_id)

    assert result["dirty"] is True
    assert result["new_failures"] == []


def test_get_verification_surfaces_weakening_reasons(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    store.append_event(
        job.job_id,
        what="verification recorded",
        evidence={"commit_sha": "abc123", "new_failures": [], "fixed": [], "preexisting_failures": []},
        rule="verify_at_exact_commit",
        judgment=Judgment.code,
    )
    store.append_event(
        job.job_id,
        what="checks passed; acceptance unresolved",
        evidence={"reasons": ["test-weakening detected: ['removed_test_function']"]},
        rule="acceptance_unresolved",
        judgment=Judgment.code,
    )

    result = api.get_verification(job.job_id)

    assert any("weakening" in r for r in result["weakening"])


def _res(argv, cwd, out="", code=0):
    return CommandResult(argv=argv, cwd=str(cwd), exit_code=code, stdout=out, stderr="", duration_s=0.0)


def test_get_diff_summary_uses_recorded_baseline_and_caps_stat(steward_home, monkeypatch):
    store = Store(steward_home)
    monkeypatch.setattr("steward.jobdiff.job_diff_base", lambda st, j: ("abc123", Path("/wt"), "HEAD"))
    calls = []

    def respond(argv, cwd):
        calls.append(argv)
        if argv[:3] == ["git", "diff", "--stat"]:
            return _res(argv, cwd, "x" * 5000)
        if argv[:3] == ["git", "diff", "--numstat"]:
            return _res(argv, cwd, "10\t2\tsteward/api.py\n-\t-\timg.png\n5\t0\tsteward/cli.py\n")
        return _res(argv, cwd)

    api = StewardAPI(store, runner_exec=FakeRunner(respond))
    job = _contracted_job(store)
    store.add_check(job.job_id, commit_sha="abc123", command="pytest", exit_code=0, results={}, label="baseline")

    result = api.get_diff_summary(job.job_id)

    assert len(result["stat"]) <= 4096
    assert result["base"] == "abc123"
    assert ["git", "diff", "--stat", "abc123..HEAD"] in calls
    assert result["files"] == ["steward/api.py", "img.png", "steward/cli.py"]
    assert (result["insertions"], result["deletions"]) == (15, 2)


def test_get_diff_summary_falls_back_to_merge_base(steward_home, monkeypatch):
    store = Store(steward_home)
    monkeypatch.setattr("steward.jobdiff.job_diff_base", lambda st, j: ("mbsha", Path("/wt"), "HEAD"))

    def respond(argv, cwd):
        if argv[:3] == ["git", "diff", "--numstat"]:
            assert argv[3] == "mbsha..HEAD"
            return _res(argv, cwd, "4077\t0\ta.py\n")
        return _res(argv, cwd, "stat")

    api = StewardAPI(store, runner_exec=FakeRunner(respond))
    job = _contracted_job(store)

    result = api.get_diff_summary(job.job_id)

    assert result["base"] == "mbsha"
    assert result["files"] == ["a.py"]
    assert result["insertions"] == 4077


def test_get_diff_summary_without_any_base_is_empty_with_note(steward_home, monkeypatch):
    store = Store(steward_home)
    monkeypatch.setattr("steward.jobdiff.job_diff_base", lambda st, j: None)
    api = StewardAPI(store, runner_exec=FakeRunner(lambda argv, cwd: _res(argv, cwd, "", 128)))
    job = _contracted_job(store)

    result = api.get_diff_summary(job.job_id)

    assert result["base"] is None and result["files"] == [] and "note" in result


def test_get_current_step_includes_live_worker_line(steward_home, monkeypatch):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    store.kv_set(job.job_id, "last_class", "progressing")
    live = {"now": "running Bash pytest (waiting on its result since 10:00:00)", "running_tool": "Bash pytest",
            "since": "2026-09-30T10:00:00Z", "last_said": "running tests", "last_activity_ts": "2026-09-30T10:00:00Z"}
    monkeypatch.setattr("steward.api.transcript_module.live_line", lambda store, jid: live)

    step = api.get_current_step(job.job_id)

    assert step["worker"] == live
    assert step["heartbeat"] == step["heartbeat_class"] == "progressing"
    assert step["next_step"] == "dispatch worker"


def test_get_current_step_survives_missing_transcript(steward_home, tmp_path, monkeypatch):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    monkeypatch.setattr("steward.transcript.Path.home", lambda: tmp_path)

    step = api.get_current_step(job.job_id)

    assert step["worker"]["now"] == "no worker transcript found"


def test_explain_blocker_reports_needs_human_reason(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    store.set_state(
        job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code
    )
    store.set_state(
        job.job_id,
        JobState.NEEDS_HUMAN,
        what="checks passed; acceptance unresolved",
        evidence={"reasons": ["contract has no done_when criteria"]},
        rule="acceptance_unresolved",
        judgment=Judgment.code,
    )

    result = api.explain_blocker(job.job_id)

    assert result["blocker"] == "needs_human"
    assert result["detail"] == "checks passed; acceptance unresolved"


def test_explain_blocker_reports_no_blocker_when_none(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)

    result = api.explain_blocker(job.job_id)

    assert result["blocker"] is None


# -- write --------------------------------------------------------------


def test_steer_records_instruction_and_returns_revision(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)

    receipt = api.steer(job.job_id, "look at retries first", verb="focus", request_id="req-1")

    assert receipt["accepted"] is True
    # revision 1 was the seeded contract boundary; this steer bumps it to 2.
    assert receipt["revision"] == 2
    record = next(
        i
        for i in Steering(store).all_instructions(job.job_id)
        if i["text"] == "look at retries first"
    )
    assert record["origin"] == "cli"


def test_steer_stale_expected_revision_returns_conflict(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    # revision is already 1 (seeded boundary) before this steer bumps it to 2.
    api.steer(job.job_id, "first", verb="focus", request_id="req-1")

    receipt = api.steer(
        job.job_id, "second", verb="focus", request_id="req-2", expected_revision=0
    )

    assert receipt["accepted"] is False
    assert "CONFLICT" in receipt["detail"]
    assert receipt["revision"] == 2
    # only the seeded boundary + the first successful steer recorded.
    assert len(Steering(store).all_instructions(job.job_id)) == 2


def test_steer_with_chatgpt_origin_persists_human_and_interpreted_text(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)

    api.steer(
        job.job_id,
        "prioritize the retry loop",
        verb="focus",
        origin="chatgpt",
        human_instruction="please look at retries first",
        interpreted_instruction="prioritize the retry loop",
        request_id="req-1",
    )

    record = next(
        i
        for i in Steering(store).all_instructions(job.job_id)
        if i["origin"] == "chatgpt"
    )
    assert record["human_instruction"] == "please look at retries first"


def test_pause_resume_go_through_controller(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    store.set_state(
        job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code
    )

    paused = api.pause(job.job_id, request_id="req-1")
    assert paused["accepted"] is True
    assert store.get_job(job.job_id).state == JobState.PAUSED

    resumed = api.resume(job.job_id, request_id="req-2")
    assert resumed["accepted"] is True
    assert store.get_job(job.job_id).state == JobState.RUNNING


def test_cancel_stops_job(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    store.set_state(
        job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code
    )

    result = api.cancel(job.job_id, request_id="req-1")

    assert result["accepted"] is True
    assert store.get_job(job.job_id).state == JobState.STOPPING


def test_control_requests_dedupe(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    store.set_state(
        job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code
    )

    first = api.pause(job.job_id, request_id="req-dup")
    second = api.pause(job.job_id, request_id="req-dup")

    assert second["duplicate"] is True
    assert second["detail"] == first["detail"]


def test_approve_resolves_pending_approval(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    approvals.open_approval(store, job.job_id, "may I proceed?", approval_id="ap-1")

    result = api.approve(job.job_id, "ap-1", decision="approve", request_id="req-1")

    assert result["accepted"] is True
    assert approvals.get_approval(store, job.job_id, "ap-1")["status"] == "approved"


def test_deny_resolves_pending_approval(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    approvals.open_approval(store, job.job_id, "may I proceed?", approval_id="ap-1")

    result = api.approve(job.job_id, "ap-1", decision="deny", request_id="req-1")

    assert result["accepted"] is True
    assert approvals.get_approval(store, job.job_id, "ap-1")["status"] == "denied"


def test_live_line_reports_running_tool_and_last_said(steward_home, tmp_path):
    import json
    from steward import transcript
    store = Store(steward_home)
    job = _contracted_job(store)
    store.kv_set(job.job_id, "worker_session_id", "sess1")
    d = tmp_path / transcript.project_slug(transcript.worktree_path(store, job.job_id))
    d.mkdir()
    rows = [
        {"type": "assistant", "timestamp": "2026-09-30T10:00:00Z", "message": {"id": "m1", "content": [
            {"type": "text", "text": "running the suite"}]}},
        {"type": "assistant", "timestamp": "2026-09-30T10:00:05Z", "message": {"id": "m2", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]}},
    ]
    (d / "sess1.jsonl").write_text("\n".join(json.dumps(r) for r in rows))

    live = transcript.live_line(store, job.job_id, projects_dir=tmp_path)

    assert live["last_said"] == "running the suite"
    assert live["since"] == "2026-09-30T10:00:05Z"
    assert live["running_tool"] and "pytest" in live["running_tool"]
    assert live["now"].startswith("running ")


def test_get_verification_finish_line_absent_without_contract_finish_line(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)

    assert "finish_line" not in StewardAPI(store).get_verification(job.job_id)


def test_get_verification_finish_line_unknown_when_gate_not_run(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, goal="Build it. FINISH LINE: `python3 check.py` exits 0; docs updated")

    result = StewardAPI(store).get_verification(job.job_id)

    assert result["verified"] is False
    assert result["finish_line"] == [
        {"text": "`python3 check.py` exits 0", "met": None, "evidence": None},
        {"text": "docs updated", "met": None, "evidence": None},
    ]


def test_get_verification_finish_line_met_and_not_met_from_gate(steward_home):
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store, goal="Build it. FINISH LINE: `python3 check.py` exits 0; docs updated")
    store.append_event(job.job_id, what="finish_line_not_met",
                       evidence={"command": "python3 check.py", "first_failing_line": "FAIL x"},
                       rule="finish_line_gate", judgment=Judgment.code)

    fl = api.get_verification(job.job_id)["finish_line"]
    assert fl[0] == {"text": "`python3 check.py` exits 0", "met": False, "evidence": "FAIL x"}
    assert fl[1]["met"] is None

    store.append_event(job.job_id, what="finish_line_met", evidence={"command": "python3 check.py"},
                       rule="finish_line_gate", judgment=Judgment.code)
    fl = api.get_verification(job.job_id)["finish_line"]
    assert fl[0]["met"] is True and "passed" in fl[0]["evidence"]
    assert fl[1]["met"] is None


def test_eta_keys_are_null_without_a_stored_eta(steward_home):
    # phase 18-06: every read carries the eta keys; a job with nothing stored (or not RUNNING) has none.
    store = Store(steward_home)
    api = StewardAPI(store)
    job = _contracted_job(store)
    row = api.list_jobs()[0]
    assert row["eta"] is None and row["eta_p50_min"] is None and row["eta_text"] is None
    assert api.get_job(job.job_id)["eta"] is None and api.get_current_step(job.job_id)["eta"] is None
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    assert api.get_current_step(job.job_id)["eta"] is None      # RUNNING but nothing stored yet


def test_list_jobs_carries_the_short_name(steward_home):
    from steward.api import StewardAPI
    from steward.store import Store

    store = Store(steward_home)
    store.create_job('Build Steward Phase 18 "ETA"', "/repo")
    assert StewardAPI(store).list_jobs()[0]["name"] == "P18 ETA"
