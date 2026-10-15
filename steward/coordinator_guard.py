"""Control-pane refusals for origin `coordinator` (spec 5.3). Same shape as unblock_agent.guard: other origins are
never touched; an op off the list, a non-nudge/focus/avoid steer, an urgent steer, an acceptance/floor approval, a
delivering answer, a stop without a done parent, or a KILL file on disk is refused with a plain reason."""
from __future__ import annotations

from steward import approvals, coordinator_link, owner, steer_agent
from steward.machine import TERMINAL_STATES
from steward.store import Store

ORIGIN = "coordinator"
STEER_VERBS = frozenset({"nudge", "focus", "avoid"})
STEER_OPS = frozenset({"goal", "focus", "avoid", "nudge", "priority", "steer"})
ALLOWED_OPS = STEER_OPS | {"send_back", "answer", "approve", "deny", "runner_restart", "stop",
                           "coordinator_hold", "coordinator_episode"}
_DELIVER_FLAGS = ("then_accept", "write_source", "and_continue", "done")


def _parent_of(store, job_id: str) -> str | None:
    link = store.kv_get(job_id, "autopilot_followup")
    if isinstance(link, str) and link:
        return link
    alt = store.kv_get(job_id, "parent_job")
    return alt if isinstance(alt, str) and alt else None


def parent_done(store, job_id: str) -> tuple[bool, str]:
    """Is the job's fix-up parent finished (terminal or incorporated)? `store` may be a Store or a read-only twin."""
    pid = _parent_of(store, job_id)
    if not pid:
        return False, "no parent recorded"
    parent = store.get_job(pid)
    if parent is None:
        return False, f"parent {pid[:8]} unknown"
    if parent.state in TERMINAL_STATES or store.kv_get(pid, "incorporated_commit"):
        return True, f"parent {pid[:8]} is {parent.state.value}"
    return False, f"parent {pid[:8]} is {parent.state.value} and not incorporated"


def _answer_refusal(store, args: dict) -> str | None:
    if any(args.get(k) for k in _DELIVER_FLAGS):
        return owner.fill("delivering or accepting is {owner_poss}; the coordinator only records answers")
    records = store.kv_get(args.get("job_id", ""), "questions", default=[]) or []
    for item in args.get("answers") or []:
        if item.get("defer"):
            return owner.fill("deferring a question is {owner_poss}")
        cited = "cite:" in str(item.get("note") or "")
        if item.get("other") is not None and "cite:" not in str(item.get("other")) and not cited:
            return "a free-text answer needs a `cite:` line naming the roadmap/spec/plan it comes from"
        opt = item.get("option")
        if opt is not None and opt != "recommended" and not cited:
            rec = next((r for r in records if r.get("qid") == item.get("qid")), None)
            if rec is None or opt not in (rec.get("recommended") or []):
                return "a non-default option needs a `cite:` line in its note naming the roadmap/spec/plan"
    return None


def _send_back_refusal(store, args: dict) -> str | None:
    """A send-back closes every pending acceptance/general approval; refuse when one is the owner's (acceptance, or a
    floor-tier hold)."""
    from steward import resolver

    job_id = args.get("job_id", "")
    job = store.get_job(job_id)
    for rec in approvals.list_approvals(store, job_id) if job is not None else []:
        if rec.get("status") != "pending":
            continue
        if rec.get("kind", "general") == "acceptance" or rec.get("question") == approvals.ACCEPTANCE_QUESTION:
            return owner.fill("a pending acceptance approval is {owner_poss}; a send-back would close it")
        if rec.get("kind", "general") == "general":
            try:
                tier, why = resolver.classify(rec, job, store=store)
            except Exception as exc:  # noqa: BLE001 - can't classify -> the safe answer
                return f"cannot classify a pending approval ({exc}); leaving the send-back for {owner.name()}"
            if tier == "floor":
                return f"a pending floor approval ({why}) would be closed by a send-back; only {owner.name()} sends back"
    return None


def guard(store, cmd: dict) -> str | None:
    if cmd.get("origin") != ORIGIN:
        return None
    if coordinator_link.killed():
        return f"coordinator KILL file present ({coordinator_link.kill_file()}); every coordinator command is refused"
    op = cmd.get("op")
    args = cmd.get("args") or {}
    if op not in ALLOWED_OPS:
        return owner.fill(f"the coordinator may not run {op!r} (accept, release, incorporate, launch are {{owner_poss}})")
    if op in STEER_OPS:
        if args.get("urgent"):
            return "the coordinator never sends an urgent steer"
        if (args.get("verb") or op) not in STEER_VERBS:
            return "the coordinator steers only with nudge, focus or avoid"
    if op in ("approve", "deny"):
        refusal = steer_agent.approval_refusal(store, op, args, "the coordinator")
        if refusal:
            return refusal
        rec = approvals.get_approval(store, args.get("job_id", ""), args.get("approval_id", ""))
        if rec is not None:
            kind = rec.get("kind", "general")
            if op == "approve" and kind != "permission" and not approvals.is_recovery_approval(rec):
                return "the coordinator approves only a recovery approval or a permission"
            if op == "deny" and kind != "permission":
                return "the coordinator denies only a permission"
            if op == "deny":
                from steward import resolver

                try:
                    tier, why = resolver.classify(rec, store.get_job(rec["job_id"]), store=store)
                except Exception as exc:  # noqa: BLE001 - can't classify -> the safe answer
                    return f"cannot classify this approval ({exc}); leaving it for {owner.name()}"
                if tier == "floor":
                    return f"floor action ({why}); only {owner.name()} decides this"
    if op == "answer":
        refusal = _answer_refusal(store, args)
        if refusal:
            return refusal
    if op == "send_back" and (args.get("model") or args.get("goal_append")):
        return "the coordinator's send-back cannot change the model or the goal"
    if op == "send_back":
        refusal = _send_back_refusal(store, args)
        if refusal:
            return refusal
    if op == "answer" and args.get("accept_recommended") and not args.get("qids"):
        return "the coordinator accepts recommended answers only for named questions (qids)"
    if op == "stop":
        from steward.train_housekeeping import MERGE_GOAL_PREFIX

        job = store.get_job(args.get("job_id", ""))
        if job is None or not job.goal.startswith(MERGE_GOAL_PREFIX):
            return "the coordinator stops only merge fix-ups of a done parent"
        ok, why = parent_done(store, job.job_id)
        if not ok:
            return f"the coordinator stops only merge fix-ups of a done parent: {why}"
    return None


def record_action(store: Store, cmd: dict, result: dict) -> None:
    """One `coordinator_action` event per coordinator cmd (R19); refusals show in the feed too."""
    if cmd.get("origin") != ORIGIN:
        return
    from steward.models import Judgment

    job_id = (cmd.get("args") or {}).get("job_id")
    if not job_id or store.get_job(job_id) is None:
        return
    store.append_event(job_id, what="coordinator_action",
                       evidence={"op": cmd.get("op"), "accepted": bool(result.get("accepted")),
                                 "detail": str(result.get("detail", ""))[:200]},
                       rule="coordinator", judgment=Judgment.code)
