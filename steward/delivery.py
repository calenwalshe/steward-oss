"""Delivery continuity (continuous job ownership §9; Plan 4).

Tracks only the milestones a job requires — worker completion, verification, acceptance, incorporation, activation —
each with evidence and a responsible actor, inside Plan 1's ownership projection (`register_milestone`). Accepted is
not incorporated and incorporated is not activated. Authority is looked up, never inferred: an explicit owner
prohibition makes a milestone not required; owner configuration ([autopilot] merge, STEWARD_AUTODEPLOY), a Plan 2
grant or an owner-approved delivery decision allows it; otherwise the owner gets the exact decision, once. This module
reads state and records decisions only; merging, verifying and activating run in the detached delivery worker.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from steward import approvals, blockers, config, ownership
from steward import incorporate as inc
from steward.actor import Actor, ActorRef
from steward.blockers import KindSpec
from steward.models import JobState, Judgment
from steward.store import Store

MILESTONES = ("worker_completion", "verification", "acceptance", "incorporation", "activation")
SWEEP_EVERY_S = 30.0
WORKER_LEASE_S = 900.0
RECEIPT_MAX_CHARS = 2000
DECISION_KIND = "delivery_decision"
WAITING_KIND = "delivery_waiting"
RED_KIND = "merged_tree_red"
LAUNCH_KIND = "repair_launch_failed"
OUTCOME_KIND = "outcome_unproven"
APPROVAL_KIND = "delivery"
SUMMARY_KEY = "delivery_summary"
TRACKING_KEY = "delivery_tracking_since"
PSEUDO_JOB = "_delivery"
WORKER_KEY = "delivery_worker"
WORKER_HEARTBEAT_S = 30.0
STUCK_KIND = "delivery_worker_stuck"
STUCK_ATTEMPTS = 3
LOG_MAX_BYTES = 1_000_000
RETRY_AFTER_S = 30.0
RETRY_MAX_S = 600.0
RETRY_KEY = "delivery_retry"   # kv(job): {"n": consecutive transient outcomes, "at": earliest next spawn}
CONTROLLER = ActorRef(Actor.CONTROLLER, "delivery")
_DONE = (JobState.ACCEPTED, JobState.RELEASED)


@dataclass(frozen=True)
class Authority:
    allowed: bool
    prohibited: bool
    source: str
    reason: str


@dataclass(frozen=True)
class Milestone:
    name: str
    required: bool
    status: str          # done | pending | blocked | not_required | unknown
    actor: str           # who is responsible next / who completed it
    evidence: tuple[str, ...]
    reason: str


def _git(repo, *args: str) -> str:
    r = inc._git(repo, *args)
    return r.stdout.strip() if r.returncode == 0 else ""


def target_branch(repo) -> str:
    return _git(repo, "symbolic-ref", "--short", "-q", "HEAD")


def _owner_decision(store: Store, job_id: str, milestone: str) -> tuple[str, str] | None:
    """The owner's latest decided delivery approval for this milestone: ("approved"|"denied", approval id)."""
    for a in reversed(approvals.list_approvals(store, job_id)):
        if a.get("kind") == APPROVAL_KIND and a.get("milestone") == milestone and a.get("status") in ("approved", "denied") \
                and a.get("decided_by") == "human":
            return a["status"], a["id"]
    return None


def _grant(store: Store, job_id: str, milestone: str, target: str) -> Authority | None:
    """Plan 2's effective delivery authority — the single place Plan 4 consumes it. A prohibition or a stopped job
    binds; a covering grant allows; `not_granted` returns None so the existing owner configuration decides."""
    from steward import authority

    v = authority.delivery_authorized(store, job_id, milestone, target)
    if v.basis.startswith("prohibited:") or v.basis == "job_stopped":
        return Authority(False, True, v.basis, v.reason)
    if v.allow:
        return Authority(True, False, v.basis, v.reason)
    return None


def incorporation_authority(store: Store, job_id: str) -> Authority:
    from steward import autopilot, deadline, fleet_closeout

    job = store.get_job(job_id)
    if job is None:
        return Authority(False, False, "", "unknown job")
    protected = autopilot.no_merge_protection(store, job_id)
    if protected:
        return Authority(False, True, f"job goal ({protected[0][:8]})", f"owner said not to merge: {protected[1]}")
    decided = _owner_decision(store, job_id, "incorporation")
    if decided and decided[0] == "denied":
        return Authority(False, True, f"owner decision {decided[1]}", "owner declined incorporation")
    if fleet_closeout.is_fleet(store, job_id) and not decided:
        return Authority(False, False, "", "fleet incorporation is the owner's decision (FLEET-14)")
    if deadline.merge_protected(store, job_id) and not decided:
        return Authority(False, False, "", "deadline job: only the owner's incorporate merges it")
    floor = autopilot.floor_reason(store, job_id)
    if floor and not decided:
        return Authority(False, False, "", f"a human merges this job: {floor} (floor action)")
    if decided:
        return Authority(True, False, f"owner approval {decided[1]}", "owner approved incorporation")
    granted = _grant(store, job_id, "incorporate", target_branch(job.repo))
    if granted is not None:
        return granted
    if autopilot.enabled():
        return Authority(True, False, "steward.toml [autopilot] merge", "auto-incorporation is enabled")
    return Authority(False, False, "", "no incorporation authority: [autopilot] merge is off and no grant covers it")


def activation_authority(store: Store, job_id: str, target: str = "control") -> Authority:
    from steward import deploy_self

    decided = _owner_decision(store, job_id, "activation")
    if decided:
        return Authority(decided[0] == "approved", decided[0] == "denied", f"owner decision {decided[1]}",
                         "owner approved activation" if decided[0] == "approved" else "owner declined activation")
    granted = _grant(store, job_id, "activate", target)
    if granted is not None:
        return granted
    if deploy_self.autodeploy_enabled():
        return Authority(True, False, "STEWARD_AUTODEPLOY", "auto-deploy is on")
    return Authority(False, False, "", "no activation authority: auto-deploy is off and no grant covers it")


def _has_unmerged_commits(job) -> bool:
    branch = inc.branch_name(job.job_id)
    if not _git(job.repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
        return False
    return inc._git(job.repo, "merge-base", "--is-ancestor", branch, "HEAD").returncode != 0


def activation_targets(store: Store, job_id: str) -> tuple[str, ...]:
    """Which running things must load this job's code: an `activation` outcome in the contract, or a merge of
    Steward's own steward/ code (the existing restart_needed / deploy-self path)."""
    from steward import autopilot, outcome_scope
    from steward.contract import load_contract

    job = store.get_job(job_id)
    targets: list[str] = []
    contract = load_contract(store, job_id)
    if contract is not None and any(o.requires == "activation" for o in outcome_scope.outcomes(contract)):
        if job is not None and not autopilot._is_steward_repo(job.repo, None):
            return ("declared",)
        targets.append("control")
    deployed = store.last_event(job_id, ("restart_needed", "auto_deployed", "auto_deploy_failed", "auto_deploy_blocked"))
    merged = inc.is_incorporated(store, job_id)
    if deployed is not None:
        targets += ["control", "runners"]
    elif merged and job is not None and autopilot._is_steward_repo(job.repo, None):
        changed = _git(job.repo, "diff", "--name-only", f"{merged}^1", merged).splitlines()
        if any(f.startswith("steward/") for f in changed):
            targets += ["control", "runners"]
    return tuple(dict.fromkeys(targets))


def _open(store: Store, job_id: str, *kinds: str) -> list:
    return [b for b in blockers.open_blockers(store, job_id) if b.kind in kinds]


def incorporation_milestone(store: Store, job_id: str) -> dict:
    job = store.get_job(job_id)
    merged = inc.is_incorporated(store, job_id)
    if merged:
        red = _open(store, job_id, RED_KIND)
        if red:
            return {"status": "blocked", "required": True, "evidence": [b.blocker_id for b in red],
                    "actor": "tech_lead", "reason": red[0].claim}
        rec = receipts(store, job_id, "incorporation")
        who = rec[-1]["actor"] if rec else "unknown (merged before receipts)"
        return {"status": "done", "required": True, "evidence": [f"merge:{merged}"], "actor": who, "reason": ""}
    if job is None or job.state not in _DONE:
        return {"status": "pending", "required": True, "evidence": [], "actor": "worker", "reason": "awaits acceptance"}
    auth = incorporation_authority(store, job_id)
    if auth.prohibited:
        return {"status": "not_required", "required": False, "evidence": [auth.source], "actor": "owner",
                "reason": auth.reason}
    if not _has_unmerged_commits(job):
        return {"status": "not_required", "required": False, "evidence": [], "actor": "controller",
                "reason": "no commits to incorporate"}
    stuck = _open(store, job_id, WAITING_KIND, RED_KIND, LAUNCH_KIND)
    if stuck:
        return {"status": "blocked", "required": True, "evidence": [b.blocker_id for b in stuck],
                "actor": blockers.spec(stuck[0].kind).responsible, "reason": stuck[0].claim}
    return {"status": "pending", "required": True, "evidence": [auth.source] if auth.source else [],
            "actor": "controller" if auth.allowed else "owner", "reason": auth.reason}


def activation_milestone(store: Store, job_id: str) -> dict:
    targets = activation_targets(store, job_id)
    if not targets:
        return {"status": "not_required", "required": False, "evidence": [], "actor": "controller",
                "reason": "nothing running needs this code"}
    merged = inc.is_incorporated(store, job_id)
    done = [r for r in receipts(store, job_id, "activation") if merged and r["revision_contains"] == merged]
    if done:
        return {"status": "done", "required": True, "evidence": [f"activation:{done[-1]['seq']}"],
                "actor": done[-1]["actor"], "reason": ""}
    if not merged:
        return {"status": "pending", "required": True, "evidence": [], "actor": "controller",
                "reason": "awaits incorporation"}
    unproven = _open(store, job_id, "activation_unproven")
    if unproven:
        return {"status": "blocked", "required": True, "evidence": [b.blocker_id for b in unproven],
                "actor": "tech_lead", "reason": unproven[0].claim}
    auth = activation_authority(store, job_id, targets[0])
    if auth.prohibited:
        return {"status": "not_required", "required": False, "evidence": [auth.source], "actor": "owner",
                "reason": auth.reason}
    return {"status": "pending", "required": True, "evidence": list(targets), "actor": "controller" if auth.allowed
            else "owner", "reason": auth.reason}


def milestones(store: Store, job_id: str) -> list[Milestone]:
    job = store.get_job(job_id)
    raw = ownership._milestones(store, job_id, job) if job is not None else {}
    out = []
    for name in MILESTONES:
        m = raw.get(name) or {"status": "unknown"}
        out.append(Milestone(name, bool(m.get("required", True)), str(m.get("status", "unknown")),
                             str(m.get("actor", "")), tuple(m.get("evidence") or ()), str(m.get("reason", ""))))
    return out


def summary(store: Store, job_id: str) -> dict:
    job = store.get_job(job_id)
    ms = milestones(store, job_id)
    # once accepted, delivery is what remains: incorporation and activation (the earlier milestones stay visible)
    pending = [m for m in ms if m.name in ("incorporation", "activation") and m.required and m.status != "done"]
    delivered = bool(job and job.state in _DONE and not pending)
    if job is None or job.state not in _DONE:
        text = ""
    elif delivered:
        notes = [f"{m.name} not required: {m.reason}" for m in ms if m.status == "not_required"
                 and m.name in ("incorporation", "activation") and m.reason]
        merged = inc.is_incorporated(store, job_id)
        text = "delivered" + (f" · merged {merged[:7]}" if merged else "")
        if any("not to merge" in n or "declined" in n for n in notes):
            text = "delivered · not merged (" + "; ".join(notes)[:80] + ")"
    else:
        nxt = pending[0]
        text = f"delivery: {nxt.name} {nxt.status} ({nxt.reason})"[:120]
    needs = None
    rw = repair_wait(store, job_id) if job is not None and job.state in _DONE and not delivered else None
    if rw:
        text = f"delivery: waiting on repair attempt {rw['attempt'][:8]} ({rw['state']})"
        if rw["human"]:
            needs = f"owner decision on {rw['attempt'][:8]}"
            text += f" · needs {needs}"
    return {"delivered": delivered, "next": pending[0].name if pending else None, "text": text[:160], "needs": needs,
            "milestones": [asdict(m) for m in ms]}


def publish(store: Store, job_id: str) -> dict:
    s = summary(store, job_id)
    rec = {"delivered": s["delivered"], "next": s["next"], "text": s["text"]}
    if store.kv_get(job_id, SUMMARY_KEY) != json.loads(json.dumps(rec)):  # the 30 s sweep republishes every accepted
        store.kv_set(job_id, SUMMARY_KEY, rec)                            # job: write (commit, WAL) only on change
    return s


def liveness_hint(store: Store, job_id: str, state: str) -> dict | None:
    """Render-path read (no git): the last published summary for an ACCEPTED job, else None."""
    if state != JobState.ACCEPTED.value:
        return None
    rec = store.kv_get(job_id, SUMMARY_KEY)
    return rec if isinstance(rec, dict) else None


def record_receipt(store: Store, job_id: str, milestone: str, *, actor: ActorRef, revision: str, evidence: dict,
                   now: float) -> int:
    from steward.diagnosis import redact

    body = redact(json.dumps(evidence, sort_keys=True, default=str))[:RECEIPT_MAX_CHARS]
    with store._conn:
        cur = store._conn.execute(
            "INSERT INTO delivery_receipts (job_id, milestone, actor_kind, actor_id, revision, evidence_json,"
            " created_epoch) VALUES (?,?,?,?,?,?,?)",
            (job_id, milestone, actor.kind.value, actor.id, revision, body, now))
    return int(cur.lastrowid)


def receipts(store: Store, job_id: str, milestone: str | None = None) -> list[dict]:
    q, a = "SELECT * FROM delivery_receipts WHERE job_id = ?", [job_id]
    if milestone:
        q, a = q + " AND milestone = ?", [job_id, milestone]
    out = []
    for r in store._conn.execute(q + " ORDER BY seq", a).fetchall():
        try:
            ev = json.loads(r["evidence_json"])
        except ValueError:
            ev = {"unparsed": r["evidence_json"]}
        out.append({"seq": r["seq"], "milestone": r["milestone"], "actor": f"{r['actor_kind']}:{r['actor_id']}",
                    "revision": r["revision"], "evidence": ev, "created_epoch": r["created_epoch"],
                    "revision_contains": ev.get("contains") if isinstance(ev, dict) else None})
    return out


def repair_wait(store: Store, job_id: str) -> dict | None:
    """The live repair attempt this job's delivery is waiting on (another job than itself), else None. A repair
    attempt in NEEDS_HUMAN counts as live: it waits on the owner, and spawning a worker for the root changes nothing."""
    from steward import repair_episode

    ep = repair_episode.open_for(store, repair_episode.root_of(store, job_id))
    if not ep or not ep.current_job or ep.current_job == job_id:
        return None
    cur = store.get_job(ep.current_job)
    if cur is None or cur.state not in repair_episode._LIVE:
        return None
    return {"attempt": cur.job_id, "state": cur.state.value, "human": cur.state == JobState.NEEDS_HUMAN}


def next_step(store: Store, job_id: str, *, now: float | None = None) -> dict:
    job = store.get_job(job_id)
    if job is None or job.state != JobState.ACCEPTED:
        return {"kind": "none"}
    from steward import replace as replace_module

    replaced = replace_module.refusal(store, job_id, "delivery")  # issue M: the sweep and the worker both skip it
    if replaced:
        return {"kind": "none", "reason": replaced}
    by_name = {m.name: m for m in milestones(store, job_id)}
    inc_m, act_m = by_name["incorporation"], by_name["activation"]
    if inc_m.required and inc_m.status == "pending":
        auth = incorporation_authority(store, job_id)
        if auth.allowed:
            rw = repair_wait(store, job_id)
            if rw:  # latency fix #7: a worker would only queue to log "repair attempt already on it"
                out = {"kind": "wait", "milestone": "incorporation",
                       "reason": f"repair attempt {rw['attempt'][:8]} is {rw['state']}"}
                if rw["human"]:
                    out["needs"] = f"owner decision on {rw['attempt'][:8]}"
                return out
            return {"kind": "incorporate", "milestone": "incorporation", "authority": auth.source}
        sha = _git(job.repo, "rev-parse", "--verify", "-q", f"refs/heads/{inc.branch_name(job_id)}")
        branch = target_branch(job.repo) or "?"
        return {"kind": "owner_decision", "milestone": "incorporation", "revision": sha,
                "decision": f"Incorporate job {job_id[:8]} ({sha[:12]}) into {branch} of {job.repo.rsplit('/', 1)[-1]}?",
                "why": auth.reason}
    if inc_m.status in ("blocked",):
        return {"kind": "wait", "milestone": "incorporation", "reason": inc_m.reason}
    if any(r["milestone"] == "incorporation" and not r["evidence"].get("tested_tree_matches", True)
           and not receipts(store, job_id, "post_merge_verification") for r in receipts(store, job_id, "incorporation")):
        return {"kind": "post_merge_verify", "milestone": "incorporation"}
    if act_m.required and act_m.status in ("pending", "blocked"):
        from steward import activation

        now = time.time() if now is None else now
        merged = inc.is_incorporated(store, job_id)
        targets = activation_targets(store, job_id)
        decided = _owner_decision(store, job_id, "activation")
        if targets == ("declared",):
            if decided and decided[0] == "approved":
                return {"kind": "attest_activation", "milestone": "activation", "revision": merged,
                        "approval_id": decided[1]}
            return {"kind": "owner_decision", "milestone": "activation", "revision": merged,
                    "decision": f"Activate job {job_id[:8]} ({str(merged)[:12]}) and confirm it is running?",
                    "why": "Steward has no activation mechanism for this target; only the owner can activate and confirm it"}
        auth = activation_authority(store, job_id, targets[0])
        if not auth.allowed:
            return {"kind": "owner_decision", "milestone": "activation", "revision": merged,
                    "decision": f"Restart Steward to activate {str(merged)[:12]} (job {job_id[:8]})?", "why": auth.reason}
        st = activation.state(store, job_id, merged, now=now)
        if st == "deploy":
            return {"kind": "activate", "milestone": "activation", "revision": merged, "authority": auth.source}
        if st == "verify":
            return {"kind": "verify_activation", "milestone": "activation", "revision": merged}
        return {"kind": "wait", "milestone": "activation", "reason": f"activation {st}"}
    return {"kind": "none"}


def ask_owner(store: Store, job_id: str, step: dict, *, now: float, notifier=None):
    """One confirmed `delivery_decision` per (milestone, revision); Plan 1 opens one linked approval and sends one
    notice. A repeat call is a no-op."""
    subject = f"{step['milestone']}:{step.get('revision') or ''}"
    b, created = blockers.raise_blocker(store, job_id, kind=DECISION_KIND, subject=subject, claim=step["decision"],
                                        evidence=[f"milestone:{step['milestone']}", f"revision:{step.get('revision')}"],
                                        origin_actor=CONTROLLER, now=now, origin_rule="delivery")
    if not created or b.disposition == "confirmed_human":
        return b
    req = blockers.validate_human_request({
        "decision": step["decision"], "why_authority_insufficient": step["why"],
        "evidence": [f"revision {step.get('revision')}", f"milestone {step['milestone']}"],
        "continuing": "verification, other milestones and other jobs continue; nothing merges or restarts until you decide",
        "options": ["approve", "decline"]})
    b = ownership.confirm_human(store, b.blocker_id, req, actor=CONTROLLER, now=now, notifier=notifier)
    aid = (b.clearing.get("params") or {}).get("approval_id")
    if aid:
        approvals.annotate(store, job_id, aid, milestone=step["milestone"], revision=step.get("revision"))
    return b


def plan(store: Store, job_id: str, *, now: float, notifier=None) -> dict:
    """DB + git-ref reads only: publish the summary and settle owner decisions; returns the next step for the
    sweep (Task 11) to hand to the detached worker."""
    step = next_step(store, job_id, now=now)
    if step["kind"] == "owner_decision":
        ask_owner(store, job_id, step, now=now, notifier=notifier)
    return {"job": job_id, "step": step, "summary": publish(store, job_id)}



def _delivery_key(job_id: str, step: dict, repo: str) -> str:
    from steward import intents

    kind = step.get("kind", "none")
    revision = str(step.get("revision") or "")
    extra: list[str] = []
    if kind == "incorporate":
        # The root's active repair attempt is the current candidate; a successfully repaired sibling is incorporated
        # on its own branch, and incorporate.mark_parents_incorporated carries the merge back to the root.
        # The target HEAD is deliberately NOT part of the key: every merge moves it, so a head-keyed intent minted a new
        # key (and a new worker) for each queued job per merge. The worker handles target moves in its merge rounds.
        extra.append(revision)
    elif kind == "verify_activation":
        from steward import activation

        last = float(step.get("last_probe") or 0.0)
        extra.append(str(int(last // activation.VERIFY_EVERY_S) + 1))
    return intents.intent_key(job_id, "delivery-worker", kind, revision, *extra)


def _candidate_job(store: Store, job_id: str):
    from steward import repair_episode

    ep = repair_episode.open_for(store, repair_episode.root_of(store, job_id))
    if ep and ep.current_job and ep.current_job != job_id:
        candidate = store.get_job(ep.current_job)
        if candidate is not None and candidate.state in _DONE:
            return candidate
    return store.get_job(job_id)


def _delivery_intent_checker(store: Store, intent):
    """Reconcile a dead worker against durable effects before deciding whether its action can be retried."""
    from steward import activation, incorporate as inc

    step = (intent.params or {}).get("step")
    revision = str((intent.params or {}).get("revision") or "")
    if step == "incorporate":
        merged = inc.is_incorporated(store, intent.job_id)
        return ("done", {"merge_commit": merged}) if merged else ("abandoned", {"reason": "no merge recorded"})
    if step == "post_merge_verify":
        found = [r for r in receipts(store, intent.job_id, "post_merge_verification") if r["revision"] == revision]
        return ("done", {"receipt": found[-1]["seq"]}) if found else ("abandoned", {"reason": "no verification receipt"})
    if step in ("activate", "verify_activation", "attest_activation"):
        found = [r for r in receipts(store, intent.job_id, "activation") if r["revision_contains"] == revision]
        if found:
            return "done", {"activation_receipt": found[-1]["seq"]}
        if step == "activate":
            # Deploy may have had an external effect before the worker died. Never repeat an uncertain deploy.
            return "unknown", {}
        events = [e for e in store.events_of(intent.job_id, ("activation_unproven",))
                  if (e.evidence or {}).get("merge_commit") == revision]
        return ("done", {"probes": len(events)}) if events else ("abandoned", {"reason": "no probe receipt"})
    return "unknown", {}


def worker_log_path(job_id: str):
    from steward import store as _store

    return _store.resolve_home() / "delivery-logs" / f"{job_id}.log"


def _open_worker_log(job_id: str):
    """Append-mode per-job log for the detached worker's stdout+stderr; one `.1` generation kept past the size cap."""
    path = worker_log_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass
    return open(path, "ab")


def _spawn_delivery(job_id: str, key: str, owner: str) -> int:
    argv = [sys.executable, "-m", "steward.delivery", job_id, key, owner]
    log = _open_worker_log(job_id)
    try:
        log.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} spawn {' '.join(argv[2:])}\n".encode())
        log.flush()
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, cwd=str(os.path.dirname(os.path.dirname(__file__))))
    finally:
        log.close()
    return proc.pid


def _last_seq(store: Store, job_id: str) -> int:
    r = store._conn.execute("SELECT MAX(seq) FROM events WHERE job_id = ?", (job_id,)).fetchone()
    return int(r[0] or 0)


def _progress_since(store: Store, job_id: str, key: str, win: dict) -> bool:
    """A delivery_step_started for this key recorded after the current silent-claim window opened."""
    return any((e.evidence or {}).get("intent_key") == key
               for e in store.events_since(job_id, int(win.get("seq") or 0)) if e.what == "delivery_step_started")


def _note_claim(store: Store, job_id: str, key: str, attempt: int) -> None:
    """Called at each claim: open a fresh silent-claim window when none exists or the last one saw progress, so an
    old step-started event never hides later silent claims."""
    wkey = f"delivery_window:{key}"
    win = store.kv_get(job_id, wkey)
    if not win or _progress_since(store, job_id, key, win):
        store.kv_set(job_id, wkey, {"base": max(attempt - 1, 0), "seq": _last_seq(store, job_id)})


def _check_stuck(store: Store, job_id: str, key: str, step: str, existing, *, now: float, notifier=None) -> bool:
    """True when this key must not be respawned: an open stuck blocker holds it, or its worker has been claimed
    STUCK_ATTEMPTS times since the last stuck episode without ever recording delivery_step_started."""
    subject = key
    ik = blockers.issue_key(STUCK_KIND, subject)
    if any(b.issue_key == ik for b in blockers.open_blockers(store, job_id)):
        return True
    if existing is None or existing.state == "done":
        return False
    win = store.kv_get(job_id, f"delivery_window:{key}") or {}
    if existing.attempt - int(win.get("base") or 0) < STUCK_ATTEMPTS or _progress_since(store, job_id, key, win):
        return False
    log = worker_log_path(job_id)
    claim = (f"delivery worker for step {step} started {existing.attempt} times without recording any progress "
             f"(no delivery_step_started); worker log: {log}")
    b, created = blockers.raise_blocker(store, job_id, kind=STUCK_KIND, subject=subject, claim=claim,
                                        evidence=[f"step:{step}", f"attempts:{existing.attempt}", f"log:{log}"],
                                        origin_actor=CONTROLLER, now=now, origin_rule="delivery")
    store.kv_set(job_id, f"delivery_window:{key}", {"base": existing.attempt, "seq": _last_seq(store, job_id)})
    if created and b.disposition != "confirmed_human":
        req = blockers.validate_human_request({
            "decision": claim[:300],
            "why_authority_insufficient": "the worker keeps exiting before doing anything; retrying again will not help",
            "evidence": [f"step {step}", f"{existing.attempt} attempts", f"log {log}"],
            "continuing": "other jobs and milestones continue; delivery of this step is not respawned until you decide",
            "options": ["retry", "leave"]})
        ownership.confirm_human(store, b.blocker_id, req, actor=CONTROLLER, now=now, notifier=notifier)
    return True


def _worker_alive(store: Store, job_id: str, now: float) -> bool:
    """One live delivery worker per job, whatever step or key it was spawned for (kv(job, WORKER_KEY))."""
    from steward import bg_baseline

    rec = store.kv_get(job_id, WORKER_KEY) or {}
    if not rec:
        return False
    pid = rec.get("pid")
    if bg_baseline._alive(pid):
        return True
    return now - float(rec.get("heartbeat_epoch") or rec.get("started_epoch") or 0.0) < WORKER_LEASE_S


def _record_worker(store: Store, job_id: str, key: str, pid: int, owner: str, step: str, *, started: float,
                   beat: float) -> None:
    rec = {"job_id": job_id, "intent_key": key, "pid": pid, "owner": owner, "step": step,
           "started_epoch": started, "heartbeat_epoch": beat}
    store.kv_set(job_id, WORKER_KEY, rec)
    store.kv_set(PSEUDO_JOB, WORKER_KEY, rec)  # last-spawned worker of any job: informational only, never consulted


def _worker_lock_path(store: Store, job_id: str):
    return Path(store.home) / f"delivery-{job_id[:8]}.lock"


def _transient(result: dict) -> bool:
    """A result that leaves the step to be retried later (the intent is abandoned, the sweep backs off)."""
    return bool(result.get("transient")) or result.get("outcome") in ("retry", "pending", "wait") \
        or result.get("step") in ("wait", "owner_decision")


def _schedule_retry(store: Store, job_id: str, *, now: float, transient: bool) -> None:
    if not transient:
        if store.kv_get(job_id, RETRY_KEY) is not None:
            store.kv_delete(job_id, RETRY_KEY)
        return
    n = int((store.kv_get(job_id, RETRY_KEY) or {}).get("n") or 0)
    store.kv_set(job_id, RETRY_KEY, {"n": n + 1, "at": now + min(RETRY_MAX_S, RETRY_AFTER_S * (2 ** n))})


def sweep(store: Store, *, now: float, spawn=None, notifier=None) -> dict:
    """Cheap, restart-safe delivery scheduler. It publishes every accepted job and spawns only actionable steps."""
    from steward import intents

    last = float(store.kv_get(PSEUDO_JOB, "delivery_sweep_at") or 0.0)
    if last and now - last < SWEEP_EVERY_S:
        return {"launched": [], "active": [], "owner_decisions": [], "errors": [], "skipped": "cadence"}
    store.kv_set(PSEUDO_JOB, "delivery_sweep_at", now)
    out = {"launched": [], "active": [], "owner_decisions": [], "errors": [], "backoff": []}
    ts = config.train_settings()
    train_repo = Path(ts["repo"]).resolve() if ts["enabled"] and ts["repo"] else None
    for job in store.list_jobs({JobState.ACCEPTED}):
        if train_repo is not None and Path(str(job.repo)).resolve() == train_repo:
            continue
        jid = job.job_id
        try:
            planned = plan(store, jid, now=now, notifier=notifier)
            step = planned["step"]
            if step["kind"] == "owner_decision":
                out["owner_decisions"].append(jid)
                continue
            if step["kind"] in ("none", "wait"):
                continue
            if step["kind"] == "incorporate":
                candidate = _candidate_job(store, jid)
                from steward import incorporate as inc

                branch = inc.branch_name(candidate.job_id) if candidate else ""
                revision = inc._git(job.repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").stdout.strip()
                step = {**step, "revision": revision or step.get("revision")}
            if step["kind"] == "verify_activation":
                step = {**step, "last_probe": store.kv_get(jid, "activation_last_probe") or 0.0}
            key = _delivery_key(jid, step, job.repo)
            if _worker_alive(store, jid, now):
                out["active"].append({"job": jid, "step": step["kind"]})
                continue
            retry = store.kv_get(jid, RETRY_KEY) or {}
            if now < float(retry.get("at") or 0.0):  # a transient miss backs off; it is not respawned every sweep
                out["backoff"].append({"job": jid, "step": step["kind"], "retry_at": retry["at"]})
                continue
            existing = intents.get(store, key)
            if _check_stuck(store, jid, key, step["kind"], existing, now=now, notifier=notifier):
                out["errors"].append({"job": jid, "step": step["kind"], "error": "delivery worker stuck"})
                continue
            if existing and existing.state == "claimed":
                if now < existing.lease_expires or _worker_alive(store, jid, now):
                    out["active"].append({"job": jid, "step": step["kind"]})
                    continue
                # Expired/dead claim becomes uncertain; its checker reconciles durable effects before retry.
                intents.claim(store, key=key, job_id=jid, kind="delivery-worker", lease_s=WORKER_LEASE_S,
                             now=now, owner="delivery-sweep-reconcile", params={"step": step["kind"],
                             "revision": step.get("revision")})
                intents.reconcile_uncertain(store, now=now)
                existing = intents.get(store, key)
                if existing and existing.state in ("done", "uncertain", "claimed"):
                    out["active" if existing.state == "claimed" else "errors"].append(
                        {"job": jid, "step": step["kind"], "state": existing.state})
                    continue
            owner = uuid.uuid4().hex
            claimed = intents.claim(store, key=key, job_id=jid, kind="delivery-worker", lease_s=WORKER_LEASE_S,
                                    now=now, owner=owner, params={"step": step["kind"],
                                    "revision": step.get("revision")})
            if claimed is None:
                out["active"].append({"job": jid, "step": step["kind"]})
                continue
            _note_claim(store, jid, key, claimed.attempt)
            try:
                pid = (spawn or _spawn_delivery)(jid, key, owner)
            except Exception as exc:  # noqa: BLE001 - a failed launch remains scheduled and reviewable in status
                intents.fail(store, key, owner=owner, reason=f"spawn failed: {type(exc).__name__}: {exc}", now=now,
                             retryable=True)
                store.append_event(jid, what="delivery_worker_launch_failed",
                                   evidence={"step": step["kind"], "error": str(exc)[:200]},
                                   rule="delivery", judgment=Judgment.code)
                out["errors"].append({"job": jid, "step": step["kind"], "error": type(exc).__name__})
                continue
            _record_worker(store, jid, key, pid, owner, step["kind"], started=now, beat=now)
            store.append_event(jid, what="delivery_worker_started", evidence={"step": step["kind"],
                               "intent_key": key, "pid": pid}, rule="delivery", judgment=Judgment.code)
            out["launched"].append({"job": jid, "step": step["kind"], "pid": pid})
        except Exception as exc:  # noqa: BLE001 - one broken job cannot stall other delivery
            out["errors"].append({"job": jid, "error": f"{type(exc).__name__}: {exc}"})
    return out


def run_delivery(store: Store, job_id: str, runner, *, now=None, deploy_self=None, probes=None, health_fn=None,
                 intent_key=None, owner=None) -> dict:
    """Detached worker body: re-check authority, perform one current delivery step, and record its result."""
    from steward import activation, check_runs, deploy_pending, intents

    clock_given = now is not None  # a real worker backs off from the moment it finishes, not from when it started
    now = time.time() if now is None else now
    def finish_early(result):
        if intent_key and owner:
            intents.complete(store, intent_key, owner=owner,
                             receipt={"step": result.get("step", "none"), "result": result}, now=now)
        return result

    job = store.get_job(job_id)
    if job is None or job.state != JobState.ACCEPTED:
        return finish_early({"step": "none", "reason": "job is no longer accepted"})
    if config.train_owns(job.repo):
        return finish_early({"step": "none", "reason": "landing train owns merges"})
    lock_fd = _take_worker_lock(store, job_id)
    if lock_fd is None:  # the guard that holds even when the DB is wrong: one live worker per job
        store.append_event(job_id, what="delivery_worker_duplicate", evidence={"intent_key": intent_key, "pid": os.getpid()},
                           rule="delivery", judgment=Judgment.code)
        if intent_key and owner:
            intents.fail(store, intent_key, owner=owner, reason="duplicate worker", now=now, retryable=True)
        return {"step": "none", "outcome": "duplicate", "reason": "duplicate worker: another delivery worker holds this job"}
    try:
        return _run_locked(store, job_id, runner, now=now, clock_given=clock_given, deploy_self=deploy_self,
                           probes=probes, health_fn=health_fn, intent_key=intent_key, owner=owner,
                           finish_early=finish_early)
    finally:
        os.close(lock_fd)  # closing the descriptor releases the flock


def _take_worker_lock(store: Store, job_id: str) -> int | None:
    import fcntl

    fd = os.open(_worker_lock_path(store, job_id), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _run_locked(store: Store, job_id: str, runner, *, now, clock_given, deploy_self, probes, health_fn, intent_key,
                owner, finish_early) -> dict:
    from steward import activation, check_runs, deploy_pending, intents

    planned = plan(store, job_id, now=now)
    step = planned["step"]
    kind = step["kind"]
    if kind in ("wait", "owner_decision") and intent_key and owner:
        # The plan moved under the worker: this key's step is not done, only not doable now. Abandon it (retryable,
        # backed off) so the stable key can be claimed again, instead of marking it done forever.
        _schedule_retry(store, job_id, now=time.time() if not clock_given else now, transient=True)
        intents.fail(store, intent_key, owner=owner, reason=f"plan is {kind}", now=now, retryable=True)
        return {"step": kind, "summary": planned["summary"]}
    if kind in ("none", "wait", "owner_decision"):
        return finish_early({"step": kind, "summary": planned["summary"]})
    stop = threading.Event()
    heartbeat_thread = None
    if intent_key and owner:
        _record_worker(store, job_id, intent_key, os.getpid(), owner, kind, started=now, beat=now)

        def heartbeat_loop():
            while not stop.wait(WORKER_HEARTBEAT_S):
                try:
                    intents.claim(store, key=intent_key, job_id=job_id, kind="delivery-worker",
                                  lease_s=WORKER_LEASE_S, now=time.time(), owner=owner,
                                  params={"step": kind, "revision": step.get("revision")}, reclaimable=True)
                    _record_worker(store, job_id, intent_key, os.getpid(), owner, kind, started=now,
                                   beat=time.time())
                except Exception:  # noqa: BLE001 - lease heartbeat is best effort; durable receipts reconcile exit
                    pass
        heartbeat_thread = threading.Thread(target=heartbeat_loop, name="delivery-heartbeat", daemon=True)
        heartbeat_thread.start()
    store.append_event(job_id, what="delivery_step_started", evidence={"step": kind,
                       "revision": step.get("revision"), "intent_key": intent_key},
                       rule="delivery", judgment=Judgment.code)
    try:
        if kind == "incorporate":
            # The root's current repair attempt is the effective candidate after a fix-up; its parent merge carries
            # that exact work back to the root job's delivery milestone.
            candidate = _candidate_job(store, job_id)
            if candidate is None or candidate.state != JobState.ACCEPTED:
                result = {"step": kind, "outcome": "wait", "reason": "current repair attempt is not accepted"}
            else:
                auth = incorporation_authority(store, job_id)
                if not auth.allowed:
                    plan(store, job_id, now=now)
                    result = {"step": "owner_decision", "reason": auth.reason}
                else:
                    result = _incorporate_step(store, job_id, candidate, runner, now=now)
        elif kind == "post_merge_verify":
            check_fn = check_runs.shared_check_fn(store, job_id, purpose="post_merge", wait=True, share=True)
            result = {"step": kind, "outcome": "verified", **verify_merged(store, job_id, runner,
                               check_fn=check_fn, now=now)}
            if not result.get("green"):  # a red merge is never deployed: its runners stop waiting for it
                deploy_pending.clear(store, job_id)
        elif kind == "activate":
            result = {"step": kind, **activation.activate(store, job_id, str(step.get("revision") or ""),
                               deploy=deploy_self, probes=probes, health_fn=health_fn, now=now, authorized=True)}
            if not result.get("activated") and result.get("reason") not in ("deploy already in progress", "wait"):
                deploy_pending.clear(store, job_id)  # this merge will not be deployed by this step: stop holding runners
        elif kind == "verify_activation":
            result = {"step": kind, **activation.verify_activation(store, job_id,
                               str(step.get("revision") or inc.is_incorporated(store, job_id) or ""),
                               probes=probes, health_fn=health_fn, now=now)}
            store.kv_set(job_id, "activation_last_probe", now)
        elif kind == "attest_activation":
            receipt = activation.attest(store, job_id, str(step.get("revision") or ""),
                                        approval_id=step["approval_id"], now=now)
            result = {"step": kind, **receipt}
        else:
            result = {"step": kind, "outcome": "unsupported"}
        store.append_event(job_id, what="delivery_step_finished", evidence={"step": kind,
                           "outcome": result.get("outcome", "done"), "result": str(result)[:400]},
                           rule="delivery", judgment=Judgment.code)
        publish(store, job_id)
        transient = _transient(result)
        _schedule_retry(store, job_id, now=time.time() if not clock_given else now, transient=transient)
        if intent_key and owner and transient:  # retryable: the intent is abandoned, the sweep respawns after backoff
            intents.fail(store, intent_key, owner=owner, reason=str(result.get("reason") or "retry")[:300], now=now,
                         retryable=True)
        elif intent_key and owner:
            intents.complete(store, intent_key, owner=owner, receipt={"step": kind, "result": result}, now=now)
        return result
    except Exception as exc:  # noqa: BLE001 - preserve the job and make the failed worker visible
        store.append_event(job_id, what="delivery_worker_failed", evidence={"step": kind,
                           "error": f"{type(exc).__name__}: {exc}"[:240]}, rule="delivery", judgment=Judgment.code)
        _schedule_retry(store, job_id, now=time.time() if not clock_given else now, transient=True)
        if intent_key and owner:
            intents.fail(store, intent_key, owner=owner, reason=f"{type(exc).__name__}: {exc}", now=now,
                         retryable=True)
        raise
    finally:
        stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
        if owner:
            for scope in (job_id, PSEUDO_JOB):
                if (store.kv_get(scope, WORKER_KEY) or {}).get("owner") == owner:
                    store.kv_delete(scope, WORKER_KEY)


def _incorporate_step(store: Store, job_id: str, candidate, runner, *, now: float) -> dict:
    """The tested merge (`autopilot.merge_when_green`: tested outside the merge lock, landed inside it; the fix-up spawn
    and post-merge verification also run with no lock) and its honest outcome (latency fix #5): `incorporated` only
    when a merge commit exists; a transient miss (busy lock, raced, moved, index.lock) is `retry` with `transient`
    (the intent is abandoned so the sweep retries); any other refusal is `blocked` and waits visibly."""
    from pathlib import Path

    from steward import autopilot, check_runs, deploy_pending

    kind = "incorporate"

    def still_allowed() -> dict | None:
        current, cand = store.get_job(job_id), store.get_job(candidate.job_id)
        if current is None or current.state != JobState.ACCEPTED or cand is None or cand.state != JobState.ACCEPTED:
            return {"outcome": "skipped", "step": "wait", "detail": "job state changed"}
        auth = incorporation_authority(store, job_id)
        if not auth.allowed:
            plan(store, job_id, now=now)
            return {"outcome": "skipped", "step": "owner_decision", "detail": auth.reason}
        return None

    checks = autopilot._check_lines(Path(candidate.repo))
    check_fn = check_runs.shared_check_fn(store, job_id, purpose="merged_tree", wait=True, share=True)
    r = autopilot.merge_when_green(store, candidate, runner, checks, check_fn=check_fn,
                                   reuse=autopilot._reuse_fn(store, candidate), actor=CONTROLLER,
                                   still_allowed=still_allowed, mark_id=job_id)
    out, pf, detail = r["outcome"], r.get("pf"), str(r.get("detail") or "")
    if out == "skipped":
        return {"step": r.get("step", "wait"), "reason": detail}
    if out == "human":
        raise_waiting(store, job_id, detail, now=now)
        return {"step": kind, "outcome": "waiting_external", "reason": detail}
    if out in ("conflict", "red"):
        failed = autopilot._failed(store, candidate, pf, detail, r.get("files") or [], runner,
                                   autopilot._default_followup, None)
        return {"step": kind, "outcome": failed.get("outcome"), **failed}
    receipt = r.get("receipt") or (inc.incorporate(store, candidate.job_id, actor=CONTROLLER) if out == "contained"
                                   else {})
    if out in ("contained", "noop") and receipt.get("noop"):
        return {"step": kind, "outcome": "already_incorporated", "receipt": receipt}
    if out in ("raced", "busy") or receipt.get("error_kind") in ("moved", "merge_lock_busy") or "index.lock" in detail:
        return {"step": kind, "outcome": "retry", "transient": True, "reason": detail or out, "receipt": receipt}
    if out != "merged":
        why = str(receipt.get("detail") or detail or "incorporate refused")
        raise_waiting(store, job_id, why, now=now)
        return {"step": kind, "outcome": "blocked", "reason": why, "receipt": receipt}
    if inc._git(candidate.repo, "rev-parse", f"{receipt['merge_commit']}^{{tree}}").stdout.strip() != r["tested_tree"]:
        check_fn = check_runs.shared_check_fn(store, job_id, purpose="post_merge", wait=True, share=True)
        receipt["post_merge_verification"] = verification = verify_merged(store, job_id, runner, check_fn=check_fn,
                                                                          checks=checks, now=now)
        if not verification.get("green"):
            deploy_pending.clear(store, job_id)
    return {"step": kind, "outcome": "incorporated", "receipt": receipt}


def main(argv=None) -> int:
    import sys
    from steward.runner_exec import SubprocessRunner

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (1, 3):
        print("usage: python -m steward.delivery JOB [INTENT_KEY OWNER]", file=sys.stderr)
        return 2
    jid = args[0]
    key, owner = (args[1], args[2]) if len(args) == 3 else (None, None)
    result = run_delivery(Store(), jid, SubprocessRunner(), intent_key=key, owner=owner)
    return 0 if result.get("outcome") not in ("failed", "unsupported") else 1


# The action intent is reconciled before retry after a worker dies. Unknown activation effects remain uncertain;
# a read-only activation probe is allowed, a second deploy is not.
from steward import intents as _intents  # noqa: E402
_intents.register_checker("delivery-worker", _delivery_intent_checker)

def _cheap_delivery_done(store: Store, job_id: str, b) -> dict | None:
    """A delivery decision is moot once its milestone is done or no longer required (e.g. the owner merged by hand)."""
    if b.kind != DECISION_KIND:
        return None
    milestone = (b.evidence[0].split(":", 1)[1] if b.evidence and b.evidence[0].startswith("milestone:") else "")
    m = next((m for m in milestones(store, job_id) if m.name == milestone), None)
    if m is not None and (m.status == "done" or not m.required):
        return {"reason": f"{milestone} {m.status}", "evidence": list(m.evidence)}
    return None


def raise_waiting(store: Store, job_id: str, why: str, *, now: float):
    b, _ = blockers.raise_blocker(store, job_id, kind=WAITING_KIND, subject="source_repo", claim=why[:400],
                                  evidence=[why[:120]], origin_actor=CONTROLLER, now=now, origin_rule="autopilot_merge",
                                  disposition="waiting_external", wake={"kind": "external", "on": "source repo clean"},
                                  clearing={"predicate": "source_repo_clean", "params": {}})
    return b


def record_post_merge(store: Store, job_id: str, merge_commit: str, red: str | None, *, now: float) -> dict:
    seq = record_receipt(store, job_id, "post_merge_verification", actor=CONTROLLER, revision=merge_commit,
                         now=now, evidence={"green": red is None, "detail": red, "merge_commit": merge_commit})
    if red:
        blockers.raise_blocker(store, job_id, kind=RED_KIND, subject=merge_commit,
                               claim=f"merged tree {merge_commit[:12]} is red: {red}"[:400],
                               evidence=[f"post_merge_verification:{seq}"], origin_actor=CONTROLLER, now=now,
                               origin_rule="post_merge_verification",
                               clearing={"predicate": "post_merge_green", "params": {"merge_commit": merge_commit}})
    return {"seq": seq, "green": red is None, "detail": red}


def verify_merged(store: Store, job_id: str, runner, *, check_fn=None, checks=None, now=None) -> dict:
    from steward import autopilot

    job = store.get_job(job_id)
    merged = inc.is_incorporated(store, job_id)
    red = autopilot._test_commit(job.repo, merged, runner, checks, check_fn=check_fn)
    return record_post_merge(store, job_id, merged, red, now=time.time() if now is None else now)


def _p_source_repo_clean(store, job_id: str, b, facts) -> str:
    try:
        pf = inc.preflight(store, job_id)
    except KeyError:
        return "uncertain"
    if pf.error:
        return "uncertain"
    return "cleared" if pf.contained or not (pf.block and not pf.conflicts) else "holds"


def _p_post_merge_green(store, job_id: str, b, facts) -> str:
    red_commit = ((b.clearing or {}).get("params") or {}).get("merge_commit")
    pm = receipts(store, job_id, "post_merge_verification")
    last_red = max((r["seq"] for r in pm if r["revision"] == red_commit and not r["evidence"].get("green")), default=0)
    return "cleared" if any(r["evidence"].get("green") and r["seq"] > last_red for r in pm) else "holds"

def _register() -> None:
    blockers.register_kind(DECISION_KIND, KindSpec(True, 2, ("delivery",), "approval_decided", "owner", True,
                                                   APPROVAL_KIND))
    blockers.register_kind(WAITING_KIND, KindSpec(False, 1, ("delivery",), "job_left_needs_human", "external"))
    blockers.register_kind(RED_KIND, KindSpec(False, 2, ("delivery",), "checks_passed_since", "tech_lead"))
    blockers.register_kind(STUCK_KIND, KindSpec(True, 2, ("delivery",), "approval_decided", "owner", True,
                                                APPROVAL_KIND))
    blockers.register_kind(LAUNCH_KIND, KindSpec(False, 2, ("delivery",), "worker_started", "tech_lead"))
    ownership.DELIVERY_KINDS.update({DECISION_KIND, STUCK_KIND, WAITING_KIND, RED_KIND, LAUNCH_KIND})
    ownership.register_milestone("incorporation", incorporation_milestone)
    ownership.register_milestone("activation", activation_milestone)
    ownership.register_cheap_check("delivery_done", _cheap_delivery_done)
    ownership.register_predicate("source_repo_clean", _p_source_repo_clean)
    ownership.register_predicate("post_merge_green", _p_post_merge_green)
    from steward import activation as _activation  # noqa: F401 - registers activation blockers/predicates


_register()


if __name__ == "__main__":
    sys.exit(main())
