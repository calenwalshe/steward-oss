from steward.closeout import accept, closeout, release
from steward.contract import Contract, store_contract
from steward.models import JobState, Judgment
from steward.steering import Steering
from steward.store import Store
from steward.baseline import CheckResult
from steward.verify import Verification
from steward.weakening import Finding


def _green(sha="sha1", **kw):
    """A verification in which checks actually ran (an empty `results` list is "no checks ran": never accepted)."""
    kw.setdefault("results", [CheckResult("pytest -q", 0, {"t::test_a": "passed"})])
    return Verification(commit_sha=sha, **kw)


def _rule(store, job_id, contract, *, ruling_="finished", sha="sha1", instructions="", evidence=None):
    """Answer the pending completion ruling the way the executor would (validated) and run the second entry point."""
    from steward import closeout as closeout_module
    from steward import ruling

    ev = [{"item": i, "refs": ["check:1"]} for i in range(len(contract.done_when))] if evidence is None else evidence
    v = ruling.validate({"ruling": ruling_, "sha": sha, "evidence": ev, "instructions": instructions}, sha=sha,
                        verification_sha=sha, done_when_count=len(contract.done_when), check_ids={"check:1"},
                        event_seqs=set(), diff_paths=set())
    assert closeout_module.record_ruling(store, job_id, v, lead="astra-tech-lead", proposal="p1") is not None
    return closeout_module.decide_after_ruling(store, job_id, contract)


def _accepted(store, contract_kw=None):
    job, contract = _finished_job(store)
    assert closeout(store, job.job_id, _green("s"), [], contract) == JobState.CHECKS_PASSED
    assert _rule(store, job.job_id, contract, sha="s") == JobState.ACCEPTED
    return job, contract


def _finished_job(store, *, done_when=("done",), goal="g"):
    job = store.create_job(goal, "/repo")
    contract = Contract(
        goal=goal,
        boundaries=[],
        done_when=list(done_when),
        next_step="",
        authority=[],
        assumptions=[],
    )
    store_contract(store, job.job_id, contract)
    store.set_state(
        job.job_id, JobState.RUNNING, what="dispatch", evidence={}, rule="r", judgment=Judgment.code
    )
    store.set_state(
        job.job_id, JobState.WORKER_FINISHED, what="done marker", evidence={}, rule="r",
        judgment=Judgment.code,
    )
    return job, contract


def test_dirty_worktree_returns_to_running_with_guidance(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)
    verification = _green(dirty=True)

    state = closeout(store, job.job_id, verification, [], contract)

    assert state == JobState.RUNNING
    assert store.get_job(job.job_id).state == JobState.RUNNING
    pending = Steering(store).pending(job.job_id)
    assert len(pending) == 1
    assert "worktree dirty" in pending[0]["text"]


def test_new_failures_return_to_running_with_guidance(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)
    verification = _green(new_failures=["t::test_x"])

    state = closeout(store, job.job_id, verification, [], contract)

    assert state == JobState.RUNNING


def test_preexisting_failures_pass_checks_but_leave_acceptance_unresolved(steward_home):
    # Pre-existing failures don't send the worker back (they aren't new), but
    # "no new failures" is not "the checks pass": acceptance stays with a human.
    store = Store(steward_home)
    job, contract = _finished_job(store)
    verification = _green(preexisting_failures=["t::test_old"])

    state = closeout(store, job.job_id, verification, [], contract)

    assert state == JobState.NEEDS_HUMAN
    events = store.events(job.job_id)
    assert any(e.what == "checks passed at exact commit" for e in events)
    assert "baseline failure(s) still failing" in str(events[-1].evidence)


def test_verify_round_budget_exhausted_escalates_to_needs_human(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)

    for _ in range(2):
        closeout(store, job.job_id, Verification(commit_sha="s", dirty=True), [], contract)
        store.set_state(
            job.job_id, JobState.WORKER_FINISHED, what="retry", evidence={}, rule="r",
            judgment=Judgment.code,
        )

    state = closeout(store, job.job_id, Verification(commit_sha="s", dirty=True), [], contract)

    assert state == JobState.NEEDS_HUMAN
    events = [e for e in store.events(job.job_id) if e.rule == "verify_rounds_exhausted"]
    assert len(events) == 1


def test_green_verification_waits_for_a_ruling_instead_of_accepting(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)

    state = closeout(store, job.job_id, _green(), [], contract)

    assert state == JobState.CHECKS_PASSED
    assert store.get_job(job.job_id).state == JobState.CHECKS_PASSED
    rec = store.kv_get(job.job_id, "completion_ruling")
    assert rec["status"] == "requested" and rec["sha"] == "sha1"
    assert [e for e in store.events(job.job_id) if e.what == "completion_ruling_requested"]
    assert not [e for e in store.events(job.job_id) if e.rule in ("preauthorized_local_acceptance", "ruling_accepted")]
    from steward.closeout import decide_after_ruling
    assert decide_after_ruling(store, job.job_id, contract) is None  # still waiting: nothing to decide


def test_finished_ruling_with_guards_reaches_accepted_as_model_informed_code_authorized(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)
    closeout(store, job.job_id, _green(), [], contract)

    assert _rule(store, job.job_id, contract) == JobState.ACCEPTED

    events = store.events(job.job_id)
    assert len([e for e in events if e.rule == "checks_pass_at_sha"]) == 1
    acc = [e for e in events if e.rule == "ruling_accepted"]
    assert len(acc) == 1 and acc[0].judgment == Judgment.model_informed_code_authorized
    assert acc[0].evidence["ruling_id"].startswith("rul-") and acc[0].evidence["ruling_sha"] == "sha1"
    assert not [e for e in events if e.rule == "preauthorized_local_acceptance"]
    assert store.kv_get(job.job_id, "completion_ruling")["status"] == "decided"


def test_not_met_ruling_returns_to_running_with_the_instructions_as_the_steer(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)
    closeout(store, job.job_id, _green(), [], contract)

    assert _rule(store, job.job_id, contract, ruling_="not_met", evidence=[],
                 instructions="add the missing retry test") == JobState.RUNNING

    pending = Steering(store).pending(job.job_id)
    assert len(pending) == 1 and "add the missing retry test" in pending[0]["text"]
    assert store.kv_get(job.job_id, "verify_rounds") == 1  # it counts a verify round


def test_not_met_again_and_again_does_not_loop_forever(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)
    state = None
    for _ in range(3):
        assert closeout(store, job.job_id, _green(), [], contract) == JobState.CHECKS_PASSED
        state = _rule(store, job.job_id, contract, ruling_="not_met", evidence=[], instructions="again")
        if state == JobState.RUNNING:
            store.set_state(job.job_id, JobState.WORKER_FINISHED, what="again", evidence={}, rule="r",
                            judgment=Judgment.code)
    assert state == JobState.NEEDS_HUMAN
    assert "ruled the goal not met" in str(store.events(job.job_id)[-1].evidence)


def test_unverified_ruling_parks_the_job_with_the_reasons(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)
    closeout(store, job.job_id, _green(), [], contract)

    state = _rule(store, job.job_id, contract, evidence=[{"item": 0, "refs": ["check:99"]}])

    assert state == JobState.NEEDS_HUMAN
    last = store.events(job.job_id)[-1]
    assert last.rule == "acceptance_unresolved" and "check:99" in str(last.evidence["reasons"])
    assert [e for e in store.events(job.job_id) if e.what == "completion_ruling_debit"]


def test_no_ruling_available_parks_the_job_acceptance_unresolved(steward_home):
    from steward.closeout import decide_after_ruling, mark_ruling_unavailable

    store = Store(steward_home)
    job, contract = _finished_job(store)
    closeout(store, job.job_id, _green(), [], contract)
    mark_ruling_unavailable(store, job.job_id, "both leads failed")

    assert decide_after_ruling(store, job.job_id, contract) == JobState.NEEDS_HUMAN
    last = store.events(job.job_id)[-1]
    assert "no completion ruling" in str(last.evidence["reasons"]) and last.rule == "acceptance_unresolved"


def test_empty_check_results_never_accept_and_buy_no_ruling(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)

    state = closeout(store, job.job_id, Verification(commit_sha="sha1"), [], contract)

    assert state == JobState.NEEDS_HUMAN
    assert "no checks ran" in str(store.events(job.job_id)[-1].evidence["reasons"])
    assert store.kv_get(job.job_id, "completion_ruling") is None


def test_docs_only_diff_with_a_finished_ruling_is_accepted(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)

    assert closeout(store, job.job_id, Verification(commit_sha="sha1", docs_only=True), [], contract) \
        == JobState.CHECKS_PASSED
    assert _rule(store, job.job_id, contract) == JobState.ACCEPTED


def test_weakening_found_after_the_ruling_still_parks_the_job(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)
    closeout(store, job.job_id, _green(), [], contract)
    store.kv_set(job.job_id, "closeout_blocked", True)  # a hard constraint trips while the ruling is out

    assert _rule(store, job.job_id, contract) == JobState.NEEDS_HUMAN


def test_a_ruling_for_another_sha_never_accepts(steward_home):
    from steward import closeout as closeout_module

    store = Store(steward_home)
    job, contract = _finished_job(store)
    closeout(store, job.job_id, _green(), [], contract)
    rec = store.kv_get(job.job_id, "completion_ruling")
    store.kv_set(job.job_id, "completion_ruling", {**rec, "status": "ruled", "ruling": "finished", "ruling_sha": "other",
                                                   "id": "rul-x", "evidence": []})

    assert closeout_module.decide_after_ruling(store, job.job_id, contract) == JobState.NEEDS_HUMAN
    assert "not the verified sha" in str(store.events(job.job_id)[-1].evidence["reasons"])


def test_weakening_findings_force_needs_human(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)
    verification = _green()
    findings = [Finding(path="tests/test_x.py", kind="deleted_test_file", detail="deleted")]

    state = closeout(store, job.job_id, verification, findings, contract)

    assert state == JobState.NEEDS_HUMAN
    events = [e for e in store.events(job.job_id) if e.what == "checks passed; acceptance unresolved"]
    assert len(events) == 1
    assert "test-weakening" in str(events[0].evidence["reasons"])
    assert events[0].judgment == Judgment.code


def test_closeout_blocked_kv_forces_needs_human(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store)
    store.kv_set(job.job_id, "closeout_blocked", True)
    verification = _green()

    state = closeout(store, job.job_id, verification, [], contract)

    assert state == JobState.NEEDS_HUMAN


def test_active_review_constraint_forces_needs_human_in_human_mode(steward_home, monkeypatch):
    # acceptance=auto waives unviolated review constraints (tests/test_unblock_rerun.py); human mode still stops
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "human")
    store = Store(steward_home)
    job, contract = _finished_job(store)
    Steering(store).record(job.job_id, "goal", "also preserve the api", "req-1")
    verification = _green()

    state = closeout(store, job.job_id, verification, [], contract)

    assert state == JobState.NEEDS_HUMAN


def test_contract_without_done_when_forces_needs_human(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store, done_when=[])
    verification = _green()

    state = closeout(store, job.job_id, verification, [], contract)

    assert state == JobState.NEEDS_HUMAN


def test_missing_contract_forces_needs_human(steward_home):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    store.set_state(
        job.job_id, JobState.CONTRACTED, what="d", evidence={}, rule="r", judgment=Judgment.code
    )
    store.set_state(
        job.job_id, JobState.RUNNING, what="d", evidence={}, rule="r", judgment=Judgment.code
    )
    store.set_state(
        job.job_id, JobState.WORKER_FINISHED, what="d", evidence={}, rule="r",
        judgment=Judgment.code,
    )
    verification = _green()

    state = closeout(store, job.job_id, verification, [], None)

    assert state == JobState.NEEDS_HUMAN


def test_closeout_from_wrong_state_raises(steward_home):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")  # CREATED, not WORKER_FINISHED
    verification = _green()

    try:
        closeout(store, job.job_id, verification, [], None)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_accept_requires_human_judgment_and_transitions_needs_human_to_accepted(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store, done_when=[])  # forces NEEDS_HUMAN (a goal steer no longer does under auto)
    closeout(store, job.job_id, Verification(commit_sha="s"), [], contract)
    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN

    state = accept(store, job.job_id)

    assert state == JobState.ACCEPTED
    events = [e for e in store.events(job.job_id) if e.rule == "operator_accept"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.human


def test_release_requires_human_judgment(steward_home):
    store = Store(steward_home)
    job, contract = _accepted(store)

    state = release(store, job.job_id)

    assert state == JobState.RELEASED
    events = [e for e in store.events(job.job_id) if e.rule == "operator_release"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.human


# -- CLI: accept/release are human-only, prompt-free but explicit ----------


def test_cli_accept_transitions_needs_human_to_accepted(steward_home):
    from steward import cli

    store = Store(steward_home)
    job, contract = _finished_job(store, done_when=[])  # forces NEEDS_HUMAN (a goal steer no longer does under auto)
    closeout(store, job.job_id, _green("s"), [], contract)
    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN

    rc = cli.main(["accept", job.job_id])

    assert rc == 0
    assert store.get_job(job.job_id).state == JobState.ACCEPTED


def test_cli_release_transitions_accepted_to_released(steward_home):
    from steward import cli

    store = Store(steward_home)
    job, contract = _accepted(store)

    rc = cli.main(["release", job.job_id])

    assert rc == 0
    assert store.get_job(job.job_id).state == JobState.RELEASED


def test_cli_accept_on_illegal_state_errors_without_mutating(steward_home):
    from steward import cli

    store = Store(steward_home)
    job = store.create_job("g", "/repo")  # CREATED, not NEEDS_HUMAN

    rc = cli.main(["accept", job.job_id])

    assert rc == 2
    assert store.get_job(job.job_id).state == JobState.CREATED


def test_cli_accept_unknown_job_errors(steward_home):
    from steward import cli

    rc = cli.main(["accept", "no-such-job"])

    assert rc == 1


# --- 2026-10-04 job 8025f70f: a baseline failure the contract excepts must not park the job -------------------

_EXCEPTED_GOAL = "Fix the batch take flow (pre-existing test_batchtake baseline failure excepted)"


def test_auto_mode_accepts_when_goal_excepts_the_remaining_preexisting_failure(steward_home):
    # replay of 8025f70f: 1 pre-existing failure, goal excepts it by name, acceptance = auto
    store = Store(steward_home)
    job, contract = _finished_job(
        store, goal=_EXCEPTED_GOAL, done_when=("the full test suite passes (test_batchtake excepted)",))
    verification = _green(preexisting_failures=["tests.test_batchtake::test_take_all"])

    assert closeout(store, job.job_id, verification, [], contract) == JobState.CHECKS_PASSED
    state = _rule(store, job.job_id, contract)

    assert state == JobState.ACCEPTED
    events = store.events(job.job_id)
    assert any(e.rule == "checks_pass_at_sha" for e in events)
    accepted = [e for e in events if e.rule == "ruling_accepted"]
    assert accepted[-1].evidence["preexisting_waived"] == ["tests.test_batchtake::test_take_all"]


def test_auto_mode_still_blocks_a_preexisting_failure_the_contract_does_not_except(steward_home):
    store = Store(steward_home)
    job, contract = _finished_job(store, goal=_EXCEPTED_GOAL, done_when=("all tests pass",))
    verification = _green(preexisting_failures=["tests.test_other::test_unrelated"])

    assert closeout(store, job.job_id, verification, [], contract) == JobState.NEEDS_HUMAN
    assert "baseline failure(s) still failing" in str(store.events(job.job_id)[-1].evidence)


def test_human_mode_unchanged_for_an_excepted_preexisting_failure(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "human")
    store = Store(steward_home)
    job, contract = _finished_job(store, goal=_EXCEPTED_GOAL)
    verification = _green(preexisting_failures=["tests.test_batchtake::test_take_all"])

    assert closeout(store, job.job_id, verification, [], contract) == JobState.NEEDS_HUMAN
    assert "baseline failure(s) still failing" in str(store.events(job.job_id)[-1].evidence)


# --- 2026-10-04 job bf5237fe: verification that ran before the background baseline finished ------------------

def _unattributed_job(store, *, goal="g", done_when=("done",)):
    from steward import bg_baseline

    job, contract = _finished_job(store, goal=goal, done_when=done_when)
    bg_baseline.start(store, job.job_id, "/repo", "base1", spawn=lambda argv: None)
    store.add_check(job.job_id, commit_sha="sha1", command="pytest -q", exit_code=1, label="verify",
                    results={"tests.test_a::test_old": "failed", "tests.test_a::test_ok": "passed"})
    store.append_event(job.job_id, what="verification recorded", rule="verify_at_exact_commit",
                       judgment=Judgment.code,
                       evidence={"commit_sha": "sha1", "baseline_missing": True,
                                 "new_failures": ["tests.test_a::test_old"], "preexisting_failures": []})
    verification = _green(new_failures=["tests.test_a::test_old"], unattributed=True)
    return job, contract, verification


def _baseline_finishes(store, job_id, results):
    from steward import bg_baseline

    store.add_check(job_id, commit_sha="base1", command="pytest -q", exit_code=1, results=results, label="baseline")
    bg_baseline.finish(store, job_id, status="done")


def test_unattributed_verification_parks_instead_of_blaming_the_worker(steward_home):
    store = Store(steward_home)
    job, contract, verification = _unattributed_job(store)

    assert closeout(store, job.job_id, verification, [], contract) == JobState.NEEDS_HUMAN
    assert "no baseline to compare" in str(store.events(job.job_id)[-1].evidence) + store.events(job.job_id)[-1].what


def test_baseline_finishing_after_an_unattributed_verification_reattributes_and_promotes(steward_home):
    from steward.closeout import reattribute_after_baseline

    store = Store(steward_home)
    job, contract, verification = _unattributed_job(
        store, goal=_EXCEPTED_GOAL, done_when=("tests pass (test_old pre-existing, excepted)",))
    closeout(store, job.job_id, verification, [], contract)
    checks_before = len(store.get_checks(job.job_id, label="verify"))
    assert reattribute_after_baseline(store, job.job_id, contract) is None  # baseline still running: nothing to do

    _baseline_finishes(store, job.job_id, {"tests.test_a::test_old": "failed", "tests.test_a::test_ok": "passed"})
    state = reattribute_after_baseline(store, job.job_id, contract)

    assert state == JobState.CHECKS_PASSED  # the reattributed green verification now waits for its ruling
    assert _rule(store, job.job_id, contract, sha="sha1") == JobState.ACCEPTED
    assert any(e.rule == "checks_pass_at_sha" for e in store.events(job.job_id))
    assert len(store.get_checks(job.job_id, label="verify")) == checks_before  # no second pytest run
    assert reattribute_after_baseline(store, job.job_id, contract) is None  # once only


def test_reattribution_still_sends_back_a_failure_the_baseline_did_not_have(steward_home):
    from steward.closeout import reattribute_after_baseline

    store = Store(steward_home)
    job, contract, verification = _unattributed_job(store)
    closeout(store, job.job_id, verification, [], contract)
    _baseline_finishes(store, job.job_id, {"tests.test_a::test_old": "passed", "tests.test_a::test_ok": "passed"})

    assert reattribute_after_baseline(store, job.job_id, contract) == JobState.RUNNING


# -- SRD-10 / O2: any accept flows into autopilot ------------------------------------------------------------------


def _parked_job(store):
    job, contract = _finished_job(store, done_when=[])  # forces NEEDS_HUMAN
    closeout(store, job.job_id, _green("s"), [], contract)
    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN
    return job


def test_operator_accept_on_a_parked_job_attempts_the_merge_once(steward_home, monkeypatch):
    from steward import autopilot, cli

    store = Store(steward_home)
    job = _parked_job(store)
    calls = []
    monkeypatch.setattr(autopilot, "ensure_attempted",
                        lambda st, jid, runner, **kw: calls.append((jid, runner, sorted(kw))) or {"status": "merged"})

    assert cli.main(["accept", job.job_id]) == 0

    assert [c[0] for c in calls] == [job.job_id]
    assert calls[0][2] == ["notify"]  # the runner exec and a phone notifier were handed over (fakeable)
    assert store.get_job(job.job_id).state == JobState.ACCEPTED


def test_operator_accept_on_a_protected_job_is_skipped_with_the_reason(steward_home, monkeypatch):
    from steward import cli, deadline

    store = Store(steward_home)
    job = _parked_job(store)
    monkeypatch.setattr(deadline, "ever", lambda st, jid: True)

    assert cli.main(["accept", job.job_id]) == 0

    skipped = [e for e in store.events(job.job_id) if e.what == "autopilot_skipped"]
    assert skipped and skipped[-1].evidence["reason"] == "deadline_mode"
    assert not [e for e in store.events(job.job_id) if e.what == "autopilot_attempt"]


def test_an_autopilot_failure_never_undoes_the_accept(steward_home, monkeypatch):
    from steward import autopilot, cli

    store = Store(steward_home)
    job = _parked_job(store)

    def boom(*a, **k):
        raise RuntimeError("merge machinery down")

    monkeypatch.setattr(autopilot, "ensure_attempted", boom)

    assert cli.main(["accept", job.job_id]) == 0
    assert store.get_job(job.job_id).state == JobState.ACCEPTED
    assert [e for e in store.events(job.job_id) if e.what == "autopilot_error"]


def test_approving_the_acceptance_approval_also_attempts_the_merge(steward_home, monkeypatch):
    from steward import approvals, autopilot
    from steward.api import StewardAPI
    from steward.runner_exec import FakeRunner

    store = Store(steward_home)
    job = _parked_job(store)
    rec = approvals.open_approval(store, job.job_id, approvals.ACCEPTANCE_QUESTION, kind="acceptance")
    calls = []
    monkeypatch.setattr(autopilot, "ensure_attempted", lambda st, jid, runner, **kw: calls.append((jid, runner)))
    fake = FakeRunner(lambda argv, cwd: None)

    StewardAPI(store, runner_exec=fake).approve(job.job_id, rec["id"], decision="approve")

    assert calls == [(job.job_id, fake)] and store.get_job(job.job_id).state == JobState.ACCEPTED
