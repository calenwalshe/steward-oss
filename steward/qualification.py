"""Observational qualification metrics for continuous ownership.

This module reports only quantities recoverable from bounded Store records. It deliberately keeps human-necessity
labels and causal links unknown unless an independent/code outcome label was explicitly recorded.
"""
from __future__ import annotations

from datetime import datetime, timezone

from steward import blockers
from steward.store import Store


def _epoch(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def observe(store: Store, job_ids: list[str] | None = None, *, now: float | None = None,
            expected_human_need: dict[str, bool] | None = None) -> dict:
    """Return observed metrics and, in deterministic fixtures, compare explicit expected-need labels.

    `expected_human_need` is keyed by blocker ID and must come from test/scenario ground truth. Live callers should
    omit it; missing labels remain unknown rather than being inferred from the agent's own decision.
    """
    import json
    import time

    now = time.time() if now is None else float(now)
    jobs = job_ids if job_ids is not None else [j.job_id for j in store.list_jobs()]
    rows = []
    review_waits = []
    delivery_lags = []
    repeated_unchanged_reviews = []
    supervision_usd = review_usd = worker_usd = 0.0
    spend_unknown_calls = 0
    spend_unknown_by_source: dict[str, int] = {}
    owner_requests = 0
    for jid in jobs:
        events = store.events(jid)
        by_blocker: dict[str, dict[str, float]] = {}
        for event in events:
            ev = event.evidence or {}
            blocker_id = ev.get("blocker_id")
            at = _epoch(event.ts)
            if blocker_id and at is not None:
                state = by_blocker.setdefault(str(blocker_id), {})
                if event.what == "independent_review_queued":
                    state["queued"] = at
                elif event.what == "independent_review_started":
                    state["started"] = at
                elif event.what in ("independent_review_completed", "independent_review_finished"):
                    state["finished"] = at
            if event.what == "acceptance scope override refused":
                rows.append({"job_id": jid, "observation": "non_owner_scope_override_refused"})
        for state in by_blocker.values():
            if "queued" in state and "started" in state:
                review_waits.append(max(0.0, state["started"] - state["queued"]))
        for b in blockers.for_job(store, jid, open_only=True):
            created = store._conn.execute("SELECT created_epoch FROM blockers WHERE blocker_id=?", (b.blocker_id,)).fetchone()
            seconds = max(0.0, now - float(created[0])) if created else None
            rows.append({"job_id": jid, "blocker_id": b.blocker_id, "kind": b.kind,
                         "disposition": b.disposition, "blocked_seconds": seconds,
                         "review_state": b.review_state})
        verified = [e for e in events if e.what == "verification recorded" and
                    (e.evidence or {}).get("commit_sha")]
        delivered = store._conn.execute("SELECT milestone,revision,created_epoch FROM delivery_receipts WHERE job_id=?"
                                        " AND milestone IN ('incorporation','activation') ORDER BY seq", (jid,)).fetchall()
        for v in verified:
            vt = _epoch(v.ts)
            sha = (v.evidence or {}).get("commit_sha")
            if vt is not None:
                delivery_lags.extend({"job_id": jid, "commit_sha": sha, "milestone": r["milestone"],
                                      "seconds": max(0.0, float(r["created_epoch"]) - vt)}
                                     for r in delivered if r["revision"] == sha and float(r["created_epoch"]) >= vt)
        starts = {}
        for e in events:
            ev = e.evidence or {}
            if e.what == "independent_review_started" and ev.get("blocker_id") and ev.get("basis_digest"):
                key = (str(ev["blocker_id"]), str(ev["basis_digest"]))
                starts[key] = starts.get(key, 0) + 1
        repeated_unchanged_reviews.extend({"job_id": jid, "blocker_id": blocker_id, "basis_digest": digest,
                                          "count": count}
                                         for (blocker_id, digest), count in starts.items() if count > 1)
    where, args = ("", [])
    if job_ids is not None:
        if not jobs:
            where, args = " WHERE job_id IN ('')", []
        else:
            where, args = f" WHERE job_id IN ({','.join('?' for _ in jobs)})", list(jobs)
    owner_requests = int(store._conn.execute(
        "SELECT COUNT(*) FROM decision_records" + where + " AND stage='effect' AND decision LIKE 'owner_request:%'"
        if where else "SELECT COUNT(*) FROM decision_records WHERE stage='effect' AND decision LIKE 'owner_request:%'",
        args).fetchone()[0])
    decisions = store._conn.execute("SELECT decision_id,blocker_id,intent_key FROM decision_records" + where +
                                    " AND stage='effect' AND decision LIKE 'owner_request:%'" if where else
                                    "SELECT decision_id,blocker_id,intent_key FROM decision_records "
                                    "WHERE stage='effect' AND decision LIKE 'owner_request:%'", args).fetchall()
    requested_blockers = {str(r["blocker_id"]) for r in decisions if r["blocker_id"]}
    expected = {str(k): bool(v) for k, v in (expected_human_need or {}).items()}
    unnecessary = sorted(bid for bid, need in expected.items() if not need and bid in requested_blockers)
    missed = sorted(bid for bid, need in expected.items() if need and bid not in requested_blockers)
    intent_rows = store._conn.execute("SELECT kind,SUM(CASE WHEN attempt>1 THEN attempt-1 ELSE 0 END) retries,"
                                      "COUNT(*) n FROM action_intents" + where + " GROUP BY kind", args).fetchall()
    recovery = store._conn.execute("SELECT outcome,COUNT(*) AS n FROM recovery_attempts" + where + " GROUP BY outcome",
                                   args).fetchall()
    duplicate_outcomes = 0
    outcomes = store._conn.execute("SELECT observed_json FROM decision_outcomes" + where, args).fetchall()
    for outcome in outcomes:
        try:
            duplicate_outcomes += int(bool(json.loads(outcome["observed_json"]).get("duplicate")))
        except (ValueError, TypeError, AttributeError):
            continue
    from steward import spend_policy
    spend_comparison = []
    for jid in jobs:
        rep = spend_policy.report(store, jid, now=now).to_dict()
        worker_usd += rep["worker_usd"]
        supervision_usd += rep["supervision_usd"]
        review_usd += rep["review_usd"]
        spend_unknown_calls += rep["unknown_calls"]
        for source, source_report in rep["by_source"].items():
            count = int(source_report.get("unknown_calls") or 0)
            if count:
                spend_unknown_by_source[source] = spend_unknown_by_source.get(source, 0) + count
        est = rep.get("estimate") or {}
        estimate_usd = float(est["usd"]) if est.get("usd") is not None else None
        spend_comparison.append({"job_id": jid, "worker_usd": rep["worker_usd"],
                                 "supervision_usd": rep["supervision_usd"], "review_usd": rep["review_usd"],
                                 "known_usd": rep["known_usd"], "unknown_calls": rep["unknown_calls"],
                                 "estimate_usd": estimate_usd,
                                 "known_over_estimate_usd": (max(0.0, rep["known_usd"] - estimate_usd)
                                                               if estimate_usd is not None else None),
                                 "basis": est.get("basis")})
    return {
        "schema": "ownership_qualification_observations_v1",
        "jobs_observed": len(jobs),
        "owner_requests_observed": owner_requests,
        "owner_request_necessity": "deterministic_expected_need_labels" if expected else
                                   "unknown_without_independent_outcome_labels",
        "unnecessary_owner_requests": unnecessary if expected else "unknown_without_independent_outcome_labels",
        "missed_genuine_owner_decisions": missed if expected else "unknown_without_independent_outcome_labels",
        "blocked_items": rows,
        "review_wait_seconds": review_waits,
        "repeated_action_attempts": {r["kind"]: int(r["retries"] or 0) for r in intent_rows},
        "repeated_unchanged_reviews": repeated_unchanged_reviews,
        "duplicate_action_outcomes": duplicate_outcomes,
        "recovery_outcomes": {r["outcome"]: int(r["n"]) for r in recovery},
        "verified_to_delivery_seconds": delivery_lags,
        "worker_spend_usd": worker_usd, "supervision_spend_usd": supervision_usd,
        "independent_review_spend_usd": review_usd, "unknown_spend_calls": spend_unknown_calls,
        "unknown_spend_by_source": spend_unknown_by_source,
        "spend_vs_estimate": spend_comparison,
        "causal_attribution": "unknown_unless_linked_by_recorded_decision_and_outcome",
        "success_claim": None,
    }
