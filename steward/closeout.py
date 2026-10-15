"""Closeout policy: WORKER_FINISHED -> CHECKS_PASSED -> ACCEPTED, or NEEDS_HUMAN (SP-16, SP-17).

Every step here is `judgment=code`: verifying at the exact commit and
checking for test-weakening are deterministic. When nothing is left
ambiguous, a *pre-authorized local acceptance* is still a code decision, not
an invented completion — it only fires when there is no weakening, no hard
constraint violation, no active review-enforcement constraint, and the
contract has at least one `done_when` criterion (SP-7's "inferred acceptance
criteria stay visible"). A dirty worktree or a new failure sends the job
back to `RUNNING` with a guidance revision rather than spending a *worker*
recovery (SP-13's budget is separate) — a small, independent `verify_rounds`
budget (max 2) escalates to `NEEDS_HUMAN` once exhausted. Release is never
automatic (SP-17): `accept`/`release` are the only paths into
`ACCEPTED`-from-`NEEDS_HUMAN` and `ACCEPTED`->`RELEASED`, and both require
`Judgment.human` (enforced again by `steward.machine`).
"""

from __future__ import annotations

import re
import time

from steward import owner
from steward.contract import Contract
from steward.machine import IllegalTransition
from steward.models import JobState, Judgment
from steward.steering import Steering
from steward.store import Store
from steward.verify import Verification
from steward.weakening import Finding

_VERIFY_ROUNDS_KEY = "verify_rounds"
UNVERIFIED_WHAT = "checks unverified; acceptance unresolved"
CANDIDATE_READY_WHAT = "deadline_candidate_ready"
UNATTRIBUTED_WHAT = "no baseline to compare; verification unattributed"
REATTRIBUTED_WHAT = "verification re-attributed against the finished baseline"
UNRESOLVED_WHATS = ("checks passed; acceptance unresolved", UNVERIFIED_WHAT, CANDIDATE_READY_WHAT)


def acceptance_mode() -> str:
    """`auto` (pre-authorized local acceptance, the v1 default) or `human`
    (workbench posture, owner decision 2026-09-28: passing checks are shown to the owner,
    who accepts). $STEWARD_ACCEPTANCE > steward.toml `acceptance` > auto."""
    import os
    import tomllib

    from steward.store import resolve_config_dir

    env = os.environ.get("STEWARD_ACCEPTANCE", "").strip().lower()
    if env in ("auto", "human"):
        return env
    path = resolve_config_dir() / "steward.toml"
    try:
        value = str(tomllib.loads(path.read_text()).get("acceptance", "auto")).lower()
    except (OSError, ValueError):
        return "auto"
    return value if value in ("auto", "human") else "auto"
_MAX_VERIFY_ROUNDS = 2


def closeout(
    store: Store,
    job_id: str,
    verification: Verification,
    weakening: list[Finding],
    contract: Contract | None,
) -> JobState:
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(f"unknown job {job_id}")
    if job.state != JobState.WORKER_FINISHED:
        raise ValueError(f"closeout called from {job.state}, expected WORKER_FINISHED")

    if verification.unattributed and not verification.dirty and not verification.unverified:
        # the baseline is still running (verification waited its full bound): blaming the worker for failures that
        # may be pre-existing would be a guess, so park; `reattribute_after_baseline` resumes this when it lands
        store.set_state(
            job_id,
            JobState.NEEDS_HUMAN,
            what=UNATTRIBUTED_WHAT,
            evidence={"reasons": ["no baseline to compare: the background baseline had not finished; "
                                  "failures are re-attributed automatically when it does"],
                      "commit_sha": verification.commit_sha},
            rule="acceptance_unresolved",
            judgment=Judgment.code,
        )
        return JobState.NEEDS_HUMAN

    if verification.dirty or verification.new_failures:
        return _send_back_to_running(store, job_id, verification)

    if verification.unverified:
        # The check command itself is broken (not found / pytest usage or collection error): nothing was
        # actually verified, so this never counts as "checks passed" and never auto-accepts.
        store.set_state(
            job_id,
            JobState.NEEDS_HUMAN,
            what=UNVERIFIED_WHAT,
            evidence={
                "reasons": [f"tests unverified: {verification.unverified_reason}"],
                "unverified": verification.broken,
                "commit_sha": verification.commit_sha,
            },
            rule="acceptance_unresolved",
            judgment=Judgment.code,
        )
        return JobState.NEEDS_HUMAN

    store.set_state(
        job_id,
        JobState.CHECKS_PASSED,
        what="checks passed at exact commit",
        evidence={
            "commit_sha": verification.commit_sha,
            "fixed": verification.fixed,
            "preexisting_failures": verification.preexisting_failures,
        },
        rule="checks_pass_at_sha",
        judgment=Judgment.code,
    )
    from steward import recovery as _recovery

    if _recovery._ownership_on():
        from steward import attempts

        attempts.progressed(store, job_id, "verification:rounds", detail={"commit_sha": verification.commit_sha},
                            now=time.time())
    verdict = _deadline_verdict(store, job_id, verification.commit_sha)
    if not (verdict is not None and verdict.get("plan_in_diff")):
        store.kv_set(job_id, _VERIFY_ROUNDS_KEY, 0)  # a plan-file send-back spends the verify-round budget
    try:
        from steward import verify_reuse

        verify_reuse.record(store, job_id, verification)  # latency #4: a merge of identical content reuses this
    except Exception:  # noqa: BLE001 - bookkeeping; a missing receipt only means the merge re-tests
        pass

    if verdict is not None and verdict.get("plan_in_diff"):
        # DL-05: the transient plan file must not ship; the worker removes it in a cleanup commit
        return _send_back_to_running(store, job_id, verification,
                                     reason=f"deadline plan file still in the final diff: {verdict['reasons'][0]}")
    results_ok = bool(verification.results) or verification.docs_only
    parked = _decide_acceptance(store, job_id, weakening, contract, verification.preexisting_failures, verdict,
                                commit_sha=verification.commit_sha, results_ok=results_ok)
    if parked is not None:
        return parked  # a hard block (weakening, no checks ran, baseline failures, deadline...): no ruling is bought
    request_ruling(store, job_id, verification, weakening)
    return JobState.CHECKS_PASSED


def reattribute_after_baseline(store: Store, job_id: str, contract: Contract | None,
                               weakening: list[Finding] | None = None) -> JobState | None:
    """A verification ran before the background baseline finished and parked the job (`UNATTRIBUTED_WHAT`); now the
    baseline is `done`: re-attribute the stored verify results against it (no second pytest run), once, and route the
    job through the normal closeout rules. None when there is nothing to do."""
    from steward import bg_baseline

    job = store.get_job(job_id)
    rec = store.kv_get(job_id, bg_baseline.KEY)
    if job is None or job.state != JobState.NEEDS_HUMAN or not isinstance(rec, dict) or rec.get("status") != "done":
        return None
    events = store.events(job_id)
    parked = next((i for i in range(len(events) - 1, -1, -1) if events[i].what == UNATTRIBUTED_WHAT), None)
    if parked is None or any(e.what == REATTRIBUTED_WHAT for e in events[parked:]):
        return None
    last = next((e for e in reversed(events[:parked]) if e.what == "verification recorded"), None)
    sha = (last.evidence.get("commit_sha") if last else None)
    base = {c["command"]: c for c in store.get_checks(job_id, label="baseline")}
    if not sha or not base:
        return None
    latest = {c["command"]: c for c in store.get_checks(job_id, label="verify") if c["commit_sha"] == sha}
    new, fixed, pre = [], [], []

    def classify(name: str, now_failed: bool, was_failed: bool) -> None:
        if now_failed and was_failed:
            pre.append(name)
        elif now_failed:
            new.append(name)
        elif was_failed:
            fixed.append(name)

    for cmd, check in latest.items():
        was = base.get(cmd)
        if check["results"]:
            before = was["results"] if was else {}
            for name, outcome in check["results"].items():
                classify(name, outcome in ("failed", "error"), before.get(name) in ("failed", "error"))
        else:
            classify(cmd, check["exit_code"] != 0, bool(was and was["exit_code"] != 0))
    store.append_event(job_id, what=REATTRIBUTED_WHAT, rule="baseline_reattribution", judgment=Judgment.code,
                       evidence={"commit_sha": sha, "new_failures": new, "preexisting_failures": pre, "fixed": fixed})
    store.set_state(job_id, JobState.RUNNING, what="baseline landed; re-attributing the parked verification",
                    evidence={"commit_sha": sha}, rule="baseline_reattribution", judgment=Judgment.code)
    store.set_state(job_id, JobState.WORKER_FINISHED, what="re-attribution against the finished baseline",
                    evidence={"commit_sha": sha}, rule="baseline_reattribution", judgment=Judgment.code)
    from steward.baseline import CheckResult

    ran = [CheckResult(command=c["command"], exit_code=int(c["exit_code"] or 0), tests=dict(c["results"] or {}))
           for c in latest.values()]
    return closeout(store, job_id, Verification(commit_sha=sha, results=ran, new_failures=new, fixed=fixed,
                                                preexisting_failures=pre), weakening or [], contract)


def _is_fleet(store: Store, job_id: str) -> bool:
    try:
        from steward import fleet_closeout

        return fleet_closeout.is_fleet(store, job_id)
    except Exception:  # noqa: BLE001 - a store without fleet tables is a legacy job
        return False


def _deadline_verdict(store: Store, job_id: str, sha: str) -> dict | None:
    try:
        from steward import deadline, deadline_evidence

        if deadline.merge_protected(store, job_id) is None:
            return None
        return deadline_evidence.verdict_for(store, job_id, sha) or {"ready": False, "reasons": [], "missing": True}
    except Exception as exc:  # noqa: BLE001 - unsure is never ready
        return {"ready": False, "reasons": [f"evidence_error: {str(exc)[:120]}"]}


def _send_back_to_running(store: Store, job_id: str, verification: Verification, reason: str | None = None) -> JobState:
    rounds = store.kv_get(job_id, _VERIFY_ROUNDS_KEY, default=0) + 1
    store.kv_set(job_id, _VERIFY_ROUNDS_KEY, rounds)
    from steward import recovery as _recovery

    ownership_on = _recovery._ownership_on()
    if ownership_on:
        from steward import attempts

        attempts.fail_pending(store, job_id, "verification:rounds", detail={"round": rounds}, now=time.time())

    reason = reason or (
        "worktree dirty (worker must commit)"
        if verification.dirty
        # v2.6 (SUP-02): the worker gets the first failing test by name, not just a list to search
        else f"first failing test {verification.new_failures[0]}; new failures: {verification.new_failures}"
    )
    said = _first_failure_message(store, job_id, verification)
    if said and not verification.dirty and reason.startswith("first failing test"):
        reason += f" (it said: {said})"

    if rounds > _MAX_VERIFY_ROUNDS:
        store.set_state(
            job_id,
            JobState.NEEDS_HUMAN,
            what="verify round budget exhausted",
            evidence={"reason": reason, "verify_rounds": rounds},
            rule="verify_rounds_exhausted",
            judgment=Judgment.code,
        )
        if ownership_on:
            from steward import ownership

            ownership.request_strategy_review(store, job_id, blocker_id=None,
                                              reason=f"verification: {rounds - 1} unproductive send-backs ({reason})",
                                              signature="verification:rounds", now=time.time())
        return JobState.NEEDS_HUMAN

    Steering(store).record(
        job_id,
        "nudge",
        f"verification failed: {reason}; fix, commit your changes, and re-report STEWARD-DONE",
        origin="steward",
    )
    if ownership_on:
        from steward import attempts
        from steward.actor import Actor, ActorRef

        attempts.record(store, job_id, issue_key="verification:rounds", actor=ActorRef(Actor.CONTROLLER, "closeout"),
                        hypothesis=f"the worker can fix: {reason}"[:400], change="send back with the first failure",
                        success_observation="checks pass at the exact commit", evidence=[f"verify_round:{rounds}"],
                        now=time.time())
    store.set_state(
        job_id,
        JobState.RUNNING,
        what="verification failed; returning to running with guidance",
        evidence={"reason": reason, "verify_rounds": rounds},
        rule="verify_round_returned_to_running",
        judgment=Judgment.code,
    )
    return JobState.RUNNING


def _first_failure_message(store: Store, job_id: str, verification: Verification) -> str:
    """What the first failing test said, from the latest verification event at this SHA (v2.6)."""
    if not verification.new_failures:
        return ""
    for e in reversed(store.events(job_id)[-50:]):
        if e.what == "verification recorded" and e.evidence.get("commit_sha") == verification.commit_sha:
            return str((e.evidence.get("failure_messages") or {}).get(verification.new_failures[0]) or "")[:200]
    return ""


DEADLINE_PENDING = "deadline verification pending"


def deadline_acceptance_gate(store: Store, job_id: str) -> str | None:
    """DL-03 Gate 1: code never accepts a deadline-protected job (its own or an ancestor's `deadline_ever`).
    Passing checks route to NEEDS_HUMAN; until per-item evidence passes (Phase 21) the reason is
    `deadline verification pending`, never `deadline_candidate_ready`."""
    from steward import deadline

    try:
        why = deadline.merge_protected(store, job_id)
    except Exception:  # noqa: BLE001 - unsure means protected
        why = "deadline_mode"
    return f"{DEADLINE_PENDING} ({why}: {owner.name()} decides acceptance and merge)" if why else None


_EXCEPT_RE = re.compile(r"excepted|not yours|pre-?existing|baseline failures?", re.I)
_EXCEPT_NEAR = 80  # chars either side of a test name / the word "tests" that an exception phrase may sit within


def _contract_excepts(contract: Contract | None, failing: list[str]) -> bool:
    """True when the goal / finish line excepts every remaining baseline failure: an exception phrase
    ("excepted", "not yours", "pre-existing", "baseline failure") sits near that test's name, or near the word
    "tests" (a blanket exception). Anything not covered keeps blocking auto-acceptance."""
    if contract is None:
        return False
    segments = [contract.goal or "", *contract.done_when]  # each sentence judged on its own: no bleed across items

    def near(word: str) -> bool:
        return any(_EXCEPT_RE.search(t[max(0, m.start() - _EXCEPT_NEAR):m.end() + _EXCEPT_NEAR])
                   for t in segments for m in re.finditer(re.escape(word), t, re.I))

    def covered(test: str) -> bool:
        names = {test.split("::")[-1], test.split("::")[0].rsplit(".", 1)[-1].rsplit("/", 1)[-1].removesuffix(".py")}
        return near("tests") or any(n and near(n) for n in names)

    return all(covered(t) for t in failing)


def open_blocking_questions(store: Store, job_id: str) -> list[str]:
    """qids of the job's open blocking questions (steward.questions). An unreadable store reads as none: the
    classifier (yield_lanes) is the fail-closed gate; this is the second, independent one."""
    try:
        from steward import questions
        return [str(r.get("qid")) for r in questions.load(store, job_id)
                if r.get("status") == "open" and r.get("blocking")]
    except Exception:  # noqa: BLE001
        return []


def _decide_acceptance(
    store: Store,
    job_id: str,
    weakening: list[Finding],
    contract: Contract | None,
    remaining_preexisting: list[str] | None = None,
    deadline_verdict: dict | None = None,
    commit_sha: str | None = None,
    results_ok: bool = True,
    ruling: dict | None = None,
) -> JobState | None:
    """Decide what a green verification means. `ruling=None` is the pre-ruling probe: anything that blocks acceptance
    parks the job now (no ruling is bought for it) and a job that nothing blocks returns None (it needs a ruling).
    With a stored `ruling` (`completion_ruling` kv) the code guards run once more and only `finished` plus every guard
    reaches ACCEPTED (`ruling_accepted`, model_informed_code_authorized)."""
    reasons: list[str] = []
    accept_evidence: dict = {}
    if not results_ok:
        reasons.append("no checks ran: the verification has no check results and the diff is not docs-only")
    if remaining_preexisting and acceptance_mode() == "auto" and _contract_excepts(contract, remaining_preexisting):
        # the contract itself says these baseline failures are not this job's to fix: record the waiver, don't park
        accept_evidence["preexisting_waived"] = list(remaining_preexisting)
    elif remaining_preexisting:
        # "No new failures" is not "the checks pass": a goal like "make the tests
        # pass" is unmet while baseline failures remain, so acceptance is unresolved.
        reasons.append(f"{len(remaining_preexisting)} baseline failure(s) still failing: {remaining_preexisting[:5]}")
    if weakening:
        reasons.append(f"test-weakening detected: {[f.kind for f in weakening]}")
    from steward import ownership as _ownership
    if _ownership.enabled():
        from steward import outcome_scope
        reasons += outcome_scope.acceptance_reasons(store, job_id, contract, commit_sha=commit_sha)
    if store.kv_get(job_id, "closeout_blocked", default=False):
        reasons.append("hard constraint violation blocks closeout")
    from steward import replace as replace_module

    replaced = replace_module.refusal(store, job_id, "acceptance")  # issue M: a replaced job never auto-accepts
    if replaced:
        reasons.append(replaced)

    open_q = open_blocking_questions(store, job_id)
    if open_q:  # an unanswered blocking question is the owner's to answer, in every acceptance mode
        reasons.append(f"{len(open_q)} open blocking question(s) for the owner: {open_q[:5]}")

    steering = Steering(store)
    review_constraints = [
        i for i in steering.active_constraints(job_id) if i["enforcement"] == "review"
    ]
    if review_constraints and acceptance_mode() == "auto":
        # A review-enforcement constraint has no deterministic check (contract parsing marks any goal sentence that merely
        # mentions api/interface/compat, and every `goal` steer, as `review`), so under acceptance=auto it only blocks when
        # a constraint was actually VIOLATED. Violations already land in `reasons` above (hard constraint block,
        # test-weakening); what remains is recorded on the acceptance so the owner can see which constraints were waived.
        accept_evidence |= {
            "review_constraints_waived": [i["text"][:160] for i in review_constraints],
            "review_constraints_violated": [],
        }
    elif review_constraints:
        reasons.append(f"{len(review_constraints)} review-enforcement constraint(s) active")

    if contract is None or not contract.done_when:
        reasons.append("contract has no done_when criteria")
    if _is_fleet(store, job_id):
        # FLEET-14: a combined fleet candidate always waits for the human, whatever acceptance_mode() says
        reasons.append(f"fleet candidate verified at the final combined SHA — {owner.name()} accepts "
                       "(acceptance, source incorporation and release are separate human actions)")
    gate = deadline_acceptance_gate(store, job_id)
    if gate and deadline_verdict is not None and deadline_verdict.get("ready") and not reasons:
        # DL-06/DL-14: every required item proven at this exact SHA; the owner accepts and merges, never code
        store.set_state(
            job_id,
            JobState.NEEDS_HUMAN,
            what=CANDIDATE_READY_WHAT,
            evidence={"reasons": [f"deadline candidate ready: every required item evidenced at "
                                  f"{str(deadline_verdict.get('sha'))[:12]}; {owner.name()} accepts and merges"],
                      "sha": deadline_verdict.get("sha"), "optional_dropped": deadline_verdict.get("optional_dropped", [])},
            rule="acceptance_unresolved",
            judgment=Judgment.code,
        )
        return JobState.NEEDS_HUMAN
    if gate:
        if deadline_verdict is None or deadline_verdict.get("missing"):
            reasons.append(gate)
        else:
            reasons.append("acceptance unresolved (deadline): " + "; ".join(deadline_verdict.get("reasons") or
                                                                           ["other acceptance reasons"]))
    if not reasons and acceptance_mode() == "human":
        reasons.append(f"checks passed — {owner.name()} accepts (acceptance mode: human)")

    if reasons:
        store.set_state(
            job_id,
            JobState.NEEDS_HUMAN,
            what="checks passed; acceptance unresolved",
            evidence={"reasons": reasons},
            rule="acceptance_unresolved",
            judgment=Judgment.code,
        )
        return JobState.NEEDS_HUMAN

    if ruling is None:
        return None
    return _apply_ruling(store, job_id, ruling, accept_evidence, commit_sha)


def _park_unresolved(store: Store, job_id: str, reasons: list[str], **extra) -> JobState:
    store.set_state(job_id, JobState.NEEDS_HUMAN, what="checks passed; acceptance unresolved",
                    evidence={"reasons": reasons, **extra}, rule="acceptance_unresolved", judgment=Judgment.code)
    return JobState.NEEDS_HUMAN


def _frozen_proof_reasons(store: Store, job_id: str, sha: str | None) -> list[str]:
    """Phase 45 hook: the frozen outcome->proof map must hold at this SHA. No plan exists yet, so nothing to refuse."""
    return []


def _apply_ruling(store: Store, job_id: str, ruling: dict, accept_evidence: dict, commit_sha: str | None) -> JobState:
    sha = str(commit_sha or ruling.get("sha") or "")
    if ruling.get("status") == "unavailable":
        return _park_unresolved(store, job_id, ["acceptance unresolved: no completion ruling "
                                                f"({str(ruling.get('reason') or 'no lead answered')[:200]})"], sha=sha)
    verdict = ruling.get("ruling")
    if verdict == "not_met":
        said = str(ruling.get("instructions") or "").strip()
        n = int(store.kv_get(job_id, "ruling_not_met_rounds", default=0)) + 1
        store.kv_set(job_id, "ruling_not_met_rounds", n)
        if n > _MAX_VERIFY_ROUNDS:  # green -> not_met -> green must not loop forever
            return _park_unresolved(store, job_id, [f"acceptance unresolved: the tech lead ruled the goal not met {n} "
                                                    f"times ({said[:200] or 'no instructions'})"], sha=sha)
        return _send_back_to_running(
            store, job_id, Verification(commit_sha=sha),
            reason="the tech lead ruled the goal is not met" + (f": {said}" if said else " (see its steer)"))
    if verdict != "finished":
        why = [str(r) for r in (ruling.get("reasons") or [])] or ["the tech lead could not verify completion"]
        return _park_unresolved(store, job_id, ["acceptance unresolved: completion ruling unverified"] + why[:6],
                                sha=sha, ruling_id=ruling.get("id"))
    guards: list[str] = []
    verify_rows = [c for c in store.get_checks(job_id, label="verify")]
    verified = verify_rows[-1]["commit_sha"] if verify_rows else None
    said_sha = ruling.get("ruling_sha")
    if said_sha != sha or (verified is not None and verified != said_sha):
        guards.append(f"ruling sha {str(said_sha)[:12]} is not the verified sha {str(verified)[:12]}")
    open_q = open_blocking_questions(store, job_id)
    if open_q:
        guards.append(f"{len(open_q)} open blocking question(s) for the owner: {open_q[:5]}")
    guards += _frozen_proof_reasons(store, job_id, sha)
    if guards:
        return _park_unresolved(store, job_id, ["acceptance unresolved: completion guard failed"] + guards,
                                sha=sha, ruling_id=ruling.get("id"))

    if _has_no_change(store, job_id):
        return release_no_change(store, job_id, judgment=Judgment.code)

    store.set_state(
        job_id,
        JobState.ACCEPTED,
        what="completion ruled finished; code guards held",
        evidence={**accept_evidence, "ruling_id": ruling.get("id"), "ruling_lead": ruling.get("lead"),
                  "ruling_sha": sha, "evidence": ruling.get("evidence")},
        rule="ruling_accepted",
        judgment=Judgment.model_informed_code_authorized,
    )
    return JobState.ACCEPTED


def request_ruling(store: Store, job_id: str, verification: Verification, weakening: list[Finding]) -> dict:
    """Green verification, nothing blocks acceptance: record that exactly one completion ruling is wanted for this SHA.
    The tech lead's review (`fleet_tech_lead.JobTechLead`) answers it; `decide_after_ruling` consumes the answer."""
    from steward import ruling as ruling_module

    rec = {"status": "requested", "sha": verification.commit_sha, "trigger": "checks_passed",
           "requested_at": time.time(), "results_n": len(verification.results), "docs_only": verification.docs_only,
           "weakening": [{"path": f.path, "kind": f.kind, "detail": f.detail} for f in weakening],
           "preexisting": list(verification.preexisting_failures)}
    store.kv_set(job_id, ruling_module.KEY, rec)
    store.append_event(job_id, what="completion_ruling_requested",
                       evidence={"sha": verification.commit_sha, "results_n": rec["results_n"],
                                 "docs_only": rec["docs_only"]},
                       rule="completion_ruling", judgment=Judgment.code)
    return rec


def record_ruling(store: Store, job_id: str, validated, *, lead: str = "", proposal: str = "") -> dict | None:
    """Store the validated ruling answer (`ruling.Validated`) on the pending request: the runner tick then calls
    `decide_after_ruling`. None when no ruling is wanted (the job moved on or the SHA is not the requested one)."""
    import uuid

    from steward import ruling as ruling_module

    rec = store.kv_get(job_id, ruling_module.KEY)
    if ruling_pending(store, job_id) not in ("requested", "in_review") or not isinstance(rec, dict):
        return None
    out = {**rec, "status": "ruled", "id": "rul-" + uuid.uuid4().hex[:8], "ruling": validated.ruling,
           "claimed": validated.claimed, "ruling_sha": validated.sha, "reasons": list(validated.reasons),
           "instructions": validated.instructions,
           "evidence": [dict(e) for e in validated.evidence], "lead": lead, "proposal": proposal,
           "ruled_at": time.time()}
    store.kv_set(job_id, ruling_module.KEY, out)
    store.append_event(job_id, what="completion_ruled",
                       evidence={"ruling_id": out["id"], "ruling": validated.ruling, "claimed": validated.claimed,
                                 "reasons": list(validated.reasons)[:6], "lead": lead, "sha": rec.get("sha"),
                                 "refs": sum(len(e["refs"]) for e in validated.evidence)},
                       rule="completion_ruling", judgment=Judgment.model)
    if validated.ruling == "unverified":  # the score hook (Phase 46 finishes it): a lead whose claim code rejected is debited
        store.append_event(job_id, what="completion_ruling_debit",
                           evidence={"lead": lead, "claimed": validated.claimed, "reasons": list(validated.reasons)[:6]},
                           rule="completion_ruling", judgment=Judgment.code)
    return out


def ruling_pending(store: Store, job_id: str) -> str | None:
    """The status of a completion ruling the job is waiting on (`requested`, `in_review`, `ruled`, `unavailable`)."""
    from steward import ruling as ruling_module

    job = store.get_job(job_id)
    rec = store.kv_get(job_id, ruling_module.KEY)
    if job is None or job.state != JobState.CHECKS_PASSED or not isinstance(rec, dict):
        return None
    return rec.get("status") if rec.get("status") in ("requested", "in_review", "ruled", "unavailable") else None


def mark_ruling_unavailable(store: Store, job_id: str, reason: str) -> None:
    from steward import ruling as ruling_module

    rec = store.kv_get(job_id, ruling_module.KEY)
    if isinstance(rec, dict) and rec.get("status") in ("requested", "in_review"):
        store.kv_set(job_id, ruling_module.KEY, {**rec, "status": "unavailable", "reason": reason[:300]})
        store.append_event(job_id, what="completion_ruling_unavailable", evidence={"reason": reason[:300],
                                                                                   "sha": rec.get("sha")},
                           rule="completion_ruling", judgment=Judgment.code)


def decide_after_ruling(store: Store, job_id: str, contract: Contract | None = None) -> JobState | None:
    """The second closeout entry point (runner tick): the ruling is in, so run the acceptance decision with it. None when
    there is no ruling to act on (not CHECKS_PASSED, or still waiting)."""
    from steward import ruling as ruling_module
    from steward.contract import load_contract

    if ruling_pending(store, job_id) not in ("ruled", "unavailable"):
        return None
    rec = store.kv_get(job_id, ruling_module.KEY)
    if contract is None:
        contract = load_contract(store, job_id)
    sha = rec.get("sha")
    findings = [Finding(path=f["path"], kind=f["kind"], detail=f["detail"]) for f in rec.get("weakening") or []]
    verdict = _deadline_verdict(store, job_id, sha)
    final = _decide_acceptance(store, job_id, findings, contract, rec.get("preexisting") or [], verdict, commit_sha=sha,
                               results_ok=bool(rec.get("results_n")) or bool(rec.get("docs_only")), ruling=rec)
    store.kv_set(job_id, ruling_module.KEY, {**rec, "status": "decided", "decided_state": final.value})
    return final


NO_CHANGE_WHAT = "released_no_change"
NO_CHANGE_REASON = "no change"
_NO_CHANGE_EXCERPT = 200


def _has_no_change(store: Store, job_id: str) -> bool:
    """True only when git positively says the job's OWN change (jobdiff) touches no files. An unknown range, a git
    failure or a fleet job (its combined candidate is the human's to rule on) is never "no change"."""
    import subprocess

    from steward import jobdiff

    job = store.get_job(job_id)
    if job is None or _is_fleet(store, job_id):
        return False
    try:
        found = jobdiff.job_diff_range(store, job)
        if found is None:
            return False
        rng, cwd = found
        p = subprocess.run(["git", "diff", "--name-only", rng], cwd=str(cwd), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and not p.stdout.strip()


def worker_reason_excerpt(store: Store, job_id: str) -> str:
    """The worker's final message, one line, capped; empty when no transcript is found."""
    from steward import export, transcript

    try:
        text = export._final_message(transcript.worker_file(store, job_id))
    except Exception:  # noqa: BLE001 — an excerpt is garnish, never a reason to fail the close
        return ""
    return " ".join(text.split())[:_NO_CHANGE_EXCERPT]


def release_no_change(store: Store, job_id: str, *, judgment: Judgment) -> JobState:
    """A finished job with no change of its own is closed, not accepted: nothing to merge, deploy or release."""
    reason = worker_reason_excerpt(store, job_id)
    store.set_state(
        job_id,
        JobState.RELEASED,
        what=NO_CHANGE_WHAT,
        evidence={"reason": NO_CHANGE_REASON, "worker_reason": reason},
        rule="released_no_change",
        judgment=judgment,
    )
    return JobState.RELEASED


def accept(store: Store, job_id: str, *, decided_by: str = "human") -> JobState:
    """Human-only path: NEEDS_HUMAN -> ACCEPTED. Never invoked by code alone."""
    evidence: dict = {}
    from steward import outcome_scope
    from steward import replace as replace_module

    replaced = replace_module.refusal(store, job_id, "accept")  # issue M: every accept route lands here
    if replaced:
        raise IllegalTransition(replaced)
    from steward.actor import Actor, ActorRef, classify, current as current_actor
    from steward.contract import load_contract

    contract = load_contract(store, job_id)
    reasons = outcome_scope.acceptance_reasons(
        store, job_id, contract, commit_sha=outcome_scope.last_verified_commit(store, job_id))
    if reasons:
        who = classify(None, decided_by) if decided_by != "human" else ActorRef(Actor.OWNER, "human")
        if who.kind != Actor.OWNER:
            raise IllegalTransition("required outcomes remain unproven; only the owner may explicitly override them")
        # This explicit owner command is an override, not proof. Keep its scope and actor visible in the accepted row.
        evidence["scope_override"] = {"actor": current_actor() or ActorRef(Actor.OWNER, "explicit_accept").label(),
                                      "outcomes": reasons, "source": "explicit owner accept"}
    if _has_no_change(store, job_id):
        return release_no_change(store, job_id, judgment=Judgment.human)
    store.set_state(
        job_id,
        JobState.ACCEPTED,
        what="operator accepted",
        evidence=evidence,
        rule="operator_accept",
        judgment=Judgment.human,
    )
    return JobState.ACCEPTED


def release(store: Store, job_id: str) -> JobState:
    """Human-only path: ACCEPTED -> RELEASED. Release is never automatic (SP-17).
    Releasing an already-released job is a no-op (a second tap is not an error)."""
    job = store.get_job(job_id)
    if job is not None and job.state == JobState.RELEASED:
        return JobState.RELEASED
    store.set_state(
        job_id,
        JobState.RELEASED,
        what="operator released",
        evidence={},
        rule="operator_release",
        judgment=Judgment.human,
    )
    return JobState.RELEASED
