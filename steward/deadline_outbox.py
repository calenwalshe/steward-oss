"""Deadline check-ins and the approval-gated outbox (v2.2, DL-09, DL-10, DL-11, DL-17).

Local first. Every adopted target slot — `(job, commitment_rev, milestone_id, target_at_utc)`, including the candidate
and superseded targets — and the due time while still open records exactly one `deadline_checkin` event plus one
unapproved local draft, in one Store transaction (`Store.append_event_with_outbox` / `_Tx.insert_draft`), so a runner
and a control pane racing on the same Store record it once. After downtime each overdue slot is recorded on its own
and one deterministic catch-up draft summarizes them (its own key, its own approval).

Outbound is a separate, per-item human act. `show` presents the exact recipient, channel and final title/body/link,
version and next attempt number; `approve` (relayed, `Judgment.human`, request id) freezes those bytes as one attempt.
Any edit bumps the version and voids an unconsumed approval. `deliver` leases one approved attempt, rechecks version,
content hash and the configured channel, and consumes the approval BEFORE its single notifier call. Failed, raised or
lease-expired attempts are recorded failed/unknown and never re-sent: a retry is a fresh draft attempt that needs a new
exact approval. Mode, channel configuration, prior approvals and catch-up batches never authorize anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from steward import deadline, deadline_evidence, deadline_plan, owner
from steward.models import Judgment
from steward.store import Store

TITLE_CAP = 40
BODY_CAP = 140
CATCHUP_LATE_S = 300.0      # a slot recorded more than 5 min after its target counts as caught up after downtime
LEASE_S = 120.0
DUE = "due"
CHANNELS = ("telegram", "moshi", "null")
_SENDABLE_DRAFT_STATES = ("local", "approved", "failed", "unknown")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hhmm(text: str, tz=None) -> str:
    dt = deadline_plan._utc(text)
    if dt is None:
        return "?"
    try:
        return dt.astimezone(tz or deadline.owner_tz()).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return dt.strftime("%H:%MZ")


def channel_config() -> dict:
    """The owner's configured outbound channel: `[deadline] channel` > $STEWARD_NOTIFIER > null. Only ever the
    owner's own channel; null means drafts stay local (A-DL-4)."""
    from steward import config

    choice = str(config._toml_section("deadline").get("channel") or os.environ.get("STEWARD_NOTIFIER") or "null")
    choice = choice.strip().lower()
    channel = choice if choice in CHANNELS else "null"
    return {"channel": channel, "recipient": f"{owner.name()} (owner's own {channel} channel)"}


def _cap(text: str, cap: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def make_draft(store: Store, job_id: str, *, key: str, kind: str, title: str, body: str, now: datetime,
               detail: dict | None = None) -> dict:
    from steward import notify

    cfg = channel_config()
    link = notify.deep_link(store.kv_get(job_id, "herdr_workspace"), store.kv_get(job_id, "herdr_tab"),
                            store.kv_get(job_id, "herdr_pane"))
    return {"key": key, "kind": kind, "created_at": _iso(now), "recipient": cfg["recipient"], "channel": cfg["channel"],
            "title": _cap(title, TITLE_CAP), "body": _cap(body, BODY_CAP), "link": link, "detail": detail or {}}


def content_hash(rec: dict) -> str:
    blob = json.dumps({k: rec.get(k) for k in ("recipient", "channel", "title", "body", "link", "version")},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


# -- slots: backfill and exactly-once recording ------------------------------------------------------------------

def backfill(store: Store, job_id: str) -> int:
    """Copy every Phase 21 target snapshot (kv `deadline_targets`, including overdue and superseded ones) and each
    uncleared revision's due time into the unique slot table. Idempotent: a second run inserts nothing."""
    rows = deadline_plan.targets(store, job_id)
    doc = deadline.commitment(store, job_id) or {"revisions": []}
    n = 0
    with store.immediate() as tx:
        for r in rows:
            cur = tx.conn.execute(
                "INSERT OR IGNORE INTO deadline_slots (job_id, commitment_rev, milestone_id, target_at_utc,"
                " created_by_plan_rev, source, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, r["commitment_rev"], r["milestone_id"], r["target_at_utc"], r.get("created_by_plan_rev"),
                 r.get("source", "worker"), r.get("title", "")))
            n += cur.rowcount
        for rev in doc["revisions"]:
            if rev.get("cleared") or not rev.get("due_at_utc"):
                continue
            cur = tx.conn.execute(
                "INSERT OR IGNORE INTO deadline_slots (job_id, commitment_rev, milestone_id, target_at_utc,"
                " created_by_plan_rev, source, title) VALUES (?, ?, ?, ?, NULL, 'commitment', 'due time')",
                (job_id, rev["rev"], DUE, rev["due_at_utc"]))
            n += cur.rowcount
    return n


def slots(store: Store, job_id: str) -> list[dict]:
    rows = store._conn.execute("SELECT * FROM deadline_slots WHERE job_id = ? ORDER BY target_at_utc, milestone_id,"
                               " commitment_rev", (job_id,)).fetchall()
    return [dict(r) for r in rows]


def slot_key(row: dict) -> str:
    return deadline_plan.slot_key(row["commitment_rev"], row["milestone_id"], row["target_at_utc"])


def _checkin_text(store: Store, job_id: str, row: dict, ctx: dict) -> tuple[str, str]:
    name = ctx["name"]
    if row["milestone_id"] == DUE:
        title = f"⏱ {name}: due time"
        body = f"Due {_hhmm(row['target_at_utc'])} reached, no candidate yet. {ctx['sign']}. Work continues."
        return title, body
    state = ctx["met"].get(row["milestone_id"])
    when = f"met {_hhmm(state)}" if state else "not met yet"
    title = f"⏱ {name}: {row['milestone_id']} {'met' if state else 'check-in'}"
    moved = f" (moved to {_hhmm(ctx['current'][row['milestone_id']])})" if ctx["current"].get(
        row["milestone_id"]) not in (None, row["target_at_utc"]) else ""
    body = (f"{ctx['sign']}. {row['milestone_id']} target {_hhmm(row['target_at_utc'])}{moved}: {when}. "
            f"Due {_hhmm(ctx['due'])}.")
    return title, body


def _context(store: Store, job_id: str) -> dict:
    from steward import deadline_status, tabnames

    job = store.get_job(job_id)
    rec = deadline.current(store, job_id) or {}
    status = store.kv_get(job_id, deadline_status.K_STATUS) or {}
    facts = store.kv_get(job_id, deadline_status.K_FACTS) or {"met": {}}
    plan = deadline_plan.current_plan(store, job_id) or {}
    return {"name": _cap(tabnames.job_name(store, job, persist=False), 18) + f" ({job_id[:8]})" if job else job_id[:8],
            "sign": status.get("label") or "At risk · estimating", "due": rec.get("due_at_utc"),
            "rev": rec.get("rev"), "met": {k: v.get("met_at") for k, v in (facts.get("met") or {}).items()},
            "current": {m["id"]: m["target_at_utc"] for m in plan.get("milestones") or []},
            "plan_rev": plan.get("plan_rev"), "ready": bool((store.kv_get(job_id, deadline_evidence.K_VERDICT) or {})
                                                            .get("ready"))}


def record_due(store: Store, job_id: str, now: datetime, *, until: datetime | None = None) -> list[str]:
    """Record every slot whose time has come (target <= now): one `deadline_checkin` event and one local draft each,
    in ONE transaction with the slot state change, plus one catch-up draft when two or more were overdue. The due
    slot is retired unfired if the candidate was ready first. Idempotent across connections and restarts."""
    ctx = _context(store, job_id)
    now_s = _iso(now)
    recorded: list[str] = []
    late: list[dict] = []
    with store.immediate() as tx:
        rows = tx.conn.execute("SELECT * FROM deadline_slots WHERE job_id = ? AND state = 'pending' AND "
                               "target_at_utc <= ? ORDER BY target_at_utc, milestone_id, commitment_rev",
                               (job_id, _iso(min(now, until)) if until else now_s)).fetchall()
        for r in (dict(x) for x in rows):
            key = slot_key(r)
            if r["milestone_id"] == DUE and ctx["ready"]:
                tx.conn.execute("UPDATE deadline_slots SET state = 'retired', recorded_at = ? WHERE job_id = ? AND "
                                "commitment_rev = ? AND milestone_id = ? AND target_at_utc = ?",
                                (now_s, job_id, r["commitment_rev"], r["milestone_id"], r["target_at_utc"]))
                continue
            title, body = _checkin_text(store, job_id, r, ctx)
            met_at = ctx["met"].get(r["milestone_id"])
            draft = make_draft(store, job_id, key=f"checkin:{key}", kind="due" if r["milestone_id"] == DUE else
                               "checkin", title=title, body=body, now=now,
                               detail={"slot": key, "milestone": r["milestone_id"], "target_at_utc": r["target_at_utc"]})
            if not tx.insert_draft(job_id, draft):
                continue
            superseded = ctx["current"].get(r["milestone_id"]) not in (None, r["target_at_utc"]) or \
                r["commitment_rev"] != ctx["rev"]
            ev = tx.append_event(job_id, what="deadline_checkin", evidence={
                "slot": key, "milestone": r["milestone_id"], "target_at_utc": r["target_at_utc"],
                "commitment_rev": r["commitment_rev"], "created_by_plan_rev": r["created_by_plan_rev"],
                "met": bool(met_at), "met_at": met_at, "status": ctx["sign"], "superseded": superseded,
                "replacement_target": ctx["current"].get(r["milestone_id"]) if superseded else None,
                "late_by_s": int((now - deadline_plan._utc(r["target_at_utc"])).total_seconds()),
                "draft": draft["key"], "outbound": f"local until {owner.name()} approves this exact item"},
                rule="deadline_target_time_checkin", judgment=Judgment.code)
            tx.conn.execute("UPDATE deadline_outbox SET event_seq = ? WHERE draft_key = ?", (ev.seq, draft["key"]))
            tx.conn.execute("UPDATE deadline_slots SET state = 'recorded', recorded_at = ?, event_seq = ? WHERE "
                            "job_id = ? AND commitment_rev = ? AND milestone_id = ? AND target_at_utc = ?",
                            (now_s, ev.seq, job_id, r["commitment_rev"], r["milestone_id"], r["target_at_utc"]))
            recorded.append(key)
            if (now - deadline_plan._utc(r["target_at_utc"])).total_seconds() > CATCHUP_LATE_S:
                late.append(r)
        if len(late) >= 2:
            keys = sorted(slot_key(r) for r in late)
            digest = hashlib.sha256("|".join(keys).encode()).hexdigest()[:16]
            parts = ", ".join(f"{r['milestone_id']} {'met' if ctx['met'].get(r['milestone_id']) else 'not met'}"
                              for r in late)
            draft = make_draft(store, job_id, key=f"catchup:{digest}", kind="catchup",
                               title=f"⏱ {ctx['name']}: {len(late)} missed check-ins",
                               body=f"While Steward was down: {parts}. {ctx['sign']}.", now=now,
                               detail={"slots": keys})
            if tx.insert_draft(job_id, draft):
                ev = tx.append_event(job_id, what="deadline_catchup", evidence={"slots": keys, "draft": draft["key"]},
                                     rule="deadline_catchup_coalesced", judgment=Judgment.code)
                tx.conn.execute("UPDATE deadline_outbox SET event_seq = ? WHERE draft_key = ?", (ev.seq, draft["key"]))
    return recorded


def skip_shadow(store: Store, job_id: str, now: datetime) -> int:
    """Shadow mode records no scheduled check-ins: due slots are marked `shadow` so turning active later does not
    replay them as a burst."""
    with store.immediate() as tx:
        cur = tx.conn.execute("UPDATE deadline_slots SET state = 'shadow', recorded_at = ? WHERE job_id = ? AND "
                              "state = 'pending' AND target_at_utc <= ?", (_iso(now), job_id, _iso(now)))
        return cur.rowcount


# -- drafts, approval and delivery ---------------------------------------------------------------------------------

def drafts(store: Store, job_id: str) -> list[dict]:
    rows = store._conn.execute("SELECT * FROM deadline_outbox WHERE job_id = ? ORDER BY created_at, draft_key",
                               (job_id,)).fetchall()
    return [_draft_dict(r) for r in rows]


def _draft_dict(row) -> dict:
    d = dict(row)
    d["detail"] = json.loads(d.pop("detail_json") or "{}")
    return d


def get_draft(store: Store, draft_key: str) -> dict | None:
    row = store._conn.execute("SELECT * FROM deadline_outbox WHERE draft_key = ?", (draft_key,)).fetchone()
    return _draft_dict(row) if row else None


def attempts(store: Store, draft_key: str) -> list[dict]:
    rows = store._conn.execute("SELECT * FROM deadline_attempts WHERE draft_key = ? ORDER BY attempt",
                               (draft_key,)).fetchall()
    return [dict(r) for r in rows]


def show(store: Store, draft_key: str) -> dict | None:
    """Exactly what an approval would freeze: recipient, channel, final title/body/link, version, next attempt."""
    d = get_draft(store, draft_key)
    if d is None:
        return None
    tries = attempts(store, draft_key)
    return {"draft_key": draft_key, "job_id": d["job_id"], "kind": d["kind"], "state": d["state"],
            "recipient": d["recipient"], "channel": d["channel"], "title": d["title"], "body": d["body"],
            "link": d["link"], "version": d["version"], "next_attempt": len(tries) + 1,
            "content_hash": content_hash(d), "attempts": [{k: t[k] for k in ("attempt", "version", "state",
                                                                              "approved_by", "result_detail")}
                                                          for t in tries]}


def _human(judgment: Judgment, origin: str) -> None:
    deadline._check_human(judgment, origin)


def approve(store: Store, draft_key: str, *, expect_version: int, attempt: int, request_id: str, origin: str,
            judgment: Judgment, now: datetime) -> dict:
    """Freeze one attempt of one draft version for one notifier call (human only, request-idempotent)."""
    _human(judgment, origin)
    with store.immediate() as tx:
        dup = tx.conn.execute("SELECT * FROM deadline_attempts WHERE approval_request_id = ?", (request_id,)).fetchone()
        if dup is not None:
            return {"accepted": True, "duplicate": True, "attempt": dup["attempt"], "version": dup["version"]}
        row = tx.conn.execute("SELECT * FROM deadline_outbox WHERE draft_key = ?", (draft_key,)).fetchone()
        if row is None:
            raise deadline.DeadlineError(f"unknown notice {draft_key}")
        d = _draft_dict(row)
        if d["channel"] == "null":
            raise deadline.DeadlineError("no owner channel is configured: the notice stays local")
        if d["state"] not in _SENDABLE_DRAFT_STATES:
            raise deadline.DeadlineError(f"notice is {d['state']}; nothing to approve")
        if int(expect_version) != d["version"]:
            raise deadline.StaleRevision(int(expect_version), d["version"])
        n = tx.conn.execute("SELECT COUNT(*) FROM deadline_attempts WHERE draft_key = ?", (draft_key,)).fetchone()[0]
        if int(attempt) != n + 1:
            raise deadline.DeadlineError(f"attempt {attempt} is not the next attempt ({n + 1})")
        if tx.conn.execute("SELECT 1 FROM deadline_attempts WHERE draft_key = ? AND state IN ('approved','claimed')",
                           (draft_key,)).fetchone():
            raise deadline.DeadlineError("an approved attempt is already waiting")
        tx.conn.execute(
            "INSERT INTO deadline_attempts (draft_key, attempt, version, content_hash, recipient, channel, title, body,"
            " link, approved_by, approved_at, approval_request_id, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'approved')",
            (draft_key, int(attempt), d["version"], content_hash(d), d["recipient"], d["channel"], d["title"],
             d["body"], d["link"], origin, _iso(now), request_id))
        tx.conn.execute("UPDATE deadline_outbox SET state = 'approved' WHERE draft_key = ?", (draft_key,))
        tx.append_event(d["job_id"], what="deadline_notice_approved",
                        evidence={"draft": draft_key, "attempt": int(attempt), "version": d["version"],
                                  "recipient": d["recipient"], "channel": d["channel"], "title": d["title"],
                                  "body": d["body"], "link": d["link"], "request_id": request_id, "origin": origin},
                        rule="deadline_exact_item_approval", judgment=Judgment.human)
    return {"accepted": True, "duplicate": False, "attempt": int(attempt), "version": d["version"]}


def deny(store: Store, draft_key: str, *, request_id: str, origin: str, judgment: Judgment) -> dict:
    _human(judgment, origin)
    d = get_draft(store, draft_key)
    if d is None:
        raise deadline.DeadlineError(f"unknown notice {draft_key}")
    with store.immediate() as tx:
        tx.conn.execute("UPDATE deadline_outbox SET state = 'denied' WHERE draft_key = ?", (draft_key,))
        tx.conn.execute("UPDATE deadline_attempts SET state = 'void', result_detail = 'denied' WHERE draft_key = ? AND "
                        "state = 'approved'", (draft_key,))
        tx.append_event(d["job_id"], what="deadline_notice_denied", evidence={"draft": draft_key,
                                                                              "request_id": request_id},
                        rule="deadline_exact_item_approval", judgment=Judgment.human)
    return {"accepted": True, "state": "denied"}


def edit(store: Store, draft_key: str, *, judgment: Judgment, origin: str, request_id: str, **fields) -> dict:
    """Change recipient/channel/title/body/link: the version goes up and any unconsumed approval is void."""
    _human(judgment, origin)
    allowed = {k: v for k, v in fields.items() if k in ("recipient", "channel", "title", "body", "link") and v is not None}
    if "title" in allowed:
        allowed["title"] = _cap(allowed["title"], TITLE_CAP)
    if "body" in allowed:
        allowed["body"] = _cap(allowed["body"], BODY_CAP)
    if "channel" in allowed and allowed["channel"] not in CHANNELS:
        raise deadline.DeadlineError(f"channel must be one of {', '.join(CHANNELS)}")
    return _bump(store, draft_key, allowed, why=f"edited by {origin}", judgment=Judgment.human,
                 request_id=request_id)


def _bump(store: Store, draft_key: str, changes: dict, *, why: str, judgment: Judgment, request_id: str = "") -> dict:
    with store.immediate() as tx:
        row = tx.conn.execute("SELECT * FROM deadline_outbox WHERE draft_key = ?", (draft_key,)).fetchone()
        if row is None:
            raise deadline.DeadlineError(f"unknown notice {draft_key}")
        d = _draft_dict(row)
        if not changes or all(d.get(k) == v for k, v in changes.items()):
            return {"accepted": True, "version": d["version"], "changed": False}
        sets = ", ".join(f"{k} = ?" for k in changes)
        tx.conn.execute(f"UPDATE deadline_outbox SET {sets}, version = version + 1, state = CASE WHEN state = "
                        "'approved' THEN 'local' ELSE state END WHERE draft_key = ?", (*changes.values(), draft_key))
        voided = tx.conn.execute("UPDATE deadline_attempts SET state = 'void', result_detail = ? WHERE draft_key = ? "
                                 "AND state = 'approved'", (f"voided: {why}", draft_key)).rowcount
        tx.append_event(d["job_id"], what="deadline_notice_edited",
                        evidence={"draft": draft_key, "from_version": d["version"], "to_version": d["version"] + 1,
                                  "changed": sorted(changes), "approvals_voided": voided, "why": why,
                                  "request_id": request_id}, rule="deadline_notice_version", judgment=judgment)
    return {"accepted": True, "version": d["version"] + 1, "changed": True, "approvals_voided": voided}


def refresh_channel(store: Store, job_id: str) -> int:
    """A channel/recipient configuration change re-versions every unsent draft (an approval of the old target is
    void). Code judgment: the config changed, not the content the owner approved."""
    cfg = channel_config()
    n = 0
    for d in drafts(store, job_id):
        if d["state"] in _SENDABLE_DRAFT_STATES and (d["channel"], d["recipient"]) != (cfg["channel"], cfg["recipient"]):
            _bump(store, d["draft_key"], {"channel": cfg["channel"], "recipient": cfg["recipient"]},
                  why="owner channel configuration changed", judgment=Judgment.code)
            n += 1
    return n


def expire_leases(store: Store, now_s: float) -> int:
    """A claimed attempt whose lease ran out without a recorded result is `unknown` (it may have reached the owner);
    it is never re-sent."""
    with store.immediate() as tx:
        rows = tx.conn.execute("SELECT * FROM deadline_attempts WHERE state = 'claimed' AND lease_expires < ?",
                               (now_s,)).fetchall()
        for r in rows:
            tx.conn.execute("UPDATE deadline_attempts SET state = 'unknown', result_detail = 'lease expired without a "
                            "result; not re-sent' WHERE draft_key = ? AND attempt = ?", (r["draft_key"], r["attempt"]))
            tx.conn.execute("UPDATE deadline_outbox SET state = 'unknown' WHERE draft_key = ?", (r["draft_key"],))
            job = tx.conn.execute("SELECT job_id FROM deadline_outbox WHERE draft_key = ?", (r["draft_key"],)).fetchone()
            tx.append_event(job["job_id"], what="deadline_notice_unknown",
                            evidence={"draft": r["draft_key"], "attempt": r["attempt"], "reason": "lease expired"},
                            rule="deadline_no_auto_resend", judgment=Judgment.code)
        return len(rows)


def _claim(store: Store, owner_token: str, now_s: float, lease_s: float) -> dict | None:
    """Lease ONE approved attempt and consume its approval in the same transaction, after rechecking that the draft
    still has the approved version and bytes and that the configured channel is still the approved one."""
    cfg = channel_config()
    with store.immediate() as tx:
        rows = tx.conn.execute("SELECT a.*, o.version AS cur_version, o.recipient AS cur_recipient, o.channel AS "
                               "cur_channel, o.title AS cur_title, o.body AS cur_body, o.link AS cur_link, o.job_id "
                               "FROM deadline_attempts a JOIN deadline_outbox o ON o.draft_key = a.draft_key WHERE "
                               "a.state = 'approved' ORDER BY a.approved_at, a.draft_key").fetchall()
        for r in rows:
            current = {"recipient": r["cur_recipient"], "channel": r["cur_channel"], "title": r["cur_title"],
                       "body": r["cur_body"], "link": r["cur_link"], "version": r["cur_version"]}
            ok = (r["cur_version"] == r["version"] and content_hash(current) == r["content_hash"]
                  and r["channel"] == cfg["channel"] and r["recipient"] == cfg["recipient"] and r["channel"] != "null")
            if not ok:
                tx.conn.execute("UPDATE deadline_attempts SET state = 'void', result_detail = 'content, version or "
                                "channel changed after approval' WHERE draft_key = ? AND attempt = ?",
                                (r["draft_key"], r["attempt"]))
                tx.append_event(r["job_id"], what="deadline_notice_void",
                                evidence={"draft": r["draft_key"], "attempt": r["attempt"]},
                                rule="deadline_exact_item_approval", judgment=Judgment.code)
                continue
            tx.conn.execute("UPDATE deadline_attempts SET state = 'claimed', lease_owner = ?, lease_expires = ?, "
                            "consumed_at = ? WHERE draft_key = ? AND attempt = ? AND state = 'approved'",
                            (owner_token, now_s + lease_s, datetime.fromtimestamp(now_s, timezone.utc).isoformat(),
                             r["draft_key"], r["attempt"]))
            return dict(r)
    return None


def deliver(store: Store, notifier, *, now_s: float, lease_s: float = LEASE_S, owner_token: str | None = None,
            limit: int = 5) -> list[dict]:
    """The only path from a deadline draft to a notifier: one invocation per approved, unconsumed attempt."""
    from steward import notify

    owner_token = owner_token or str(uuid.uuid4())
    expire_leases(store, now_s)
    out = []
    for _ in range(limit):
        att = _claim(store, owner_token, now_s, lease_s)
        if att is None:
            break
        try:  # exactly the approved bytes, or nothing: an unbuildable message fails unsent
            event = notify.PhoneEvent(kind="DEADLINE", job_id=att["job_id"], title=att["title"], body=att["body"],
                                      deep_link=att["link"], revision=f"{att['draft_key']}#{att['attempt']}")
        except ValueError as exc:
            event, state, detail = None, "failed", f"not sent: {str(exc)[:160]}"
        if event is not None:
            try:
                ok = notify.send_approved(store, notifier, event)
                state, detail = ("sent", "delivered") if ok else ("failed", "notifier reported failure")
            except Exception as exc:  # noqa: BLE001 - a raised send may have reached the owner: unknown, never retried
                state, detail = "unknown", f"notifier raised: {str(exc)[:160]}"
        with store.immediate() as tx:
            tx.conn.execute("UPDATE deadline_attempts SET state = ?, result_at = ?, result_detail = ? WHERE "
                            "draft_key = ? AND attempt = ? AND lease_owner = ?",
                            (state, datetime.fromtimestamp(now_s, timezone.utc).isoformat(), detail, att["draft_key"],
                             att["attempt"], owner_token))
            tx.conn.execute("UPDATE deadline_outbox SET state = ? WHERE draft_key = ?", (state, att["draft_key"]))
            tx.append_event(att["job_id"], what=f"deadline_notice_{state}",
                            evidence={"draft": att["draft_key"], "attempt": att["attempt"], "channel": att["channel"],
                                      "detail": detail, "retry": "needs a new exact approval" if state != "sent"
                                      else None}, rule="deadline_exact_item_approval", judgment=Judgment.code)
        out.append({"draft_key": att["draft_key"], "attempt": att["attempt"], "state": state})
    return out


def notifier_for_channel(channel: str, runner_exec):
    """The notifier for the SAME channel `_claim` checks approvals against (never a different transport)."""
    from steward import notify

    if channel == "telegram":
        return notify.TelegramNotifier(runner_exec)
    if channel == "moshi":
        return notify.MoshiNotifier()
    return None


def deliver_all(store: Store, runner_exec) -> list[dict]:
    """Control-pane tick hook: deliver approved attempts through the configured owner channel (never a draft). With
    no channel configured nothing is delivered (and nothing could have been approved)."""
    import time

    if not store._conn.execute("SELECT 1 FROM deadline_attempts WHERE state IN ('approved','claimed') LIMIT 1").fetchone():
        return []
    notifier = notifier_for_channel(channel_config()["channel"], runner_exec)
    if notifier is None:
        return []
    return deliver(store, notifier, now_s=time.time())


def outbox_view(store: Store, job_id: str) -> list[dict]:
    return [{k: d[k] for k in ("draft_key", "kind", "state", "version", "title", "body", "channel", "created_at")}
            for d in drafts(store, job_id)]


def sweep(store: Store, job_id: str, now: datetime, *, until: datetime | None = None) -> dict:
    """Phase 22 slot step inside `deadline_status.sweep`: backfill, then record due slots (active mode) or mark them
    shadow (shadow mode). `until` (a terminal job's end) limits it to slots that came due while the job was live.
    Never invokes a notifier."""
    backfill(store, job_id)
    if deadline.mode(store, job_id) != "active":
        return {"shadow": skip_shadow(store, job_id, min(now, until) if until else now)}
    refresh_channel(store, job_id)
    return {"recorded": record_due(store, job_id, now, until=until)}
