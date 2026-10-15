"""Deadline evidence (v2.2, DL-06): milestones and the candidate are met only by code-checked facts at a real SHA.

`commit` milestone: the earliest commit on the job branch (base..HEAD) at which every declared path has been
changed and still exists. A matching subject line, a worker statement or a `met` field proves nothing.
`units:<label>`: the ETA's structured unit completion from git (planning summaries or unit commits) — never the
agent's own descriptions. Candidate (`verify`): for every current required item (owner-required, worker-added and
worker-proposed-optional, which stays required until the owner classifies it) an evidence record at the exact verified
candidate SHA: the item's declared paths changed vs the baseline and present at that SHA, and its declared tests
passing in that same closeout verification run (no second test runner). A prior SHA that passed and later regressed
does not count. The transient plan file in the final diff, a stale plan revision or any unproven required item
leaves the candidate `acceptance unresolved`; human acceptance is a later decision, never item proof.
"""

from __future__ import annotations

from datetime import datetime, timezone

from steward import deadline, deadline_plan
from steward.store import Store

K_VERDICT = "deadline_candidate"
UNIT_SOURCES = ("planning", "commits")  # git-derived unit completion only


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_test(name: str) -> str:
    """`tests/test_x.py::TestC::test_y[a]` and JUnit's `tests.test_x.TestC::test_y[a]` -> one comparable id."""
    head, sep, rest = name.partition("[")
    head = head.replace("\\", "/")
    if ".py::" in head:
        head = head.replace(".py::", "::")
    return head.replace("/", ".").replace("::", ".") + sep + rest


def history(worktree, runner, base: str, head: str, cap: int = 2000) -> list[tuple[str, float, dict]]:
    """[(sha, commit epoch, {path: status})] oldest first for base..head (one git call, no renames)."""
    rng = f"{base}..{head}" if base else head
    res = runner.run(["git", "log", "--reverse", "--no-renames", "--name-status", "-n", str(cap),
                      "--format=%x1e%H%x1f%ct", rng], cwd=worktree)
    if res.exit_code != 0:
        raise RuntimeError(f"git log failed: {(res.stderr or '').strip()[:120]}")
    out = []
    for block in (res.stdout or "").split("\x1e"):
        block = block.strip("\n")
        if not block:
            continue
        first, _, rest = block.partition("\n")
        sha, _, ct = first.partition("\x1f")
        changes = {}
        for line in rest.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0]:
                changes[parts[-1]] = parts[0][0]
        try:
            out.append((sha.strip(), float(ct), changes))
        except ValueError:
            continue
    return out


def commit_milestone(hist: list[tuple[str, float, dict]], paths: list[str]) -> dict | None:
    """The first commit where every declared path is changed (vs base) and present; None when not (yet) met."""
    if not paths:
        return None
    touched: set[str] = set()
    for sha, at, changes in hist:
        for path, status in changes.items():
            if path in paths:
                if status == "D":
                    touched.discard(path)
                else:
                    touched.add(path)
        if touched >= set(paths):
            return {"met_sha": sha, "met_at": _iso(at), "evidence": {"kind": "commit", "paths": list(paths)}}
    return None


def units_milestone(structure, label: str) -> dict | None:
    if structure is None or getattr(structure, "units_source", "none") not in UNIT_SOURCES:
        return None
    at = (getattr(structure, "done", None) or {}).get(label)
    if at is None:
        return None
    return {"met_sha": getattr(structure, "head", "") or "", "met_at": _iso(float(at)),
            "evidence": {"kind": "units", "label": label, "source": structure.units_source}}


def milestone_evidence(milestone: dict, *, hist, structure=None) -> dict | None:
    kind = milestone.get("evidence") or ""
    if kind == "commit":
        return commit_milestone(hist, milestone.get("paths") or [])
    if kind.startswith("units:"):
        return units_milestone(structure, kind.split(":", 1)[1])
    return None  # `verify` is the candidate (closeout); `time` is a fallback checkpoint with no evidence


# -- the candidate ---------------------------------------------------------------------------------------

def _test_results(verification) -> tuple[dict[str, str], dict[str, int]]:
    tests: dict[str, str] = {}
    commands: dict[str, int] = {}
    for r in getattr(verification, "results", None) or []:
        commands[r.command] = r.exit_code
        for name, outcome in (r.tests or {}).items():
            tests[canonical_test(name)] = outcome
    return tests, commands


def _git_set(runner, worktree, *args) -> set[str]:
    res = runner.run(["git", *args], cwd=worktree)
    if res.exit_code != 0:
        raise RuntimeError(f"git {args[0]} failed: {(res.stderr or '').strip()[:120]}")
    return {ln for ln in (res.stdout or "").splitlines() if ln}


def candidate_verdict(*, commitment: dict, plan: dict | None, verification, worktree, runner, base: str,
                      job_id: str) -> dict:
    """Pure over its inputs (git reads only): the per-item evidence manifest at `verification.commit_sha`."""
    sha = verification.commit_sha
    reasons: list[str] = []
    tests, commands = _test_results(verification)
    changed = _git_set(runner, worktree, "diff", "--name-only", base, sha) if base else set()
    present = _git_set(runner, worktree, "ls-tree", "-r", "--name-only", sha)
    plan_in_diff = deadline_plan.in_final_diff(worktree, runner, base, sha, job_id)
    if plan_in_diff:
        reasons.append(f"plan_artifact_in_diff: remove {deadline_plan.plan_path(job_id)} in a cleanup commit")
    if plan is None:
        reasons.append("no_adopted_plan: no item-to-evidence mapping")
    elif plan.get("commitment_rev") != commitment.get("rev"):
        reasons.append(f"stale_plan_rev: plan for rev {plan.get('commitment_rev')}, commitment is rev "
                       f"{commitment.get('rev')}")
    elif plan.get("source") != "worker":
        reasons.append("fallback_plan: no item-to-evidence mapping")
    plan_items = {i["id"]: i for i in (plan or {}).get("items") or []}
    dropped = set((plan or {}).get("dropped") or [])
    owner = {i["id"]: i for i in commitment.get("items") or []}
    ids = list(owner) + [i for i in plan_items if i not in owner]
    records = []
    for iid in ids:
        owner_cls = (owner.get(iid) or {}).get("class")
        cls = "optional" if owner_cls == "optional" else "required"
        rec: dict = {"item": iid, "class": cls, "sha": sha, "text": (owner.get(iid) or plan_items.get(iid) or {}).get("text", "")}
        if cls == "optional" and iid in dropped:
            records.append({**rec, "outcome": "dropped", "reasons": ["owner-optional item dropped by the plan"]})
            continue
        mapping = (plan_items.get(iid) or {}).get("evidence") or {}
        paths, item_tests = list(mapping.get("paths") or []), list(mapping.get("tests") or [])
        why = []
        if iid not in plan_items:
            why.append("no_plan_mapping")
        if not paths:
            why.append("no_declared_artifact")
        for p in paths:
            if p not in changed:
                why.append(f"artifact_unchanged:{p}")
            elif p not in present:
                why.append(f"artifact_missing_at_sha:{p}")
        if not item_tests:
            why.append("no_item_check")
        results = {}
        for t in item_tests:
            if t.startswith("cmd:"):
                code = commands.get(t[4:].strip())
                results[t] = "passed" if code == 0 else ("missing" if code is None else f"exit {code}")
            else:
                results[t] = tests.get(canonical_test(t), "missing")
            if results[t] != "passed":
                why.append(f"check_{'missing' if results[t] == 'missing' else 'failed'}:{t}")
        records.append({**rec, "paths": paths, "tests": item_tests, "results": results,
                        "outcome": "proven" if not why else "unproven", "reasons": why})
    unproven = [r["item"] for r in records if r["class"] == "required" and r["outcome"] != "proven"]
    if unproven:
        reasons.append(f"unproven_required_items: {', '.join(unproven)}")
    return {"sha": sha, "ready": not reasons, "reasons": reasons, "items": records,
            "plan_rev": (plan or {}).get("plan_rev"), "plan_source": (plan or {}).get("source"),
            "commitment_rev": commitment.get("rev"), "plan_in_diff": plan_in_diff,
            "optional_dropped": sorted(r["item"] for r in records if r["outcome"] == "dropped")}


def record_verdict(store: Store, job_id: str, verdict: dict) -> None:
    """Persist the verdict keyed by its SHA (closeout reads it for that exact SHA only) plus one event."""
    from steward.models import Judgment

    store.kv_set(job_id, K_VERDICT, verdict)
    store.append_event(job_id, what="deadline_candidate_evidence",
                       evidence={"sha": verdict["sha"], "ready": verdict["ready"], "reasons": verdict["reasons"],
                                 "items": [{k: r.get(k) for k in ("item", "class", "outcome", "reasons")}
                                           for r in verdict["items"]],
                                 "plan_rev": verdict.get("plan_rev"), "commitment_rev": verdict.get("commitment_rev")},
                       rule="deadline_item_evidence_at_sha", judgment=Judgment.code)


def verdict_for(store: Store, job_id: str, sha: str) -> dict | None:
    v = store.kv_get(job_id, K_VERDICT)
    return v if isinstance(v, dict) and v.get("sha") == sha else None


def evaluate_candidate(store: Store, job_id: str, *, verification, worktree, runner, now=None) -> dict | None:
    """Runner hook before closeout: compute + persist the verdict for a deadline job whose checks are clean.
    Never raises: an error is itself an unresolved verdict (unsure is never READY)."""
    rec = deadline.current(store, job_id)
    if deadline.merge_protected(store, job_id) is None:
        return None
    if getattr(verification, "dirty", False) or getattr(verification, "new_failures", None):
        return None
    try:
        if rec is None:
            verdict = {"sha": verification.commit_sha, "ready": False, "items": [], "plan_in_diff": False,
                       "reasons": ["no deadline in force: acceptance is the owner's"], "optional_dropped": []}
        else:
            job = store.get_job(job_id)
            from steward import questions

            base = questions._scan_base(store, job_id, job, verification.commit_sha, runner)
            verdict = candidate_verdict(commitment=rec, plan=deadline_plan.current_plan(store, job_id),
                                        verification=verification, worktree=worktree, runner=runner, base=base,
                                        job_id=job_id)
    except Exception as exc:  # noqa: BLE001
        verdict = {"sha": verification.commit_sha, "ready": False, "items": [], "plan_in_diff": False,
                   "reasons": [f"evidence_error: {str(exc)[:160]}"], "optional_dropped": []}
    verdict["at"] = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    record_verdict(store, job_id, verdict)
    return verdict
