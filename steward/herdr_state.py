"""Mirror a job to Herdr's agent state for its worker pane, so Herdr alerts only when it matters.

Herdr raises a needs-input toast for `blocked` and a finished toast when a pane settles to `idle`, so:

* `blocked`  only while the job truly waits on the owner: NEEDS_HUMAN, an approval routed to them (no resolver still
             deciding it), or a blocking question;
* `idle`     (Herdr's "done") only once the job reached its final outcome: STOPPED/FAILED, or ACCEPTED and merged
             (or accepted with nothing to merge);
* `working`  for everything else (turns ending, permissions Steward handles, resolvers deciding, restarts, fix-ups).

Best-effort and deduplicated through the job's kv: a state is sent only when it changes, so a final outcome reports
`idle` once. Helper chat panes are never reported here, so they raise nothing of their own.
"""

from __future__ import annotations

from steward import approvals as approvals_module
from steward import questions as questions_module
from steward.models import JobState
from steward.runner_exec import Runner, SubprocessRunner
from steward.store import Store

KV_KEY = "herdr_agent_state"


def awaits_owner(store: Store, job_id: str) -> bool:
    job = store.get_job(job_id)
    if job is not None and job.state == JobState.NEEDS_HUMAN:
        return True
    for a in approvals_module.list_approvals(store, job_id, status="pending"):
        if not a.get("resolver_pending"):  # a resolver still deciding is not the owner's turn yet
            return True
    try:
        return questions_module.summary(questions_module.load(store, job_id))["blocking"] > 0
    except Exception:  # noqa: BLE001 - an unreadable queue never changes the state report
        return False


def _nothing_to_merge(store: Store, job_id: str) -> bool:
    if store.kv_get(job_id, "incorporated_commit"):
        return True
    try:
        from steward import incorporate  # lazy: incorporate calls back into this module

        pf = incorporate.preflight(store, job_id)
        return bool(pf.contained and not pf.error)
    except Exception:  # noqa: BLE001 - unknown means "not done yet"
        return False


def desired(store: Store, job_id: str) -> str:
    job = store.get_job(job_id)
    if job is not None:
        if job.state in (JobState.STOPPED, JobState.FAILED):
            return "idle"
        if job.state == JobState.ACCEPTED:
            return "idle" if _nothing_to_merge(store, job_id) else "working"
        if job.state == JobState.RELEASED:
            return "idle" if store.kv_get(job_id, "incorporated_commit") else "working"
    return "blocked" if awaits_owner(store, job_id) else "working"


def sync(store: Store, runner_exec: Runner | None, job_id: str) -> None:
    pane = store.kv_get(job_id, "herdr_pane")
    if not pane:
        return
    state = desired(store, job_id)
    if store.kv_get(job_id, KV_KEY) == state:
        return
    try:
        res = (runner_exec or SubprocessRunner()).run(
            ["herdr", "pane", "report-agent", "--source", "steward", "--agent", "steward-shim", "--state", state, pane],
            cwd=store.home)
    except Exception:  # noqa: BLE001 - Herdr is never a hard dependency
        return
    if res.exit_code == 0:
        store.kv_set(job_id, KV_KEY, state)
