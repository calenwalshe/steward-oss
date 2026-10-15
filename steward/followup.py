"""Follow-up jobs: a new job that continues an already-finished one.

`steward new "<goal>" --follow-up <PARENT>`: the parent must be ACCEPTED or
RELEASED (STOPPED is allowed with a warning). If the parent was incorporated
its merge is already in the repo's HEAD, so the child's worktree starts there
as usual; otherwise it starts from the parent's branch `steward/<parent>`.
The child's dispatch brief carries a capped, redacted PARENT JOB section
(stored in kv `parent_brief` at creation, rendered by `steward.brief`).
"""

from __future__ import annotations

from pathlib import Path

from steward import evidence
from steward import incorporate as incorporate_module
from steward.diagnosis import redact
from steward.models import JobState, Judgment
from steward.store import Store

BRIEF_CAP = 3000
_ALLOWED = (JobState.ACCEPTED, JobState.RELEASED)


def resolve_job(store: Store, ref: str):
    """A job by full id or unique id prefix; None when unknown or ambiguous."""
    job = store.get_job(ref)
    if job is not None or not ref:
        return job
    hits = [j for j in store.list_jobs() if j.job_id.startswith(ref)]
    return hits[0] if len(hits) == 1 else None


def check_parent(store: Store, ref: str) -> tuple[object | None, str | None, str | None]:
    """`(parent_job, warning, error)`; exactly one of parent/error is set."""
    parent = resolve_job(store, ref)
    if parent is None:
        return None, None, f"unknown follow-up parent: {ref}"
    if parent.state in _ALLOWED:
        return parent, None, None
    if parent.state == JobState.STOPPED:
        return parent, f"parent {parent.job_id[:8]} is STOPPED; its work may be incomplete", None
    return None, None, (f"parent {parent.job_id[:8]} is {parent.state.value}; a follow-up needs an "
                        "ACCEPTED or RELEASED parent (or STOPPED)")


def start_point(store: Store, parent) -> str | None:
    """None (use repo HEAD) when the parent is incorporated, else the parent's branch."""
    return None if incorporate_module.is_incorporated(store, parent.job_id) else incorporate_module.branch_name(parent.job_id)


def _clip(text: str, cap: int) -> str:
    text = text.strip()
    return text if len(text) <= cap else text[: max(cap - 3, 0)].rstrip() + "..."


def changed_files(store: Store, job) -> tuple[int, int, list[str]] | None:
    """`(added, deleted, paths)` the job's own change (see jobdiff): the incorporate merge's own diff once
    merged, else everything since the merge-base with the branch it will be incorporated into."""
    return evidence._numstat(store, job.job_id)


def parent_section(store: Store, parent) -> str:
    """The plain-text PARENT JOB body for the child's brief (redacted, <= ~3k chars)."""
    jid = parent.job_id
    lines = [f"parent: {jid[:8]} ({parent.state.value})",
             "goal: " + _clip(" ".join(parent.goal.split()), 500)]
    try:
        panel = evidence.panel(store, jid, width=100, dec={"kind": "acceptance", "approval": None})
    except Exception:  # noqa: BLE001 - a missing worktree must not block a follow-up
        panel = []
    body = [ln for ln in _skip_worker(panel) if not ln.startswith(("goal:", "      "))]
    if body:
        lines.append("verdict/evidence:")
        lines += ["  " + ln for ln in body[:20]]
    text = evidence.last_worker_text(store, jid)
    if text:
        lines.append("parent's final worker message:")
        lines.append(_clip(text, 900))
    ns = changed_files(store, parent)
    if ns:
        files = ns[2]
        lines.append(f"files changed ({len(files)}): " + ", ".join(files[:15]) + (" ..." if len(files) > 15 else ""))
    merge = incorporate_module.is_incorporated(store, jid)
    lines.append(f"merge commit: {merge[:12]} (already in the repo's HEAD)" if merge
                 else f"not incorporated: this worktree starts from the parent's branch steward/{jid}")
    return _clip(redact("\n".join(lines)), BRIEF_CAP)


def _skip_worker(panel: list[str]) -> list[str]:
    """Panel lines minus the worker-said block (it is included in full separately)."""
    out, skipping = [], False
    for ln in panel:
        if ln.startswith("worker said:"):
            skipping = True
            continue
        if skipping and ln.startswith("  "):
            continue
        skipping = False
        out.append(ln)
    return out


def link(store: Store, parent_id: str, child_id: str) -> None:
    store.kv_set(child_id, "parent_job", parent_id)
    from steward import autopilot

    autopilot.inherit_no_merge_policy(store, child_id, parent_id)
    children = list(store.kv_get(parent_id, "children", default=[]) or [])
    if child_id not in children:
        children.append(child_id)
    store.kv_set(parent_id, "children", children)
    for jid, other, role in ((child_id, parent_id, "parent"), (parent_id, child_id, "child")):
        store.append_event(jid, what="follow_up_created", evidence={role: other},
                           rule="follow_up", judgment=Judgment.human)


def follows_label(store: Store, job_id: str) -> str | None:
    pid = store.kv_get(job_id, "parent_job")
    if not pid:
        return None
    p = store.get_job(pid)
    return f"↳ follow-up of {pid[:8]}" + (f" ({p.state.value})" if p else "")


def repair_start(store: Store, root_id: str, ref: str) -> str | None:
    """Only allow a repair attempt to inherit a linked job's own existing branch."""
    if not ref.startswith("steward/"):
        return None
    att = ref.split("/", 1)[1]
    if store.kv_get(att, "autopilot_followup") != root_id or store.get_job(att) is None:
        return None
    job = store.get_job(root_id)
    if job is None:
        return None
    ok = incorporate_module._git(job.repo, "rev-parse", "--verify", "-q", f"refs/heads/{ref}").returncode == 0
    return ref if ok else None
