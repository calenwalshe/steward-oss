"""Runner consumption of typed fabric frames (WRK-03, WRK-02).

No real Herdr, no real shim: `_FabricFakeWorker` is a `FakeWorker` whose
`kind = "fabric"` (the same duck-typed marker `steward.fabric_worker.FabricWorker`
sets) so `steward.runner._is_fabric()` routes it to the typed-event path,
scripted with `WorkerObservation(events=(...))` batches instead of
`output_tail` markers. `FabricWorker`'s own frame plumbing is covered by
tests/test_fabric_worker.py; this file is only about what `steward.runner`
does with the frames once observed.
"""

from __future__ import annotations

from ruling_support import StubLead, finish as finish_ticks, patch_green_verification
from steward import approvals as approvals_module
from steward import decision_trace
from steward.contract import Contract, store_contract
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.fabric import Frame
from steward.models import JobState, Judgment
from steward.recovery import RecoveryPolicy
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner
from steward.steering import Steering
from steward.store import Store
from steward.worker import FakeWorker, WorkerObservation


class _FabricFakeWorker(FakeWorker):
    kind = "fabric"

    def __init__(self, *args, attach_result: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        # Mirrors `FabricWorker.close_pane()`'s (10-04) duck-typed surface:
        # a real one only ever attempts a close once and only for a pane it
        # created, but that plumbing is unit-tested in test_fabric_worker.py;
        # here only "did the runner ask, exactly once" matters.
        self.pane_id = "w1:pF"
        self.close_pane_calls = 0
        self.attach_result = attach_result
        self.attach_calls: list[str] = []

    def close_pane(self) -> bool:
        self.close_pane_calls += 1
        return True

    def attach(self, pane_id: str) -> bool:
        """Mirrors `FabricWorker.attach()`'s (10-04) contract: on failure it
        leaves itself observably dead (`pid_alive=False`), so the very next
        `observe()` drives the runner's existing bounded-recovery path — the
        real plumbing (Herdr liveness check, rebind frame, ack wait) is
        unit-tested in test_fabric_worker.py; here only what `steward.runner`
        does with the boolean result matters."""
        self.attach_calls.append(pane_id)
        if not self.attach_result:
            self.script = [WorkerObservation(lifecycle="exited", output_tail="", pid_alive=False, ts="t")] + self.script
        return self.attach_result


def _contracted_job(store, goal="do the thing", repo="/repo", authority=()):
    job = store.create_job(goal, repo)
    contract = Contract(
        goal=goal, boundaries=[], done_when=["done"], next_step="", authority=list(authority), assumptions=[]
    )
    store_contract(store, job.job_id, contract)
    return job


def _git_runner(*, head="sha-1", diff="", untracked=()):
    def respond(argv, cwd):
        if argv[:2] == ["git", "merge-base"]:  # the job's start commit (differs from `head`: a new commit exists)
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout="base-0\n", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=head + "\n", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "diff"]:
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=diff, stderr="", duration_s=0.0)
        if argv[:2] == ["git", "status"]:
            porcelain = "".join(f"?? {u}\n" for u in untracked)
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=porcelain, stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout="", stderr="", duration_s=0.0)

    return FakeRunner(respond)


class _Clock:
    def __init__(self, start: float = 1000.0, step: float = 1.0):
        self._next = start
        self._step = step

    def __call__(self) -> float:
        value = self._next
        self._next += self._step
        return value


class _FabricFactory:
    """Pops a scripted `_FabricFakeWorker` per fresh dispatch (mirrors
    test_runner.py's `_Factory`, but the fabric-tagged worker class)."""

    def __init__(self, scripts=None):
        self._scripts = list(scripts) if scripts else []
        self.created: list[_FabricFakeWorker] = []

    def __call__(self, existing_ref):
        script = self._scripts.pop(0) if self._scripts else []
        worker = _FabricFakeWorker(script=script)
        self.created.append(worker)
        return worker


def _fake_diagnoser(verdict="retry_with_guidance", guidance="try again", hypothesis="hang"):
    return FakeDiagnoser(Diagnosis(verdict=verdict, guidance=guidance, hypothesis=hypothesis))


def _obs(lifecycle="working", events=(), pid_alive=True):
    return WorkerObservation(lifecycle=lifecycle, output_tail="", pid_alive=pid_alive, ts="t", events=events)


def _frame(seq, type_, body, job="jobA"):
    return Frame(v=1, seq=seq, job=job, type=type_, body=body)


# -- brief drops the marker protocol on the fabric path ----------------------


def test_start_renders_fabric_brief_without_marker_protocol(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _FabricFactory(scripts=[[]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())

    runner.start(job.job_id)

    brief = factory.created[0].started_brief
    assert "STEWARD-DONE" not in brief
    assert "STEWARD-ACK" not in brief
    assert "git add + git commit" in brief or "git commit" in brief


# -- steer delivery: only typed ack frames advance the receipt ---------------


def test_pending_instruction_is_sent_exactly_once_not_resent_every_tick(steward_home):
    """A live run (2026-09-27) showed the fabric worker resending the same
    steer frame every tick while the instruction waited on the shim's own
    ack — the agent saw the same steering text twice. `mark_delivered` (from
    adapter-send success) has to run for every adapter so the instruction
    leaves `pending()` after its first send, same as the legacy path."""
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _FabricFactory(scripts=[[_obs(), _obs()]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    Steering(store).record(job.job_id, "focus", "work on X", "req-1")

    runner.tick(job.job_id)
    runner.tick(job.job_id)

    assert factory.created[0].delivered == ["[rev=1] work on X"]  # not sent twice
    instructions = Steering(store).all_instructions(job.job_id)
    assert instructions[0]["status"] == "delivered"


def test_acknowledged_ack_frame_advances_past_send_success_delivered(steward_home):
    acked_obs = _obs(events=(_frame(11, "ack", {"rev": 1, "stage": "acknowledged"}),))
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _FabricFactory(scripts=[[_obs(), acked_obs]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    Steering(store).record(job.job_id, "focus", "work on X", "req-1")

    runner.tick(job.job_id)
    instructions = Steering(store).all_instructions(job.job_id)
    assert instructions[0]["status"] == "delivered"  # from adapter-send success

    runner.tick(job.job_id)
    instructions = Steering(store).all_instructions(job.job_id)
    assert instructions[0]["status"] == "acknowledged"
    assert instructions[0]["acknowledged_at"] is not None

    ack_events = [e for e in store.events(job.job_id) if e.rule == "fabric_ack_frame"]
    assert any(e.evidence.get("seq") == 11 and e.evidence.get("stage") == "acknowledged" for e in ack_events)


# -- permission_request frames open a typed approval --------------------------


def test_permission_request_frame_opens_approval_without_text_extraction(steward_home):
    """`ls` is not on SP-36's outbound/irreversible list, so it is opened
    AND auto-approved by the default-allow policy (NB-02) in the same tick
    — the approval record (and the frame-derived question) is still there,
    it just doesn't stay pending."""
    store = Store(steward_home)
    job = _contracted_job(store)
    blocked_obs = _obs(
        lifecycle="blocked",
        events=(_frame(3, "permission_request", {"request_id": "r1", "tool": "Bash", "input": {"command": "ls"}}),),
    )
    factory = _FabricFactory(scripts=[[blocked_obs]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    runner.tick(job.job_id)

    from steward import approvals as approvals_module

    all_approvals = approvals_module.list_approvals(store, job.job_id)
    assert len(all_approvals) == 1
    assert all_approvals[0]["kind"] == "permission"
    assert "Bash" in all_approvals[0]["question"]
    assert all_approvals[0]["status"] == "approved"
    assert all_approvals[0]["decided_by"] == "code"

    # TUI speed plan 10b: an auto-allowed request is ONE event (the request's facts plus the relay result)
    perm_events = [e for e in store.events(job.job_id)
                   if e.rule == "worker_permission_requested_frame" or approvals_module.is_single_permission_event(e)]
    assert perm_events and perm_events[0].evidence["request_id"] == "r1"
    assert [e.what for e in perm_events] == ["permission_auto_allowed"] and perm_events[0].evidence["delivery"] == "relayed"


def test_permission_request_does_not_open_a_second_approval_while_one_pending(steward_home):
    """`git push` IS on SP-36's outbound list, so it stays pending across
    ticks — the dedup guard (one open `permission` approval at a time,
    `FabricWorker.answer_prompt`'s own assumption) is exercised here for an
    ask-decision command."""
    store = Store(steward_home)
    job = _contracted_job(store)
    frame = _frame(3, "permission_request", {"request_id": "r1", "tool": "Bash", "input": {"command": "git push origin main"}})
    blocked_obs = _obs(lifecycle="blocked", events=(frame,))
    factory = _FabricFactory(scripts=[[blocked_obs, blocked_obs]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    runner.tick(job.job_id)
    runner.tick(job.job_id)

    from steward import approvals as approvals_module

    pending = approvals_module.list_approvals(store, job.job_id, status="pending")
    assert len(pending) == 1


def test_approved_permission_is_relayed_via_answer_prompt(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    frame = _frame(3, "permission_request", {"request_id": "r1", "tool": "Bash", "input": {}})
    blocked_obs = _obs(lifecycle="blocked", events=(frame,))
    idle_obs = _obs(lifecycle="idle")
    factory = _FabricFactory(scripts=[[blocked_obs, idle_obs]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    runner.tick(job.job_id)

    from steward import api as api_module

    steward_api = api_module.StewardAPI(store)
    pending_id = approvals_module.list_approvals(store, job.job_id)[0]["id"]  # a table since TUI speed plan 10a
    steward_api.approve(job.job_id, pending_id, decision="approve")

    runner.tick(job.job_id)

    assert factory.created[0].answered == [True]


def test_parallel_permission_requests_are_each_opened_and_answered_by_id(steward_home):
    # Regression: 3 parallel WebSearch permission requests; only one was answered and the
    # rest were marked stale, leaving the worker waiting forever.
    store = Store(steward_home)
    job = _contracted_job(store)
    frames = tuple(
        _frame(3 + i, "permission_request", {"request_id": rid, "tool": "WebSearch", "input": {"query": rid}})
        for i, rid in enumerate(("r1", "r2", "r3"))
    )
    factory = _FabricFactory(scripts=[[_obs(lifecycle="blocked", events=frames), _obs(lifecycle="idle"), _obs(lifecycle="idle")]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    runner.tick(job.job_id)  # opens + auto-allows all three (WebSearch is default-allow)
    runner.tick(job.job_id)  # relays each decision to its own request

    perms = [a for a in approvals_module.list_approvals(store, job.job_id) if a["kind"] == "permission"]
    assert [a["worker_request_id"] for a in perms] == ["r1", "r2", "r3"]
    assert all(a["status"] == "approved" and a["relayed_at"] for a in perms)
    assert factory.created[0].answered_ids == ["r1", "r2", "r3"]


# -- done frame -> WORKER_FINISHED -> closeout --------------------------------


BASE, NEW = "1" * 40, "2" * 40


def _yield_git(*, head=NEW, base=BASE, porcelain=""):
    """A git runner that answers the classifier's questions: worktree HEAD, the job's merge-base, porcelain."""
    def respond(argv, cwd):
        def ok(out=""):
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=out, stderr="", duration_s=0.0)
        if argv[:2] == ["git", "merge-base"]:
            return ok(base + "\n")
        if argv[:2] == ["git", "rev-parse"]:
            return ok(head + "\n")
        if argv[:2] == ["git", "status"]:
            return ok(porcelain)
        return ok()
    return FakeRunner(respond)


def _yields(store, job, lane=None):
    ev = [e for e in store.events(job.job_id) if e.what == "yield_classified"]
    return [e for e in ev if lane is None or e.evidence["lane"] == lane]


def _facts(store, job, kind):
    doc = store.kv_get(job.job_id, "fleet_arbiter") or {}
    return [f for f in doc.get("facts", []) if f["kind"] == kind]


class _StopJev:
    """Stands in for JevObserver: records ask_stop calls and answers from a script."""
    enabled = True

    def __init__(self, answer=None):
        self.answer, self.calls = answer, []

    def ask_stop(self, store, job_id, last_message, signals):
        self.calls.append((last_message, signals))
        return self.answer

    def observe(self, *a, **k):
        return None


def _yield_runner(steward_home, git, *, jev=None, clock=None, frame_body=None):
    store = Store(steward_home)
    job = _contracted_job(store)
    done_obs = _obs(lifecycle="idle", events=(_frame(9, "done", frame_body or {"sha": NEW}),))
    factory = _FabricFactory(scripts=[[done_obs]])
    runner = Runner(store, factory, git, _fake_diagnoser(), clock=clock or _Clock(), jev_observer=jev,
                    activity_fn=lambda st, jid: {"turns": [{"kind": "said", "text": "Which database should I use?",
                                                            "ts": "2026-10-06T00:00:00Z"}]})
    runner.start(job.job_id)
    return store, job, runner, factory


def test_candidate_yield_reaches_worker_finished_and_accepted(steward_home, monkeypatch):
    patch_green_verification(monkeypatch)  # checks ran (none are discoverable in this fixture)
    store, job, runner, factory = _yield_runner(steward_home, _yield_git())
    runner.job_lead = StubLead(store)

    first = runner.tick(job.job_id)
    assert first.state == "CHECKS_PASSED"  # green: the one completion ruling comes next
    report = finish_ticks(runner, job.job_id)

    marker_events = [e for e in store.events(job.job_id) if e.rule == "fabric_done_frame"]
    assert len(marker_events) == 1
    assert marker_events[0].evidence == {"sha": NEW, "seq": 9, "run_id": decision_trace.run_id(store, job.job_id)}
    (yc,) = _yields(store, job)
    assert yc.evidence == {"lane": "CANDIDATE", "sha": NEW, "seq": 9, "failed_preconditions": []}
    assert report.action == "closeout"
    assert report.state == "ACCEPTED"

    # 10-03 finding 3: a resting terminal state closes the worker's own pane
    # instead of leaving it open showing an idle shell.
    assert factory.created[0].close_pane_calls == 1
    pane_events = [e for e in store.events(job.job_id) if e.rule == "worker_pane_closed"]
    assert len(pane_events) == 1 and pane_events[0].evidence["pane_id"] == "w1:pF"


def test_midwork_yield_without_a_new_sha_never_reaches_worker_finished(steward_home):
    store, job, runner, _ = _yield_runner(steward_home, _yield_git(head=BASE), jev=_StopJev())

    report = runner.tick(job.job_id)

    assert report.state == "RUNNING"
    (yc,) = _yields(store, job)
    assert yc.evidence["lane"] == "STOPPED" and yc.evidence["failed_preconditions"] == ["no_new_commit"]
    assert not [e for e in store.events(job.job_id) if e.rule == "fabric_done_frame"]


def test_dirty_tree_yield_is_stopped_not_a_commit_nudge_or_closeout(steward_home):
    store, job, runner, _ = _yield_runner(steward_home, _yield_git(porcelain=" M a.py\n"), jev=_StopJev())
    assert runner.tick(job.job_id).state == "RUNNING"
    assert _yields(store, job)[0].evidence["failed_preconditions"] == ["dirty_worktree"]


def test_question_yield_emits_worker_asking_and_no_closeout(steward_home):
    store, job, runner, _ = _yield_runner(steward_home, _yield_git(), jev=_StopJev())
    store.kv_set(job.job_id, "questions", [{"qid": "q1", "status": "open", "blocking": True}])

    report = runner.tick(job.job_id)

    assert report.state == "RUNNING" and _yields(store, job, "ASKING")
    assert len(_facts(store, job, "worker_asking")) == 1
    assert not [e for e in store.events(job.job_id) if e.rule == "fabric_done_frame"]


def _real_worktree_yield(steward_home, kind, *, tmp_path):
    """A yield at a REAL commit that adds a blocking question; the kv `questions` scan has NOT run (the live bug:
    the scan came ~1.5 s after the yield, so classify saw no blocking question)."""
    import json
    import os
    import subprocess

    box = {"head": NEW, "base": BASE}

    def respond(argv, cwd):
        def ok(out=""):
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=out, stderr="", duration_s=0.0)
        if argv[:2] == ["git", "merge-base"]:
            return ok(box["base"] + "\n")
        if argv[:2] == ["git", "rev-parse"]:
            return ok(box["head"] + "\n")
        if argv[:2] == ["git", "status"]:
            return ok("")
        return ok()

    store, job, runner, _ = _yield_runner(steward_home, FakeRunner(respond), jev=_StopJev())
    wt = store.home / "worktrees" / job.job_id
    wt.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*a):
        return subprocess.run(["git", *a], cwd=wt, check=True, capture_output=True, text=True, env=env).stdout.strip()

    git("init", "-q")
    (wt / "README.md").write_text("x\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    box["base"] = git("rev-parse", "HEAD")
    store.add_check(job.job_id, commit_sha=box["base"], command="p", exit_code=0, results={}, label="baseline")
    if kind == "json":
        (wt / "STEWARD-QUESTIONS.json").write_text(json.dumps(
            [{"id": "q1", "question": "Which database?", "options": ["a", "b"], "recommended": "a", "blocking": True}]))
    else:
        (wt / "NOTES.md").write_text('```steward-questions\n[{"id": "q1", "question": "Which database?", '
                                     '"options": ["a", "b"], "recommended": "a", "blocking": true}]\n```\n')
    git("add", "-A")
    git("commit", "-qm", "ask")
    box["head"] = git("rev-parse", "HEAD")
    assert not store.kv_get(job.job_id, "questions")
    return store, job, runner


def test_yield_committing_a_blocking_question_is_asking_before_the_scan_ran(steward_home, tmp_path):
    for kind in ("json", "fence"):
        home = steward_home / kind
        home.mkdir()
        store, job, runner = _real_worktree_yield(home, kind, tmp_path=tmp_path)

        report = runner.tick(job.job_id)

        assert report.state == "RUNNING", kind
        assert _yields(store, job, "ASKING") and not _yields(store, job, "CANDIDATE"), kind
        assert len(_facts(store, job, "worker_asking")) == 1, kind
        assert not [e for e in store.events(job.job_id) if e.rule == "fabric_done_frame"], kind
        assert not [e for e in store.events(job.job_id) if e.what in ("completion_ruling_requested",
                                                                       "verification recorded")], kind


def test_stopped_yield_with_confident_jev_label_becomes_a_fact_in_the_same_tick(steward_home):
    jev = _StopJev({"choice": "stuck", "confidence": 0.85})
    store, job, runner, _ = _yield_runner(steward_home, _yield_git(head=BASE), jev=jev)

    runner.tick(job.job_id)

    (fact,) = _facts(store, job, "worker_stopped")
    assert fact["data"]["label"] == "stuck" and fact["route"] == "outer"
    assert len(jev.calls) == 1 and "Which database" in jev.calls[0][0]
    assert store.kv_get(job.job_id, "stop_fact_due") is None


def test_stopped_yield_with_jev_unavailable_emits_the_fact_after_120s_by_code(steward_home):
    now = [1000.0]
    jev = _StopJev(None)
    store, job, runner, _ = _yield_runner(steward_home, _yield_git(head=BASE), jev=jev, clock=lambda: now[0])

    runner.tick(job.job_id)
    assert _facts(store, job, "worker_stopped") == []  # not yet
    due = store.kv_get(job.job_id, "stop_fact_due")
    assert due and due["due_at"] == 1000.0 + Runner.STOP_FACT_DELAY_S

    now[0] += 60
    runner.tick(job.job_id)
    assert _facts(store, job, "worker_stopped") == []
    now[0] += 61
    runner.tick(job.job_id)
    (fact,) = _facts(store, job, "worker_stopped")
    assert fact["data"]["label"] == "unlabeled"
    now[0] += 500
    runner.tick(job.job_id)
    assert len(_facts(store, job, "worker_stopped")) == 1  # never twice


def test_low_confidence_jev_label_also_waits_for_the_timer(steward_home):
    jev = _StopJev({"choice": "asking", "confidence": 0.4})
    store, job, runner, _ = _yield_runner(steward_home, _yield_git(head=BASE), jev=jev)
    runner.tick(job.job_id)
    assert _facts(store, job, "worker_stopped") == [] and store.kv_get(job.job_id, "stop_fact_due")


def test_errored_result_is_stopped_errored_with_no_jev_call_and_no_closeout(steward_home):
    jev = _StopJev({"choice": "stuck", "confidence": 0.9})
    store, job, runner, _ = _yield_runner(
        steward_home, _yield_git(), jev=jev, frame_body={"sha": NEW, "is_error": True, "result_subtype": "error_max_turns"})

    report = runner.tick(job.job_id)

    assert report.state == "RUNNING" and jev.calls == []
    assert [e for e in store.events(job.job_id) if e.rule == "fabric_turn_error"]
    (yc,) = _yields(store, job)
    assert yc.evidence["lane"] == "STOPPED" and yc.evidence["failed_preconditions"] == ["errored"]
    (fact,) = _facts(store, job, "worker_stopped")
    assert fact["data"]["label"] == "errored"


def test_yield_at_the_last_ruled_sha_is_claims_done_and_starts_no_verification(steward_home):
    jev = _StopJev({"choice": "stuck", "confidence": 0.9})
    store, job, runner, _ = _yield_runner(steward_home, _yield_git(), jev=jev)
    store.kv_set(job.job_id, "last_ruled_sha", NEW)

    report = runner.tick(job.job_id)

    assert report.state == "RUNNING" and jev.calls == []
    assert _yields(store, job)[0].evidence["failed_preconditions"] == ["same_as_last_ruled"]
    assert _facts(store, job, "worker_stopped")[0]["data"]["label"] == "claims_done"
    assert not [e for e in store.events(job.job_id) if e.what.startswith("verification")]


def test_closeout_records_the_ruled_sha(steward_home):
    store, job, runner, _ = _yield_runner(steward_home, _yield_git())
    runner.tick(job.job_id)
    assert store.kv_get(job.job_id, "last_ruled_sha")


# -- error frame -> dead -> bounded replace, no marker/text parsing ----------


def test_error_frame_triggers_replacement_with_no_output_tail_parsing(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    dead_obs = _obs(lifecycle="exited", pid_alive=False, events=(_frame(4, "error", {"exit_code": 1}),))
    factory = _FabricFactory(scripts=[[dead_obs], []])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    report = runner.tick(job.job_id)

    assert report.action == "replace"
    assert len(factory.created) == 2
    error_events = [e for e in store.events(job.job_id) if e.rule == "fabric_error_frame"]
    assert error_events[0].evidence == {"exit_code": 1, "seq": 4}
    # the replacement worker also got the marker-free fabric brief
    assert "STEWARD-DONE" not in factory.created[1].started_brief
    # replacement (not a terminal resting state) never closes the old
    # worker's pane — a live gate already confirmed its shell exits on its
    # own; leaving the pane open is deliberate, for operator inspection.
    assert factory.created[0].close_pane_calls == 0


# -- needs_human escalation closes the worker pane ----------------------------


def test_needs_human_after_recovery_budget_exhausted_closes_pane(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    dead_obs = _obs(lifecycle="exited", pid_alive=False, events=(_frame(4, "error", {"exit_code": 1}),))
    factory = _FabricFactory(scripts=[[dead_obs], [dead_obs], [dead_obs]])
    runner = Runner(
        store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), policy=RecoveryPolicy(max_recoveries=2)
    )
    runner.start(job.job_id)

    runner.tick(job.job_id)
    runner.tick(job.job_id)
    report = runner.tick(job.job_id)

    assert report.state == "NEEDS_HUMAN"
    assert report.action == "needs_human"
    current_worker = factory.created[-1]
    assert current_worker.close_pane_calls == 1
    pane_events = [e for e in store.events(job.job_id) if e.rule == "worker_pane_closed"]
    assert len(pane_events) == 1


# -- operator stop closes the worker pane -------------------------------------


def test_operator_stop_confirmed_closes_pane(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    factory = _FabricFactory(scripts=[[]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    store.set_state(
        job.job_id, JobState.STOPPING, what="operator stop", evidence={}, rule="operator_stop",
        judgment=Judgment.human,
    )

    report = runner.tick(job.job_id)

    assert report.state == "STOPPED"
    assert factory.created[0].close_pane_calls == 1
    pane_events = [e for e in store.events(job.job_id) if e.rule == "worker_pane_closed"]
    assert len(pane_events) == 1


# -- fabric resume after controller restart (10-04) ---------------------------


def test_resume_all_fabric_attach_succeeds_no_second_writer(steward_home):
    store_a = Store(steward_home)
    job = _contracted_job(store_a)
    factory_a = _FabricFactory(scripts=[[]])
    runner_a = Runner(store_a, factory_a, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner_a.start(job.job_id)
    # Simulate a controller crash: no tick() ever ran on runner_a.

    store_b = Store(steward_home)
    pane_id = store_b.kv_get(job.job_id, "attempts")[0]["worker_ref"]
    reattached = _FabricFakeWorker(attach_result=True)

    def factory_b(existing_ref):
        assert existing_ref == pane_id
        return reattached

    runner_b = Runner(store_b, factory_b, _git_runner(), _fake_diagnoser(), clock=_Clock())

    resumed = runner_b.resume_all([job.job_id])

    assert resumed == [job.job_id]
    assert reattached.attach_calls == [pane_id]
    reattach_events = [e for e in store_b.events(job.job_id) if e.rule == "fabric_resume_attach"]
    assert len(reattach_events) == 1
    assert reattach_events[0].evidence == {"attempt_no": 1, "pane_id": pane_id, "attached": True}
    assert len(store_b.kv_get(job.job_id, "attempts")) == 1  # no second writer started


def test_resume_all_fabric_attach_fails_falls_back_to_dead_worker_replacement(steward_home):
    store_a = Store(steward_home)
    job = _contracted_job(store_a)
    factory_a = _FabricFactory(scripts=[[]])
    runner_a = Runner(store_a, factory_a, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner_a.start(job.job_id)

    store_b = Store(steward_home)
    pane_id = store_b.kv_get(job.job_id, "attempts")[0]["worker_ref"]
    dead_reattach = _FabricFakeWorker(attach_result=False)
    replacement = _FabricFakeWorker()
    built = [dead_reattach, replacement]

    def factory_b(existing_ref):
        return built.pop(0)

    runner_b = Runner(store_b, factory_b, _git_runner(), _fake_diagnoser(), clock=_Clock())

    resumed = runner_b.resume_all([job.job_id])
    assert resumed == [job.job_id]

    report = runner_b.tick(job.job_id)

    assert report.action == "replace"
    reattach_events = [e for e in store_b.events(job.job_id) if e.rule == "fabric_resume_attach"]
    assert reattach_events[0].evidence["attached"] is False
    # a fresh attempt (a new pane, via the existing single-writer path) —
    # attach() never starts a second writer at the old, unreachable one.
    attempts = store_b.kv_get(job.job_id, "attempts")
    assert len(attempts) == 2


def test_done_with_unacknowledged_steer_is_deferred_until_ack_and_fresh_done(steward_home, monkeypatch):
    patch_green_verification(monkeypatch)
    """11-03 acceptance: a steer delivered in the same tick as a done from the
    previous turn was accepted-over and never acted on. The stale done must be
    deferred; closeout happens only on a done after the steer's ack."""
    store = Store(steward_home)
    job = _contracted_job(store)
    stale_done = _obs(lifecycle="idle", events=(_frame(9, "done", {"sha": "old111"}),))
    ack_then_done = _obs(lifecycle="idle", events=(
        _frame(10, "ack", {"rev": 1, "stage": "acknowledged"}),
        _frame(11, "done", {"sha": "new222"}),
    ))
    factory = _FabricFactory(scripts=[[stale_done, ack_then_done]])
    runner = Runner(store, factory, _yield_git(), _fake_diagnoser(), clock=_Clock())
    runner.job_lead = StubLead(store)
    runner.start(job.job_id)
    Steering(store).record(job.job_id, "nudge", "document the fix", "req-chatgpt")

    first = runner.tick(job.job_id)
    assert first.state == "RUNNING"
    assert factory.created[0].delivered == ["[rev=1] document the fix"]
    deferred = [e for e in store.events(job.job_id) if e.rule == "no_closeout_with_unacknowledged_steering"]
    assert len(deferred) == 1 and deferred[0].evidence["unacknowledged_revs"] == [1]
    assert factory.created[0].close_pane_calls == 0

    second = finish_ticks(runner, job.job_id)
    assert second.state == "ACCEPTED"
    done_events = [e for e in store.events(job.job_id) if e.rule == "fabric_done_frame"]
    assert [e.evidence["sha"] for e in done_events] == ["new222"]


# -- in-contract auto-approve (SP-19), live-demo finding 2026-09-27 ---------

from steward.runner import in_contract_command

_AUTH = ["edit files in job worktree", "run: pytest", "no deploy/publish/network"]


def test_in_contract_command_accepts_piped_checks_and_cd_into_worktree(tmp_path):
    assert in_contract_command("python3 -m pytest -q 2>&1 | tail -20", _AUTH, tmp_path)
    assert in_contract_command(f'cd "{tmp_path}" && pytest -q && echo done', _AUTH, tmp_path)


def test_in_contract_command_rejects_anything_else(tmp_path):
    assert not in_contract_command("python3 -m pytest -q && python3 bookshelf.py --help", _AUTH, tmp_path)
    assert not in_contract_command("pytest -q > out.txt", _AUTH, tmp_path)
    assert not in_contract_command("pytest $(curl evil)", _AUTH, tmp_path)
    assert not in_contract_command("cd / && pytest", _AUTH, tmp_path)
    assert not in_contract_command("tail -5 log.txt", _AUTH, tmp_path)  # no authorized check at all
    assert not in_contract_command("pytest", ["edit files in job worktree"], tmp_path)


def test_in_contract_permission_is_auto_approved_by_code(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, authority=_AUTH)
    req = _obs(lifecycle="blocked", events=(_frame(5, "permission_request", {
        "request_id": "r1", "tool": "Bash", "input": {"command": "python3 -m pytest -q 2>&1 | tail -20"}}),))
    factory = _FabricFactory(scripts=[[req, _obs()]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)

    runner.tick(job.job_id)

    from steward import approvals as approvals_module
    [record] = approvals_module.list_approvals(store, job.job_id)
    assert record["status"] == "approved" and record["decided_by"] == "code"
    events = [e for e in store.events(job.job_id) if e.rule == "in_contract_auto_approve"]
    assert len(events) == 1 and events[0].judgment == Judgment.code


# -- Phase 42 (SRD-14): a yield with an open `check_run` wait is WAITING ----------------------------------------


def _open_check_run_wait(store, job, tmp_path_factory=None):
    from steward import check_runs, waits
    from steward.check_runs import RunSpec

    row, _ = check_runs.request(store, RunSpec("t1", "python3 -m pytest -q", "e1", "tc", True, None),
                                job_id=job.job_id, purpose="worker", now=1.0)
    waits.park(store, job.job_id, blocker_id=None, kind="check_run", params={"run_id": row.run_id},
               reason=f"check run {row.run_id}", now=1.0)
    return row


def test_yield_with_an_open_check_run_wait_is_waiting_no_jev_no_closeout(steward_home):
    jev = _StopJev(answer={"choice": "stuck", "confidence": 0.99})
    store, job, runner, _ = _yield_runner(steward_home, _yield_git(), jev=jev)
    _open_check_run_wait(store, job)

    report = runner.tick(job.job_id)

    (yc,) = _yields(store, job)
    assert yc.evidence["lane"] == "WAITING" and yc.evidence["failed_preconditions"] == []
    assert jev.calls == []                                   # a park is not a stop: no model call
    assert report.state == "RUNNING" and report.action != "closeout"
    assert store.get_job(job.job_id).state == JobState.RUNNING
    assert not [e for e in store.events(job.job_id) if e.rule in ("fabric_done_frame", "closeout")]
    assert _facts(store, job, "worker_stopped") == [] and _facts(store, job, "worker_asking") == []


def test_finished_run_resumes_the_worker_after_a_waiting_yield(steward_home):
    from steward import check_runs
    from steward.baseline import CheckRun

    store, job, runner, factory = _yield_runner(steward_home, _yield_git(), jev=_StopJev())
    row = _open_check_run_wait(store, job)
    runner.tick(job.job_id)
    assert check_runs.try_start(store, row.run_id, owner="o", pid=1, now=5.0)
    check_runs.complete(store, row.run_id, owner="o", run=CheckRun(exit_code=0, tests={"t::a": "passed"},
                                                                   used="x", output=""), junit_src=None, now=6.0)
    runner.tick(job.job_id)
    worker = factory.created[0]
    resumes = [t for t in worker.delivered if "[steward] check run" in t]
    assert len(resumes) == 1 and "exit 0" in resumes[0]
