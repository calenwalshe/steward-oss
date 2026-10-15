"""SUP-12: the tech lead's actions - unblock, send_back, takeover, escalate - each through floor then policy.

Takeover keeps exactly one writer (pause -> lease -> edit -> commit -> release -> resume with a note); it never
overlaps a live writer; a push inside a takeover is refused by the floor; too big a diff is undone and becomes a
send-back. Judgment-based fixes need the policy's confidence. Precision is scored per action."""

from __future__ import annotations

import json
import subprocess

import pytest

from steward import approvals, lease
from steward import tech_lead_actions as tla
from steward import tech_lead_floor as floor
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.runner_exec import SubprocessRunner
from steward.steering import Steering
from steward.store import Store
from test_runner import _contracted_job
from test_tech_lead_no_clog import ASTRA, FABLE


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, "fix it. Finish line: `pytest -q tests/test_a.py`")
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    wt = store.home / "worktrees" / job.job_id
    (wt / "pkg").mkdir(parents=True)
    (wt / "tests").mkdir()
    (wt / "pkg" / "runtime.py").write_text("# the owner's switch\nX = 1\n")
    (wt / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    git(wt, "init", "-q", "-b", "main")
    git(wt, "add", "-A")
    git(wt, "-c", "user.name=w", "-c", "user.email=w@x", "commit", "-q", "-m", "base")
    lease.acquire(store, job.job_id, "worker-1")
    return store, job.job_id, wt


class Worker:
    def __init__(self):
        self.interrupted = 0

    def interrupt(self):
        self.interrupted += 1


def sync_bg(fn):
    """Synchronous stand-in for the background edit (tests)."""
    return tla.Background(fn)


def run(store, jid, wt, raw, *, policy=None, writing=False, edit=None, worker=None, notes=None):
    """Parse a proposal and execute it through the same gate the JobTechLead uses; finish a takeover."""
    notes = notes if notes is not None else []
    pol = policy or tpol.Policy()

    def takeover_invoke(entry, prompt, worktree, timeout):
        assert lease.takeover_holder(store, jid)["holder"] == f"tech_lead:{entry.id}"  # we are the one writer
        (edit or (lambda w: (w / "pkg" / "runtime.py").write_text("# the operator's switch\nX = 1\n")))(worktree)
        return "edited"

    ex = tla.ActionExecutor(store, runner_exec=SubprocessRunner(), notify_fn=lambda *a: notes.append(a),
                            worker_fn=lambda j: worker, worker_writing_fn=lambda j: writing,
                            takeover_invoke=takeover_invoke, start_bg=sync_bg)
    prop = tla.parse_job_proposal(raw)
    ctx = tla.ActionContext(jid, ASTRA.id, ASTRA.model, prop, wt, pol, entry=ASTRA, facts=[{"kind": "x"}])
    fctx = floor.context_for(store, jid, wt)
    out = []
    for a in prop.actions:
        decision, why, extra = tla.gate(a, ctx, fctx)
        if decision == "refused":
            out.append(ex.refuse(ctx, a, why, extra["class"]))
            break
        if decision == "policy_blocked":
            out.append(ex.record(ctx, a, "policy_blocked", why))
            continue
        if decision == "shadow":
            out.append(ex.record(ctx, a, "shadowed", why, would_act=a.to_dict()))
            continue
        out.append(ex.execute(ctx, a))
    if ex.takeover_pending(jid):
        import time
        for _ in range(200):
            res = ex.poll_takeover(jid, pol, fctx)
            if res is not None:
                out.append(res)
                break
            time.sleep(0.01)
    return out, ex


def prop(*actions, judgment=False, confidence=0.95, signature="sig"):
    return json.dumps({"proposal_id": "p1", "trigger_id": "t", "confidence": confidence, "judgment": judgment,
                       "signature": signature, "plan_item": "", "actions": list(actions)})


# -- parse ---------------------------------------------------------------------------------------------------

def test_parse_accepts_the_four_actions_and_names_floor_types():
    p = tla.parse_job_proposal(prop({"type": "send_back", "params": {"text": "x"}},
                                    {"type": "push", "params": {}}))
    assert [a.type for a in p.actions] == ["send_back", "push"]
    with pytest.raises(tla.ProposalError) as exc:
        tla.parse_job_proposal(prop({"type": "split", "params": {}}))
    assert exc.value.reason == "unknown_action"
    with pytest.raises(tla.ProposalError):
        tla.parse_job_proposal(prop({"type": "unblock", "params": {"decision": "maybe", "text": "x"}}))
    with pytest.raises(tla.ProposalError):
        tla.parse_job_proposal("{not json")


# -- floor before policy, every time ---------------------------------------------------------------------------

def test_a_push_proposal_is_refused_logged_and_escalated(env):
    store, jid, wt = env
    notes: list = []
    out, _ = run(store, jid, wt, prop({"type": "push", "params": {}}, {"type": "send_back",
                                                                       "params": {"text": "x"}}), notes=notes)
    assert out[0]["outcome"] == "refused" and len(out) == 1  # nothing after a floor hit runs
    ev = next(e for e in store.events(jid) if e.what == "tech_lead_floor_refused")
    assert ev.evidence["class"] == "outside_world"
    assert [a for a in approvals.list_approvals(store, jid, status="pending") if a["kind"] == "tech_lead"]
    assert notes and notes[0][1] == "NEEDS_YOU"
    assert not [i for i in Steering(store).all_instructions(jid) if i["origin"] == "tech_lead"]


def test_the_floor_refuses_a_push_inside_a_takeover(env):
    store, jid, wt = env
    out, _ = run(store, jid, wt, prop({"type": "takeover", "params": {
        "instructions": "fix it then git push", "files": ["pkg/runtime.py"], "commands": ["git push origin HEAD"]}}))
    assert out[0]["outcome"] == "refused" and out[0]["floor_class"] == "outside_world"
    assert lease.takeover_holder(store, jid) is None and git(wt, "log", "--oneline").count("\n") == 0


def test_judgment_fix_needs_the_policy_confidence(env):
    store, jid, wt = env
    out, _ = run(store, jid, wt, prop({"type": "send_back", "params": {"text": "x"}}, judgment=True,
                                      confidence=0.7))
    assert out[0]["outcome"] == "policy_blocked" and "below" in out[0]["detail"]
    out, _ = run(store, jid, wt, prop({"type": "send_back", "params": {"text": "x"}}, judgment=True,
                                      confidence=0.85))
    assert out[0]["outcome"] == "executed"


def test_shadow_policy_records_would_act_only(env):
    store, jid, wt = env
    out, _ = run(store, jid, wt, prop({"type": "send_back", "params": {"text": "x"}}),
                 policy=tpol.Policy(mode="shadow"))
    assert out[0]["outcome"] == "shadowed" and out[0]["would_act"]["type"] == "send_back"
    assert not [i for i in Steering(store).all_instructions(jid) if i["origin"] == "tech_lead"]


# -- unblock ---------------------------------------------------------------------------------------------------

def test_unblock_clear_approves_within_policy_and_says_proceed(env):
    store, jid, wt = env
    rec = approvals.open_approval(store, jid, "worker permission: Write(notes.md)", kind="permission")
    out, _ = run(store, jid, wt, prop({"type": "unblock", "params": {
        "approval_id": rec["id"], "decision": "clear", "text": "notes.md is inside the contract; write it"}}))
    assert out[0]["outcome"] == "executed"
    got = approvals.get_approval(store, jid, rec["id"])
    assert got["status"] == "approved" and got["decided_by"] == "tech_lead:astra-tech-lead"
    steer = [i for i in Steering(store).all_instructions(jid) if i["origin"] == "tech_lead"][-1]
    assert steer["text"].startswith("Cleared, proceed:")


def test_unblock_redirect_denies_and_says_out_of_bounds(env):
    store, jid, wt = env
    rec = approvals.open_approval(store, jid, "worker permission: Bash(curl https://x)", kind="permission")
    run(store, jid, wt, prop({"type": "unblock", "params": {
        "approval_id": rec["id"], "decision": "redirect", "text": "use the vendored copy instead"}}))
    assert approvals.get_approval(store, jid, rec["id"])["status"] == "denied"
    steer = [i for i in Steering(store).all_instructions(jid) if i["origin"] == "tech_lead"][-1]
    assert steer["text"].startswith("Out of bounds, instead:")


def test_unblock_outside_the_permission_scope_does_not_grant(env):
    store, jid, wt = env
    rec = approvals.open_approval(store, jid, "worker permission: Write(notes.md)", kind="permission")
    run(store, jid, wt, prop({"type": "unblock", "params": {"approval_id": rec["id"], "decision": "clear",
                                                            "text": "go"}}),
        policy=tpol.Policy(permissions="none"))
    assert approvals.get_approval(store, jid, rec["id"])["status"] == "pending"


def test_unblock_never_decides_an_acceptance(env):
    store, jid, wt = env
    rec = approvals.open_approval(store, jid, approvals.ACCEPTANCE_QUESTION, kind="acceptance")
    out, _ = run(store, jid, wt, prop({"type": "unblock", "params": {"approval_id": rec["id"], "decision": "clear",
                                                                     "text": "accept it"}}))
    assert out[0]["outcome"] == "refused" and out[0]["floor_class"] == "final_decision"
    assert approvals.get_approval(store, jid, rec["id"])["status"] == "pending"


# -- send_back and escalate ------------------------------------------------------------------------------------

def test_send_back_names_the_first_failing_evidence(env):
    store, jid, wt = env
    run(store, jid, wt, prop({"type": "send_back", "params": {
        "text": "Remove the owner's name from the comment in pkg/runtime.py",
        "evidence": "tests/test_owner_sweep.py::test_owner_name_never_in_package"}}))
    steer = [i for i in Steering(store).all_instructions(jid) if i["origin"] == "tech_lead"][-1]
    assert "first failing evidence: tests/test_owner_sweep.py::test_owner_name_never_in_package" in steer["text"]


@pytest.mark.legacy_ownership_off  # Plan 1 G12: freeform escalation is replaced by an exact owner decision
def test_escalate_opens_needs_you_with_the_reason(env):
    store, jid, wt = env
    notes: list = []
    run(store, jid, wt, prop({"type": "escalate", "params": {"reason": "the contract contradicts the plan"}}),
        notes=notes)
    pend = [a for a in approvals.list_approvals(store, jid, status="pending") if a["kind"] == "tech_lead"]
    assert pend and "contradicts" in pend[0]["question"] and notes[0][1] == "NEEDS_YOU"
    assert store.get_job(jid).state == JobState.RUNNING  # the job keeps running


# -- takeover --------------------------------------------------------------------------------------------------

def test_takeover_one_writer_edit_commit_release_resume(env):
    store, jid, wt = env
    worker = Worker()
    before = git(wt, "rev-parse", "HEAD")
    out, _ = run(store, jid, wt, prop({"type": "takeover", "params": {
        "instructions": "replace 'owner' with 'operator' in the comment", "files": ["pkg/runtime.py"],
        "summary": "remove owner name from 1 comment"}}), worker=worker)
    assert worker.interrupted == 1  # paused before the lease
    done = out[-1]
    assert done["outcome"] == "executed" and done["lines"] == 2 and done["files"] == ["pkg/runtime.py"]
    assert git(wt, "rev-parse", "HEAD") != before and "operator" in (wt / "pkg" / "runtime.py").read_text()
    assert git(wt, "log", "-1", "--format=%an %s").startswith("Steward tech lead tech lead (astra-tech-lead) takeover")
    assert lease.takeover_holder(store, jid) is None  # released
    whats = [e.what for e in store.events(jid)]
    assert whats.index("takeover lease acquired") < whats.index("takeover lease released")
    note = [i for i in Steering(store).all_instructions(jid) if i["origin"] == "tech_lead"][-1]
    assert note["text"].startswith("Tech lead (astra-tech-lead) took over and committed")


def test_takeover_never_overlaps_a_live_writer(env):
    store, jid, wt = env
    out, _ = run(store, jid, wt, prop({"type": "takeover", "params": {"instructions": "fix the comment",
                                                                      "files": ["pkg/runtime.py"]}}), writing=True)
    assert out[0]["outcome"] == "executed" and out[0]["action"] == "send_back"  # downgraded, not overlapped
    assert any(e.what == "tech_lead_action" and e.evidence["outcome"] == "downgraded" for e in store.events(jid))
    assert git(wt, "status", "--porcelain") == "" and lease.takeover_holder(store, jid) is None


def test_a_second_takeover_cannot_take_a_held_worktree(env):
    store, jid, wt = env
    lease.takeover_acquire(store, jid, "tech_lead:other", worker_writing=False)
    with pytest.raises(lease.LeaseHeld):
        lease.takeover_acquire(store, jid, "tech_lead:astra", worker_writing=False)
    with pytest.raises(lease.LeaseHeld):
        lease.acquire(store, jid, "worker-2")  # no new worker attempt while the tech lead writes
    lease.takeover_release(store, jid, "tech_lead:other")


def test_too_big_a_diff_is_undone_and_becomes_a_send_back(env):
    store, jid, wt = env
    before = git(wt, "rev-parse", "HEAD")

    def big(w):
        (w / "pkg" / "runtime.py").write_text("".join(f"Y{i} = {i}\n" for i in range(40)))
    out, _ = run(store, jid, wt, prop({"type": "takeover", "params": {"instructions": "rewrite runtime.py",
                                                                      "files": ["pkg/runtime.py"]}}), edit=big)
    assert out[-1]["outcome"] == "executed" and out[-1]["action"] == "send_back"
    assert git(wt, "rev-parse", "HEAD") == before and git(wt, "status", "--porcelain") == ""
    assert lease.takeover_holder(store, jid) is None


def test_a_takeover_that_weakens_tests_is_reset_and_escalated(env):
    store, jid, wt = env
    before = git(wt, "rev-parse", "HEAD")

    def weaken(w):
        (w / "tests" / "test_a.py").unlink()
    out, _ = run(store, jid, wt, prop({"type": "takeover", "params": {"instructions": "x",
                                                                      "files": ["tests/test_a.py"]}}), edit=weaken)
    assert out[-1]["outcome"] == "refused" and out[-1]["floor_class"] == "test_weakening"
    assert git(wt, "rev-parse", "HEAD") == before and (wt / "tests" / "test_a.py").is_file()


def test_takeover_off_in_policy_becomes_send_back(env):
    store, jid, wt = env
    out, _ = run(store, jid, wt, prop({"type": "takeover", "params": {"instructions": "x",
                                                                      "files": ["pkg/runtime.py"]}}),
                 policy=tpol.Policy(takeover=False))
    assert out[0]["action"] == "send_back"


def test_takeover_invocation_never_fakes_a_capability():
    import inspect

    src = inspect.getsource(tla.invoke_takeover_via_runtime)
    assert '"--dangerously' not in src and "workspace-write" in src and "Bash" not in src.split("allowedTools")[1][:60]


# -- precision -------------------------------------------------------------------------------------------------

@pytest.mark.legacy_ownership_off  # Plan 1 G12: freeform escalation is no longer an executed owner decision
def test_precision_is_scored_per_action(env):
    store, jid, wt = env
    run(store, jid, wt, prop({"type": "send_back", "params": {"text": "x"}}))
    run(store, jid, wt, prop({"type": "escalate", "params": {"reason": "y"}}))
    assert tla.score(store, jid)["by_action"]["send_back"]["pending"] == 1
    tla.resolve_precision(store, jid, ["x"])  # the same fact kind came back: the actions were wrong
    s = tla.score(store, jid)
    assert s["wrong"] == 2 and s["precision"] == 0.0
    run(store, jid, wt, prop({"type": "send_back", "params": {"text": "z"}}))
    tla.resolve_precision(store, jid, [], passed=True)
    assert tla.score(store, jid)["correct"] == 1


def test_job_lead_executes_through_the_same_gate(env):
    """End to end through JobTechLead: fact -> review -> takeover -> commit -> note."""
    import time

    from steward import code_facts
    from steward import fleet_runtime as rt
    from steward import fleet_tech_lead as tl

    store, jid, wt = env
    arb = rt.Arbiter(store)

    def takeover_invoke(entry, prompt, worktree, timeout):
        (worktree / "pkg" / "runtime.py").write_text("# the operator's switch\nX = 1\n")
        return "ok"
    ex = tla.ActionExecutor(store, runner_exec=SubprocessRunner(), takeover_invoke=takeover_invoke)
    answer = prop({"type": "takeover", "params": {"instructions": "owner -> operator", "files": ["pkg/runtime.py"],
                                                  "summary": "remove owner name"}}, signature="owner_name_leak")
    lead = tl.JobTechLead(store, [ASTRA, FABLE], ex, arbiter=arb, invoke=lambda e, p, t: answer,
                          available=lambda e: None, policy_fn=lambda: tpol.Policy())
    arb.submit(jid, code_facts.SOURCE, "new_failures_repeated", key="k", test="t::owner")
    lead.on_event(jid)
    for _ in range(300):
        res = lead.on_event(jid)
        if res["status"] == "takeover_finished":
            break
        time.sleep(0.01)
    assert res["status"] == "takeover_finished" and res["result"]["outcome"] == "executed"
    assert "operator" in (wt / "pkg" / "runtime.py").read_text()
    assert any(e.what == "tech_lead_answered" and e.evidence["answered_by"] == "astra-tech-lead"
               for e in store.events(jid))


def test_tech_lead_decisions_and_steers_are_never_recorded_as_human(env):
    store, jid, wt = env
    rec = approvals.open_approval(store, jid, "worker permission: Write(notes.md)", kind="permission")
    run(store, jid, wt, prop({"type": "unblock", "params": {"approval_id": rec["id"], "decision": "clear",
                                                            "text": "go"}}))
    decided = next(e for e in store.events(jid) if e.what == "approval approved")
    assert decided.judgment == Judgment.model_informed_code_authorized
    steer = next(e for e in store.events(jid) if e.what.endswith("instruction recorded")
                 and e.evidence.get("origin") == "tech_lead")
    assert steer.judgment == Judgment.model_informed_code_authorized


def test_a_takeover_counts_once_for_precision_and_checks_passing_resolves_it(env):
    import time

    from steward import code_facts
    from steward import fleet_runtime as rt
    from steward import fleet_tech_lead as tl

    store, jid, wt = env
    run(store, jid, wt, prop({"type": "takeover", "params": {"instructions": "owner -> operator",
                                                             "files": ["pkg/runtime.py"]}}))
    assert tla.score(store, jid)["by_action"]["takeover"] == {"correct": 0, "wrong": 0, "pending": 1}
    lead = tl.JobTechLead(store, [ASTRA], tla.ActionExecutor(store, runner_exec=SubprocessRunner()),
                          arbiter=rt.Arbiter(store), invoke=lambda e, p, t: None, available=lambda e: None,
                          policy_fn=lambda: tpol.Policy())
    store.append_event(jid, what="checks passed at exact commit", evidence={"commit_sha": "x"},
                       rule="checks_pass_at_sha", judgment=Judgment.code)
    lead.on_event(jid)
    assert tla.score(store, jid)["correct"] == 1 and tla.score(store, jid)["precision"] == 1.0
    _ = (code_facts, time)


# -- v2.7 Phase 41: rule_completion --------------------------------------------------------------------------------------


def _rule(**p):
    return {"type": "rule_completion", "params": {"ruling": "finished", "sha": "abc", "evidence": [], **p}}


def test_rule_completion_parses_and_is_not_offered_to_ordinary_reviews():
    assert tla.parse_job_proposal(prop(_rule())).actions[0].type == "rule_completion"
    assert "rule_completion" in tla.JOB_ACTION_TYPES and "rule_completion" not in tla.ORDINARY_ACTION_TYPES
    with pytest.raises(tla.ProposalError):
        tla.parse_job_proposal(prop(_rule(ruling="accepted")))


def test_rule_completion_is_refused_outside_a_completion_review(env):
    store, jid, wt = env
    out, _ = run(store, jid, wt, prop(_rule()))  # facts=[{"kind": "x"}]: an ordinary review
    assert out[0]["outcome"] == "refused" and "completion review" in out[0]["detail"]


@pytest.mark.parametrize("kind", ["accept", "merge", "incorporate", "release"])
def test_final_decisions_are_still_refused_as_final_decision(env, kind):
    store, jid, wt = env
    out, _ = run(store, jid, wt, prop({"type": kind, "params": {}}))
    assert out[0]["outcome"] == "refused" and out[0]["floor_class"] == "final_decision"
    assert "rule_completion" not in floor._FINAL_TYPES


# -- mechanics item 11: a blockerless sweep answered with `wait` is an answer, not a malformed proposal -----------

# the reply both leads gave on job f257aa7b's sweeps (rejected as "malformed: bad_field" -> review unavailable)
_SWEEP_WAIT = {"type": "wait", "params": {"blocker_id": None, "until": "evidence_change", "minutes": 30,
                                          "reason": "full suite for the candidate is running; review when it ends"}}


def test_a_sweep_wait_without_a_blocker_parses():
    p = tla.parse_job_proposal(prop(_SWEEP_WAIT))
    assert p.actions[0].type == "wait" and "blocker_id" not in p.actions[0].params
    with pytest.raises(tla.ProposalError) as exc:  # a present blocker id is still validated
        tla.parse_job_proposal(prop({"type": "wait", "params": {"blocker_id": "x" * 41, "until": "time",
                                                                "minutes": 5}}))
    assert exc.value.reason == "bad_field" and "wait blocker_id" in str(exc.value)


def test_a_sweep_wait_without_a_blocker_executes_as_a_no_op(env):
    store, jid, wt = env
    (res,) = run(store, jid, wt, prop(_SWEEP_WAIT))[0]
    assert res["outcome"] == "executed" and "nothing to act on" in res["detail"]
    assert store.get_job(jid).state == JobState.RUNNING


def test_job_lead_sweep_wait_is_answered_not_unavailable(env, monkeypatch):
    import time

    from steward import fleet_runtime as rt
    from steward import fleet_tech_lead as tl

    monkeypatch.setenv("STEWARD_OWNERSHIP", "1")
    store, jid, wt = env
    ex = tla.ActionExecutor(store, runner_exec=SubprocessRunner())
    lead = tl.JobTechLead(store, [ASTRA, FABLE], ex, arbiter=rt.Arbiter(store),
                          invoke=lambda e, p, t: prop(_SWEEP_WAIT), available=lambda e: None,
                          policy_fn=lambda: tpol.Policy())
    res = lead.on_event(jid, sweep=True, reason="outer_loop")
    assert res["status"] == "review_started"
    for _ in range(300):
        res = lead.on_event(jid)
        if res["status"] != "review_in_flight":
            break
        time.sleep(0.01)
    whats = [e.what for e in store.events(jid)]
    assert "tech_lead_review_unavailable" not in whats and "tech_lead_answered" in whats
    (act,) = [e for e in store.events(jid) if e.what == "tech_lead_action"]
    assert act.evidence["action"] == "wait" and act.evidence["outcome"] == "executed"
