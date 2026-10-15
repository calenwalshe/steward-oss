"""The question queue, delivery half (Phase 17-03: G5, G6, G7, G8, G9, G13).

`questions.apply` records the owner's answers in the store; this module gets them to the job or the branch, by the job's
state at flush time (G6):

- RUNNING / PAUSED     one Steering revision (verb focus, origin `questions`, never urgent): the worker is the only
                       safe writer of its own worktree (SP-1), so it is never written to.
- NEEDS_HUMAN          one commit `docs(<scope>): record <name> decisions` in the job worktree (no worker is live);
                       with `and_continue` a send-back whose instructions are the answers plus the patch.
- ACCEPTED, unmerged   the same commit, before incorporation.
- merged / RELEASED / STOPPED / FAILED   store only; a merged job's answers wait for the owner-gated `write_source`.
- anything transitional holds and retries at the next flush.

Write-back is batched (G7): the flush happens when no pending answer is newer than BATCH_QUIET_S, or at once when
forced (`--done`). Every function here is idempotent and `tick` never raises. No model, no network.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from steward import exceptions
from steward import incorporate as incorporate_module
from steward import notify
from steward import ownership
from steward import owner
from steward import questions
from steward import sendback as sendback_module
from steward import store as store_module
from steward.models import JobState, Judgment
from steward.question_adapters.base import one_line
from steward.runner_exec import Runner
from steward.steering import Steering
from steward.store import Store

BATCH_QUIET_S = 60.0
STEER_CAP = 6000
COMPACT_MAX_LINES = 80
RETRY_SPACING_S = 60.0
MAX_TRIES = 3
RESCAN_EVERY_S = 30.0
HUMAN_ORIGINS = ("cli", "moshi", "dash", "job-chat")

FAIL_KEY = "questions_flush_failed"
LAST_SKIP_KEY = "questions_last_skip"
SOURCE_UNWRITTEN_KEY = "questions_source_unwritten"
RESCAN_AT_KEY = "questions_rescan_at"
PARKED_AT_KEY = "parked_last_review_at"

_RULE = "operator_question_delivery"
_FALLBACK_IDENTITY = ("-c", "user.name=Steward", "-c", "user.email=steward@localhost")
_HOLD_STATES = (JobState.CREATED, JobState.CONTRACTED, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED,
                JobState.STOPPING)


# -- small helpers -------------------------------------------------------------------------------------------


def _epoch(iso: str | None) -> float:
    try:
        return datetime.fromisoformat(iso or "").timestamp()
    except (TypeError, ValueError):
        return 0.0


def _anchor(rec: dict) -> str:
    return (rec.get("source") or {}).get("anchor", "") or rec.get("qid", "")


def pending(store: Store, job_id: str) -> list[dict]:
    """Answered or deferred BY THE OWNER, not stale, not yet written anywhere."""
    return [r for r, _ in questions._pending(questions.load(store, job_id), None)]


def newest_answer_epoch(records: list[dict]) -> float:
    return max((_epoch((r.get("answer") or {}).get("at")) for r in records), default=0.0)


def delivery_mode(store: Store, job, *, and_continue: bool = False) -> tuple[str, str]:
    """(mode, why): commit | send_back | steer | store_only | hold. `why` names the state or the store-only reason."""
    state = job.state
    if state in (JobState.RUNNING, JobState.PAUSED):
        return "steer", state.value
    if state == JobState.NEEDS_HUMAN:
        return ("send_back" if and_continue else "commit"), state.value
    if state == JobState.ACCEPTED:
        if incorporate_module.is_incorporated(store, job.job_id):
            return "store_only", "merged"
        return "commit", state.value
    if state == JobState.RELEASED:
        return "store_only", "released"
    if state == JobState.STOPPED:
        return "store_only", "stopped"
    if state == JobState.FAILED:
        return "store_only", "failed"
    return "hold", f"state {state.value}"


def _skip(store: Store, job_id: str, reason: str) -> bool:
    """One questions_delivery_skipped event per distinct reason (a repeated flush stays silent)."""
    reason = one_line(reason, 200)
    if store.kv_get(job_id, LAST_SKIP_KEY) == reason:
        return False
    store.kv_set(job_id, LAST_SKIP_KEY, reason)
    store.append_event(job_id, what="questions_delivery_skipped", evidence={"reason": reason},
                       rule=_RULE, judgment=Judgment.code)
    return True


def _failure_gate(store: Store, job_id: str, newest: float, now: float) -> bool:
    """True when retries are exhausted or too soon for this batch (a new answer or --done resets it)."""
    f = store.kv_get(job_id, FAIL_KEY) or {}
    if f.get("newest") != newest:
        return False
    return f.get("count", 0) >= MAX_TRIES or now - f.get("at", 0.0) < RETRY_SPACING_S


def _note_failure(store: Store, job_id: str, newest: float, now: float) -> None:
    f = store.kv_get(job_id, FAIL_KEY) or {}
    count = f.get("count", 0) + 1 if f.get("newest") == newest else 1
    store.kv_set(job_id, FAIL_KEY, {"newest": newest, "count": count, "at": now})


def _clear_failure(store: Store, job_id: str) -> None:
    store.kv_delete(job_id, FAIL_KEY)
    store.kv_delete(job_id, LAST_SKIP_KEY)


def _mark(records: list[dict], qids, written: dict) -> None:
    wanted = set(qids)
    for r in records:
        if r["qid"] in wanted:
            r["written"] = dict(written)


def _mark_store_only(store: Store, job_id: str, qids, why: str) -> list[str]:
    qids = list(qids)
    if not qids:
        return []
    records = questions.load(store, job_id)
    _mark(records, qids, {"via": "store_only", "why": why, "at": store_module.now_iso()})
    questions.save(store, job_id, records)
    return qids


def _add_source_unwritten(store: Store, job_id: str, qids) -> None:
    have = list(store.kv_get(job_id, SOURCE_UNWRITTEN_KEY) or [])
    store.kv_set(job_id, SOURCE_UNWRITTEN_KEY, have + [q for q in qids if q not in have])


def _git(runner: Runner, cwd: Path | str, *args: str, ident: bool = False):
    argv = ["git", *(_FALLBACK_IDENTITY if ident else ()), *args]
    return runner.run(argv, cwd=cwd)


def _commit(runner: Runner, cwd: Path | str, message: str, paths: list[str]) -> tuple[bool, str]:
    """`git commit -m msg -- paths` (pathspec-limited); one retry with a fixed identity when the first try fails
    (typically: no user.name / user.email configured)."""
    first = _git(runner, cwd, "commit", "-q", "-m", message, "--", *paths)
    if first.exit_code == 0:
        return True, ""
    second = _git(runner, cwd, "commit", "-q", "-m", message, "--", *paths, ident=True)
    if second.exit_code == 0:
        return True, ""
    err = (second.stderr or second.stdout or first.stderr or "").strip()
    return False, err


def _write_edits(root: Path, edits) -> list[tuple[Path, str | None]]:
    """Write every edit under root; returns [(path, previous text or None when created)] for the undo."""
    undo: list[tuple[Path, str | None]] = []
    for e in edits:
        target = root / e.path
        prev = target.read_text() if target.exists() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(e.new_text)
        undo.append((target, prev))
    return undo


def _undo_writes(runner: Runner, root: Path, paths: list[str], undo: list[tuple[Path, str | None]]) -> None:
    """Put back what a failed commit left: unstage, restore the previous text, remove what we created."""
    try:
        _git(runner, root, "reset", "-q", "--", *paths)
    except Exception:  # noqa: BLE001
        pass
    for target, prev in undo:
        try:
            if prev is None:
                target.unlink(missing_ok=True)
            else:
                target.write_text(prev)
        except OSError:
            pass


def _use_git(store: Store, job_id: str, evidence: dict) -> str | None:
    try:
        exceptions.use(store, job_id, "git", evidence)
    except exceptions.UnregisteredPath:
        return "git exception path is not registered"
    return None


# -- the steering text (G6, G13) ---------------------------------------------------------------------------------


def _answer_text(rec: dict) -> str:
    a = rec.get("answer") or {}
    if a.get("option") == "other":
        return f'"{a.get("other", "")}"'
    if a.get("option") == "recommended":
        return f"recommended ({rec.get('recommended_text') or 'as written'})"
    keys = [o["key"] for o in rec.get("options") or []]
    label = a.get("label") or ""
    opt = str(a.get("option", ""))
    return f"({opt}) {label}".strip() if opt and all(k in keys for k in opt.split("+")) else opt


def _assumed_text(rec: dict) -> str:
    keys = ", ".join(rec.get("recommended") or [])
    text = rec.get("recommended_text") or "as written"
    return f"({keys}) {text}" if keys else text


def _answer_line(rec: dict) -> list[str]:
    a = rec.get("answer") or {}
    tag = _anchor(rec)
    option = a.get("option")
    starred = bool(rec.get("starred"))
    if option == "defer":
        default = "the recommended default" if starred else "your current default"
        lines = [f"{tag}: DEFERRED - keep proceeding on {default}"]
    elif rec.get("blocking"):
        lines = [f"{tag}: UNBLOCKED - {_answer_text(rec)}"]
    elif option == "recommended" or (option not in ("other", "defer") and a.get("recommended")):
        lines = [f"{tag}: recommended"]
    elif starred:
        lines = [f"{tag}: CHANGE - you were proceeding on the recommended default {_assumed_text(rec)}; "
                 f"{owner.name()} chose {_answer_text(rec)}. Switch to it."]
    else:
        lines = [f"{tag}: {_answer_text(rec)}"]
    if a.get("note"):
        lines.append(f"  note: {a['note']}")
    return lines


def _compact(edits) -> list[str]:
    """The changed lines only (`-old`, `+new`), at most COMPACT_MAX_LINES of them across every file."""
    out: list[str] = []
    total = 0
    budget = COMPACT_MAX_LINES
    for e in edits:
        changed = [ln for ln in (e.diff or "").splitlines()
                   if ln[:1] in ("+", "-") and not ln.startswith(("+++", "---"))]
        total += len(changed)
        out.append(f"--- {e.path}")
        take = changed[:budget]
        budget -= len(take)
        out += take
    if total > COMPACT_MAX_LINES:
        out.append(f"... {total - COMPACT_MAX_LINES} more changed lines not shown")
    return out


def steer_text(plan: "questions.WritebackPlan", records: list[dict]) -> str:
    """The instruction a worker receives: the answers, then the exact patch (or, past STEER_CAP, the compact list of
    changed lines)."""
    lines = [f"{owner.upper()} ANSWERED {len(records)} QUESTION(S) (recorded in Steward)."]
    for rec in records:
        lines += _answer_line(rec)
    if plan.edits:
        lines.append(f"Apply these edits to the source files exactly, commit them as {plan.commit_message}, "
                     "and continue.")
        diffs = "\n".join(e.diff.rstrip("\n") for e in plan.edits if e.diff)
        if len(diffs) > STEER_CAP:
            lines.append("The full diff is too long to send; this is the compact list of changed lines "
                         "(`-` lines are removed, `+` lines are added).")
            lines += _compact(plan.edits)
        else:
            lines.append(diffs)
    return "\n".join(lines)


def _request_id(job_id: str, records: list[dict]) -> str:
    stamp = "\n".join(sorted(f"{r['qid']}@{(r.get('answer') or {}).get('at', '')}" for r in records))
    return f"questions-{job_id[:8]}-{hashlib.sha256(stamp.encode()).hexdigest()[:12]}"


# -- delivery modes ---------------------------------------------------------------------------------------------------


def _result(accepted: bool, mode: str, detail: str, **extra) -> dict:
    return {"accepted": accepted, "mode": mode, "detail": detail, **extra}


def _plan_for(store: Store, job_id: str, runner: Runner, records: list[dict]):
    """Render the pending answers; split off the ones with no deterministic edit (store only) and the ones that
    cannot be rendered right now (their area moved: they wait for the next scan to make them stale or withdrawn)."""
    plan = questions.render_writeback(store, job_id, runner)
    rendered = set(plan.qids)
    reasons = {s["qid"]: s["reason"] for s in plan.skipped}
    no_edit = [r["qid"] for r in records if r["qid"] not in rendered
               and reasons.get(r["qid"], "no deterministic edit") == "no deterministic edit"]
    blocked = {q: why for q, why in reasons.items() if why != "no deterministic edit" and q not in rendered}
    return plan, no_edit, blocked


def _blocked_reason(blocked: dict[str, str], records: list[dict]) -> str:
    anchors = {r["qid"]: _anchor(r) for r in records}
    first = sorted(blocked)[0]
    return f"{blocked[first]}: {anchors.get(first, first)}"


def _deliver_commit(store: Store, job_id: str, runner: Runner, records: list[dict], newest: float, now: float,
                    origin: str) -> dict:
    job = store.get_job(job_id)
    worktree = store.home / "worktrees" / job_id
    if not worktree.is_dir():
        _skip(store, job_id, "no worktree")
        _note_failure(store, job_id, newest, now)
        return _result(False, "commit", "no worktree")
    head_before = _git(runner, worktree, "rev-parse", "HEAD").stdout.strip()
    plan, no_edit, blocked = _plan_for(store, job_id, runner, records)
    if blocked:
        reason = _blocked_reason(blocked, records)
        _skip(store, job_id, reason)
        _note_failure(store, job_id, newest, now)
        return _result(False, "commit", reason)
    stored = _mark_store_only(store, job_id, no_edit, "no deterministic edit")
    if not plan.edits:
        _clear_failure(store, job_id)
        return _result(True, "commit", f"kept {len(stored)} answer(s) in the store (no deterministic edit)",
                       store_only=stored)

    paths = [e.path for e in plan.edits]

    def refuse(reason: str) -> dict:
        _skip(store, job_id, reason)
        _note_failure(store, job_id, newest, now)
        return _result(False, "commit", reason)

    # SP-1: re-read everything a live worker or another writer could have changed, right before the write.
    job = store.get_job(job_id)
    if job is None:
        return refuse("job vanished")
    if job.state == JobState.ACCEPTED:
        if incorporate_module.is_incorporated(store, job_id):
            return refuse("state changed")
    elif job.state != JobState.NEEDS_HUMAN:
        return refuse("state changed")
    if sendback_module.rework_pending(store, job_id):
        return refuse("rework pending")
    if _git(runner, worktree, "rev-parse", "HEAD").stdout.strip() != head_before:
        return refuse("head moved")
    status = _git(runner, worktree, "status", "--porcelain", "-uall", "--", *paths)
    dirty = [ln[3:] for ln in status.stdout.splitlines() if len(ln) > 3]
    if status.exit_code != 0:
        return refuse(f"git status failed: {(status.stderr or '').strip()[:120]}")
    if dirty:
        return refuse(f"dirty {dirty[0]}")
    problem = _use_git(store, job_id, {"action": "record_answers"})
    if problem:
        return refuse(problem)

    undo = _write_edits(worktree, plan.edits)
    add = _git(runner, worktree, "add", "--", *paths)
    if add.exit_code != 0:
        _undo_writes(runner, worktree, paths, undo)
        return refuse(f"git add failed: {(add.stderr or '').strip()[:120]}")
    ok, err = _commit(runner, worktree, plan.commit_message, paths)
    if not ok:
        _undo_writes(runner, worktree, paths, undo)
        return refuse(f"git commit failed: {err[:160]}")
    sha = _git(runner, worktree, "rev-parse", "HEAD").stdout.strip()

    records_now = questions.load(store, job_id)
    _mark(records_now, plan.qids, {"via": "commit", "commit": sha, "at": store_module.now_iso()})
    questions.save(store, job_id, records_now)
    store.append_event(job_id, what="questions_written",
                       evidence={"commit": sha, "qids": list(plan.qids), "paths": paths, "by": origin},
                       rule=_RULE, judgment=Judgment.human)
    _clear_failure(store, job_id)
    questions.scan(store, job_id, runner, force=True)  # the markers are confirmed by the next scan
    return _result(True, "commit", f"committed {sha[:7]} {plan.commit_message} ({len(plan.qids)} answer(s))",
                   commit=sha, qids=list(plan.qids), store_only=stored)


def _steering_rev(store: Store, job_id: str, text: str, receipt) -> int:
    instructions = Steering(store).all_instructions(job_id)
    if not getattr(receipt, "duplicate", False):
        return len(instructions)
    for ins in reversed(instructions):
        if ins.get("origin") == "questions" and ins.get("text") == text:
            return ins["rev"]
    return len(instructions)


def _deliver_steer(store: Store, job_id: str, runner: Runner, records: list[dict], newest: float, now: float,
                   origin: str) -> dict:
    plan, no_edit, blocked = _plan_for(store, job_id, runner, records)
    if blocked:
        reason = _blocked_reason(blocked, records)
        _skip(store, job_id, reason)
        _note_failure(store, job_id, newest, now)
        return _result(False, "steer", reason)
    included = [r for r in records if r["qid"] in set(plan.qids) | set(no_edit)]
    if not included:
        return _result(True, "steer", "nothing to send")
    text = steer_text(plan, included)
    rid = _request_id(job_id, included)
    receipt = Steering(store).record(job_id, "focus", text, request_id=rid, origin="questions")
    if not receipt.accepted:
        _skip(store, job_id, f"steering refused: {receipt.detail}")
        _note_failure(store, job_id, newest, now)
        return _result(False, "steer", receipt.detail)
    rev = _steering_rev(store, job_id, text, receipt)
    records_now = questions.load(store, job_id)
    _mark(records_now, [r["qid"] for r in included],
          {"via": "steering", "rev": rev, "at": store_module.now_iso(), "patch": bool(plan.edits)})
    questions.save(store, job_id, records_now)
    qids = [r["qid"] for r in included]
    if not receipt.duplicate:
        store.append_event(job_id, what="questions_delivered",
                           evidence={"steering_rev": rev, "qids": qids, "mode": "steer", "by": origin},
                           rule=_RULE, judgment=Judgment.human)
    _clear_failure(store, job_id)
    return _result(True, "steer", f"steered rev {rev} ({len(qids)} answer(s))", steering_rev=rev, qids=qids)


def _deliver_send_back(store: Store, job_id: str, runner: Runner, records: list[dict], newest: float, now: float,
                       origin: str, send_back_fn: Callable | None) -> dict:
    if send_back_fn is None:
        return _result(False, "send_back", "no send-back path available")
    plan, no_edit, blocked = _plan_for(store, job_id, runner, records)
    if blocked:
        reason = _blocked_reason(blocked, records)
        _skip(store, job_id, reason)
        _note_failure(store, job_id, newest, now)
        return _result(False, "send_back", reason)
    included = [r for r in records if r["qid"] in set(plan.qids) | set(no_edit)]
    if not included:
        return _result(True, "send_back", "nothing to send")
    text = steer_text(plan, included)
    res = send_back_fn(job_id, text, origin, _request_id(job_id, included))
    if not res.get("accepted"):
        _skip(store, job_id, f"send-back failed: {res.get('detail', '')}")
        _note_failure(store, job_id, newest, now)
        return _result(False, "send_back", str(res.get("detail", "send-back failed")))
    rev = res.get("steering_rev")
    qids = [r["qid"] for r in included]
    records_now = questions.load(store, job_id)
    _mark(records_now, qids, {"via": "send_back", "rev": rev, "at": store_module.now_iso(), "patch": bool(plan.edits)})
    questions.save(store, job_id, records_now)
    store.append_event(job_id, what="questions_delivered",
                       evidence={"steering_rev": rev, "qids": qids, "mode": "send_back", "by": origin},
                       rule=_RULE, judgment=Judgment.human)
    _clear_failure(store, job_id)
    return _result(True, "send_back", f"sent back with the answers (steering rev {rev})", steering_rev=rev, qids=qids,
                   send_back=res)


def _deliver_store_only(store: Store, job_id: str, records: list[dict], why: str) -> dict:
    qids = _mark_store_only(store, job_id, [r["qid"] for r in records], why)
    if why in ("merged", "released"):
        _add_source_unwritten(store, job_id, qids)
    _skip(store, job_id, f"job is {why}: answers kept in the store")
    return _result(True, "store_only", f"kept {len(qids)} answer(s) in the store ({why})", store_only=qids)


# -- flush --------------------------------------------------------------------------------------------------------------


def flush(
    store: Store,
    job_id: str,
    runner: Runner,
    *,
    now: float | None = None,
    force: bool = False,
    and_continue: bool = False,
    origin: str = "cli",
    send_back_fn: Callable | None = None,
) -> dict:
    """Deliver the pending answers by the job's state. Idempotent: what was delivered is marked `written`, so a
    second flush finds nothing. `now` is epoch seconds; the age of a pending answer is `now` minus its `answer.at`."""
    job = store.get_job(job_id)
    if job is None:
        return _result(False, "none", f"unknown job: {job_id}", error_kind="unknown_job")
    records = pending(store, job_id)
    if not records:
        return _result(True, "none", "nothing to write")
    now = time.time() if now is None else now
    newest = newest_answer_epoch(records)
    if not force:
        age = now - newest
        if age < BATCH_QUIET_S:
            return _result(True, "none", "waiting", waiting_s=round(BATCH_QUIET_S - age, 1))
        if _failure_gate(store, job_id, newest, now):
            return _result(True, "none", "retry later")
    mode, why = delivery_mode(store, job, and_continue=and_continue)
    try:
        if mode == "commit":
            return _deliver_commit(store, job_id, runner, records, newest, now, origin)
        if mode == "steer":
            return _deliver_steer(store, job_id, runner, records, newest, now, origin)
        if mode == "send_back":
            return _deliver_send_back(store, job_id, runner, records, newest, now, origin, send_back_fn)
        if mode == "store_only":
            return _deliver_store_only(store, job_id, records, why)
        _skip(store, job_id, why)
        return _result(True, "hold", f"holding: {why}")
    except Exception as exc:  # noqa: BLE001 - a broken repo or runner must not break the caller
        reason = f"error: {type(exc).__name__}: {exc}"
        _skip(store, job_id, reason)
        _note_failure(store, job_id, newest, now)
        return _result(False, mode, reason)


def flush_now(store: Store, job_id: str, runner: Runner | None = None) -> dict:
    """Force the flush at a hand-off (accept, incorporate) instead of waiting for the batch window. Never raises:
    a failed flush leaves the answers saved and must not block the hand-off. Before the merge this commits the answers
    to the job branch (G6 ACCEPTED/NEEDS_HUMAN); after it, what cannot be committed lands in SOURCE_UNWRITTEN_KEY."""
    try:
        from steward.runner_exec import SubprocessRunner
        return flush(store, job_id, runner or SubprocessRunner(), force=True)
    except Exception as exc:  # noqa: BLE001
        return _result(False, "none", f"error: {type(exc).__name__}: {exc}")


# -- the owner-gated write into the source branch (merged and RELEASED jobs) -------------------------------------------


def write_source(store: Store, job_id: str, runner: Runner, *, origin: str = "cli") -> dict:
    """`steward answer JOB --write-source`: commit the answers a merged or released job kept in the store onto the
    source repo's CURRENT branch, from that branch's own HEAD text. Human origins only; pathspec-limited commit; never
    forces, stashes or resets, and refuses (changing nothing) when the repo is mid-operation, has staged changes on
    the target paths or a dirty file overlapping them (T-17-12)."""
    def refuse(reason: str) -> dict:
        return {"accepted": False, "mode": "source", "detail": reason, "error_kind": "refused"}

    if origin not in HUMAN_ORIGINS:
        return refuse("write-source needs a human origin")
    job = store.get_job(job_id)
    if job is None:
        return {"accepted": False, "mode": "source", "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
    qids = list(store.kv_get(job_id, SOURCE_UNWRITTEN_KEY) or [])
    if not qids:
        return {"accepted": True, "mode": "source", "detail": "nothing to write to the source branch"}
    repo = Path(job.repo)
    if not repo.is_dir():
        return refuse(f"source repo {job.repo} is not a directory")
    head = _git(runner, repo, "symbolic-ref", "--short", "-q", "HEAD")
    if head.exit_code != 0 or not head.stdout.strip():
        return refuse("source repo has a detached HEAD (check out a branch first)")
    branch = head.stdout.strip()
    mid = incorporate_module._mid_operation(repo)
    if mid:
        return refuse(f"source repo is mid-{mid}; finish or abort it first")

    plan = questions.render_writeback(store, job_id, runner, qids=qids, cwd=repo, ref="HEAD",
                                      include_store_only=True)
    records = {r["qid"]: r for r in questions.load(store, job_id)}
    if not plan.edits:
        if plan.skipped:
            s = plan.skipped[0]
            return refuse(f"{s['reason']}: {_anchor(records.get(s['qid'], {'qid': s['qid']}))}")
        return refuse("nothing in the source branch to write the answers into")
    paths = [e.path for e in plan.edits]
    dirty, staged = incorporate_module._porcelain(repo)
    for p in paths:
        if p in staged:
            return refuse(f"staged changes on {p}")
    for p in paths:
        if p in dirty:
            return refuse(f"dirty {p}")
    problem = _use_git(store, job_id, {"action": "record_answers_source", "repo": job.repo})
    if problem:
        return refuse(problem)

    undo = _write_edits(repo, plan.edits)
    add = _git(runner, repo, "add", "--", *paths)
    if add.exit_code != 0:
        _undo_writes(runner, repo, paths, undo)
        return refuse(f"git add failed: {(add.stderr or '').strip()[:120]}")
    ok, err = _commit(runner, repo, plan.commit_message, paths)
    if not ok:
        _undo_writes(runner, repo, paths, undo)
        return refuse(f"git commit failed: {err[:160]}")
    sha = _git(runner, repo, "rev-parse", "HEAD").stdout.strip()

    written = set(plan.qids)
    all_records = questions.load(store, job_id)
    _mark(all_records, plan.qids, {"via": "source_branch", "commit": sha, "branch": branch,
                                   "at": store_module.now_iso()})
    questions.save(store, job_id, all_records)
    skipped = {s["qid"] for s in plan.skipped if s["reason"] != "no deterministic edit"}
    store.kv_set(job_id, SOURCE_UNWRITTEN_KEY, [q for q in qids if q in skipped])
    store.append_event(job_id, what="questions_written",
                       evidence={"commit": sha, "qids": sorted(written), "branch": branch, "paths": paths, "by": origin},
                       rule=_RULE, judgment=Judgment.human)
    detail = f"committed {sha[:7]} to {branch}: {plan.commit_message} ({len(written)} answer(s))"
    if skipped:
        detail += f"; {len(skipped)} not written (the text moved)"
    return {"accepted": True, "mode": "source", "detail": detail, "commit": sha, "branch": branch,
            "qids": sorted(written)}


# -- the control pane's flusher (G7) ----------------------------------------------------------------------------------------


_RESTING = (JobState.NEEDS_HUMAN, JobState.ACCEPTED)
_TICK_STATES = {JobState.RUNNING, JobState.PAUSED, JobState.NEEDS_HUMAN, JobState.ACCEPTED}


def _rescan_resting(store: Store, job_id: str, runner: Runner, now: float, out: dict, notifier=None,
                    head_fn=None) -> None:
    """NEEDS_HUMAN and ACCEPTED jobs have no runner any more (run_forever stops at NEEDS_HUMAN), so the control pane
    re-reads their commits when HEAD moved since the last scan - at most every RESCAN_EVERY_S."""
    if incorporate_module.is_incorporated(store, job_id):
        return
    last = store.kv_get(job_id, RESCAN_AT_KEY)
    if last is not None and now - last < RESCAN_EVERY_S:
        return
    store.kv_set(job_id, RESCAN_AT_KEY, now)
    worktree = store.home / "worktrees" / job_id
    if not worktree.is_dir():
        return
    if head_fn is not None:
        head = head_fn(job_id) or ""
    else:
        res = _git(runner, worktree, "rev-parse", "HEAD")
        head = res.stdout.strip() if res.exit_code == 0 else ""
    if not head or head == store.kv_get(job_id, questions.SCANNED_HEAD_KEY):
        return
    result = questions.scan(store, job_id, runner)
    out["scanned"].append({"job": job_id, "head": head, "error": result.error})
    notify_queue(store, notifier, job_id, result, link_fn=default_link_fn(store, runner))


def _parked_pass(store: Store, job_id: str, job_lead, now: float, out: dict, policy_fn) -> None:
    """Spec 2026-10-04 §4: a stuck NEEDS_HUMAN job gets the tech lead's review on the [parked] cadence. A review
    already in flight is polled every tick (so it can finish) without opening a new cadence slot."""
    from steward import hold
    hold.resolve_reopens(store, job_id)
    k = hold.kind(store, job_id)
    if not k or k["kind"] != "stuck" or sendback_module.rework_pending(store, job_id):
        return
    in_flight = bool((job_lead.state(job_id) or {}).get("in_flight"))
    last = store.kv_get(job_id, PARKED_AT_KEY)
    cadence_s = float(policy_fn().parked_cadence_min) * 60.0
    due = last is None or now - float(last) >= cadence_s
    if not in_flight and not due:
        return
    if not in_flight:
        store.kv_set(job_id, PARKED_AT_KEY, now)
    out["parked"].append({"job": job_id, "reason": "in_flight" if in_flight else "due"})
    job_lead.on_event(job_id, sweep=True, reason="parked_stuck")


def default_link_fn(store: Store, runner: Runner) -> Callable[[str], str | None]:
    """The phone-tab deep link (else the job's own tab) for a notification, through `runner`."""
    def link(job_id: str) -> str | None:
        from steward import phone  # lazy: phone imports half the package

        return phone.phone_link(runner, store, job_id)

    return link


def notify_queue(store: Store, notifier, job_id: str, result, *, link_fn: Callable[[str], str | None]) -> bool:
    """The one place both scan sites (the runner tick and the control tick) use: a scan that changed the waiting
    queue and left something waiting sends one NEEDS_YOU (deduped by the queue hash inside `notify.emit`)."""
    if notifier is None or result is None or not getattr(result, "queue_changed", False):
        return False
    waiting = int((result.summary or {}).get("waiting", 0))
    if waiting <= 0:
        return False
    job = store.get_job(job_id)
    try:
        link = link_fn(job_id)
    except Exception:  # noqa: BLE001 - no link is better than no notification
        link = None
    return notify.emit_questions(
        store, notifier, job_id, waiting=waiting, starred=int((result.summary or {}).get("starred", 0)),
        goal=job.goal if job else "", qhash=result.queue_hash, link=link)


def _head_fn(store: Store, runner: Runner) -> Callable[[str], str | None]:
    """HEAD of a job's worktree (cached for one tick) for the verification-current predicate."""
    cache: dict[str, str | None] = {}

    def head(job_id: str) -> str | None:
        if job_id not in cache:
            wt = store.home / "worktrees" / job_id
            res = _git(runner, wt, "rev-parse", "HEAD") if wt.is_dir() else None
            cache[job_id] = res.stdout.strip() if res is not None and res.exit_code == 0 else None
        return cache[job_id]

    return head


def tick(store: Store, runner: Runner, *, now: float | None = None, notifier=None, job_lead=None,
         policy_fn=None, launch_fn=None, independent=None, delivery_spawn=None) -> dict:
    """One pass of the flusher, run by the control pane on its loop for EVERY job (the runner only covers its own
    RUNNING job). Idempotent, no network, and it never raises: each job is wrapped on its own. With `job_lead`
    (the control pane's JobTechLead) stuck NEEDS_HUMAN jobs also get the parked pass (spec 2026-10-04)."""
    now = time.time() if now is None else now
    out: dict = {"flushed": [], "scanned": [], "skipped": [], "errors": [], "parked": []}
    head_fn = _head_fn(store, runner)
    if job_lead is not None and policy_fn is None:
        from steward import tech_lead_policy
        policy_fn = lambda: tech_lead_policy.load_or_defaults()[0]  # noqa: E731
    try:
        jobs = store.list_jobs(_TICK_STATES)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append({"job": "*", "error": f"{type(exc).__name__}: {exc}"})
        return out
    for job in jobs:
        jid = job.job_id
        try:
            resting = job.state in _RESTING
            if not questions.load(store, jid) and not resting:
                continue
            if resting:
                _rescan_resting(store, jid, runner, now, out, notifier, head_fn=head_fn)
                if job_lead is not None and job.state == JobState.NEEDS_HUMAN and not ownership.enabled():
                    _parked_pass(store, jid, job_lead, now, out, policy_fn)
            if pending(store, jid):
                res = flush(store, jid, runner, now=now)
                (out["flushed"] if res["mode"] != "none" else out["skipped"]).append(
                    {"job": jid, "mode": res["mode"], "detail": res["detail"], "accepted": res["accepted"]})
        except Exception as exc:  # noqa: BLE001 - a broken job must not stop the others
            out["errors"].append({"job": jid, "error": f"{type(exc).__name__}: {exc}"})
    if ownership.enabled():
        try:
            from steward import grant_parse

            out["grants_migrated"] = [r.job_id for r in grant_parse.migrate_pending(store, now=now)]
        except Exception:  # noqa: BLE001 - migration never blocks the cheap tick
            out["grants_migrated"] = []
        try:
            from steward import authority

            out["grants_settled"] = {j: ids for j in _pending_permission_jobs(store)
                                     if (ids := authority.settle_pending_asks(store, j, now=now))}
        except Exception:  # noqa: BLE001 - never blocks the cheap tick
            out["grants_settled"] = {}
        try:
            res = ownership.sweep(store, now=now, job_lead=job_lead, notifier=notifier,
                                  head_fn=head_fn,
                                  policy=policy_fn() if policy_fn else None, launch_fn=launch_fn,
                                  outage=getattr(job_lead, "outage", None))
            out["ownership"] = res
            out["parked"] = [{"job": r["job"], "reason": r["reason"]} for r in res["reviews"]
                             if r.get("via") == "control_lead"]
            out["errors"] += res["errors"]
        except Exception as exc:  # noqa: BLE001 - the sweep never breaks the flusher
            out["errors"].append({"job": "*ownership*", "error": f"{type(exc).__name__}: {exc}"})
        try:
            from steward import delivery

            out["delivery"] = delivery.sweep(store, now=now, spawn=delivery_spawn, notifier=notifier)
            out["errors"] += [{"job": x.get("job", "*delivery*"), "error": x.get("error", "delivery worker error")}
                              for x in out["delivery"].get("errors", [])]
        except Exception as exc:  # noqa: BLE001 - a delivery scheduling failure must not stop the control loop
            out["errors"].append({"job": "*delivery*", "error": f"{type(exc).__name__}: {exc}"})
        try:
            from steward import check_runs

            out["check_runs"] = check_runs.reconcile(store, now=now)
            out["check_runs_finished"] = check_runs.sweep_finished(store, now=now)
        except Exception as exc:  # noqa: BLE001 - stale shared checks reconcile on a later tick
            out["check_runs"] = {"error": type(exc).__name__}
    try:
        from steward import spend_policy

        out["spend"] = spend_policy.enforce_all(store, now=now, notify_fn=getattr(runner, "_notify", None))
    except Exception as exc:  # noqa: BLE001 - the delivery tick never fails on spend bookkeeping
        out["spend"] = {"error": type(exc).__name__}
    from steward import independent_review as _ir  # noqa: F401 - registers the strategy reviewer and hook
    from steward import waits as _waits

    try:
        out["waits"] = _waits.sweep(store, now=now)
    except Exception as exc:  # noqa: BLE001 - each tick remains best effort
        out["waits"] = {"error": type(exc).__name__}
    if independent is not None:
        try:
            out["independent"] = independent.tick()
        except Exception as exc:  # noqa: BLE001 - review service failures do not stop delivery
            out["independent"] = {"error": type(exc).__name__}
    try:
        from steward import judgment_ledger

        last = float(store.kv_get("_steward", "judgment_purge_at") or 0)
        if now - last >= judgment_ledger.PURGE_EVERY_S:
            out["judgment_purged"] = judgment_ledger.purge(store, now=now)
            store.kv_set("_steward", "judgment_purge_at", now)
    except Exception as exc:  # noqa: BLE001 - retention is best effort
        out["judgment_purge_error"] = type(exc).__name__
    return out


def _pending_permission_jobs(store: Store) -> list[str]:
    from steward import approvals

    done = {JobState.RELEASED, JobState.FAILED, JobState.STOPPED}
    return [job.job_id for job in store.list_jobs() if job.state not in done
            and any(a.get("kind") == "permission" for a in
                    approvals.list_approvals(store, job.job_id, status="pending"))]
