"""Landing-train housekeeping (2026-10-08 spec): stop fix-ups made pointless by a landing, archive orphan branches.
Never deletes a branch: orphans are renamed under archive/."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from steward import incorporate as inc
from steward.models import JobState
from steward.store import Store

LIVE = {JobState.CREATED, JobState.CONTRACTED, JobState.RUNNING, JobState.PAUSED, JobState.WORKER_FINISHED,
        JobState.CHECKS_PASSED, JobState.NEEDS_HUMAN}  # a repair of a done parent is pointless in any state

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MERGE_GOAL_PREFIX = "Merge `"


def stop_superseded_fixups(store: Store, stop_fn: Callable[[str], object], *,
                           include_needs_human: bool = True) -> list[str]:
    stopped = []
    for job in store.list_jobs(LIVE):
        if job.state == JobState.NEEDS_HUMAN and not include_needs_human:
            continue
        parent = store.kv_get(job.job_id, "autopilot_followup")
        if job.goal.startswith(MERGE_GOAL_PREFIX) and parent and inc.is_incorporated(store, parent):
            stop_fn(job.job_id)
            stopped.append(job.job_id)
    return stopped


def stop_marked_fixups(store: Store, stop_fn: Callable[[str], object]) -> list[str]:
    from steward import cleanup
    from steward.models import Judgment
    out = []
    for child, cause in cleanup.pending_fixup_stops(store):
        job = store.get_job(child)
        if job is not None and job.state in LIVE:
            res = stop_fn(child)
            if isinstance(res, dict) and not res.get("accepted", True):
                continue  # relay refused: keep the mark, retry next tick
            store.append_event(child, what="fixup_stopped_parent_done", evidence={"cause": cause},
                               rule="cleanup", judgment=Judgment.code)
            out.append(child)
        cleanup.clear_fixup_stop(store, child)
    return out


def archive_orphan_branches(store: Store, repo) -> list[str]:
    lst = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/steward/"],
                         cwd=Path(repo), capture_output=True, text=True)
    if lst.returncode != 0:
        return []
    out = lst.stdout.split()
    moved = []
    for ref in out:
        job_id = ref.removeprefix("steward/")
        if not _UUID.match(job_id):
            continue
        if store.get_job(job_id) is not None:
            continue
        r = subprocess.run(["git", "branch", "-m", ref, f"archive/{ref}"], cwd=Path(repo),
                           capture_output=True, text=True)
        if r.returncode == 0:
            moved.append(ref)
    return moved
