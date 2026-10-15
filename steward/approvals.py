"""Runner-owned approvals for NEEDS_HUMAN / permission-prompt waits (API-04, SP-19).

Approvals are the human-judgment control surface a phone (Moshi push
notification, or a ChatGPT `approve_action` tool call) actually acts on:
`open_approval` is called whenever the runner reaches a state a human needs
to weigh in on (a NEEDS_HUMAN transition, or a permission-prompt wait), and
`decide` is the only path that resolves one — `Judgment.human`, except that
`decided_by="resolver:<model>"` (a risk-routed resolver, `steward.resolver`,
non-floor permission holds only) records `model_informed_code_authorized`.
Acceptance and the outbound FLOOR stay human (SP-19).

Approvals persist one row each in the `approvals` table (TUI speed plan
10a; see "storage" below); they used to be one JSON list per job under
kv['approvals'], which is still read for jobs not migrated yet.
`decide` follows the same dedupe-by-request-id contract every other control
request in this codebase uses (SP-20): a request id already seen returns the
stored receipt again, unmodified.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace

from steward import closeout
from steward.machine import IllegalTransition
from steward.models import JobState, Judgment, Receipt
from steward.store import Store, now_iso

_APPROVALS_KEY = "approvals"

# `steward.closeout._decide_acceptance` opens (via the runner, wired in a
# later task) a NEEDS_HUMAN question with exactly this text when checks
# passed but acceptance is unresolved. Approving that specific approval is
# equivalent to a human running `steward accept` (SP-17): it is the existing
# human-only acceptance path, not a new one.
ACCEPTANCE_QUESTION = "checks passed; acceptance unresolved"


def open_approval(
    store: Store,
    job_id: str,
    question: str,
    *,
    approval_id: str | None = None,
    kind: str = "general",
    worker_request_id: str | None = None,
    record_event: bool = True,
) -> dict:
    """Record a new pending approval. Never resolves anything by itself.

    `kind` distinguishes what a decision means downstream: `"acceptance"` for
    the `steward.closeout` question (see `ACCEPTANCE_QUESTION`), `"permission"`
    for a worker permission-prompt relay (`steward.runner`'s
    `_handle_permission_prompt`/`_relay_permission_decisions`), and the
    default `"general"` for every other NEEDS_HUMAN escalation, unchanged
    from before `kind` existed.

    `record_event=False` defers the `approval opened` event: the runner records it (`record_opened`) only when
    the request is not one code allows by itself (TUI speed plan 10b: an auto-allowed call records ONE event).
    """
    approval_id = approval_id or str(uuid.uuid4())
    approvals = _load(store, job_id)
    record = {
        "id": approval_id,
        "job_id": job_id,
        "question": question,
        "status": "pending",
        "kind": kind,
        "created_at": now_iso(),
        "decided_at": None,
        "decided_by": None,
        "relayed_at": None,
    }
    if worker_request_id:
        # which worker permission request this answers (parallel tool calls ask several at once)
        record["worker_request_id"] = worker_request_id
    approvals.append(record)
    _save(store, job_id, approvals)
    if record_event:
        record_opened(store, job_id, record)
    return record


def record_opened(store: Store, job_id: str, record: dict) -> None:
    """The `approval opened` event (deferred by `open_approval(record_event=False)`)."""
    store.append_event(
        job_id,
        what="approval opened",
        evidence={"approval_id": record["id"], "question": record["question"]},
        rule="approval_opened",
        judgment=Judgment.code,
    )


# TUI speed plan 10b (owner-approved 2026-10-05): a permission that code allowed by itself (policy default allow, in-contract
# command, owner grant) records ONE event instead of six (approval opened, worker permission requested, approval
# approved, the auto-allow event, relayed, delivered). The approval record carries that event under SINGLE_EVENT
# until the answer's delivery result is known: written once when the answer is sent to the worker (`relayed`, with
# `ack_expected` for the fabric, whose later ack then adds no event) or when it goes stale unsent (`stale`).
# Anything a human, resolver or lead decides keeps the full chain.
SINGLE_EVENT = "single_event"
SINGLE_WRITTEN = "single_event_written"
AUTO_ALLOWED_WHATS = ("permission_auto_allowed", "in-contract permission auto-approved",
                      "owner-granted permission auto-approved")


def is_single_permission_event(event) -> bool:
    """A one-event record of a permission code allowed (it stands for the whole chain, incl. the request)."""
    return event.what in AUTO_ALLOWED_WHATS and "delivery" in (event.evidence or {})


def _emit_single(store: Store, job_id: str, record: dict, delivery: str, **extra) -> bool:
    pending = record.pop(SINGLE_EVENT, None)
    if not pending:
        return False
    record[SINGLE_WRITTEN] = True
    store.append_event(job_id, what=pending["what"], evidence={**pending["evidence"], "delivery": delivery, **extra},
                       rule=pending["rule"], judgment=Judgment.code)
    return True


def list_approvals(store: Store, job_id: str, *, status: str | None = None) -> list[dict]:
    approvals = _load(store, job_id, status=status)
    if status is None:
        return approvals
    return [a for a in approvals if a["status"] == status]


def get_approval(store: Store, job_id: str, approval_id: str) -> dict | None:
    return next((a for a in _load(store, job_id) if a["id"] == approval_id), None)


def decide(
    store: Store,
    job_id: str,
    approval_id: str,
    decision: str,
    *,
    request_id: str | None = None,
    decided_by: str = "human",
    single_event: dict | None = None,
) -> Receipt:
    """Resolve a pending approval. `decision` is `"approve"` or `"deny"`.

    Approving the specific approval `steward.closeout` opens for "checks
    passed; acceptance unresolved" additionally drives the job through the
    existing human-only `closeout.accept` path (SP-17) — it is the same
    acceptance decision a human typing `steward accept JOB` would make, not a
    second way to reach `ACCEPTED`.

    `single_event` (`{"what", "rule", "evidence"}`, code auto-allows only): no `approval approved` event; that one
    event is written when the answer's delivery result is known (see SINGLE_EVENT).
    """
    if decision not in ("approve", "deny"):
        raise ValueError(f"unknown approval decision: {decision}")

    request_id = request_id or str(uuid.uuid4())
    existing = store.get_receipt(request_id)
    if existing is not None:
        return replace(existing, duplicate=True)

    approvals = _load(store, job_id)
    record = next((a for a in approvals if a["id"] == approval_id), None)
    ts = now_iso()

    if record is None:
        receipt = Receipt(
            request_id=request_id,
            job_id=job_id,
            kind=decision,
            accepted=False,
            detail=f"unknown approval: {approval_id}",
            ts=ts,
        )
        store.put_receipt(receipt)
        return receipt

    if record["status"] != "pending":
        receipt = Receipt(
            request_id=request_id,
            job_id=job_id,
            kind=decision,
            accepted=False,
            detail=f"approval {approval_id} already {record['status']}",
            ts=ts,
        )
        store.put_receipt(receipt)
        return receipt

    if decision == "approve" and record["question"] == ACCEPTANCE_QUESTION:
        from steward import actor as actor_module, outcome_scope
        from steward.contract import load_contract

        contract = load_contract(store, job_id)
        missing = outcome_scope.acceptance_reasons(
            store, job_id, contract, commit_sha=outcome_scope.last_verified_commit(store, job_id))
        who = (actor_module.ActorRef(actor_module.Actor.OWNER, "human") if decided_by == "human"
               else actor_module.classify(None, decided_by))
        if missing and who.kind != actor_module.Actor.OWNER:
            detail = "required outcomes remain unproven; only the owner may explicitly override them"
            store.append_event(job_id, what="acceptance scope override refused",
                               evidence={"approval_id": approval_id, "actor": who.label(), "outcomes": missing},
                               rule="owner_scope_override_required", judgment=Judgment.code)
            receipt = Receipt(request_id=request_id, job_id=job_id, kind=decision, accepted=False,
                              detail=detail, ts=ts)
            store.put_receipt(receipt)
            return receipt

    record["status"] = "approved" if decision == "approve" else "denied"
    record["decided_at"] = ts
    record["decided_by"] = decided_by
    if single_event:
        record[SINGLE_EVENT] = single_event
    _save(store, job_id, approvals)
    if not single_event:
        store.append_event(
            job_id,
            what=f"approval {record['status']}",
            evidence={"approval_id": approval_id, "question": record["question"]},
            rule="operator_approval_decision",
            judgment=_judgment_for(decided_by),
        )

    detail = f"approval {approval_id} {record['status']}"
    if decision == "approve" and record["question"] == ACCEPTANCE_QUESTION:
        try:
            from steward import questions_delivery  # lazy: avoids an import cycle
            questions_delivery.flush_now(store, job_id)
            detail += ("; job accepted" if closeout.accept(store, job_id, decided_by=decided_by) == JobState.ACCEPTED
                       else "; closed as no change (nothing to merge)")
        except IllegalTransition as exc:
            detail += f"; accept not applied: {exc}"
    elif decision == "approve" and is_recovery_approval(record):
        detail += _resume_after_recovery_approval(store, job_id, approval_id, decided_by)
    elif decision == "deny" and record["question"] == ACCEPTANCE_QUESTION:
        # Rejecting acceptance closes the job; left at NEEDS_HUMAN it had no way out.
        job = store.get_job(job_id)
        if job is not None and job.state == JobState.NEEDS_HUMAN:
            store.set_state(
                job_id,
                JobState.STOPPED,
                what="acceptance rejected",
                evidence={"approval_id": approval_id},
                rule="operator_approval_decision",
                judgment=Judgment.human,
            )
            detail += "; job stopped"

    receipt = Receipt(
        request_id=request_id,
        job_id=job_id,
        kind=decision,
        accepted=True,
        detail=detail,
        ts=ts,
    )
    store.put_receipt(receipt)
    return receipt


RECOVERY_RULE = "recovery_budget_exhausted"
RESUME_RELAUNCH_KEY = "recovery_resume_relaunch"


def is_recovery_approval(record: dict) -> bool:
    """The approval a job gets when bounded recovery ran out (`recovery budget exhausted ...`)."""
    return record.get("recovery_rule") == RECOVERY_RULE or str(record.get("question", "")).startswith(
        "recovery budget exhausted")


def _resume_after_recovery_approval(store: Store, job_id: str, approval_id: str, decided_by: str) -> str:
    """Approving "recovery budget exhausted" means "keep going": the job returns to RUNNING with a fresh recovery
    budget, and its runner is relaunched so steers queued while it waited are delivered (seen live on d22d6633,
    2026-10-02: the approval left it in NEEDS_HUMAN and only a send-back got it out). A worker attempt that is still
    live is reattached; with none left, the job gets a fresh attempt in the same worktree (its brief carries every
    steering revision). Denying keeps today's behaviour (the job stays in NEEDS_HUMAN)."""
    job = store.get_job(job_id)
    if job is None or job.state != JobState.NEEDS_HUMAN:
        return ""
    store.kv_set(job_id, "recoveries_used", 0)
    store.kv_set(job_id, "stall_episode_diagnosed", False)
    live = any(a.get("status") in ("running", "stopping") and not a.get("confirmed_stopped")
               for a in store.kv_get(job_id, "attempts", default=[]) or [])
    if not live:
        from steward import sendback  # lazy: avoids an import cycle

        store.kv_set(job_id, sendback.REWORK_KEY, {"text": "The owner approved continuing after the recovery budget "
                                                           "ran out; carry on with the task.", "rev": None})
    store.set_state(job_id, JobState.RUNNING, what="recovery approved; resuming with a fresh recovery budget",
                    evidence={"approval_id": approval_id, "reattach": live}, rule="recovery_approval_resume",
                    judgment=_judgment_for(decided_by))
    store.kv_set(job_id, RESUME_RELAUNCH_KEY, True)
    return "; job resumed (RUNNING, fresh recovery budget)"


def _judgment_for(decided_by: str) -> Judgment:
    """Who decided -> the event's judgment. `resolver:<model>` (steward.resolver, owner decision 2026-09-29:
    risk-routed resolvers for non-floor holds) is a model opinion that code authorized."""
    if decided_by == "code":
        return Judgment.code
    if decided_by.startswith(("resolver:", "tech_lead:")):  # v2.6: the tech lead's decision, authorized by code
        return Judgment.model_informed_code_authorized
    return Judgment.human


def annotate(store: Store, job_id: str, approval_id: str, **fields) -> dict | None:
    """Attach extra fields (e.g. `resolver`, `resolver_pending`) to an approval record. Decides nothing."""
    approvals = _load(store, job_id)
    record = next((a for a in approvals if a["id"] == approval_id), None)
    if record is None:
        return None
    record.update(fields)
    _save(store, job_id, approvals)
    return record


def close_for_send_back(store: Store, job_id: str, reason: str, *, decided_by: str = "human") -> list[str]:
    """Close every pending acceptance/general approval as denied WITHOUT the "rejected -> STOPPED"
    side effect `decide` has: used by send-back, where the job goes back to work. Returns
    the closed approval ids."""
    approvals = _load(store, job_id)
    closed = []
    ts = now_iso()
    for record in approvals:
        if record["status"] == "pending" and (
            record.get("kind", "general") in ("acceptance", "general") or record["question"] == ACCEPTANCE_QUESTION
        ):
            record.update(status="denied", decided_at=ts, decided_by=decided_by, closed_reason=reason)
            closed.append(record["id"])
    if closed:
        _save(store, job_id, approvals)
        store.append_event(
            job_id,
            what="approval denied",
            evidence={"approval_ids": closed, "reason": reason},
            rule="operator_approval_decision",
            judgment=Judgment.human,
        )
    return closed


def withdraw(store: Store, job_id: str, approval_id: str, *, by: str, reason: str) -> dict | None:
    """Withdraw a pending request without making a decision for its owner."""
    approvals = _load(store, job_id)
    record = next((a for a in approvals if a["id"] == approval_id), None)
    if record is None or record["status"] != "pending":
        return None
    record.update(status="withdrawn", decided_at=now_iso(), decided_by=by, closed_reason=reason[:200])
    _save(store, job_id, approvals)
    store.append_event(job_id, what="approval withdrawn", evidence={"approval_id": approval_id, "by": by,
                                                                     "reason": reason[:200]},
                       rule="approval_withdrawn", judgment=Judgment.code)
    return record

def close_tech_lead_superseded(store: Store, job_id: str, event: str, *, blocker_ids=None) -> list[str]:
    """Close every pending tech_lead escalation as superseded by `event` (send-back, a new worker attempt,
    accept/release/stop): the issue it flagged has moved on. One event lists the closed ids.
    `blocker_ids` limits the close to approvals linked to those blockers (cause cleared)."""
    approvals = _load(store, job_id)
    closed = []
    ts = now_iso()
    for record in approvals:
        if record["status"] == "pending" and record.get("kind") == "tech_lead" and (
                blocker_ids is None or record.get("blocker_id") in blocker_ids):
            record.update(status="denied", decided_at=ts, decided_by="code", closed_reason=f"superseded: {event}")
            closed.append(record["id"])
    if closed:
        _save(store, job_id, approvals)
        store.append_event(
            job_id,
            what="tech_lead approvals superseded",
            evidence={"approval_ids": closed, "reason": f"superseded: {event}"},
            rule="tech_lead_superseded",
            judgment=Judgment.code,
        )
    return closed


def mark_relayed(store: Store, job_id: str, approval_id: str, *, ack_expected: bool = False,
                 relayed_by: str | None = None) -> dict | None:
    """Record that a decided `permission` approval's answer was actually sent
    to the worker (`WorkerAdapter.answer_prompt` returned `ok=True`).

    `ack_expected` (fabric workers): the answer stays persisted as "in flight" until the shim acks delivery
    (`mark_delivery_acked`); `relayed_by` names the runner instance that sent it, so a restarted runner re-sends
    every answer a previous instance sent but never saw acked (lost-approval hang, job 8a641f9b retro).

    Called only by `steward.runner` — the runner is the only component that
    touches worker input (SP-2); this function never does so itself.
    """
    approvals = _load(store, job_id)
    record = next((a for a in approvals if a["id"] == approval_id), None)
    if record is None:
        return None
    record["relayed_at"] = now_iso()
    if ack_expected:
        record["ack_expected"] = True
    if relayed_by:
        record["relayed_by"] = relayed_by
    if record.get(SINGLE_EVENT):
        _emit_single(store, job_id, record, "relayed", ack_expected=bool(ack_expected))
        _save(store, job_id, approvals)
        return record
    _save(store, job_id, approvals)
    store.append_event(
        job_id,
        what="permission decision relayed",
        evidence={"approval_id": approval_id, "decision": record["status"]},
        rule="permission_decision_relayed",
        judgment=Judgment.human,
    )
    return record


def mark_stale(store: Store, job_id: str, approval_id: str, *, reason: str) -> dict | None:
    """Mark an approval `stale`: no longer actionable, whether because the
    worker was no longer blocked by the time the runner tried to relay a
    decision, or because a human answered directly in the pane before a
    decision was made (break-glass — the runner records its own
    `break_glass_input` event for that case; see `steward.runner`).

    Never sends worker input itself (SP-2 belongs to `steward.runner`).
    """
    approvals = _load(store, job_id)
    record = next((a for a in approvals if a["id"] == approval_id), None)
    if record is None:
        return None
    if record["status"] in ("approved", "denied"):
        record["stale_from"] = record["status"]  # the answer is kept: a still-pending request gets it back
    record["status"] = "stale"
    record["decided_at"] = record.get("decided_at") or now_iso()
    _emit_single(store, job_id, record, "stale")  # an auto-allow that never landed still records what code allowed
    _save(store, job_id, approvals)
    store.append_event(
        job_id,
        what="approval stale",
        evidence={"approval_id": approval_id, "reason": reason},
        rule="approval_stale",
        judgment=Judgment.code,
    )
    return record


def clear_relayed(store: Store, job_id: str, approval_id: str, *, rule: str = "jev_hung_call_rerelay") -> dict | None:
    """Forget that a decided permission answer was relayed so the runner sends it again
    (Jev's hung_call fix path: the worker is still blocked after we answered)."""
    approvals = _load(store, job_id)
    record = next((a for a in approvals if a["id"] == approval_id), None)
    if record is None:
        return None
    record["relayed_at"] = None
    _save(store, job_id, approvals)
    store.append_event(job_id, what="permission relay retried", evidence={"approval_id": approval_id},
                       rule=rule, judgment=Judgment.code)
    return record


def mark_delivery_acked(store: Store, job_id: str, approval_id: str, *, duplicate: bool = False) -> dict | None:
    """The shim confirmed it wrote this answer to the worker: the answer is no longer in flight."""
    approvals = _load(store, job_id)
    record = next((a for a in approvals if a["id"] == approval_id), None)
    if record is None or record.get("delivery_acked_at"):
        return record
    record["delivery_acked_at"] = now_iso()
    _save(store, job_id, approvals)
    if record.get(SINGLE_WRITTEN):
        return record  # an auto-allow's one event already says it was sent
    store.append_event(job_id, what="permission answer delivered",
                       evidence={"approval_id": approval_id, "request_id": record.get("worker_request_id"),
                                 "decision": record["status"], "duplicate": duplicate},
                       rule="permission_delivery_acked", judgment=Judgment.code)
    return record


def by_request_id(store: Store, job_id: str, request_id: str) -> dict | None:
    """The newest permission approval that answers worker request `request_id`."""
    return next((a for a in reversed(_load(store, job_id))
                 if a.get("kind") == "permission" and a.get("worker_request_id") == request_id), None)


def answer_in_flight(record: dict) -> bool:
    """A decided permission answer the worker may not have received yet: not yet relayed, or relayed over the
    fabric with no delivery ack from the shim."""
    if record.get("kind") != "permission" or record.get("status") not in ("approved", "denied"):
        return False
    if record.get("delivery_acked_at"):
        return False
    return not record.get("relayed_at") or bool(record.get("ack_expected"))


def reopen(store: Store, job_id: str, approval_id: str, *, rule: str = "jev_hung_call_reopen") -> dict | None:
    """A stale permission approval whose request the worker still has pending: give it back its answer when
    it had one (to be re-relayed), else turn it back into a pending question. Never invents a decision."""
    approvals = _load(store, job_id)
    record = next((a for a in approvals if a["id"] == approval_id), None)
    if record is None or record["status"] != "stale":
        return None
    prior = record.pop("stale_from", None)
    if prior in ("approved", "denied"):
        record.update(status=prior, relayed_at=None)
    else:
        record.update(status="pending", decided_at=None, decided_by=None, relayed_at=None)
    record.pop("delivery_acked_at", None)
    _save(store, job_id, approvals)
    store.append_event(job_id, what="approval reopened", evidence={"approval_id": approval_id,
                       "status": record["status"]}, rule=rule, judgment=Judgment.code)
    return record


# -- storage (TUI speed plan 10a) ----------------------------------------------------------------------------------
#
# One row per approval in the `approvals` table, updated in place: a decision rewrites one row, not the job's whole
# history. Jobs from before carry a JSON list in kv['approvals']; until that list is migrated its records are still
# read from it and written back to it (compatibility for a runner of the previous version still driving the job,
# which reads only the kv list). A job's list is migrated (atomically, `Store.migrate_approvals`) once no previous
# runner can own it: when the job is finished (on the first read, or `migrate_finished` in bulk) or when a runner
# of this version takes it (`migrate`). Callers keep the list-of-dicts shape: `_load` returns the job's records in
# creation order, `_save` writes back only the records that changed (and inserts new ones into the table).

_FINISHED = (JobState.ACCEPTED, JobState.RELEASED, JobState.STOPPED, JobState.FAILED)


class _Records(list):
    """The loaded records, remembering where each came from: ("t", rowid, json) or ("kv", None, json)."""

    def __init__(self, items, origin):
        super().__init__(items)
        self.origin = origin  # id(record) -> (source, rowid, json as loaded)


def migrate(store: Store, job_id: str) -> int:
    """Move the job's legacy kv list into the table (a runner of this version owns the job). Returns records moved."""
    try:
        return store.migrate_approvals(job_id)
    except Exception:  # noqa: BLE001 - a busy database leaves the list where it is; reads still see it
        return 0


def migrate_finished(store: Store) -> int:
    """Migrate every finished job's legacy kv list (one-time bulk move; reads would also do it lazily)."""
    rows = store._conn.execute(
        "SELECT kv.job_id FROM kv JOIN jobs ON jobs.job_id = kv.job_id WHERE kv.key = ? AND jobs.state IN (?, ?, ?, ?)",
        (_APPROVALS_KEY, *(s.value for s in _FINISHED))).fetchall()
    return sum(migrate(store, row[0]) for row in rows)


def _load(store: Store, job_id: str, *, status: str | None = None) -> list[dict]:
    """The job's records in creation order (`status`: only the table rows with that status are decoded; the result
    is then for reading, not for `_save`)."""
    legacy = store.kv_get(job_id, _APPROVALS_KEY)
    if legacy is not None:
        job = store.get_job(job_id)
        if job is None or job.state in _FINISHED:
            migrate(store, job_id)
            legacy = store.kv_get(job_id, _APPROVALS_KEY)
    items, origin = [], {}
    for rowid, raw in store.approval_rows(job_id, status):
        record = json.loads(raw)
        items.append(record)
        origin[id(record)] = ("t", rowid, raw)
    if legacy:
        have = {r["id"] for r in items} if status is None else {r[0] for r in store._conn.execute(
            "SELECT approval_id FROM approvals WHERE job_id = ?", (job_id,))}
        for record in legacy:
            if record.get("id") not in have:
                items.append(record)
                origin[id(record)] = ("kv", None, json.dumps(record))
    items.sort(key=lambda r: r.get("created_at") or "")  # stable: creation order, as the old list was
    return _Records(items, origin)


def _save(store: Store, job_id: str, approvals: list[dict]) -> None:
    origin = getattr(approvals, "origin", None)
    if origin is None:
        store.approval_replace(job_id, list(approvals))
        return
    inserts, updates, kv_records, kv_changed = [], [], [], False
    for record in approvals:
        src = origin.get(id(record))
        if src is None:
            inserts.append(record)
            continue
        kind, rowid, raw = src
        changed = json.dumps(record) != raw
        if kind == "kv":
            kv_records.append(record)
            kv_changed = kv_changed or changed
        elif changed:
            updates.append((rowid, record))
    rowids = store.approval_write(job_id, inserts, updates, kv_records if kv_changed else None)
    for record, rowid in zip(inserts, rowids):  # a later _save of the same list diffs against what is stored now
        origin[id(record)] = ("t", rowid, json.dumps(record))
    for _rowid, record in updates:
        src = origin[id(record)]
        origin[id(record)] = (src[0], src[1], json.dumps(record))
    if kv_changed:
        for record in kv_records:
            origin[id(record)] = ("kv", None, json.dumps(record))
