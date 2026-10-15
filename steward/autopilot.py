"""Autopilot merge: an automatically ACCEPTED job is also incorporated automatically (owner, 2026-09-30).

Runs right after closeout auto-accepts. Merge only when ALL hold: (a) the merge into the source branch is
clean, (b) the job's discovered checks (else `python3 -m pytest -q` when the repo has tests/) pass on the
MERGED tree in a temporary worktree BEFORE the source branch moves, (c) the job did no floor action and its
goal does not ask for one (deploy/publish/push/messages/payments/credentials), (d) steward.toml
`[autopilot] merge` (default true when acceptance = "auto").

A conflict or a red merged tree never merges: a follow-up job (like `steward new --follow-up`) is started to
merge the source branch into the job branch and resolve; after `MAX_FOLLOWUPS` failed follow-ups the job is
escalated to the owner (NEEDS_YOU). Anything else that blocks (dirty source repo, mid-merge, floor) is left for
the human path. A conflict first gets an in-code merge of the source into the job branch (patience / whitespace-tolerant)
before any model fix-up, and `deploy-self` only runs on a merge result whose tree was tested (else it is re-tested). Merges are serialized per repo. When the merged repo is Steward itself a `restart_needed`
event is emitted (runners read as OUTDATED via the existing commit-behind badge) unless `steward deploy-self`\nruns automatically (default; STEWARD_AUTODEPLOY=0 turns it off).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from steward import config
from steward import deadline as deadline_module
from steward import deploy_pending, incorporate as inc
from steward import owner
from steward import push as push_module
from steward.models import JobState, Judgment
from steward.runner_exec import Runner
from steward.store import Store, resolve_config_dir

MAX_FOLLOWUPS = 2
DEPLOY_RETRY_EVERY_S = 30.0
DEPLOY_RETRY_FOR_S = 600.0
ATTEMPT_KEY = "autopilot_attempted"
NO_MERGE_POLICY_KEY = "no_merge_policy"
_FLOOR_GOAL_SRC = (
    r"\b(deploy(ing|ment)?(\s+[\w-]+){0,3}?\s+(to\s+)?(prod(uction)?|public|the\s+public|app\s+store|play\s+store)\b|publish(ing)?|git\s+push|push(ing)?\s+(to|the|it|this|branch|origin)|"
    r"(e-?mail(ing|s)?|telegram|slack|tweet(ing)?|sms|text\s+(him|her|them|{names})|(send|post|dm)\w*\s+(a\s+|an\s+|the\s+)?"
    r"(messages?|mail|notifications?|posts?)|call\s+(him|her|them)|phone\s+call)\b|"
    r"payments?|pay\s+for|purchase|credentials?|api[\s_-]?keys?|secrets?|release\s+to|go\s+live)\b")


def _floor_goal() -> "re.Pattern[str]":
    """The floor-goal grammar, with the owner's name(s) filled in at use time (never at import)."""
    names = "|".join(re.escape(n.lower().split()[-1] if n.lower().startswith("the ") else n.lower()) for n in owner.parser_names())
    return re.compile(_FLOOR_GOAL_SRC.replace("{names}", names), re.I)
_tl = threading.local()  # `_run_checks` leaves the last green "N passed" here for the pane line
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def enabled() -> bool:
    """steward.toml `[autopilot] merge`; default true when acceptance = "auto"."""
    import tomllib

    from steward import closeout

    try:
        value = tomllib.loads((resolve_config_dir() / "steward.toml").read_text()).get("autopilot", {}).get("merge")
    except (OSError, ValueError, AttributeError):
        value = None
    if isinstance(value, bool):
        return value
    return closeout.acceptance_mode() == "auto"


_NEGATION = re.compile(r"\b(never|not|no|don'?t|do\s+not|must\s+not|without|nothing|avoid|forbidden|refuse\w*|skip)\b", re.I)
_CLAUSE_BREAK = re.compile(r"[.;:!?](?=\s|$)|\n|\s-\s|\(|\)")


_NO_MERGE = re.compile(
    r"\b(?:do(?:es)?\s+not|don'?t|never|must\s+not|should\s+not|shall\s+not|cannot|can'?t|not\s+to|without)\s+"
    r"(?:\w+\s+){0,2}?(?:auto-?\s*)?(?:merg|incorporat)\w*", re.I)


def no_merge_ask(goal: str) -> str | None:
    """The instruction not to merge/incorporate the goal states ("Do not merge/incorporate", "never auto-merge",
    "without merging"), else None. A goal that merely mentions merging ("merge master into your branch") is not one,
    and neither is a negation that is itself negated away ("no need to avoid merging" is rare enough to ignore)."""
    for m in _NO_MERGE.finditer(goal):
        a = m.start()
        if a and goal[a - 1] in "/._-`":
            continue
        if _describes_system(goal, a, m.group(0)):
            continue
        return m.group(0).strip()
    return None


_NM_CLAUSE_BREAK = re.compile(r"[.;:!?,](?=\s|$)|\n|\s-\s|\(|\)|\b(?:so|but|and|because|since|where|which|when|while)\b", re.I)
_NM_ADDRESSEE = frozenset(
    "please also you your worker job task this it we i then just always ok okay note important remember "
    "sure ensure that agent assistant claude".split())

_NM_ADVERBS = frozenset("absolutely definitely really strictly firmly simply only still now never always".split())
_NM_PREPOSITIONS = frozenset("for in on at with during after before from within throughout as under over about of".split())
_NM_DETERMINERS = frozenset("this that these those the a an any each every all my our your its their".split())


def _describes_system(goal: str, start: int, matched: str) -> bool:
    """True when the negation is descriptive prose about the system ("so ACCEPTED jobs never finish incorporation",
    "the runner never merges") rather than an instruction: some other subject sits in front of an indicative
    negation. Imperatives ("Do not merge"), text addressed to the worker/job ("you must not merge", "this job
    never merges"), deontics (must/should/shall not) and "without"/"not to" are instructions."""
    if re.match(r"(?:must|should|shall)\s+not|without|not\s+to", matched, re.I):
        return False
    clause_start = max((x.end() for x in _NM_CLAUSE_BREAK.finditer(goal, 0, start)), default=0)
    prefix = re.findall(r"[\w'-]+", goal[clause_start:start])
    # Only a subject is evidence of description. Adverbs ("carefully"), addressees ("this job") and a leading
    # adjunct ("For this change") are not subjects: peel them off and see whether anything is left.
    while prefix and _is_filler(prefix[-1]):
        prefix.pop()
    while prefix and _is_filler(prefix[0]):
        prefix.pop(0)
    if prefix and prefix[0].lower() in _NM_PREPOSITIONS:
        rest = prefix[1:]
        if rest and rest[0].lower() in _NM_DETERMINERS:
            rest = rest[1:]
        prefix = rest[2:]  # the adjunct's noun phrase is at most two words; anything after it is the subject
    return bool(prefix)


def _is_filler(word: str) -> bool:
    w = word.lower()
    return w in _NM_ADDRESSEE or w in _NM_ADVERBS or (len(w) > 4 and w.endswith("ly"))


def no_merge_protection(store: Store, job_id: str) -> tuple[str, str] | None:
    """Return (source job, reason) for a durable no-merge policy in this job's lineage.

    New jobs persist the policy at creation and follow-ups inherit it. The ancestry walk also
    recognizes pre-policy jobs created before that metadata existed. Unknown/deep ancestry fails
    closed because merge permission must not be inferred from missing lineage data.
    """
    seen: set[str] = set()
    pending = [job_id]
    while pending and len(seen) < 256:
        cur = pending.pop(0)
        if not cur or cur in seen:
            continue
        seen.add(cur)
        policy = store.kv_get(cur, NO_MERGE_POLICY_KEY)
        if isinstance(policy, dict) and policy.get("source_job") and policy.get("reason"):
            return str(policy["source_job"]), str(policy["reason"])
        job = store.get_job(cur)
        if job is None:
            return None
        reason = no_merge_ask(job.goal)
        if reason:
            return cur, reason
        for key in ("parent_job", "autopilot_followup"):
            nxt = store.kv_get(cur, key)
            if isinstance(nxt, str) and nxt and nxt not in seen:
                pending.append(nxt)
    if pending:
        return job_id, "ancestry too deep to verify"
    return None


def inherit_no_merge_policy(store: Store, job_id: str, parent_id: str | None = None) -> dict | None:
    """Persist a direct or inherited no-merge policy for a newly created job."""
    reason = no_merge_ask((store.get_job(job_id).goal if store.get_job(job_id) else ""))
    source = job_id if reason else None
    if source is None and parent_id:
        inherited = no_merge_protection(store, parent_id)
        if inherited:
            source, reason = inherited
    if source is None or reason is None:
        return None
    policy = {"source_job": source, "reason": reason, "inherited_from": parent_id}
    store.kv_set(job_id, NO_MERGE_POLICY_KEY, policy)
    return policy


def floor_ask(goal: str) -> str | None:
    """The first floor action the goal actually ASKS for, else None. A mention is ignored when it is negated in its
    own clause ("never git push", "no deploy/publish", "do not touch credentials") or is part of a path or
    identifier ("deploy/cd.sh", "--no-publish"): every worker brief forbids pushing, which is not a request to push."""
    for m in _floor_goal().finditer(goal):
        a, b = m.span()
        if (a and goal[a - 1] in "/._-`") or (b < len(goal) and goal[b] in "/._-`"):
            continue
        start = max((x.end() for x in _CLAUSE_BREAK.finditer(goal, 0, a)), default=0)
        before = goal[start:a].split()[-6:]
        if _NEGATION.search(" ".join(before)):
            continue
        in_list = re.search(r"[,/]\s*$", goal[start:a]) or re.match(r"\s*[,/)]", goal[b:])
        if start and goal[start - 1] == "(" and ")" not in goal[start:a] and in_list:
            # an item of a parenthesized list inherits the enclosing clause's negation: "must never include
            # contacting anyone outside (tg/telegram, email, Moshi, ...)" forbids email (mechanics item 13a)
            outer = max((x.end() for x in _CLAUSE_BREAK.finditer(goal, 0, start - 1)), default=0)
            if _NEGATION.search(" ".join(goal[outer:start - 1].split()[-6:])):
                continue
        return m.group(0).strip()
    return None


def floor_reason(store: Store, job_id: str) -> str | None:
    """Why this job needs a human merge (did / asks for a floor action), else None."""
    job = store.get_job(job_id)
    if job is None:
        return "unknown job"
    from steward import evidence

    goal = evidence.split_goal(job.goal)[0] or job.goal
    m = floor_ask(goal)
    if m:
        return f"goal asks for a floor action ({m})"
    m = no_merge_ask(goal)
    if m:
        return f"goal says not to merge ({m}); a human merges"
    protected = no_merge_protection(store, job_id)
    if protected:
        source, reason = protected
        if source != job_id:
            return f"merge is protected by job goal (ancestor {source[:8]}: {reason})"
    from steward import approvals

    # only the events that can say "floor" (TUI speed plan 12b: not every event of the job)
    for e in store.events_where(job_id, whats=("floor_action",), evidence={"tier": "floor"}):
        aid = e.evidence.get("approval_id")
        if e.evidence.get("tier") == "floor" and aid:
            # a floor-tier CLASSIFICATION is not an action: only an approved AND relayed (executed) permission counts;
            # denied / stale / held / still-pending ones never ran
            a = approvals.get_approval(store, job_id, str(aid))
            if a is not None and a.get("status") == "approved" and a.get("relayed_at"):
                return "job did a floor action"
        elif e.evidence.get("tier") == "floor" or e.what == "floor_action":
            return "job did a floor action"
    return None


def _repo_lock(store: Store, repo: str) -> tuple[threading.Lock, Path]:
    key = hashlib.sha1(str(Path(repo).resolve()).encode()).hexdigest()[:12]
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    return lock, inc.lock_path(store, repo)


def followup_depth(store: Store, job_id: str) -> int:
    """How many autopilot follow-ups lead to this job (0 for an ordinary job)."""
    depth, seen = 0, set()
    while job_id and job_id not in seen and store.kv_get(job_id, "autopilot_followup"):
        seen.add(job_id)
        depth += 1
        job_id = store.kv_get(job_id, "parent_job")
    return depth


def _is_steward_repo(repo: str, steward_root: Path | str | None) -> bool:
    from steward.stability import REPO_ROOT

    try:
        return Path(repo).resolve() == Path(steward_root or REPO_ROOT).resolve()
    except OSError:
        return False


def _check_lines(tree: Path) -> list[str]:
    from steward.discovery import discover

    lines = [c.line for c in discover(tree).commands if c.kind == "check"]
    if not lines and (tree / "tests").is_dir():
        lines = ["python3 -m pytest -q"]
    return lines


CHECK_TIMEOUT_S = 2 * 3600.0  # the full Steward suite takes ~50 min; a check past 2 h is hung, not slow
TIMED_OUT = "merged tree check timed out"


def _accepts_timeout(fn) -> bool:
    import inspect

    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "timeout_s" or p.kind is p.VAR_KEYWORD for p in params)


def _relative_judge(store: Store, repo, base_sha: str, runner: Runner):
    """§5.5 for the merged-tree check: `judge(line, run, fn, kw)` -> the failing ids of a red pytest run that do NOT
    also fail on `base_sha` (same line, same env; the base run is cached like the train's), or None when the
    comparison cannot be made (not a plain test failure, no per-test ids, unusable base): the run stays red."""
    from steward import check_runs, pytest_parallel
    from steward import train_gates as tg

    def failing(run) -> set[str]:
        return {k for k, v in (getattr(run, "tests", None) or {}).items() if v in ("failed", "error")}

    def judge(line: str, run, fn, kw: dict, tree: Path) -> list[str] | None:
        if run.exit_code != 1 or getattr(run, "broken", None) or not failing(run):
            return None

        def run_base(btree: Path) -> set[str]:
            with tempfile.TemporaryDirectory() as tmp:
                b = fn(runner, line, btree, Path(tmp) / "base.xml", "", **kw)
            ids = failing(b)
            if getattr(b, "broken", None) or b.exit_code not in (0, 1) or (b.exit_code == 1 and not ids):
                return {tg.SUITE_ID}
            return ids
        try:
            digest = tg.suite_digest(check_runs.env_digest(execution=pytest_parallel.execution_label([line], tree)),
                                     line)
            base = tg.base_failures(store, repo, base_sha, env_digest=digest, worktree=tree, now=time.time(),
                                    suite_fn=tg.base_tree_suite(repo, base_sha, _scratch_dir(), run_base))
        except Exception:  # noqa: BLE001 - no base verdict: the absolute red stands
            return None
        if tg.SUITE_ID in base:
            return None
        new, pre, _fixed = tg.compare(failing(run), base)
        _tl.preexisting = sorted(set(getattr(_tl, "preexisting", None) or []) | set(pre))
        return new
    return judge


def _run_checks(tree: Path, runner: Runner, checks: list[str] | None, *, check_fn=None,
                judge=None) -> str | None:
    """Run the checks in `tree`; None when green, else why it is red (or that it timed out). The default path is
    `baseline.run_check`, so a merged-tree suite gets the same execution policy (parallel xdist, fallback) as a
    baseline or verification; an injected `check_fn` keeps its seam and is handed CHECK_TIMEOUT_S when it takes one.
    With `judge` (`_relative_judge`), a red pytest line whose failing ids all fail on the base too counts as green."""
    import tempfile as _tf

    from steward.baseline import run_check

    _tl.passed = None
    _tl.preexisting = []
    lines = checks if checks is not None else _check_lines(tree)
    fn = check_fn or run_check
    kw = {"timeout_s": CHECK_TIMEOUT_S} if _accepts_timeout(fn) else {}
    if fn is run_check:
        kw["fallback"] = False  # the merged-tree check always ran the line as discovered; no python3 -m retry
    for line in lines:
        with _tf.TemporaryDirectory() as tmp:
            run = fn(runner, line, tree, Path(tmp) / "merged.xml", "", **kw)
        if run.exit_code == 124 and (run.broken or "").startswith("timed out"):
            return f"{TIMED_OUT}: `{line}` ran over {int(CHECK_TIMEOUT_S)}s"
        passed = sum(1 for outcome in (run.tests or {}).values() if outcome == "passed")
        if not passed and run.exit_code == 0:
            m = re.search(r"(\d[\d,]*) passed", getattr(run, "output", "") or "")
            passed = int(m.group(1).replace(",", "")) if m else 0
        if run.exit_code == 0 and not run.broken:
            _tl.passed = passed or _tl.passed
            continue
        new = judge(line, run, fn, kw, tree) if judge is not None else None
        if new is not None and not new:      # every failure is pre-existing on the base: not this merge's red
            _tl.passed = passed or _tl.passed
            continue
        return (f"merged tree is unverified: `{line}`: {run.broken}" if run.broken
                else f"merged tree is red: `{line}` exited {run.exit_code}"
                + (f"; new failures vs base: {', '.join(new[:5])}" if new else ""))
    return None


def _scratch_dir() -> Path:
    """Temp trees live under the Steward state dir, not /tmp (a small usrquota tmpfs; a killed run leaves its tree)."""
    from steward.store import resolve_home

    root = resolve_home() / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _test_merged_tree(job, branch: str, runner: Runner, checks: list[str] | None,
                      reuse: Callable[[str, list[str]], bool] | None = None,
                      progress: Callable[..., None] | None = None, *, check_fn=None,
                      base: str = "HEAD", store: Store | None = None) -> tuple[bool, str, list[str]]:
    """Merge `branch` (a name or a pinned sha) into a detached temp worktree of the repo at `base` (HEAD, or the
    pinned target sha) and run the checks there.
    `(ok, detail, conflicted files)`; the source branch is never touched. On green, `detail` is the
    resulting tree's sha so the caller can tell the tested content from what was actually merged.
    `progress(what, **evidence)` hears when the (possibly ~50 min) check run starts and ends.
    With `store`, the check uses the train's relative gate (§5.5): red only on failures that are new vs `base`."""
    repo = job.repo
    tmp = Path(tempfile.mkdtemp(prefix="steward-automerge-", dir=_scratch_dir()))
    tree = tmp / "tree"
    try:
        add = inc._git(repo, "worktree", "add", "--detach", str(tree), base)
        if add.returncode != 0:
            return False, f"temp worktree failed: {(add.stderr or add.stdout).strip()[:150]}", []
        merge = inc._git(tree, "-c", "user.name=Steward", "-c", "user.email=steward@localhost",
                         "merge", "--no-ff", "--no-edit", branch)
        if merge.returncode != 0:
            return False, "merge conflict", sorted(set(inc._git(tree, "diff", "--name-only", "--diff-filter=U").stdout.split()))
        merged_tree = inc._git(tree, "rev-parse", "HEAD^{tree}").stdout.strip()
        if reuse is not None:
            lines = checks if checks is not None else _check_lines(tree)
            if reuse(merged_tree, list(lines)):
                _tl.passed = None
                return True, merged_tree, []
        t0 = time.monotonic()
        if progress is not None:
            progress("merged_tree_verification_started", merged_tree=merged_tree, tree=str(tmp),
                     checks=list(checks if checks is not None else _check_lines(tree)), pid=os.getpid())
        judge = None
        if store is not None:
            base_sha = inc._git(repo, "rev-parse", base).stdout.strip()
            judge = _relative_judge(store, repo, base_sha, runner) if base_sha else None
        red = _run_checks(tree, runner, checks, check_fn=check_fn, judge=judge)
        if progress is not None:
            extra = {"preexisting": list(_tl.preexisting)} if getattr(_tl, "preexisting", None) else {}
            progress("merged_tree_verification_finished", merged_tree=merged_tree, ok=not red, reason=red or "",
                     duration_s=round(time.monotonic() - t0, 1), **extra)
        if red:
            return False, red, []
        return True, merged_tree, []
    finally:
        inc._git(repo, "worktree", "remove", "--force", str(tree))
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _test_commit(repo: str, sha: str, runner: Runner, checks: list[str] | None, *, check_fn=None) -> str | None:
    """Run the checks on the committed tree `sha` (the real merge result) in a detached temp worktree;
    None when green, else why it is red."""
    tmp = Path(tempfile.mkdtemp(prefix="steward-postmerge-", dir=_scratch_dir()))
    tree = tmp / "tree"
    try:
        add = inc._git(repo, "worktree", "add", "--detach", str(tree), sha)
        if add.returncode != 0:
            return f"temp worktree failed: {(add.stderr or add.stdout).strip()[:150]}"
        return _run_checks(tree, runner, checks, check_fn=check_fn)
    finally:
        inc._git(repo, "worktree", "remove", "--force", str(tree))
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _branch_worktree(repo: str, branch: str) -> Path | None:
    out = inc._git(repo, "worktree", "list", "--porcelain").stdout
    cur = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and cur:
            return Path(cur)
    return None


_SYNC_OPTS = ("-Xpatience", "-Xignore-space-change", "-Xpatience -Xignore-space-change")


def _sync_branch(job, pf) -> bool:
    """Merge the source branch INTO the job branch in code (no model) when a plain merge conflicts but a
    whitespace/diff-algorithm-tolerant one resolves it. True when the job branch now contains the source head.
    Only ever touches the job's own clean worktree; any failure aborts and leaves the branch as it was."""
    wt = _branch_worktree(job.repo, pf.branch)
    if wt is None or not wt.is_dir() or inc._git(wt, "status", "--porcelain").stdout.strip():
        return False
    for opts in _SYNC_OPTS:
        res = inc._git(wt, "-c", "user.name=Steward", "-c", "user.email=steward@localhost",
                       "merge", "--no-edit", *opts.split(), pf.source_branch)
        if res.returncode == 0:
            return True
        inc._git(wt, "merge", "--abort")
    return False


def _event(store: Store, job_id: str, what: str, **evidence) -> None:
    store.append_event(job_id, what=what, evidence=evidence, rule="autopilot_merge", judgment=Judgment.code)


DEADLINE_GATE_NOTED = "deadline_merge_gate_noted"


def deadline_gate(store: Store, job_id: str) -> dict | None:
    """DL-03 Gate 2: a job with a lifetime `deadline_ever` marker (its own or an ancestor's) never auto-merges,
    whatever its current commitment, mode, the feature flag or a human accept say. Only a human `incorporate`
    merges it. Returns the stable skipped result (one bounded event per job), else None."""
    why = deadline_module.merge_protected(store, job_id)
    if not why:
        return None
    if not store.kv_get(job_id, DEADLINE_GATE_NOTED):
        store.kv_set(job_id, DEADLINE_GATE_NOTED, True)
        _event(store, job_id, "autopilot_skipped", reason=why, needs="human", gate="deadline")
    return {"outcome": "skipped", "detail": why}


def _default_followup(store: Store, parent_id: str, goal: str) -> dict:
    """Create the fix-up job AND launch it (same workspace / worker as the parent). A job that is only created sits in
    CONTRACTED for ever, so a launch that fails comes back as `launch_error` for `_failed` to escalate."""
    from steward import cli

    gated = deadline_gate(store, parent_id)
    if gated:
        return {"accepted": False, "detail": gated["detail"]}
    protected = no_merge_protection(store, parent_id)
    if protected:
        return {"accepted": False,
                "detail": f"merge is protected by job goal (ancestor {protected[0][:8]})"}
    parent = store.get_job(parent_id)
    handlers = cli._build_control_handlers(store)
    from steward import repair_episode

    start_ref = store.kv_get(parent_id, repair_episode.START_REF_KEY)
    res = handlers["new"]({"args": {"goal": goal, "follow_up": parent_id, "repo": parent.repo, "fixup_linked": True,
                                    **({"start_ref": start_ref} if start_ref else {}),
                                    "created_by": store.kv_get(parent_id, "created_by") or None}})
    child = res.get("job_id") if isinstance(res, dict) else None
    if not child:
        return res if isinstance(res, dict) else {"accepted": False, "detail": str(res)}
    launch_args = dict(store.kv_get(parent_id, "launch_args") or {})
    try:
        launched = handlers["launch"]({"args": {**launch_args, "job_id": child}})
    except Exception as exc:  # noqa: BLE001 - a failed launch is an escalation, never a crash
        launched = {"accepted": False, "detail": str(exc)[:200]}
    if not launched.get("accepted"):
        return {**res, "launch_error": launched.get("detail") or "launch failed"}
    return {**res, "launched": True, "tab_id": launched.get("tab_id")}


def _push(store: Store, job_id: str, event: str, runner: Runner, **facts) -> None:
    """Hand an outcome to the pane notice (one coalesced line per outcome; see `steward.push`)."""
    push_module.notify_creator(store, job_id, event, runner, facts=facts)


def try_merge(store: Store, job_id: str, runner: Runner, *, check_runner: Runner | None = None,
              checks: list[str] | None = None, start_followup: Callable[[Store, str, str], dict] | None = None,
              notify: Callable[..., object] | None = None, steward_root: Path | str | None = None,
              deploy_self: Callable[..., dict] | None = None, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic, check_fn=None) -> dict:
    """Try to auto-incorporate an ACCEPTED job. Returns `{"outcome": ..., "detail": ...}`; never raises."""
    try:
        gated = deadline_gate(store, job_id)  # before any lock, attempt, follow-up, incorporate or deploy
    except Exception as exc:  # noqa: BLE001 - unsure means protected
        gated = {"outcome": "skipped", "detail": f"deadline gate error: {str(exc)[:150]}"}
    if gated:
        return gated
    try:
        res = _try_merge(store, job_id, runner, check_runner or runner, checks,
                         start_followup or _default_followup, notify, steward_root, deploy_self, sleep, clock, check_fn)
    except Exception as exc:  # noqa: BLE001 - autopilot must never break closeout
        _event(store, job_id, "autopilot_error", error=str(exc)[:200])
        res = {"outcome": "error", "detail": str(exc)[:200]}
    push_module.settle(store, job_id, runner, outcome=res.get("outcome"), detail=str(res.get("detail") or ""))
    return res


def _try_merge(store, job_id, runner, check_runner, checks, start_followup, notify, steward_root, deploy_self=None,
               sleep=time.sleep, clock=time.monotonic, check_fn=None) -> dict:
    job = store.get_job(job_id)
    if job is None or job.state != JobState.ACCEPTED:
        return {"outcome": "skipped", "detail": "job is not ACCEPTED"}
    if inc.is_incorporated(store, job_id):
        return {"outcome": "skipped", "detail": "already incorporated"}
    from steward import replace as replace_module

    replaced = replace_module.refusal(store, job_id, "auto-merge")  # issue M: never merge a superseded job
    if replaced:
        _event(store, job_id, "autopilot_skipped", reason=replaced)
        return {"outcome": "skipped", "detail": replaced}
    if config.train_owns(job.repo):
        return {"outcome": "skipped", "detail": "landing train owns merges for this repo"}
    if not enabled():
        return {"outcome": "skipped", "detail": "autopilot merge is off"}
    why = floor_reason(store, job_id)
    if why:
        _event(store, job_id, "autopilot_skipped", reason=why, needs="human")
        return {"outcome": "human", "detail": why}

    try:
        res = _merge(store, job, runner, check_runner, checks, start_followup, notify, steward_root, check_fn=check_fn)
        pending = res.pop("_deploy", None)  # a deploy may wait minutes: never while holding the merge lock
        tested = res.pop("_tested", True)
        merged = res.get("merge_commit")
        if merged and not tested:
            from steward import delivery

            red = _test_commit(job.repo, merged, check_runner, checks, check_fn=check_fn)
            delivery.record_post_merge(store, job_id, merged, red, now=time.time())
            if red:
                if pending:
                    _event(store, job.job_id, "auto_deploy_blocked", merge_commit=pending, reason=red)
                    _event(store, job.job_id, "restart_needed", merge_commit=pending,
                           reason=f"auto-deploy skipped: merged result is red ({red}); fix master, then restart manually")
                    _push(store, job.job_id, "restart_needed", runner,
                          reason=f"merged, but the result fails its checks so Steward was not reloaded: {red}"[:150])
                    res["detail"] += f"; deploy skipped ({red})"
                return res
        if pending and deadline_gate(store, job_id) is None:
            _deploy_or_flag(store, job, pending, runner, deploy_self, steward_root, sleep=sleep, clock=clock)
        return res
    finally:  # this merge attempt is over, whatever happened: its runners stop waiting
        deploy_pending.clear(store, job_id)


MAX_MERGE_ROUNDS = 3
SLOT_POLL_S = 2.0
LAND_LOCK_WAIT_S = 120.0


def _rev(repo, ref: str) -> str:
    r = inc._git(repo, "rev-parse", "--verify", "-q", ref)
    return r.stdout.strip() if r.returncode == 0 else ""


def test_slot_path(store: Store, repo) -> Path:
    return Path(store.home) / f"merge-test-{inc._target_key(repo)}.lock"


class _SlotStop(Exception):
    """Raised inside `_test_slot` when `abort()` stops a wait for the slot; carries the result to return."""

    def __init__(self, result: dict):
        super().__init__("test slot wait stopped")
        self.result = result


@contextmanager
def _test_slot(store: Store, repo, *, purpose: str, job_id: str, abort: Callable[[], dict | None] | None = None):
    """One merged-tree test-and-land per target repo at a time (a cross-process flock, NOT the merge lock: a human
    incorporate or the control pane never waits on it). Under the strict re-test rule two concurrent tests on one
    target can never both land, so running them side by side only burns a suite; autopilot's own suites (no check-run
    queue) were bounded by the merge lock before, and are by this now. The holder record matches `target_lock`'s."""
    path = test_slot_path(store, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _event(store, job_id, "merge_test_slot_wait", holder=inc.lock_holder(path))
            while True:  # poll so a waiter whose job was released/changed leaves at once instead of queueing a suite
                stop = abort() if abort else None
                if stop:
                    raise _SlotStop(stop)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(SLOT_POLL_S)
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps({"pid": os.getpid(), "purpose": purpose, "job": job_id,
                                      "since": time.time()}).encode(), 0)
            yield
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _pinned_preflight(store: Store, job):
    pinned = _rev(job.repo, "HEAD")  # pinned BEFORE the preflight: a later move is caught at the land
    pf = inc.preflight(store, job.job_id)
    pf.target_head = pinned
    return pf


def _stops(pf) -> bool:
    return bool(pf.error or pf.contained or (pf.block and not pf.conflicts))


def _round(store, job, runner, checks, check_fn, reuse, progress, actor, steward_root, still_allowed, mark_id,
           rnd) -> dict | None:
    """One merge_when_green round (under the test slot); None: re-test (the target or the branch moved)."""
    from steward import questions_delivery

    job_id, repo = job.job_id, job.repo
    stop = still_allowed()
    if stop:
        return stop
    questions_delivery.flush_now(store, job_id)  # answers commit onto the branch now, never after the test
    pf = _pinned_preflight(store, job)
    if not _stops(pf) and pf.conflicts and _sync_branch(job, pf):  # the cheap in-code merge before any fix-up
        _event(store, job_id, "autopilot_synced", source_branch=pf.source_branch, was_conflicting=pf.conflicts)
        pf = _pinned_preflight(store, job)
    if _stops(pf):
        return {"outcome": "contained" if pf.contained and not pf.error else "human", "pf": pf,
                "detail": pf.error or ("already contained" if pf.contained else pf.block)}
    if pf.conflicts:
        return {"outcome": "conflict", "pf": pf, "detail": "merge conflict", "files": pf.conflicts}
    pinned, bsha = pf.target_head, getattr(pf, "head", "") or _rev(repo, f"refs/heads/{pf.branch}")
    ok, detail, files = _test_merged_tree(job, bsha, runner, checks, reuse, progress, check_fn=check_fn, base=pinned,
                                          store=store)
    if not ok:
        return {"outcome": "red", "pf": pf, "detail": detail, "files": files}
    try:
        with inc.target_lock(store, repo, timeout=LAND_LOCK_WAIT_S, purpose=f"land {job_id[:8]}", job_id=job_id):
            t0 = time.monotonic()
            stop = still_allowed()
            if stop:
                return {**stop, "pf": pf}
            if _rev(repo, f"refs/heads/{pf.branch}") != bsha:
                _event(store, job_id, "merge_retest", reason="job branch moved", branch=bsha, round=rnd)
                return None
            head = _rev(repo, "HEAD")
            if head != pinned:
                mt = inc._git(repo, "merge-tree", "--write-tree", head, bsha)
                would = mt.stdout.split("\n", 1)[0].strip() if mt.returncode == 0 else ""
                lines = checks if checks is not None else _check_lines(Path(repo))
                if not would or (would != detail and not (reuse is not None and reuse(would, list(lines)))):
                    _event(store, job_id, "merge_retest", reason="target moved", pinned=pinned, head=head, round=rnd)
                    return None
            steward = _is_steward_repo(repo, steward_root) and any(f.startswith("steward/") for f in pf.files)
            if steward:  # before it lands: runners wait for the deploy instead of moving ahead of the control (B)
                deploy_pending.mark(store, mark_id)
            receipt = inc.incorporate(store, job_id, tested_tree=detail, actor=actor, expected_head=head,
                                      expected_branch=bsha)
            if steward and not receipt.get("merge_commit"):
                deploy_pending.clear(store, mark_id)
            if receipt.get("error_kind") == "moved":
                _event(store, job_id, "merge_retest", reason=receipt.get("detail", "moved"), round=rnd)
                return None
    except inc.LockBusy as busy:
        return {"outcome": "busy", "pf": pf, "detail": str(busy), "holder": busy.holder}
    merged = bool(receipt.get("merge_commit")) and not receipt.get("noop")
    outcome = "merged" if merged else ("noop" if receipt.get("noop") else "blocked")
    _event(store, job_id, "merge_landed", outcome=outcome, round=rnd, lock_held_s=round(time.monotonic() - t0, 2),
           merge_commit=receipt.get("merge_commit"))  # the live proof that the lock is held for the land only
    return {"outcome": outcome, "pf": pf,
            "receipt": receipt, "tested_tree": detail, "head": head, "steward_merge": steward and merged,
            "detail": receipt.get("detail", "")}


def merge_when_green(store: Store, job, runner: Runner, checks: list[str] | None, *, check_fn=None,
                     reuse: Callable[[str, list[str]], bool] | None = None, progress: Callable[..., None] | None = None,
                     actor=None, steward_root: Path | str | None = None,
                     still_allowed: Callable[[], dict | None] = lambda: None, mark_id: str | None = None) -> dict:
    """Test outside the merge lock, merge inside it (delivery latency fix #2, 2026-10-07): the one tested-merge routine
    of autopilot and the delivery worker. Each round pins the target HEAD and the job branch sha and tests that merge
    in a temp tree with NO lock held (the suite and any heavy-slot wait take minutes); `target_lock` is held only to
    land. HEAD moved meanwhile: it lands only when the merge's tree is the tested one (or reusable), else the round
    re-tests on the new HEAD (strict re-test, the owner's choice 2026-10-07); a moved branch always re-tests. After
    MAX_MERGE_ROUNDS lost races: "raced". Rounds of one target take turns on `_test_slot` (test + land), so Steward's
    own merges do not invalidate each other's tests. `still_allowed()` (state/authority/deadline, re-checked under the
    lock) returns a result dict to stop. A Steward-code merge marks deploy_pending (as `mark_id`, default the job)
    before it lands (cleared when nothing merged).
    Outcomes: human | contained | conflict | red | skipped | merged | noop | blocked | raced | busy."""
    job_id, repo = job.job_id, job.repo
    mark_id = mark_id or job_id
    for rnd in range(1, MAX_MERGE_ROUNDS + 1):
        try:
            with _test_slot(store, repo, purpose=f"test+land {job_id[:8]}", job_id=job_id, abort=still_allowed):
                r = _round(store, job, runner, checks, check_fn, reuse, progress, actor, steward_root, still_allowed,
                           mark_id, rnd)
        except _SlotStop as stopped:
            return {**stopped.result, "pf": None}
        if r is not None:
            return r
    _event(store, job_id, "merge_raced", rounds=MAX_MERGE_ROUNDS)
    return {"outcome": "raced", "detail": f"the target or the job branch moved during each of {MAX_MERGE_ROUNDS} tests"}


def _merge(store, job, runner, check_runner, checks, start_followup, notify, steward_root, *, check_fn=None,
           _retry=False) -> dict:
    job_id = job.job_id

    def still_allowed() -> dict | None:
        gated = deadline_gate(store, job_id)  # re-checked after the (long) merged-tree test: a deadline may have landed
        if gated:
            return gated
        if store.get_job(job_id).state != JobState.ACCEPTED:
            _event(store, job_id, "autopilot_skipped", reason="job left ACCEPTED during the merge; not merging")
            return {"outcome": "skipped", "detail": "job left ACCEPTED during the merge"}
        return None

    from steward.actor import Actor, ActorRef

    r = merge_when_green(store, job, check_runner, checks, check_fn=check_fn, reuse=_reuse_fn(store, job),
                         progress=_progress_fn(store, job_id), actor=ActorRef(Actor.CONTROLLER, "autopilot"),
                         steward_root=steward_root, still_allowed=still_allowed)
    out, pf, detail = r["outcome"], r.get("pf"), r.get("detail", "")
    if out in ("human", "contained"):
        _event(store, job_id, "autopilot_skipped", reason=detail, needs="human")
        if pf.error.startswith("chain target mismatch:"):
            from steward import delivery

            delivery.raise_waiting(store, job_id, pf.error, now=time.time())
        elif pf.block and not pf.contained and not pf.error:
            from steward import delivery

            delivery.raise_waiting(store, job_id, pf.block, now=time.time())
        return {"outcome": "human", "detail": detail}
    if out in ("conflict", "red") and detail.startswith(TIMED_OUT):  # a hung suite is not something a fix-up can resolve
        _event(store, job_id, "autopilot_blocked", reason=detail, files=[], follow_ups_so_far=followup_depth(store, job_id))
        _escalate(store, job_id, detail, followup_depth(store, job_id), runner, notify)
        return {"outcome": "escalated", "detail": detail}
    if out in ("conflict", "red"):
        res = _failed(store, job, pf, detail, r.get("files", []), runner, start_followup, notify)
        if res.get("outcome") == "retry" and not _retry:
            return _merge(store, store.get_job(job_id), runner, check_runner, checks, start_followup, notify,
                          steward_root, check_fn=check_fn, _retry=True)
        return res
    if out == "skipped":
        return {"outcome": "skipped", "detail": detail}
    if out in ("raced", "busy"):  # transient: the delivery worker retries the merge later
        _event(store, job_id, "autopilot_skipped", reason=detail, outcome=out, retry="delivery")
        return {"outcome": out, "detail": detail}
    if out != "merged":
        _event(store, job_id, "autopilot_skipped", reason=detail or "incorporate refused", needs="human")
        return {"outcome": "human", "detail": detail}
    sha = r["receipt"]["merge_commit"]
    _event(store, job_id, "auto_merged", merge_commit=sha, source_branch=pf.source_branch,
           commits=pf.commit_count, tested_head=r["head"])
    store.kv_set(job_id, "auto_merged", {"merge_commit": sha, "source_branch": pf.source_branch})
    _push(store, job_id, "auto_merged", runner, files=len(pf.files), tests=getattr(_tl, "passed", None))
    same = inc._git(job.repo, "rev-parse", f"{sha}^{{tree}}").stdout.strip() == r["tested_tree"]
    res = {"outcome": "merged", "merge_commit": sha, "detail": f"auto-merged into {pf.source_branch}", "_tested": same}
    return {**res, "_deploy": sha} if r["steward_merge"] else res


def _reuse_fn(store: Store, job) -> Callable[[str, list[str]], bool]:
    """Latency #4: skip the merged-tree re-test when it is byte-identical to the tree that passed at closeout
    with the same checks and toolchain (steward.verify_reuse). Called outside the merge lock, and at the land when HEAD
    moved (with the would-be merged tree)."""
    from steward import verify_reuse

    def reuse(merged_tree: str, lines: list[str]) -> bool:
        receipt = store.kv_get(job.job_id, verify_reuse.RECEIPT_KEY) or {}
        commit = receipt.get("commit_sha") if isinstance(receipt, dict) else None
        job_tree = inc._git(job.repo, "rev-parse", f"{commit}^{{tree}}").stdout.strip() if commit else None
        ok, why = verify_reuse.reusable(store, job.job_id, merged_tree=merged_tree, job_tree=job_tree, commands=lines)
        if ok:
            verify_reuse.note_reused(store, job.job_id, merged_tree=merged_tree, commands=lines)
        else:
            _event(store, job.job_id, "merged_tree_verification_needed", reason=why)
        return ok

    return reuse


def _progress_fn(store: Store, job_id: str) -> Callable[..., None]:
    """Merged-tree test progress: an event (the operator sees the ~50 min run is alive) plus the run record's phase."""
    from steward import autopilot_bg

    def progress(what: str, **evidence) -> None:
        _event(store, job_id, what, **evidence)
        autopilot_bg.note(store, job_id, phase=what, tree=evidence.get("tree"))
        if what == "merged_tree_verification_started":
            # Publish the phase synchronously before checks can block or the heartbeat can be delayed.
            _event(store, job_id, "autopilot_progress", phase=what, elapsed_s=0.0, pid=os.getpid())

    return progress


def _in_flight(res: dict) -> bool:
    return bool(res.get("refused")) and "still in flight" in str(res.get("detail") or res.get("summary") or "")


def _deploy_with_retry(deploy, store, root, sleep, clock) -> tuple[dict, int]:
    """Run deploy-self; while it is refused only because a permission answer is in flight, retry every
    DEPLOY_RETRY_EVERY_S for up to DEPLOY_RETRY_FOR_S. Returns (final result, attempts)."""
    start, attempts = clock(), 0
    while True:
        attempts += 1
        try:
            res = deploy(store, root=root)
        except Exception as exc:  # noqa: BLE001 - a failed deploy never breaks the merge
            return {"ok": False, "summary": f"deploy-self error: {str(exc)[:150]}"}, attempts
        if res.get("ok") or not _in_flight(res) or clock() - start + DEPLOY_RETRY_EVERY_S > DEPLOY_RETRY_FOR_S:
            return res, attempts
        sleep(DEPLOY_RETRY_EVERY_S)


def _deploy_or_flag(store, job, sha, runner, deploy_self, steward_root, sleep=time.sleep, clock=time.monotonic) -> None:
    """Steward itself changed: run `deploy-self` (unless STEWARD_AUTODEPLOY=0) and say what was restarted and the
    health result; when it is off or does not come back clean, fall back to the `restart_needed` flag."""
    from steward import deploy_self as ds

    job_id = job.job_id
    res = None
    if ds.autodeploy_enabled():
        res, attempts = _deploy_with_retry(deploy_self or ds.deploy, store, Path(steward_root or job.repo), sleep, clock)
        tries = f" (after {attempts} attempts)" if attempts > 1 else ""
        _event(store, job_id, "auto_deployed" if res.get("ok") else "auto_deploy_failed", merge_commit=sha,
               summary=res.get("summary"), done=res.get("done"), health=res.get("health"), attempts=attempts)
        if res.get("ok"):
            _push(store, job_id, "auto_deployed", runner)
        else:
            _push(store, job_id, "restart_needed", runner,
                  reason=f"merged, but Steward did not reload{tries}: {str(res.get('summary'))[:100]}")
        if res.get("ok"):
            return
    _event(store, job_id, "restart_needed", merge_commit=sha,
           reason="Steward's own code changed; runners are OUTDATED; restart manually"
                  + (" (STEWARD_AUTODEPLOY=0)" if res is None else f" (auto-deploy failed: {res.get('summary')})"))
    if res is None:
        _push(store, job_id, "restart_needed", runner, reason="merged, but Steward must be restarted (auto-deploy is off)")


def _escalate(store, job_id, reason, follow_ups, runner, notify, **extra) -> None:
    _event(store, job_id, "autopilot_escalated", reason=reason, follow_ups=follow_ups, **extra)
    why = f"merge not done after {follow_ups} fix-ups: {reason}" if follow_ups else f"merge not done: {reason}"
    if notify is not None:  # the runner's phone notifier (NEEDS_YOU), same grammar as the pane line
        notify(job_id, "NEEDS_YOU", "", why[:140])
    _push(store, job_id, "auto_escalated", runner, reason=why[:150])


def _hint(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    if base in ("STEWARD-QUESTIONS.json", "STEWARD-ANSWERS.json"):
        return ("keep the source branch's copy where it is; move THIS job's own question/answer files to "
                ".steward/questions/<job8>/ so the two never share a path")
    if path.endswith(".py"):
        return ("if the conflict is only import lines, keep BOTH sides' names (union); if the source branch turned "
                "this file into a thin wrapper or moved its code, take the source's version and PORT this job's change "
                "to the code's new home: never revert the source's refactor")
    return "keep both sides' intent; when unsure prefer the source branch's structure and re-apply this job's change on top"


def _followup_goal(job, pf, detail, files, start_ref=None) -> str:
    src, parent = pf.source_branch, job.job_id[:8]
    start_text = (f"the previous repair attempt ({start_ref}; its resolution is kept)" if start_ref
                  else f"job {parent}'s head")
    lines = [f"Merge `{src}` into YOUR branch: you are on your own fresh branch, which starts at {start_text}. "
             f"Run `git merge {src}` here (never check out or commit to {pf.branch}) and resolve "
             f"{'the conflicts' if files else 'the problem'}: {detail}."]
    lines += [f"- {f}: {_hint(f)}" for f in files[:12]]
    lines.append("Never weaken or delete a test to get green. Run the project's checks on the merged result and make "
                 "them pass; do not push.")
    lines.append("Background only, do NOT redo it (that work is already committed on your branch): "
                 + " ".join(job.goal.split())[:300])
    return "\n".join(lines)


def _failed(store, job, pf, detail, files, runner, start_followup, notify) -> dict:
    job_id = job.job_id
    depth = followup_depth(store, job_id)
    from steward import repair_episode

    if repair_episode.enabled():  # _repair emits autopilot_blocked itself, with the episode's attempt facts
        return _repair(store, job, pf, detail, files, runner, start_followup, notify)
    _event(store, job_id, "autopilot_blocked", reason=detail, files=files, follow_ups_so_far=depth)
    if depth >= MAX_FOLLOWUPS:
        _escalate(store, job_id, detail, depth, runner, notify)
        return {"outcome": "escalated", "detail": detail}
    # Claim BEFORE the (slow) create+launch: three [ fix merge ] taps plus autopilot started four fix-ups because the
    # link was only recorded after launch. Every path checks the claim; it expires so a crashed start is retryable.
    if not _claim_fixup(store, job_id):
        return {"outcome": "pending", "detail": "a fix-up is already being started"}
    res = start_followup(store, job_id, _followup_goal(job, pf, detail, files))
    child = res.get("job_id") if isinstance(res, dict) else None
    if not child:
        _escalate(store, job_id, f"follow-up could not start: {res}", depth, runner, notify)
        return {"outcome": "escalated", "detail": f"follow-up could not start: {res}"}
    store.kv_set(child, "autopilot_followup", job_id)
    if res.get("launch_error"):  # created but never launched: it would sit in CONTRACTED; the owner launches it
        _event(store, job_id, "auto_followup_launch_failed", follow_up=child, error=res["launch_error"], attempt=depth + 1)
        _escalate(store, job_id, f"follow-up {child[:8]} created but could not launch: {res['launch_error']}",
                  depth, runner, notify, launch_failed=child, follow_up=child)
        return {"outcome": "escalated", "follow_up": child, "detail": f"follow-up could not launch: {res['launch_error']}"}
    _event(store, job_id, "auto_followup_started", follow_up=child, reason=detail, attempt=depth + 1)
    _push(store, job_id, "auto_followup", runner, detail=detail, conflict_files=files[:3], attempt=depth + 1,
          max=MAX_FOLLOWUPS)
    return {"outcome": "followup", "follow_up": child, "detail": detail}



def _repair(store, job, pf, detail, files, runner, start_followup, notify, *, max_depth: int | None = None) -> dict:
    """Use one root-owned repair episode; new attempts start from the latest resolution branch. `max_depth` caps the
    episode's attempts (the landing train's rebase fix-up passes 1): past it the outcome is "ask", nothing started."""
    from steward import blockers, delivery, intents, repair_episode

    now = time.time()
    d = repair_episode.decide(store, job, pf, detail=detail, files=files, now=now,
                              pf_target_head=getattr(pf, "target_head", None), max_depth=max_depth)
    ep = d["episode"]
    cur = store.get_job(ep.current_job) if ep.current_job else None
    # follow_ups_so_far is how many follow-ups lead TO this job (0 for every root); the repair facts are the episode's.
    _event(store, job.job_id, "autopilot_blocked", reason=detail, files=files,
           follow_ups_so_far=followup_depth(store, job.job_id), repair_attempts=ep.attempts,
           current_attempt=ep.current_job, attempt_state=cur.state.value if cur is not None else None,
           episode=ep.episode_id)
    if d["action"] in ("retry_in_place", "wait_attempt", "strategy_review"):
        _event(store, ep.root_job, f"repair_{d['action']}", reason=d["reason"], episode=ep.episode_id)
        outcome = {"retry_in_place": "retry", "wait_attempt": "pending", "strategy_review": "strategy_review"}
        return {"outcome": outcome[d["action"]], "detail": d["reason"], "follow_up": ep.current_job}
    if d["action"] == "ask":                  # depth cap: the caller asks the owner; no attempt is started
        _event(store, ep.root_job, "repair_depth_capped", reason=d["reason"], episode=ep.episode_id,
               max_depth=max_depth)
        return {"outcome": "ask", "detail": d["reason"], "follow_up": None}
    if not _claim_fixup(store, ep.root_job):
        return {"outcome": "pending", "detail": "a fix-up is already being started"}
    key = intents.intent_key(ep.root_job, "repair-attempt", ep.episode_id, ep.attempts + 1)
    if intents.claim(store, key=key, job_id=ep.root_job, kind="repair-attempt", lease_s=600.0, now=now) is None:
        store.kv_delete(ep.root_job, FIXUP_CLAIM_KEY)
        return {"outcome": "pending", "detail": "this repair attempt is already being started"}
    root = store.get_job(ep.root_job)
    if d["start_ref"]:
        store.kv_set(ep.root_job, repair_episode.START_REF_KEY, d["start_ref"])
    try:
        res = start_followup(store, ep.root_job,
                             _followup_goal(root, pf, detail, files, start_ref=d["start_ref"]))
    except Exception as exc:  # noqa: BLE001 - launch failure is reviewable, not an owner escalation or stuck claim
        res = {"accepted": False, "launch_error": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        store.kv_delete(ep.root_job, repair_episode.START_REF_KEY)
    child = res.get("job_id") if isinstance(res, dict) else None
    if not child or res.get("launch_error"):
        why = res.get("launch_error") if child else f"follow-up could not start: {res}"
        intents.fail(store, key, reason=str(why), now=now, retryable=True)
        store.kv_delete(ep.root_job, FIXUP_CLAIM_KEY)
        blockers.raise_blocker(store, ep.root_job, kind=delivery.LAUNCH_KIND, subject=ep.episode_id,
                               claim=f"repair attempt could not launch: {why}"[:400],
                               evidence=[f"episode:{ep.episode_id}"], origin_actor=delivery.CONTROLLER,
                               now=now, origin_rule="autopilot_merge")
        if child:
            store.kv_set(child, "autopilot_followup", ep.root_job)
        _event(store, ep.root_job, "auto_followup_launch_failed", follow_up=child, error=str(why)[:200],
               attempt=ep.attempts + 1)
        return {"outcome": "blocked", "follow_up": child, "detail": f"repair attempt could not launch: {why}"}
    store.kv_set(child, "autopilot_followup", ep.root_job)
    repair_episode.record_attempt(store, ep.episode_id, child, start_ref=d["start_ref"], conflicts=files,
                                  digest=d["digest"], now=now)
    intents.complete(store, key, receipt={"child": child, "start_ref": d["start_ref"]}, now=now)
    store.kv_delete(ep.root_job, FIXUP_CLAIM_KEY)
    _event(store, ep.root_job, "auto_followup_started", follow_up=child, reason=detail, attempt=ep.attempts + 1,
           episode=ep.episode_id, start_ref=d["start_ref"])
    _push(store, ep.root_job, "auto_followup", runner, detail=detail, conflict_files=files[:3],
          attempt=ep.attempts + 1, max=repair_episode.ATTEMPTS_BEFORE_STRATEGY)
    return {"outcome": "followup", "follow_up": child, "detail": detail}

def record_manual_fixup(store: Store, parent_id: str, child_id: str) -> None:
    """A hand-started `steward new --follow-up PARENT` of an ACCEPTED, unmerged parent IS that parent's fix-up: same
    link and event as autopilot's, so the row shows it and its merge releases the parent."""
    attempt = followup_depth(store, parent_id) + 1
    store.kv_set(child_id, "autopilot_followup", parent_id)
    _event(store, parent_id, "auto_followup_started", follow_up=child_id, reason="manual follow-up", attempt=attempt)


FIXUP_CLAIM_KEY = "fixup_claim"
FIXUP_CLAIM_TTL_S = 600.0
_claim_lock = threading.Lock()


def fixup_claimed(store: Store, job_id: str, now: float | None = None) -> bool:
    rec = store.kv_get(job_id, FIXUP_CLAIM_KEY)
    now = time.time() if now is None else now
    return isinstance(rec, dict) and now - float(rec.get("at", 0)) < FIXUP_CLAIM_TTL_S


def _claim_fixup(store: Store, job_id: str) -> bool:
    """Atomically take the one 'starting a fix-up' slot for this job (process lock + kv; TTL for crashed starts)."""
    with _claim_lock:
        if fixup_claimed(store, job_id):
            return False
        store.kv_set(job_id, FIXUP_CLAIM_KEY, {"at": time.time()})
        return True


def fix_merge_needed(store: Store, job_id: str, pf=None) -> bool:
    """True when `[ fix merge ]` is the way forward: ACCEPTED, not merged, its merge would conflict, no fix-up in flight."""
    job = store.get_job(job_id)
    if job is None or job.state != JobState.ACCEPTED or inc.is_incorporated(store, job_id):
        return False
    if followup_status(store, job_id) or fixup_claimed(store, job_id):  # in flight / being started / escalated
        return False
    pf = pf or inc.preflight(store, job_id)
    return not pf.error and not pf.contained and bool(pf.conflicts)


def start_fix_merge(store: Store, job_id: str, runner: Runner, *,
                    start_followup: Callable[[Store, str, str], dict] | None = None,
                    notify: Callable[..., object] | None = None) -> dict:
    """The `[ fix merge ]` tap: start autopilot's own fix-up for a conflicting ACCEPTED job, linked to it
    (`autopilot_followup`), so the row reads "merge blocked · fix-up <id8> running (n/2)" and the parent is released
    when the fix-up merges. `{"accepted": bool, "detail": ..., "follow_up": id}`."""
    job = store.get_job(job_id)
    if job is None:
        return {"accepted": False, "detail": f"unknown job {job_id}"}
    if config.train_owns(job.repo):
        if job.state != JobState.ACCEPTED or inc.is_incorporated(store, job_id):
            return {"accepted": False, "detail": "fix merge needs an ACCEPTED job that is not merged yet"}
        protected = no_merge_protection(store, job_id)
        if protected:
            return {"accepted": False, "detail": f"merge is protected by job goal (ancestor {protected[0][:8]})"}
        from steward import landing_queue  # local: landing_queue imports incorporate

        return landing_queue.route_to_train(store, job_id, job.repo, origin="fix-merge")
    gated = deadline_gate(store, job_id)
    if gated:
        return {"accepted": False, "detail": f"{gated['detail']}: merge it yourself with `steward incorporate`"}
    protected = no_merge_protection(store, job_id)
    if protected:
        return {"accepted": False, "detail": f"merge is protected by job goal (ancestor {protected[0][:8]})"}
    if job.state != JobState.ACCEPTED or inc.is_incorporated(store, job_id):
        return {"accepted": False, "detail": "fix merge needs an ACCEPTED job that is not merged yet"}
    fix = followup_status(store, job_id)
    if fix and fix["kind"] in ("running", "not_started"):
        return {"accepted": False, "detail": f"fix-up {fix['child'][:8]} is already on it"}
    if fixup_claimed(store, job_id):
        return {"accepted": False, "detail": "a fix-up is already being started"}
    pf = inc.preflight(store, job_id)
    if pf.error or pf.contained or not pf.conflicts:
        return {"accepted": False, "detail": pf.error or "nothing to fix: the merge would not conflict"}
    from steward import repair_episode

    depth = followup_depth(store, job_id)
    if depth >= MAX_FOLLOWUPS and not repair_episode.enabled():
        return {"accepted": False, "detail": f"already {depth} fix-ups; needs you"}
    res = _failed(store, job, pf, "merge conflict", pf.conflicts, runner, start_followup or _default_followup, notify)
    return {**res, "accepted": res.get("outcome") == "followup"}


_FIXUP_EVENTS = ("auto_followup_started", "autopilot_escalated")
_DEAD_FIXUP_STATES = (JobState.STOPPED, JobState.RELEASED)  # a stopped/released fix-up is not the chain; never follow it
_LIVE_FIXUP_STATES = (JobState.CREATED, JobState.CONTRACTED, JobState.RUNNING, JobState.PAUSED, JobState.STOPPING,
                      JobState.WORKER_FINISHED, JobState.CHECKS_PASSED, JobState.NEEDS_HUMAN, JobState.ACCEPTED)


def _launched(store: Store, child: str, c) -> bool:
    return bool(store.kv_get(child, "herdr_tab")) or c.state not in (JobState.CREATED, JobState.CONTRACTED)


def _fixup_children(store: Store, cur: str) -> list:
    """The fix-up children `cur` started, oldest first, each once (a duplicate start of the same child counts once)."""
    out, seen = [], set()
    for e in store.events_of(cur, ("auto_followup_started",)):
        child = str(e.evidence.get("follow_up") or "")
        if child and child not in seen and (c := store.get_job(child)) is not None:
            seen.add(child)
            out.append((child, c, e))
    return out


def followup_status(store: Store, job_id: str) -> dict | None:
    """What the pane says about an accepted job whose auto-merge was blocked, else None (no fix-up ever started).
    `{"kind": "running"|"not_started"|"needs_you", "child": id, "n": attempt, "max": MAX_FOLLOWUPS}`. Follows the
    chain of LIVE fix-ups to the newest one: STOPPED and RELEASED children (stopped duplicates, never-launched
    leftovers) and children a live sibling superseded are skipped and do not count toward the n/max budget. A launch
    failure reads `not_started` (the actionable part is `[ launch ]`); a chain with no live child reads `needs_you`
    with no child (there is nothing to open)."""
    if inc.is_incorporated(store, job_id):
        return None
    cur, n, seen, status = job_id, 0, set(), None
    while cur not in seen:
        seen.add(cur)
        kids = _fixup_children(store, cur)
        fx = store.events_of(cur, _FIXUP_EVENTS)
        if not fx:
            break
        alive = [k for k in kids if k[1].state not in _DEAD_FIXUP_STATES and not inc.is_incorporated(store, k[0])]
        live = [k for k in alive if k[1].state in _LIVE_FIXUP_STATES]
        if not live and fx[-1].what == "autopilot_escalated":
            lf = fx[-1].evidence.get("launch_failed")
            status = ({"kind": "not_started", "child": lf, "n": n + 1} if lf
                      else {"kind": "needs_you", "child": status["child"] if status else "", "n": n})
            break
        if not alive:  # every fix-up it started was stopped/released: nothing live to follow
            status = {"kind": "needs_you", "child": "", "n": n}
            break
        # prefer a launched live child, then the newest started; else the newest failed one
        pool = live or alive
        launched_ones = [k for k in pool if _launched(store, *k[:2])]
        pick = (launched_ones or pool)[-1]
        child, c, _e = pick
        n += 1
        kind = ("needs_you" if c.state in (JobState.FAILED, JobState.NEEDS_HUMAN)
                else "running" if _launched(store, child, c) else "not_started")
        status = {"kind": kind, "child": child, "n": n}
        cur = child
    if status:
        status["max"] = MAX_FOLLOWUPS
    from steward import repair_episode

    ep = repair_episode.open_for(store, job_id)
    if status and ep is not None:
        status["n"], status["max"] = ep.attempts, repair_episode.ATTEMPTS_BEFORE_STRATEGY
        if ep.state == "strategy_review":
            status["kind"] = "reviewing"
    return status


def label(store: Store, job_id: str) -> str:
    """Short `steward jobs` marker: auto-merged sha, or a pending follow-up."""
    try:
        from steward import delivery

        job = store.get_job(job_id)
        hint = delivery.liveness_hint(store, job_id, job.state.value) if job else None
        if hint and not hint.get("delivered") and hint.get("text"):
            return str(hint["text"])[:60]
    except Exception:  # noqa: BLE001 - a label never breaks a row
        pass
    rec = store.kv_get(job_id, "auto_merged")
    if isinstance(rec, dict):
        return f"auto-merged {str(rec.get('merge_commit', ''))[:7]}"
    for e in reversed(store.events(job_id)):
        if e.what == "auto_followup_started":
            return f"merge follow-up {str(e.evidence.get('follow_up', ''))[:8]}"
        if e.what == "autopilot_escalated":
            return "auto-merge escalated"
    return ""


def ensure_attempted(store: Store, job_id: str, runner: Runner, **kw) -> dict | None:
    """Idempotent closeout: an ACCEPTED, not-incorporated job whose autopilot attempt was never recorded gets it now,
    exactly once (the attempt is recorded BEFORE it runs, so a restart never starts a second one). The attempt itself
    is a durable run record (steward.autopilot_bg): a long merged-tree test runs detached so no runner restart or tab
    close can kill it, and a run whose process died without a terminal event is resumed (or escalated) here."""
    from steward import autopilot_bg

    job = store.get_job(job_id)
    if job is None or job.state != JobState.ACCEPTED or inc.is_incorporated(store, job_id):
        return autopilot_bg.resume_if_orphaned(store, job_id, runner, notify=kw.get("notify"))
    if config.train_owns(job.repo):  # the landing train owns merges: do not consume the attempt
        return None
    gated = deadline_gate(store, job_id)  # before the attempt is consumed: clearing later never revives it
    if gated:
        return gated
    if store.kv_get(job_id, ATTEMPT_KEY):
        return autopilot_bg.resume_if_orphaned(store, job_id, runner, notify=kw.get("notify"))
    store.kv_set(job_id, ATTEMPT_KEY, True)
    _event(store, job_id, "autopilot_attempt", via="ensure")
    if autopilot_bg.needed(store, job_id, runner):
        return autopilot_bg.start(store, job_id)
    return autopilot_bg.run_inline(store, job_id, runner, **kw)


def after_accept(store: Store, job_id: str, runner, *, notify=None) -> dict | None:
    """SRD-10 (O2): any accept flows into autopilot. A NEEDS_HUMAN job accepted by hand has no runner, so nothing would
    merge it; the accepting process attempts the (once-only, recorded) merge itself, detached by `autopilot_bg` when the
    merged-tree test is predicted long. Protections (deadline gate, no-merge policy) live inside `ensure_attempted`.
    Never raises: the accept already happened."""
    if notify is None:
        def notify(jid, kind, title, body):  # same event shape as Runner._notify
            from steward import notify as notify_module
            from steward import push as push_module

            title, body = push_module.phone_event(store, jid, kind, body)
            notify_module.emit(store, notify_module.notifier_from_env(runner), notify_module.PhoneEvent(
                kind=kind, job_id=jid, title=title, body=body,
                deep_link=notify_module.deep_link(store.kv_get(jid, "herdr_workspace"), store.kv_get(jid, "herdr_tab"),
                                                  store.kv_get(jid, "herdr_pane")),
                revision=store.get_revision(jid)))
    try:
        return ensure_attempted(store, job_id, runner, notify=notify)
    except Exception as exc:  # noqa: BLE001
        _event(store, job_id, "autopilot_error", error=f"{type(exc).__name__}: {exc}"[:200], via="accept")
        return None
