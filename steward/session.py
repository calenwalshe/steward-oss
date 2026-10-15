"""Session-facing reads for any Claude session driving Steward: `jobs`, `wait`, `result`.

Read-only over the store (nothing here writes a job, a worktree or a pane).
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from steward import approvals as approvals_module
from steward import owner
from steward import autopilot as autopilot_module
from steward import evidence, followup, jobview
from steward import questions as questions_module
from steward import incorporate as incorporate_module
from steward import replace as replace_module
from steward.diagnosis import redact
from steward.models import JobState
from steward.store import Store

_FINISHED = {JobState.STOPPED, JobState.FAILED, JobState.RELEASED}
_DONE = {JobState.ACCEPTED, JobState.RELEASED, JobState.STOPPED, JobState.FAILED}


def creator_from_env() -> dict:
    """Who is creating a job: the active harness session, Herdr pane, cwd (only what is set)."""
    out = {
        "session": (os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
                    or os.environ.get("CODEX_THREAD_ID") or None),
        "herdr_pane": os.environ.get("HERDR_PANE_ID") or None,
        "cwd": os.getcwd(),
    }
    return {k: v for k, v in out.items() if v}


def _mine(creator: dict | None, me: dict) -> bool:
    creator = creator or {}
    if me.get("session"):
        return creator.get("session") == me["session"]
    if me.get("herdr_pane"):
        return creator.get("herdr_pane") == me["herdr_pane"]
    return False


def _age(created_at: str, now: datetime | None = None) -> str:
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return "?"
    now = now or datetime.now(created.tzinfo or timezone.utc)
    return jobview.ago(max((now - created).total_seconds(), 0))


def _who(creator: dict | None) -> str:
    c = creator or {}
    if c.get("session"):
        return "session:" + c["session"][:8]
    if c.get("herdr_pane"):
        return "pane:" + c["herdr_pane"]
    return "-"


def list_rows(store: Store, *, mine: bool = False, all_jobs: bool = False, me: dict | None = None,
              now: datetime | None = None) -> list[dict]:
    from steward import ownership

    me = creator_from_env() if me is None else me
    carried = ownership.carried_decisions(store)  # a finished job stays listed while its owner decision is open
    rows = []
    for job in store.list_jobs():
        if not all_jobs and job.state in _FINISHED and job.job_id not in carried:
            continue
        creator = store.kv_get(job.job_id, "created_by")
        if mine and not _mine(creator, me):
            continue
        rows.append({"job_id": job.job_id, "id8": job.job_id[:8], "state": job.state.value,
                     "age": _age(job.created_at, now), "goal": " ".join(job.goal.split())[:70],
                     "created_by": creator, "parent_job": store.kv_get(job.job_id, "parent_job"),
                     "autopilot": autopilot_module.label(store, job.job_id),
                     "replaced_by": replace_module.replaced_by(store, job.job_id),
                     "replaces": replace_module.replaces(store, job.job_id),
                     "owner_decisions": carried.get(job.job_id, [])})
    return rows


def _finished_secs(job, now: datetime | None = None) -> float | None:
    """Seconds since the job's last state change (when it finished), else None."""
    try:
        at = datetime.fromisoformat(job.updated_at)
    except ValueError:
        return None
    now = now or datetime.now(at.tzinfo or timezone.utc)
    return max((now - at).total_seconds(), 0.0)


def finished_rows(store: Store, *, since_days: float | None = None, now: datetime | None = None) -> list[dict]:
    """Every finished job (ACCEPTED, RELEASED, STOPPED, FAILED, NEEDS_HUMAN at acceptance) that finished within
    `since_days` (None: all of history), newest first: the list `steward export` can target any old job from."""
    from steward import export as export_module
    from steward import tabnames

    rows = []
    for job in store.list_jobs():
        if not export_module.eligible(store, job):
            continue
        secs = _finished_secs(job, now)
        if since_days is not None and (secs is None or secs > since_days * 86400):
            continue
        rows.append({"job_id": job.job_id, "id8": job.job_id[:8], "state": job.state.value,
                     "name": tabnames.job_name(store, job, persist=False), "finished_at": job.updated_at,
                     "finished_ago": jobview.ago(secs) if secs is not None else "?",
                     "exported": export_module.exported(store, job.job_id),
                     "goal": " ".join(job.goal.split())[:70]})
    rows.sort(key=lambda r: r["finished_at"], reverse=True)
    return rows


def render_finished(rows: list[dict]) -> str:
    if not rows:
        return "(no finished jobs)"
    return "\n".join(f"{r['id8']}  {r['state']:<15} {r['finished_ago'] + ' ago':>10}  {r['name']:<24} "
                     + (f"exported → {r['exported']}" if r["exported"] else "not exported") for r in rows)


def render_rows(rows: list[dict]) -> str:
    if not rows:
        return "(no jobs)"
    return "\n".join(f"{r['id8']}  {r['state']:<15} {r['age']:>6}  {_who(r['created_by']):<18} {r['goal']}"
                     + (f"  [{r['autopilot']}]" if r.get("autopilot") else "")
                     + (f"  [replaced by {r['replaced_by'][:8]}]" if r.get("replaced_by") else "")
                     + (f"  [replaces {r['replaces'][:8]}]" if r.get("replaces") else "")
                     + "".join(f"\n          ◆ needs you: {d['text']}" for d in r.get("owner_decisions") or [])
                     for r in rows)


def wait_state(store: Store, job_id: str, until: str) -> tuple[str, str] | None:
    """`(STATE, reason)` when the wait condition holds now, else None."""
    job = store.get_job(job_id)
    if job is None:
        return None
    if until in ("decision", "any"):
        dec = evidence.decision(store, job_id)
        if dec is not None and not (dec.get("approval") or {}).get("resolver_pending"):  # resolver deciding: transient
            approvals = approvals_module.list_approvals(store, job_id)
            events = jobview.event_dicts(store.events(job_id)[-30:])
            return job.state.value, jobview.reason(approvals, job.state.value, events) or f"waiting on {owner.name()}"
    if until in ("done", "any") and job.state in _DONE:
        return job.state.value, f"job is {job.state.value}"
    return None


SETTLE_S = 30.0  # a live job's owner-needed state must hold this long: the resolver / tech lead often clears it first


def wait(store: Store, job_id: str, until: str = "any", timeout: float | None = None, *,
         poll: float = 10.0, sleep=time.sleep, clock=time.monotonic, settle_s: float = SETTLE_S) -> tuple[int, str]:
    """Block until the condition holds; `(0, line)` or `(2, timeout line)`. A parked (NEEDS_HUMAN) or finished job
    returns at once; an ask on a still-live job returns only once it has held for `settle_s` (mechanics item 14:
    `wait` returned "RUNNING: resolver deciding (7s)" twice for a blocker the tech lead resolved seconds later)."""
    start, seen = clock(), None
    while True:
        hit = wait_state(store, job_id, until)
        if hit:
            settled = hit[0] in {s.value for s in _DONE} or hit[0] == JobState.NEEDS_HUMAN.value
            seen = clock() if seen is None else seen
            if settled or clock() - seen >= settle_s:
                return 0, f"{job_id[:8]} {hit[0]}: {hit[1]}"
        else:
            seen = None
        if timeout is not None and clock() - start >= timeout:
            job = store.get_job(job_id)
            return 2, f"{job_id[:8]} {job.state.value if job else '?'}: timed out after {timeout:g}s"
        sleep(poll if seen is None else max(0.5, min(poll, settle_s - (clock() - seen))))


def _next_commands(state: JobState, jid: str, incorporated: bool, waiting: int = 0) -> list[str]:
    cmds = _state_commands(state, jid, incorporated)
    if waiting > 0:  # the question queue (Phase 17-06): `--json` feeds AskUserQuestion rounds of 4
        cmds = [*cmds, f"steward questions {jid} --json"]
    return cmds


def _state_commands(state: JobState, jid: str, incorporated: bool) -> list[str]:
    i8 = jid[:8]
    if state == JobState.NEEDS_HUMAN:
        return [f"steward accept {jid}", f'steward send-back {jid} "<instructions>"', f"steward stop {jid}"]
    if state in (JobState.ACCEPTED, JobState.RELEASED):
        cmds = [] if incorporated else [f"steward incorporate {jid} --dry-run", f"steward incorporate {jid}"]
        if state == JobState.ACCEPTED:
            cmds.append(f"steward release {jid}")
        return cmds + [f'steward new "<goal>" --follow-up {i8}']
    if state == JobState.STOPPED:
        return [f'steward new "<goal>" --follow-up {i8}']
    if state in (JobState.RUNNING, JobState.PAUSED, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED):
        return [f"steward wait {i8}", f"steward status {jid}"]
    if state in (JobState.CREATED, JobState.CONTRACTED):
        return [f"steward launch {jid}"]
    return []


def build_result(store: Store, job_id: str, projects_dir: Path | None = None,
                 sessions_dir: Path | None = None) -> dict | None:
    job = store.get_job(job_id)
    if job is None:
        return None
    goal, finish = evidence.split_goal(job.goal)
    dec = evidence.decision(store, job_id)
    panel = evidence.panel(store, job_id, width=100, projects_dir=projects_dir,
                           dec={"kind": "acceptance", "approval": None})
    ns = followup.changed_files(store, job)
    merge = incorporate_module.is_incorporated(store, job_id)
    cost = evidence.job_cost_usd(store, job_id)
    events = jobview.event_dicts(store.events(job_id)[-30:])
    approvals = approvals_module.list_approvals(store, job_id)
    wt = evidence.chat_module.worktree_path(store, job_id)
    text = evidence.last_worker_text(store, job_id, projects_dir, sessions_dir)
    parent = store.kv_get(job_id, "parent_job")
    qs = questions_module.summary(questions_module.load(store, job_id))
    queue = {"waiting": qs["waiting"], "starred": qs["starred"], "needs_you": qs["needs_you"]} if qs["waiting"] else None
    from steward import fleet_closeout
    fleet = fleet_closeout.projection(store, job_id)  # fleet jobs only: legacy results are unchanged
    digest = tech_lead_digest(store, job_id)  # v2.6 (SUP-16): only when the tech lead acted
    from steward import blockers as blockers_module, ownership

    proj = ownership.projection(store, job_id)
    # The stored projection is only refreshed by a reconcile tick, so it goes stale when a blocker resolves between
    # ticks (or on a job reconcile no longer visits): the status line comes from the live view of open blockers.
    try:
        view = ownership.owner_view(store, job_id)
        live = {"status": view.status, "text": view.text, "next_actor": view.next_actor,
                "next_retry_at": view.next_retry_at,
                "owner_kinds": sorted({b.kind for b in blockers_module.open_blockers(store, job_id)
                                       if b.disposition == "confirmed_human"})}
    except Exception:  # noqa: BLE001 - result never fails on the status line; fall back to the snapshot
        live = {k: proj.get(k) for k in ("status", "text", "next_actor", "next_retry_at")}
    own = live if live.get("status") not in (None, "none") else None
    return {
        **({"fleet_candidate": fleet} if fleet is not None else {}),
        **({"tech_lead": digest} if digest is not None else {}),
        **({"ownership": own} if own is not None else {}),
        "job_id": job_id, "state": job.state.value, "goal": redact(goal or job.goal),
        "finish_line": [redact(x) for x in evidence.finish_items(finish)] if finish else [],
        f"waiting_on_{owner.actor_key()}": jobview.reason(approvals, job.state.value, events,
                                                           ownership=live) if dec else None,
        "evidence": [redact(x) for x in panel],
        "worker_message": redact(text) if text else None,
        "files_changed": ns[2] if ns else [],
        "lines_added": ns[0] if ns else None, "lines_deleted": ns[1] if ns else None,
        "branch": incorporate_module.branch_name(job_id), "worktree": str(wt),
        "merge_commit": merge, "cost_usd": cost,
        "parent_job": parent, "children": store.kv_get(job_id, "children", default=[]) or [],
        "replaced_by": replace_module.replaced_by(store, job_id), "replaces": replace_module.replaces(store, job_id),
        "questions": queue,
        "next_commands": _next_commands(job.state, job_id, bool(merge), qs["waiting"]),
    }


def tech_lead_digest(store: Store, job_id: str) -> dict | None:
    """Every autonomous tech-lead decision (action, model, signature, outcome, plan item) and every escalation."""
    actions, escalations = [], []
    for e in store.events(job_id):
        ev = e.evidence or {}
        if e.what == "tech_lead_action":
            actions.append({"ts": e.ts, "action": ev.get("action"), "outcome": ev.get("outcome"),
                            "lead": ev.get("lead"), "model": ev.get("model"), "signature": ev.get("signature"),
                            "plan_item": ev.get("plan_item") or "", "detail": ev.get("detail") or "",
                            "commit": ev.get("commit"), "floor_class": ev.get("floor_class")})
        elif e.what == "tech_lead_escalated":
            escalations.append({"kind": ev.get("kind"), "text": ev.get("text")})
    if not actions and not escalations:
        return None
    from steward import tech_lead_actions

    return {"actions": actions, "escalations": escalations, "precision": tech_lead_actions.score(store, job_id)}


_TL_VERBS = {"executed": {"unblock": "cleared/redirected", "send_back": "sent back", "takeover": "took over",
                          "escalate": "escalated"},
             "started": {"takeover": "started a takeover"}}


def _tech_lead_lines(d: dict) -> list[str]:
    out = []
    for a in d["actions"]:
        verb = _TL_VERBS.get(a["outcome"], {}).get(a["action"]) or f"{a['action']} {a['outcome']}"
        detail = a["detail"]
        if a["action"] == "takeover" and a["outcome"] == "executed" and detail.startswith("took over: "):
            detail = detail[len("took over: "):]
        line = f"  tech lead ({a['lead']}, {a['model']}) {verb}: {detail[:160]}"
        tags = [t for t in (f"sig {a['signature']}" if a["signature"] else "",
                            f"plan {a['plan_item']}" if a["plan_item"] else "",
                            f"commit {str(a['commit'])[:8]}" if a.get("commit") else "",
                            f"floor {a['floor_class']}" if a.get("floor_class") else "") if t]
        out.append(line + (f"  [{'; '.join(tags)}]" if tags else ""))
    for x in d["escalations"]:
        out.append(f"  NEEDS_YOU ({x['kind']}): {str(x['text'])[:180]}")
    p = d.get("precision") or {}
    if p.get("precision") is not None:
        out.append(f"  precision: {p['correct']} correct / {p['wrong']} wrong ({p['precision']:.0%})")
    return out


def render_result(r: dict) -> str:
    waiting = r.get(f"waiting_on_{owner.actor_key()}")
    out = [f"JOB {r['job_id']}  [{r['state']}]", "", "GOAL", r["goal"]]
    if r["finish_line"]:
        out += ["", "FINISH LINE"] + [f"  - {x}" for x in r["finish_line"]]
    out += ["", "STATE", r["state"] + (f"  (waiting: {waiting})" if waiting else "")]
    own = r.get("ownership")
    if own:
        out += ["", "STEWARD", f"  {own['text'] or own['status']}  (next: {own.get('next_actor') or '?'})"]
    out += ["", "VERDICT / EVIDENCE"] + (["  " + ln for ln in r["evidence"]] or ["  (none)"])
    out += ["", "WORKER'S FINAL MESSAGE", r["worker_message"] or "(no transcript found)"]
    n = len(r["files_changed"])
    out += ["", f"OUTPUT FILES ({n})"] + ([f"  {f}" for f in r["files_changed"]] or ["  (none / no baseline)"])
    if r.get("fleet_candidate") is not None:
        from steward import fleet_closeout
        out += ["", "FLEET CANDIDATE"] + ["  " + ln for ln in fleet_closeout.candidate_lines(r["fleet_candidate"])]
    if r.get("tech_lead"):
        out += ["", "TECH LEAD"] + _tech_lead_lines(r["tech_lead"])
    out += ["", "BRANCH", f"  {r['branch']}", f"  worktree {r['worktree']}",
            "  merge commit " + (r["merge_commit"] or "none (not incorporated)")]
    out += ["", "COST", "  " + ("$%.2f" % r["cost_usd"] if r["cost_usd"] is not None else "n/a")]
    if r["parent_job"] or r["children"] or r.get("replaced_by") or r.get("replaces"):
        out += ["", "LINKS"]
        if r["parent_job"]:
            out.append(f"  follow-up of {r['parent_job']}")
        out += [f"  follow-up child {c}" for c in r["children"]]
        if r.get("replaced_by"):
            out.append(f"  replaced by {r['replaced_by'][:8]}  (accept/incorporate refused; work on the replacement)")
        if r.get("replaces"):
            out.append(f"  replaces {r['replaces'][:8]}")
    q = r.get("questions")
    if q:
        out += ["", f"questions: {q['waiting']} waiting ({q['starred']} ★) - steward questions {r['job_id']} --json"]
    out += ["", "NEXT"] + ([f"  {c}" for c in r["next_commands"]] or ["  (nothing to do)"])
    return "\n".join(out)
