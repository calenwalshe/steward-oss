"""Liveness sweep (spec 2026-10-08 §5.1): every non-terminal job names a live actor or gets a bounded remedy ladder."""
from __future__ import annotations

import time

KEY = "liveness"
ORPHAN_KEY = "liveness_orphan_since"
TRIES_KEY = "liveness_tries"
ASK_KIND = "liveness_stuck"
REMEDY_ERROR_KEY = "liveness_remedy_error"
NOISE_WHATS = ("runner_self_restart", "heartbeat", "heartbeat class changed", "tech_lead_review_deferred",
               "ownership_status", "jev_shadow", "jev_flag", "jev_diagnosis_requested", "tech_lead_sweep",
               "instruction delivery observed", "tech_lead_idle_paused")


def record_runner(store, job_id: str, *, turn_open: bool, progress_ts: float, pid: int) -> None:
    store.kv_set(job_id, KEY, {"turn_open": bool(turn_open), "progress_ts": float(progress_ts), "pid": int(pid),
                               "at": time.time()})
    if turn_open and store.kv_get(job_id, "tl_idle_paused_at") is not None:
        store.kv_delete(job_id, "tl_idle_paused_at")  # a worker turn lifts the supervision idle pause (spec §5.2)


# -- actor derivation (spec §5.1): a pure read; the first rule that holds names the actor; None = orphan ------------

# Steward's own supervision records (trajectory review, efficiency, the TL idle pause) are not worker progress either:
# counting them let a trajectory review reset an idle job's orphan clock (RC-5 / F1).
NOISE_PREFIXES = ("heartbeat", "jev_", "liveness_", "trajectory_", "efficiency_")
NOISE_RULES = ("ownership", "liveness", "supervision_allowance")  # blocker bookkeeping, sweep + supervision records
HUMAN_APPROVAL_KINDS = ("acceptance", "general", "permission")
HUMAN_DISPOSITIONS = ("confirmed_human", "waiting_external")


def _default_runner_alive(store):
    from steward import launch
    return lambda jid: launch.runner_alive(store.home, jid)


def _default_check_runs_live(store, now: float):
    from steward import check_runs

    def live(jid: str) -> bool:
        row = store._conn.execute(
            "SELECT 1 FROM check_run_waiters w JOIN check_runs r ON r.run_id = w.run_id WHERE w.job_id = ?"
            f" AND w.consumed_epoch IS NULL AND r.state IN ({','.join('?' * len(check_runs.LIVE))})"
            " AND COALESCE(r.heartbeat_epoch, 0) > ? LIMIT 1",
            (jid, *check_runs.LIVE, now - check_runs.HEARTBEAT_STALE_S)).fetchone()
        return row is not None
    return live


def _epoch(ts: str) -> float:
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _noise(what: str, rule: str) -> bool:
    return what in NOISE_WHATS or what.startswith(NOISE_PREFIXES) or rule in NOISE_RULES


def _recent_real_event(store, job_id: str, now: float, idle_s: float) -> bool:
    """A non-noise event within idle_s. Newest first, stops at the first event older than the window (seq ~ time)."""
    cur = store._conn.execute("SELECT what, rule, ts FROM events WHERE job_id = ? ORDER BY seq DESC", (job_id,))
    for what, rule, ts in cur:
        try:
            age = now - _epoch(ts)
        except (TypeError, ValueError):
            continue
        if age >= idle_s:
            return False
        if not _noise(what or "", rule or ""):
            return True
    return False


# u2 (stall fixes 2026-10-14): what the WORKER itself produced. A RUNNING/PAUSED job is alive only on this — never on
# the runner's or the supervisors' own records (a nudge recorded, a runner restart, a tech-lead review), which is what
# kept a job that never had a worker "alive" for 8 h. Frames the worker's shim forwards, the worker's own tool calls
# hitting the permission hook, the worker's check/pause verbs; plus its transcript and its commits/diff (below).
WORKER_EVIDENCE_RULES = ("fabric_ack_frame", "fabric_done_frame", "fabric_error_frame",
                         "worker_permission_requested_frame", "permission_policy_allow", "check_verbs")
WORKER_EVIDENCE_WHATS = ("pause_review_requested",)
DISPATCH_WHATS = ("worker dispatch started", "worker attempt started")
NO_WORKER_S = 900.0  # dispatch grace: past it with no worker evidence since dispatch, the job is a ghost
PROJECTS_DIR = None  # Claude's per-project transcript root; None = ~/.claude/projects (tests point it elsewhere)


def worker_progress_ts(prev_sig, sig, *, now: float, previous: float) -> float:
    """When the worker last changed the worktree (HEAD or diff). The first observation after a runner (re)start has
    no previous signals to compare with: that is the runner starting, not the worker working (u2)."""
    if prev_sig is None:
        return previous
    if sig.head_sha != prev_sig.head_sha or sig.diff_fingerprint != prev_sig.diff_fingerprint:
        return now
    return previous


def _transcript_ts(store, job_id: str) -> float:
    from pathlib import Path

    from steward import chat
    base = Path(PROJECTS_DIR) if PROJECTS_DIR is not None else Path.home() / ".claude" / "projects"
    d = base / chat.project_slug(chat.worktree_path(store, job_id))
    try:
        return max((f.stat().st_mtime for f in d.glob("*.jsonl")), default=0.0)
    except OSError:
        return 0.0


def _latest_ts(store, job_id: str, where: str, params: tuple) -> float:
    row = store._conn.execute(f"SELECT ts FROM events WHERE job_id = ? AND ({where}) ORDER BY seq DESC LIMIT 1",
                              (job_id, *params)).fetchone()
    if not row:
        return 0.0
    try:
        return _epoch(row[0])
    except (TypeError, ValueError):
        return 0.0


def worker_evidence_ts(store, job_id: str) -> float:
    """The newest worker evidence for the job (epoch, 0.0 = none): a worker event, its transcript, its commits/diff."""
    marks_r, marks_w = ",".join("?" * len(WORKER_EVIDENCE_RULES)), ",".join("?" * len(WORKER_EVIDENCE_WHATS))
    ev = _latest_ts(store, job_id, f"rule IN ({marks_r}) OR what IN ({marks_w})",
                    (*WORKER_EVIDENCE_RULES, *WORKER_EVIDENCE_WHATS))
    rec = store.kv_get(job_id, KEY)
    progress = float(rec.get("progress_ts") or 0.0) if isinstance(rec, dict) else 0.0
    return max(ev, progress, _transcript_ts(store, job_id))


def _dispatch_ts(store, job) -> float:
    ts = _latest_ts(store, job.job_id, f"what IN ({','.join('?' * len(DISPATCH_WHATS))})", DISPATCH_WHATS)
    if ts:
        return ts
    try:
        return _epoch(str(job.created_at))
    except (TypeError, ValueError):
        return 0.0


def ghost(store, job, *, now: float, settings: dict) -> bool:
    """RUNNING/PAUSED with no worker evidence since the last dispatch, past the dispatch grace (u2)."""
    from steward.models import JobState
    if job.state not in (JobState.RUNNING, JobState.PAUSED):
        return False
    dispatched = _dispatch_ts(store, job)
    if now - dispatched < float(settings.get("no_worker_s") or NO_WORKER_S):
        return False
    return worker_evidence_ts(store, job.job_id) < dispatched


def _wake_live(rec, now: float) -> bool:
    if not isinstance(rec, dict) or rec.get("wake_at") is None:
        return False
    try:
        return float(rec["wake_at"]) > now
    except (TypeError, ValueError):
        return False


def _tl_wait_live(store, job_id: str, now: float) -> bool:
    from steward import blockers
    if any(_wake_live(b.diagnosis, now) for b in blockers.open_blockers(store, job_id)):
        return True
    return _wake_live(store.kv_get(job_id, "tech_lead_wait"), now)  # a job-level TL wait (no blocker)


def _human_pending(store, job_id: str) -> str | None:
    from steward import approvals, blockers
    if any(b.disposition in HUMAN_DISPOSITIONS for b in blockers.open_blockers(store, job_id)):
        return "human:blocker"
    if any((a.get("kind") or "general") in HUMAN_APPROVAL_KINDS
           for a in approvals.list_approvals(store, job_id, status="pending")):
        return "human:approval"
    return None


def _runner_rule(store, jid: str, closeout: bool, now: float, idle_s: float, runner_alive, check_runs_live,
                 settings: dict) -> str | None:
    if closeout:
        # WORKER_FINISHED / CHECKS_PASSED: a live check-run waiter (no runner needed), or runner live + closeout event
        if check_runs_live(jid):
            return "check_run"
        if runner_alive(jid) and _recent_real_event(store, jid, now, idle_s):
            return "runner:recent_event"
        return None
    # RUNNING / PAUSED: runner live AND (turn open | live check run | worker evidence within idle_s). u2: an open
    # turn counts only once the worker has shown anything since dispatch (or inside the dispatch grace).
    if not runner_alive(jid):
        return None
    rec = store.kv_get(jid, KEY)
    rec = rec if isinstance(rec, dict) else {}
    job = store.get_job(jid)
    if rec.get("turn_open") and now - float(rec.get("at") or 0) < idle_s \
            and not ghost(store, job, now=now, settings=settings):
        return "runner:turn_open"
    if check_runs_live(jid):
        return "runner:check_run"
    if now - worker_evidence_ts(store, jid) < idle_s:
        return "worker:recent_evidence"
    return None


def _accepted_rule(store, jid: str, now: float) -> str | None:
    from steward import autopilot_bg, delivery, incorporate
    from steward import landing_queue as q
    if incorporate.is_incorporated(store, jid):
        return "incorporated"
    e = q.get(store, jid)
    if e is not None and e.status == "queued":
        return "queue:landing"
    if e is not None and e.fixup:  # §5.4: the train's rebase fix-up is alive; the parent waits on it
        from steward.machine import TERMINAL_STATES
        fx = store.get_job(e.fixup)
        if fx is not None and fx.state not in TERMINAL_STATES:
            return "fixup"
    if delivery._worker_alive(store, jid, now):
        return "delivery_worker"
    rec = autopilot_bg.record(store, jid)
    if rec and rec.get("status") in autopilot_bg._LIVE and autopilot_bg.owner_alive(rec, now):
        return "autopilot_bg"
    return None


def _staged_rule(store, jid: str) -> str | None:
    from steward import deadline, sequencing
    if sequencing.is_queued(store, jid):
        return "staged"
    if deadline.current(store, jid) is not None:  # this job's commitment in force = it holds the deadline slot
        return "deadline_slot"
    return None


def actor(store, job, *, now: float, settings: dict, runner_alive=None, check_runs_live=None) -> str | None:
    """The rule naming `job`'s live actor, "terminal" for a finished job, or None (= orphan). Reads only."""
    from steward.machine import TERMINAL_STATES
    from steward.models import JobState

    runner_alive = runner_alive or _default_runner_alive(store)
    check_runs_live = check_runs_live or _default_check_runs_live(store, now)
    jid, st, idle_s = job.job_id, job.state, float(settings["idle_s"])
    if st in TERMINAL_STATES:
        return "terminal"
    name = None
    if st in (JobState.RUNNING, JobState.PAUSED, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED):
        name = _runner_rule(store, jid, st in (JobState.WORKER_FINISHED, JobState.CHECKS_PASSED), now, idle_s,
                            runner_alive, check_runs_live, settings)
        if name is None and st == JobState.PAUSED and runner_alive(jid):
            # a pause with a live runner is its owner's (governor or operator): resuming is theirs, not an orphan's
            # ladder. A PAUSED job whose runner died stays an orphan (heal_runner).
            name = "runner:paused"
    elif st == JobState.STOPPING:
        name = "runner" if runner_alive(jid) else None
    elif st == JobState.NEEDS_HUMAN:
        name = _human_pending(store, jid)
    elif st == JobState.ACCEPTED:
        name = _accepted_rule(store, jid, now)
    elif st in (JobState.CREATED, JobState.CONTRACTED):
        name = _staged_rule(store, jid)
    if name is None and _ask_open(store, jid):  # §5.1 "The ask": the open ask is itself the actor (never re-asked)
        name = "human:liveness_ask"
    if name is None and _tl_wait_live(store, jid, now):  # spec §5.1 "any" row, last in order
        name = "tl_wait"
    return name


def _ask_open(store, jid: str) -> bool:
    from steward import blockers
    return any(b.kind == ASK_KIND for b in blockers.open_blockers(store, jid))


# -- the sweep (spec §5.1): runs on the control tick, throttled by its own kv clock --------------------------------

PSEUDO = "_liveness"


def sweep(store, *, now=None, settings=None, remedy=None, heal_lock=None, runner_alive=None, check_runs_live=None,
          stop_fn=None, release_fn=None) -> dict:
    """One liveness pass. `stop_fn(job_id)` stops the fix-ups marked for stopping (§5.3); the control tick passes
    `code_stop` (code judgment), never the operator stop. `release_fn(store, job_id) -> bool` releases an
    incorporated ACCEPTED job (§5.3 "Called from ... the liveness sweep"); default
    `incorporate.release_on_incorporation`."""
    from steward import config
    from steward.machine import TERMINAL_STATES
    from steward.models import JobState, Judgment
    s = settings or config.liveness_settings()
    if not s["enabled"]:
        return {"skipped": "off"}
    now = time.time() if now is None else now
    last = float(store.kv_get(PSEUDO, "tick_at") or 0.0)
    if last and now - last < s["every_s"]:
        return {"skipped": "throttle"}
    store.kv_set(PSEUDO, "tick_at", now)
    remedy = remedy or globals().get("remedy_default")
    heal_lock = heal_lock or _default_heal_lock
    out = {"checked": 0, "orphans": [], "remedied": [], "healed_locks": [], "released": []}
    if stop_fn is not None:  # §5.3 / Task 5: fix-ups whose parent finished are stopped through the normal stop
        from steward import train_housekeeping
        try:
            out["fixups_stopped"] = train_housekeeping.stop_marked_fixups(store, stop_fn)
        except Exception as exc:  # noqa: BLE001 - one bad mark never stops the sweep; the mark is retried next tick
            out["fixups_error"] = f"{type(exc).__name__}: {exc}"[:200]
    oldest: dict = {}  # repo -> oldest non-terminal job (the events FK forbids a pseudo job for the heal event)
    for job in store.list_jobs():
        if job.state in TERMINAL_STATES:
            continue
        out["checked"] += 1
        cur = oldest.get(job.repo)
        if cur is None or str(job.created_at) < str(cur.created_at):
            oldest[job.repo] = job
        rule = actor(store, job, now=now, settings=s, runner_alive=runner_alive, check_runs_live=check_runs_live)
        if rule == "incorporated" and job.state == JobState.ACCEPTED and _release(store, job.job_id, release_fn):
            out["released"].append(job.job_id)
            rule = "terminal"
        since = store.kv_get(job.job_id, ORPHAN_KEY)
        if rule is not None:
            if since is not None:
                store.kv_delete(job.job_id, ORPHAN_KEY)
                store.append_event(job.job_id, what="liveness_actor", evidence={"rule": rule, "at": _iso(now)},
                                   rule="liveness", judgment=Judgment.code)
            if rule.startswith("worker:") or rule == "runner:turn_open":
                _settle_sent(store, job.job_id, "worker responded")
            continue
        if since is None:
            store.kv_set(job.job_id, ORPHAN_KEY, now)
            store.append_event(job.job_id, what="liveness_orphan", evidence={"state": job.state.value, "since": _iso(now)},
                               rule="liveness", judgment=Judgment.code)
            since = now
        out["orphans"].append(job.job_id)
        if now - float(since) >= s["orphan_s"] and remedy is not None:
            try:
                res = remedy(store, job, now=now, settings=s, orphan_since=float(since))
            except Exception as exc:  # noqa: BLE001 - a failed remedy is a result (§7), never a crash of the sweep
                res = _remedy_crashed(store, job.job_id, exc)
            if res:
                out["remedied"].append(job.job_id)
    for repo, job in oldest.items():
        h = heal_lock(store, repo, now=now, job_id=job.job_id)
        if h.get("healed"):
            out["healed_locks"].append(h)
    return out


RELEASE_RESULT_KEY = "liveness_release_result"


def _release(store, jid: str, release_fn) -> bool:
    """§5.3 release on incorporation from the sweep. A refusal (no reachable receipt) or an error is one
    `liveness_remedy {rung: release}` event per distinct result, never a crash and never per-tick spam."""
    from steward import incorporate
    from steward.models import Judgment
    fn = release_fn or incorporate.release_on_incorporation
    try:
        if fn(store, jid):
            return True
        job = store.get_job(jid)
        why = incorporate.release_evidence(store, job).get("reason", "") if job is not None else "job gone"
        result = f"refused: {str(why)[:120]}"
    except Exception as exc:  # noqa: BLE001 - a failed release is a result, not a crash of the sweep
        result = f"error: {type(exc).__name__}: {str(exc)[:120]}"
    if store.kv_get(jid, RELEASE_RESULT_KEY) != result:
        store.kv_set(jid, RELEASE_RESULT_KEY, result)
        store.append_event(jid, what="liveness_remedy", evidence={"rung": "release", "try": 0, "result": result},
                           rule="liveness", judgment=Judgment.code)
    return False


FIXUP_STOP_RULE = "cleanup_fixup_stop"


def code_stop(store, job_id: str) -> dict:
    """The sweep's stop of a fix-up whose parent is done (§5.3): the normal stop transitions, decided by code under
    FIXUP_STOP_RULE (Judgment.code). The owner's own stop (`steward stop`, a "stop" answer to the ask) stays on the
    operator path (Judgment.human)."""
    from steward import cleanup
    from steward.control import Controller
    mark = store.kv_get(job_id, cleanup.FIXUP_STOP_KEY)
    cause = str((mark or {}).get("cause", "")) if isinstance(mark, dict) else ""
    ok, detail = Controller(store).stop_by_code(job_id, what="fix-up stopped: its parent is done",
                                                rule=FIXUP_STOP_RULE, evidence={"cause": cause} if cause else None)
    return {"accepted": ok, "detail": detail}


def _iso(t: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _default_heal_lock(store, repo, *, now, job_id):
    from steward import incorporate as inc
    try:
        with inc.target_lock(store, repo, timeout=0, purpose="liveness heal lock"):
            return inc.heal_index_lock(store, job_id, repo, now=now)
    except inc.LockBusy:
        return {}
    except Exception:  # noqa: BLE001 - a repo that is gone or not git: nothing to heal
        return {}


def component(store, *, now: float):
    from steward.health import Component
    from steward.machine import TERMINAL_STATES
    last = float(store.kv_get(PSEUDO, "tick_at") or 0.0)
    if not last:
        return Component("liveness", None, "sweep has not run")
    age = now - last
    worst = 0.0
    for job in store.list_jobs():
        s = store.kv_get(job.job_id, ORPHAN_KEY)
        if s is not None and job.state not in TERMINAL_STATES:
            worst = max(worst, now - float(s))
    if age > 300.0:
        return Component("liveness", False, f"sweep stale {age:.0f}s", [f"liveness sweep last ran {age:.0f}s ago"])
    if worst > 3600.0:
        return Component("liveness", False, f"orphan {worst/3600:.1f}h", [f"a job has had no actor for {worst/3600:.1f}h"])
    from steward import ownership
    lost = ownership.undelivered_notices(store)
    if lost:  # u4: an escalation the owner never received is as stuck as a job with no actor
        names = ", ".join(f"{u['job_id'][:8]}/{u['blocker_id']}" for u in lost[:4])
        return Component("liveness", False, f"undelivered escalation: {names}",
                         [f"{u['job_id'][:8]}: owner notice for {u['blocker_id']} undelivered ({u.get('reason')})"
                          for u in lost])
    settings = {"no_worker_s": NO_WORKER_S}
    ghosts = [j.job_id[:8] for j in store.list_jobs() if ghost(store, j, now=now, settings=settings)]
    if ghosts:  # u2: a RUNNING job whose worker never showed up is red, whatever the runner says
        return Component("liveness", False, f"no worker evidence: {', '.join(ghosts)}",
                         [f"{jid}: running with no worker evidence since dispatch" for jid in ghosts])
    return Component("liveness", True, f"ok, worst orphan {worst/60:.0f}m")


# -- the remedy ladder (spec §5.1 "Remedy ladder") -------------------------------------------------------------------
#
# Every rung records its try BEFORE acting (kv TRIES_KEY, timestamps within 24 h), writes one `liveness_remedy`
# {rung, try, result} event (Judgment.code) and returns that dict. A rung past its cap is skipped. When no rung is
# left for an orphan the single ask is raised; the open ask is itself the actor (`actor()`), so it is never repeated.
# No rung sends an urgent steer or writes into a worker's worktree (SP-1): nudges are plain `Steering` records the
# runner delivers, and everything else is a store write, a control handler, or a relay.

DAY_S = 86400.0
NUDGE_TEXT = ("Your turn ended without a done report. If the work is done, reply with your done report now; "
              "otherwise continue the work. Do not wait for anything.")
# rung -> max tries per 24 h. `None` = `[liveness] tries_per_day` (2). The two nudges share one counter; the TL
# completion review, the NEEDS_HUMAN re-verify, resume-closeout, relaunch and the ask are "once" rungs.
RUNGS: dict[str, int | None] = {"finish_stop": None, "heal_runner": None, "nudge": 2, "completion_review": 1,
                                "requeue_verify": None, "resume_closeout": 1, "release": None, "relaunch": 1,
                                "ask": 1}
ANSWERS = ("retry", "stop", "release")


def _controller():
    from steward import ownership
    return ownership.CONTROLLER


def _recent(rec, rung: str, now: float) -> list[float]:
    out = []
    for x in (rec or {}).get(rung) or []:
        try:
            if now - float(x) < DAY_S:
                out.append(float(x))
        except (TypeError, ValueError):
            continue
    return out


def _tries(store, jid: str, rung: str, now: float, cap: int) -> int | None:
    """Record a try BEFORE acting; None when the rung is exhausted for the day. A plain read-modify-write of kv
    TRIES_KEY, not atomic: it assumes a single writer, the control loop's liveness TickStep (one sweep per tick, one
    control pane). A second concurrent sweep could lose a try and so exceed a cap by one."""
    t = dict(store.kv_get(jid, TRIES_KEY) or {})
    recent = _recent(t, rung, now)
    if len(recent) >= cap:
        return None
    recent.append(now)
    t[rung] = recent
    store.kv_set(jid, TRIES_KEY, t)
    return len(recent)


def _result(res) -> str:
    if res is False:
        return "refused: returned False"
    if isinstance(res, dict) and res.get("accepted", True) is False:
        return f"refused: {str(res.get('detail') or res.get('error_kind') or '')[:120]}"
    if not isinstance(res, dict) and getattr(res, "accepted", True) is False:  # a Steering Receipt
        return f"refused: {str(getattr(res, 'detail', ''))[:120]}"
    return "ok"


# u2: rungs that only SEND something to the worker. Their result is "sent", never "ok": whether it worked is the
# worker's answer, recorded later as one `liveness_remedy_outcome` ("worker responded" / "no response").
SEND_RUNGS = ("nudge", "completion_review")
SENT_KEY = "liveness_sent"


def _settle_sent(store, jid: str, result: str) -> None:
    from steward.models import Judgment
    sent = store.kv_get(jid, SENT_KEY)
    if not isinstance(sent, dict):
        return
    store.kv_delete(jid, SENT_KEY)
    store.append_event(jid, what="liveness_remedy_outcome",
                       evidence={"rung": sent.get("rung"), "try": sent.get("try"), "result": result},
                       rule="liveness", judgment=Judgment.code)


def _awaiting_response(store, jid: str, now: float, settings: dict) -> bool:
    """A sent remedy is still inside its response window (orphan_s): the worker gets that long to answer before the
    next rung. Past it, the outcome is recorded as "no response"."""
    sent = store.kv_get(jid, SENT_KEY)
    if not isinstance(sent, dict):
        return False
    if now - float(sent.get("at") or 0.0) < float(settings.get("orphan_s") or 0.0):
        return True
    _settle_sent(store, jid, "no response")
    return False


def _act(store, job, rung: str, n: int, fn, now: float | None = None) -> dict:
    from steward.models import Judgment
    try:
        result = _result(fn())
    except Exception as exc:  # noqa: BLE001 - a failed remedy is a result, not a crash of the sweep
        result = f"error: {type(exc).__name__}: {str(exc)[:120]}"
    if result == "ok" and rung in SEND_RUNGS:
        result = "sent"
        store.kv_set(job.job_id, SENT_KEY, {"rung": rung, "try": n, "at": time.time() if now is None else now})
    store.append_event(job.job_id, what="liveness_remedy", evidence={"rung": rung, "try": n, "result": result},
                       rule="liveness", judgment=Judgment.code)
    return {"rung": rung, "try": n, "result": result}


def _remedy_crashed(store, jid: str, exc: Exception) -> dict:
    """The remedy itself raised (outside a rung): one `liveness_remedy` event per distinct error, never a crash."""
    from steward.models import Judgment
    result = f"error: {type(exc).__name__}: {str(exc)[:120]}"
    if store.kv_get(jid, REMEDY_ERROR_KEY) == result:
        return {}
    store.kv_set(jid, REMEDY_ERROR_KEY, result)
    store.append_event(jid, what="liveness_remedy", evidence={"rung": "remedy", "try": 0, "result": result},
                       rule="liveness", judgment=Judgment.code)
    return {"rung": "remedy", "try": 0, "result": result}


def _tried(store, jid: str, now: float) -> list[str]:
    """Every rung try in the last 24 h, oldest first (the ask's "Tried:")."""
    t = store.kv_get(jid, TRIES_KEY) or {}
    pairs = sorted((ts, rung) for rung in t if rung != "ask" for ts in _recent(t, rung, now))
    return [rung for _, rung in pairs]


def _stuck_since(store, jid: str, orphan_since: float, settings: dict, now: float) -> float:
    """When the job was first found actor-less for this ladder: a nudge creates an actor and restarts the orphan
    clock, so the first try (minus the orphan threshold) is the honest start."""
    t = store.kv_get(jid, TRIES_KEY) or {}
    first = min((ts for rung in t if rung != "ask" for ts in _recent(t, rung, now)), default=None)
    return orphan_since if first is None else min(orphan_since, first - float(settings.get("orphan_s") or 0.0))


# -- per-state predicates ---------------------------------------------------------------------------------------------

def _last_verify_green(store, jid: str) -> bool:
    """The stored background verification is green: `done` (or consumed by closeout) with a result, no new failures,
    not dirty, nothing broken, not waiting on a baseline."""
    from steward import bg_verify
    rec = bg_verify.record(store, jid)
    if not rec or rec.get("status") not in ("done", "consumed") or not isinstance(rec.get("result"), dict):
        return False
    if rec.get("status") == "consumed" and rec.get("outcome") not in (None, "consumed"):
        return False  # discarded / lost / pre-empted / crashed runs are not a verification
    r = rec["result"]
    return (r.get("new_failures") or []) == [] and not r.get("dirty") and not r.get("broken") \
        and not r.get("unattributed")


def _nothing_pending(store, jid: str) -> bool:
    """NEEDS_HUMAN "nothing pending": no open blocker of any disposition (a candidate/reviewing one is the ownership
    review's, never bypassed) and no pending approval of any kind."""
    from steward import approvals, blockers
    return not blockers.open_blockers(store, jid) and not approvals.list_approvals(store, jid, status="pending")


# -- default rung actions (each injectable; the control tick passes in-process handlers for heal/relaunch) -----------

def _relay(store, op: str, args: dict) -> dict:
    """Out-of-pane default: relay to the control pane. Never used from inside the control pane's own tick (that
    would wait on itself); cli's tick passes the handler instead."""
    import uuid

    from steward import cli
    try:
        return cli._relay(store, op, args, request_id=f"liveness-{uuid.uuid4()}", origin="liveness")
    except cli._RelayFailed as exc:
        return {"accepted": False, "detail": str(exc)}


def _default_heal(store):
    return lambda jid: _relay(store, "runner_restart", {"job_id": jid, "mode": "heal"})


def _default_relaunch(store):
    def relaunch(jid: str) -> dict:
        args = store.kv_get(jid, "launch_args")
        if not isinstance(args, dict) or not args:
            return {"accepted": False, "detail": "no stored launch_args: this job was never launched"}
        return _relay(store, "launch", {**args, "job_id": jid})
    return relaunch


def _default_nudge(store):
    import uuid

    from steward.steering import Steering
    # verb `nudge`, origin `liveness` (code provenance), never urgent: Steering refuses an urgent non-human steer
    return lambda jid, text: Steering(store).record(jid, "nudge", text, request_id=f"liveness-{uuid.uuid4()}",
                                                    origin="liveness")


def _default_completion_review(store):
    """The runner's tech lead reads "the worker stopped" (Arbiter source `yield`, kind `worker_stopped`, route outer):
    the existing entry for "is this work complete or what next". Not the closeout `completion_candidate` (that
    one is a CHECKS_PASSED ruling at a verified SHA and would be malformed for a RUNNING job)."""
    from steward.fleet_runtime import Arbiter
    return lambda jid: Arbiter(store).submit(jid, Arbiter.YIELD, "worker_stopped",
                                             reason="liveness completion review: turn ended, two nudges unanswered")


def _back_to_worker_finished(store, jid: str, *, what: str, rule: str, evidence: dict) -> None:
    """NEEDS_HUMAN/CHECKS_PASSED -> RUNNING -> WORKER_FINISHED (the `closeout.reattribute_after_baseline`
    precedent): the runner's closeout then takes the job (healed by the next sweep if it is not running)."""
    from steward.models import JobState, Judgment
    job = store.get_job(jid)
    if job.state != JobState.RUNNING:
        store.set_state(jid, JobState.RUNNING, what=what, evidence=evidence, rule=rule, judgment=Judgment.code)
    store.set_state(jid, JobState.WORKER_FINISHED, what=what, evidence=evidence, rule=rule, judgment=Judgment.code)


def _default_requeue_verify(store):
    """Re-queue the verify: a lost/failed background run is consumed as `requeued` (never "crashed") and the runner's
    closeout path starts a fresh verification; a parked or passed job goes back to WORKER_FINISHED for it."""
    def requeue(jid: str) -> dict:
        from steward import bg_baseline, bg_verify
        from steward.models import JobState, Judgment
        job = store.get_job(jid)
        rec = bg_verify.record(store, jid) or {}
        if rec.get("status") == "running" and bg_baseline._alive(rec.get("pid")):
            return {"accepted": False, "detail": "a background verification is still running"}
        prev = rec.get("status")
        if prev in ("running", "done", "failed"):
            bg_verify.consume(store, jid, "requeued")
        store.append_event(jid, what="verify_requeued", evidence={"state": job.state.value, "previous": prev},
                           rule="liveness_requeue_verify", judgment=Judgment.code)
        if job.state in (JobState.NEEDS_HUMAN, JobState.CHECKS_PASSED):
            _back_to_worker_finished(store, jid, what="verification re-queued by sweep",
                                     rule="liveness_requeue_verify", evidence={"from": job.state.value})
        return {"accepted": True, "detail": f"verification re-queued from {job.state.value}"}
    return requeue


def _default_resume_closeout(store):
    """Resume closeout from the stored green verification: re-arm it as `done` and return the job to
    WORKER_FINISHED, so the runner's own `_bg_verify_tick` -> `_finish_closeout` decides (weakening detection,
    deadline evidence, completion ruling) exactly as it would have. A moved HEAD makes that run re-verify."""
    def resume(jid: str) -> dict:
        from steward import bg_verify
        from steward.models import JobState
        job = store.get_job(jid)
        if job.state != JobState.NEEDS_HUMAN or not _last_verify_green(store, jid):
            return {"accepted": False, "detail": "no green stored verification to resume from"}
        rec = dict(bg_verify.record(store, jid) or {})
        rec.update(status="done", rearmed_by="liveness", rearmed_at=time.time())
        store.kv_set(jid, bg_verify.KEY, rec)
        _back_to_worker_finished(store, jid, what="closeout resumed by sweep from the stored green verification",
                                 rule="liveness_resume_closeout", evidence={"commit_sha": rec.get("sha")})
        return {"accepted": True, "state": JobState.WORKER_FINISHED.value}
    return resume


def _job_panes(store, jid: str, ex) -> tuple[str | None, str | None, list[dict]]:
    """(runner pane, worker pane, all panes) still present for this job. The worker pane counts only when it is the
    recorded kv `herdr_pane` AND sits in the job's recorded tab (a reused pane id elsewhere is never the job's)."""
    from steward import launch
    job = store.get_job(jid)
    panes = launch.list_panes(ex, job.repo)
    runner = launch.runner_pane_id(ex, store, job)
    worker_id, tab = store.kv_get(jid, "herdr_pane"), store.kv_get(jid, "herdr_tab")
    worker = next((p["pane_id"] for p in panes if worker_id and p.get("pane_id") == worker_id
                   and (not tab or p.get("tab_id") == tab)), None)
    return runner, worker, panes


def _default_pane_gone(store):
    """The job's runner pane AND its recorded worker pane (kv `herdr_pane`) are both gone. Unreadable panes = not
    gone (a STOPPED job with a live worker would be an unsupervised writer). A recorded worker pane id seen in
    another tab is not provably gone either: it is left alone (never closed) and the ladder ends in the ask."""
    from steward.runner_exec import SubprocessRunner

    def gone(jid: str) -> bool:
        try:
            runner, _worker, panes = _job_panes(store, jid, SubprocessRunner())
            wid = store.kv_get(jid, "herdr_pane")
            return runner is None and not (wid and any(p.get("pane_id") == wid for p in panes))
        except Exception:  # noqa: BLE001
            return False
    return gone


def _default_close_panes(store):
    """Close a STOPPING job's leftover runner and worker panes (the runner is dead, so nothing else will): the same
    `herdr pane close` a settled runner runs on its own pane (`_close_runner_pane_on_exit`) and `HerdrWorker.stop`
    runs on its worker pane. The operator's stop already decided the worker ends; its branch and worktree are kept."""
    from steward.runner_exec import SubprocessRunner

    def close(jid: str) -> dict:
        ex = SubprocessRunner()
        job = store.get_job(jid)
        runner, worker, _ = _job_panes(store, jid, ex)
        closed, failed = [], []
        for pane in (p for p in (runner, worker) if p):
            res = ex.run(["herdr", "pane", "close", pane], cwd=job.repo)
            (closed if res.exit_code == 0 else failed).append(pane)
        return {"closed": closed, "failed": failed}
    return close


def _finish_stop(store, job, pane_gone, close_panes) -> dict:
    """STOPPING, runner dead (§5.1): close the leftover panes through an explicit path, re-check, then finish the
    stop. Never re-sends the operator stop (control answers "already STOPPING; no action taken" to that)."""
    from steward.models import JobState, Judgment
    jid = job.job_id
    if not pane_gone(jid):
        try:
            res = close_panes(jid) or {}
        except Exception as exc:  # noqa: BLE001
            res = {"closed": [], "failed": [], "error": f"{type(exc).__name__}: {str(exc)[:80]}"}
        store.append_event(jid, what="liveness_panes_closed", evidence={**res, "reason": "STOPPING, runner dead"},
                           rule="liveness_finish_stop", judgment=Judgment.code)
        if not pane_gone(jid):
            return {"accepted": False, "detail": f"runner dead; leftover pane(s) still present after close "
                                                 f"(closed {res.get('closed')}, failed {res.get('failed')}"
                                                 f"{', ' + res['error'] if res.get('error') else ''}); "
                                                 f"retried next sweep"}
    store.set_state(jid, JobState.STOPPED, what="stop finished by sweep",
                    evidence={"reason": "runner dead, worker pane gone"}, rule="liveness_finish_stop",
                    judgment=Judgment.code)
    return {"accepted": True}


def _ladder(store, job, *, runner_alive, pane_gone, close_panes, fns, now: float, settings: dict) -> list | None:
    """[(rung, cap_or_None, fn)] for this orphan's shape, in order; None = an actor-equivalent condition holds
    (nothing to do, never ask)."""
    from steward import incorporate
    from steward.launch import HEAL_STATES
    from steward.models import JobState
    jid, st = job.job_id, job.state
    if st == JobState.STOPPING:
        if runner_alive(jid):
            return None
        return [("finish_stop", None, lambda: _finish_stop(store, job, pane_gone or _default_pane_gone(store),
                                                           close_panes or _default_close_panes(store)))]
    if st in HEAL_STATES and not runner_alive(jid):
        return [("heal_runner", None, lambda: fns["heal"](jid))]
    if st == JobState.RUNNING:
        if ghost(store, job, now=now, settings=settings):
            return []  # u2: no worker ever answered this dispatch — nudging nobody helps; straight to the one ask
        nudge = lambda: fns["nudge"](jid, NUDGE_TEXT)  # noqa: E731
        return [("nudge", None, nudge), ("nudge", None, nudge),
                ("completion_review", None, lambda: fns["completion_review"](jid))]
    if st in (JobState.WORKER_FINISHED, JobState.CHECKS_PASSED):
        return [("requeue_verify", None, lambda: fns["requeue_verify"](jid))]
    if st == JobState.NEEDS_HUMAN:
        if not _nothing_pending(store, jid):
            return []  # the ownership review owns an open blocker: no code rung bypasses it, straight to the ask
        out = []
        if _last_verify_green(store, jid):
            out.append(("resume_closeout", None, lambda: fns["resume_closeout"](jid)))
        out.append(("requeue_verify", 1, lambda: fns["requeue_verify"](jid)))  # "re-run verify once"
        return out
    if st == JobState.ACCEPTED:
        if incorporate.is_incorporated(store, jid):
            return [("release", None, lambda: incorporate.release_on_incorporation(store, jid))]
        return []  # parked: the train retries / starts the one rebase fix-up itself (§5.4); the sweep only asks
    if st in (JobState.CREATED, JobState.CONTRACTED):
        return [("relaunch", None, lambda: fns["relaunch"](jid))]
    return []


def remedy_default(store, job, *, now, settings, orphan_since, runner_alive=None, pane_gone=None, heal_fn=None,
                   nudge_fn=None, completion_review_fn=None, requeue_verify_fn=None, resume_closeout_fn=None,
                   relaunch_fn=None, close_panes_fn=None, ask_fn=None, notifier=None) -> dict:
    """One step of the ladder for an orphan: the first rung with tries left acts; none left -> the single ask.
    Returns {"rung", "try", "result"} or {} (an actor-equivalent condition, or already asked)."""
    jid = job.job_id
    if _ask_open(store, jid):
        return {}  # already asked: the ask is the actor
    runner_alive = runner_alive or _default_runner_alive(store)
    fns = {"heal": heal_fn or _default_heal(store), "nudge": nudge_fn or _default_nudge(store),
           "completion_review": completion_review_fn or _default_completion_review(store),
           "requeue_verify": requeue_verify_fn or _default_requeue_verify(store),
           "resume_closeout": resume_closeout_fn or _default_resume_closeout(store),
           "relaunch": relaunch_fn or _default_relaunch(store)}
    if _awaiting_response(store, jid, now, settings):
        return {}  # u2: the last nudge/review is still inside its response window
    ladder = _ladder(store, job, runner_alive=runner_alive, pane_gone=pane_gone, close_panes=close_panes_fn, fns=fns,
                     now=now, settings=settings)
    if ladder is None:
        return {}
    default_cap = int(settings["tries_per_day"])
    for rung, cap, fn in ladder:
        cap = cap if cap is not None else (RUNGS.get(rung) or default_cap)
        n = _tries(store, jid, rung, now, cap)
        if n is None:
            continue
        return _act(store, job, rung, n, fn, now)
    if len(_recent(store.kv_get(jid, TRIES_KEY), "ask", now)) >= RUNGS["ask"]:
        return {}
    hours = (now - _stuck_since(store, jid, orphan_since, settings, now)) / 3600.0
    tried = _tried(store, jid, now)
    if ask_fn is not None:
        bid = ask_fn(store, job, tried=tried, now=now, hours=hours)
    else:
        bid = ask(store, job, tried=tried, now=now, hours=hours, notifier=notifier)
    if not bid:  # no blocker was raised: not asked, so the ask try is not spent (retried next sweep)
        return {"rung": "ask", "try": 0, "result": bid}
    n = _tries(store, jid, "ask", now, RUNGS["ask"])
    return {"rung": "ask", "try": n or RUNGS["ask"], "result": bid}


# -- the ask (spec §5.1 "The ask") ------------------------------------------------------------------------------------

def _options(store, job) -> tuple[str, ...]:
    from steward import incorporate
    from steward.models import JobState
    opts = ("retry",)
    if job.state != JobState.ACCEPTED:  # ACCEPTED -> STOPPED is not a transition: stop is not an answer there
        opts += ("stop",)
    if incorporate.is_incorporated(store, job.job_id):
        opts += ("release",)
    return opts


def _hours_text(hours: float) -> str:
    return f"{hours:.0f}h" if hours >= 1.0 else f"{max(hours, 0.0) * 60:.0f}m"


def ask(store, job, *, tried: list[str], now: float, hours: float | None = None, notifier=None) -> str:
    """The single owner ask: one `liveness_stuck` blocker confirmed human (one linked `general` approval, one
    notice through ownership's own dedupe). Returns the blocker id ("" when the row could not be opened)."""
    from steward import approvals, blockers, ownership
    from steward.models import Judgment
    jid = job.job_id
    if hours is None:
        hours = (now - float(store.kv_get(jid, ORPHAN_KEY) or now)) / 3600.0
    goal = " ".join(str(job.goal or "").split())[:60]
    opts = _options(store, job)
    decision = (f"{jid[:8]} {goal}: stuck in {job.state.value} for {_hours_text(hours)}. "
                f"Tried: {', '.join(tried) or 'nothing applicable'}. Say: {' / '.join(opts)}.")
    since = store.kv_get(jid, ORPHAN_KEY)
    evidence = [f"state:{job.state.value}", f"orphan_since:{_iso(float(since)) if since else _iso(now)}",
                f"asked_at:{_iso(now)}", *[f"tried:{r}" for r in tried]]
    b, _created = blockers.raise_blocker(
        store, jid, kind=ASK_KIND, subject=jid, claim=f"no live actor; every code remedy tried ({', '.join(tried) or 'none applicable'})",
        evidence=evidence, origin_actor=ownership.CONTROLLER, now=now, origin_rule="liveness")
    if not b.is_open:
        return ""
    if b.disposition != "confirmed_human":
        req = blockers.HumanRequest(
            decision=decision,
            why_authority_insufficient="every code remedy is exhausted; stopping or releasing is the owner's",
            evidence=tuple(tried), continuing="other jobs continue; this job gets no further code remedies until "
                                              "you answer (approve = retry, deny = stop)",
            options=opts)
        b = ownership.confirm_human(store, b.blocker_id, req, actor=ownership.CONTROLLER, now=now, notifier=notifier)
    aid = ((b.clearing or {}).get("params") or {}).get("approval_id")
    if aid:
        approvals.annotate(store, jid, aid, liveness_ask=True, blocker_id=b.blocker_id, options=list(opts))
    store.append_event(jid, what="liveness_ask", evidence={"blocker_id": b.blocker_id, "options": list(opts),
                                                           "tried": tried, "approval_id": aid},
                       rule="liveness", judgment=Judgment.code)
    return b.blocker_id


def answer_for(approval: dict | None, op: str) -> str | None:
    """The liveness answer an owner's approve/deny of the ask's approval means: approve = retry, deny = stop."""
    if not isinstance(approval, dict) or not approval.get("liveness_ask"):
        return None
    return "retry" if op == "approve" else "stop"


def apply_answer(store, job_id: str, answer: str, *, stop_fn, now: float, actor=None) -> dict:
    """The owner's answer to the ask (§5.1): retry = reset tries and run the ladder again; stop = the normal stop;
    release = code release on incorporation (reachability evidence only; else the owner's `steward release`).
    Resolves the open ask; the owner's decision is the audit trail (Judgment.human)."""
    from steward import blockers, incorporate
    from steward.models import Judgment
    if answer not in ANSWERS:
        return {"accepted": False, "detail": f"unknown answer {answer!r} (retry / stop / release)"}
    if answer == "retry":
        store.kv_delete(job_id, TRIES_KEY)
        store.kv_delete(job_id, REMEDY_ERROR_KEY)
        res = {"accepted": True, "detail": "liveness ladder reset"}
    elif answer == "stop":
        if stop_fn is None:
            return {"accepted": False, "detail": "no stop path available here"}
        res = stop_fn(job_id)
        res = res if isinstance(res, dict) else {"accepted": True}
    else:
        ok = incorporate.release_on_incorporation(store, job_id)
        res = {"accepted": bool(ok), "detail": "released" if ok else "no reachable merge receipt: use `steward release`"}
    if res.get("accepted", True):
        for b in blockers.open_blockers(store, job_id):
            if b.kind == ASK_KIND:
                try:
                    blockers.transition(store, b.blocker_id, "resolved", actor=actor or _controller(),
                                        reason=f"owner answered {answer}", now=now, resolution={"answer": answer})
                except blockers.BlockerError:
                    pass  # resolved concurrently (e.g. ownership saw the approval decided first)
    store.append_event(job_id, what="liveness_answer", evidence={"answer": answer, "result": res.get("detail", "")[:200],
                                                                 "accepted": bool(res.get("accepted", True))},
                       rule="liveness_answer", judgment=Judgment.human)
    return res
