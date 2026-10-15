"""Deterministic Flow controller: investigator proposal -> existing guarded mechanism (v2.3 FLOW-07/08/11).

Steward, not Jev and not the investigator, decides. `validate` re-checks everything a proposal rests on (current
and episode mode, job state, evidence refs, episode confidence, job revision, the finite allowlist) and returns a
`Decision`. Only `MECHANISMS` can ever map, each onto something Steward already guards:

  nudge / focus   -> `Steering.record(origin="flow")`: nonhuman, no urgency, expected-revision, request-id
                     idempotency and a pending-ack fence over every unacknowledged instruction (a human's included)
  watcher_review  -> `watcher_authority.flow_watcher_review`: quarantine and trust-tier gates, then a normal
                     watcher run whose own verdict passes the watcher gate
  recovery        -> `recovery.flow_recovery_check`: only reports whether the runner's own predicate and budget
                     already qualify recovery; Flow never spends a recovery or replaces a worker itself
  escalate_human  -> recorded for human review; nothing is decided for the human

Reserved human decisions (stop, permission, acceptance, merge, release, outbound contact, ...) can never map.
Unknown or unsafe input rejects or defers with a recorded reason; missing infrastructure is a no-op, never a block.
"""

from __future__ import annotations

from dataclasses import dataclass

from steward import recovery, watcher_authority
from steward.diagnosis import redact
from steward.models import JobState
from steward.steering import Steering
from steward.store import Store

MECHANISMS = {"no_action": None, "nudge": "steering", "focus": "steering", "watcher_review": "watcher_trust",
              "recovery": "recovery_predicate", "escalate_human": "human_review"}
RESERVED = frozenset({
    "stop", "cancel", "pause", "resume", "interrupt", "permission", "approve", "deny", "accept", "acceptance",
    "reject", "incorporate", "merge", "push", "publish", "release", "deploy", "contact", "email", "message",
    "notify_external", "grant", "budget", "promote", "trust", "goal", "avoid", "authority", "delete", "reset",
})
FRICTION_LABELS = ("wrong_direction", "self_correction", "supervisor_correction", "retry_or_rebuild",
                   "blocked_handoff", "supervision_churn")
ACTION_MIN_CONFIDENCE = 0.8  # stricter than the 0.7 recording bar: weaker episodes are evidence only
COOLDOWN_S = 1800.0  # at most one applied Flow action per job per 30 minutes
INSTRUCTION_MAX = 400


@dataclass(frozen=True)
class Decision:
    kind: str  # "map" | "reject" | "defer" | "no_action"
    action: str
    reason: str | None = None
    mechanism: str | None = None


def _last_applied_epoch(store: Store, job_id: str) -> float | None:
    rows = store.flow_decisions(job_id)
    applied = [r["created_epoch"] for r in rows if r.get("decision") == "applied"]
    return max(applied) if applied else None


def validate(store: Store, episode: dict, investigation: dict | None, *, mode: str, now: float) -> Decision:
    """The controller's verdict on one investigation result. Pure reads; never mutates."""
    result = (investigation or {}).get("result") or {}
    action = str(result.get("recommended_action") or "")
    if not investigation or investigation.get("state") != "accepted" or not result:
        return Decision("reject", action, "investigation_not_accepted")
    if action in RESERVED:
        return Decision("reject", action, "reserved_human_decision")
    if action not in MECHANISMS:
        return Decision("reject", action, "unknown_action")
    if mode != "active":
        return Decision("defer", action, "mode_not_active")
    if episode.get("mode") != "active":
        return Decision("defer", action, "episode_not_active")
    known = set(episode.get("source_refs") or [])
    refs = result.get("evidence_refs") or []
    if not refs or any(r not in known for r in refs):
        return Decision("reject", action, "evidence_not_in_episode")
    if float(episode.get("confidence") or 0) < ACTION_MIN_CONFIDENCE:
        return Decision("defer", action, "weak_evidence")
    if action == "no_action":
        return Decision("no_action", action, "investigator_recommended_no_action")
    job = store.get_job(episode["job_id"])
    if job is None or job.state != JobState.RUNNING:
        return Decision("defer", action, "job_not_running")
    if episode.get("job_revision") is not None and store.get_revision(episode["job_id"]) != episode["job_revision"]:
        return Decision("defer", action, "stale_revision")
    last = _last_applied_epoch(store, episode["job_id"])
    if last is not None and now - last < COOLDOWN_S:
        return Decision("defer", action, "cooldown")
    if MECHANISMS[action] == "steering" and not str(result.get("instruction") or "").strip():
        return Decision("reject", action, "missing_instruction")
    return Decision("map", action, None, MECHANISMS[action])


# -- dispatch onto existing mechanisms -----------------------------------------------------------------------------


def _steer(store: Store, episode: dict, action: str, instruction: str) -> tuple[str, str | None, dict]:
    text = "[flow] " + redact(" ".join(instruction.split()))[:INSTRUCTION_MAX]
    request_id = f"flow-{episode['episode_id']}"
    r = Steering(store).record(episode["job_id"], action, text, request_id, origin="flow",
                               expected_revision=episode.get("job_revision"))
    receipt = {"request_id": r.request_id, "accepted": r.accepted, "detail": r.detail[:200], "verb": action}
    if r.accepted:
        return "applied", None, receipt
    reason = ("stale_revision" if "CONFLICT" in r.detail else
              "pending_unacked" if "pending_unacked" in r.detail else "steering_refused")
    return "deferred", reason, receipt


def _dispatch(store: Store, episode: dict, decision: Decision, result: dict) -> tuple[str, str | None, dict | None]:
    job_id = episode["job_id"]
    if decision.mechanism == "steering":
        return _steer(store, episode, decision.action, str(result.get("instruction") or ""))
    if decision.mechanism == "watcher_trust":
        got = watcher_authority.flow_watcher_review(store, job_id)
        return ("applied", None, got) if got.get("ok") else ("deferred", got.get("reason"), got)
    if decision.mechanism == "recovery_predicate":
        got = recovery.flow_recovery_check(store, job_id)
        return "deferred", got["reason"], got
    if decision.mechanism == "human_review":
        return "escalated", "human_review_requested", {"root_cause": str(result.get("root_cause") or "")[:300]}
    return "rejected", "unknown_mechanism", None


def process(store: Store, episode: dict, investigation: dict | None, *, mode: str, now: float) -> dict | None:
    """Decide (once per episode) what an accepted investigation may do, and do it only through its existing
    mechanism. Returns the final decision record, or None when there is no accepted proposal or the episode was
    already decided (duplicate trigger, restart). Never raises out of the dispatch."""
    if not investigation or investigation.get("state") != "accepted" or not investigation.get("result"):
        return None
    result = investigation["result"]
    proposal = {k: result.get(k) for k in ("root_cause", "evidence_refs", "recommended_action", "instruction")}
    if not store.claim_flow_decision(episode["episode_id"], episode["job_id"], proposal=proposal, now=now):
        return None
    d = validate(store, episode, investigation, mode=mode, now=now)
    outcome, reason, receipt = {"reject": ("rejected", d.reason, None), "defer": ("deferred", d.reason, None),
                                "no_action": ("no_action", d.reason, None)}.get(d.kind, (None, None, None))
    if d.kind == "map":
        try:
            outcome, reason, receipt = _dispatch(store, episode, d, result)
        except Exception as exc:  # noqa: BLE001 - a mechanism outage defers; ordinary work is never blocked
            outcome, reason, receipt = "deferred", f"error:{type(exc).__name__}", None
    finished = store.finish_flow_decision(episode["episode_id"], outcome, reason=reason, mechanism=d.mechanism,
                                          receipt=receipt)
    if outcome in ("escalated", "deferred"):
        try:
            from steward import ownership

            ownership.raise_flow_finding(store, episode, result, outcome=outcome, reason=reason, now=now)
        except Exception:  # noqa: BLE001 - ownership is additive to Flow's existing durable decision
            pass
    return finished


# -- human adjudication, outcomes and read-only evaluation (FLOW-09/10) ------------------------------------------

REVIEW_LABELS = ("true_positive", "false_positive", "unclear")
ACTION_VERDICTS = ("helpful", "neutral", "harmful")
OUTCOME_WINDOW_S = 1800.0


def _require_human(actor: str) -> None:
    if actor not in watcher_authority.human_actors():
        raise watcher_authority.AuthorityError(f"Flow adjudication is human-only; {actor!r} is not a human actor")


def adjudicate(store: Store, episode_id: str, label: str, *, actor: str, note: str | None = None,
               action_verdict: str | None = None) -> dict:
    """A human's verdict on an episode (was the friction real?) and, optionally, on the action taken. Feeds the
    evaluation only; it never changes thresholds, policy or trust (no automatic promotion)."""
    _require_human(actor)
    if label not in REVIEW_LABELS:
        raise ValueError(f"label must be one of {REVIEW_LABELS}")
    if action_verdict is not None and action_verdict not in ACTION_VERDICTS:
        raise ValueError(f"action_verdict must be one of {ACTION_VERDICTS}")
    episode = store.flow_episode(episode_id)
    if episode is None:
        raise KeyError(f"no Flow episode {episode_id}")
    return store.review_flow_episode(episode_id, episode["job_id"], label=label, actor=actor, note=note,
                                     action_verdict=action_verdict)


def report_miss(store: Store, job_id: str, label: str, *, actor: str, note: str | None = None) -> None:
    """A human saw friction Flow did not record (a miss): one `flow_miss_reported` event, label only."""
    from steward import masking
    from steward.models import Judgment

    _require_human(actor)
    if label not in FRICTION_LABELS:
        raise ValueError(f"label must be a friction label {FRICTION_LABELS}")
    store.append_event(job_id, what="flow_miss_reported",
                       evidence={"label": label, "actor": actor, "note": masking.mask(note or "")[:300] or None},
                       rule="flow_adjudication", judgment=Judgment.human)


def record_outcomes(store: Store, job_id: str, now: float) -> list[dict]:
    """Once `OUTCOME_WINDOW_S` has passed since a decision, record (once) whether the same friction recurred on the
    job inside that window. Code-computed, label-level only."""
    out = []
    episodes = store.flow_episodes(job_id)
    for d in store.flow_decisions(job_id):
        if d["decision"] == "pending" or d["outcome"] is not None or now - d["created_epoch"] < OUTCOME_WINDOW_S:
            continue
        ep = next((e for e in episodes if e["episode_id"] == d["episode_id"]), None)
        if ep is None:
            continue
        recurred = [e["episode_id"] for e in episodes if e["label"] == ep["label"] and e["episode_id"] != ep["episode_id"]
                    and d["created_epoch"] < e["created_epoch"] <= d["created_epoch"] + OUTCOME_WINDOW_S]
        job = store.get_job(job_id)
        rec = store.set_flow_outcome(d["episode_id"], {"outcome": "recurred" if recurred else "not_recurred",
                                                        "recurred_episodes": recurred[:5],
                                                        "window_s": OUTCOME_WINDOW_S,
                                                        "job_state": job.state.value if job else None})
        if rec is not None:
            out.append(rec)
    return out


def _rate(num: int, den: int) -> dict:
    return {"value": (num / den) if den else None, "num": num, "den": den}


def evaluate(store: Store, job_ids: list[str] | None = None) -> dict:
    """Read-only quality report over recorded Flow history: counts and rates, every rate with its numerator and
    denominator. Labels, states and counts only (no snapshots, no turns). Nothing here tunes anything."""
    from collections import Counter

    jobs = job_ids if job_ids is not None else sorted({e["job_id"] for e in store.flow_episodes(None, limit=100000)})
    episodes, investigations, decisions, reviews, misses = [], [], [], [], 0
    for job_id in jobs:
        episodes += store.flow_episodes(job_id, limit=100000)
        investigations += store.flow_investigations(job_id)
        decisions += store.flow_decisions(job_id)
        reviews += store.flow_reviews(job_id)
        misses += sum(1 for e in store.events(job_id) if e.what == "flow_miss_reported")
    labels = Counter(r["label"] for r in reviews)
    tp, fp = labels["true_positive"], labels["false_positive"]
    verdicts = Counter(r["action_verdict"] for r in reviews if r["action_verdict"])
    outcomes = Counter((d["outcome"] or {}).get("outcome") for d in decisions if d["outcome"])
    applied = sum(1 for d in decisions if d["decision"] == "applied")
    return {
        "jobs": len(jobs),
        "episodes": {"n": len(episodes), "by_label": dict(Counter(e["label"] for e in episodes)),
                     "by_mode": dict(Counter(e["mode"] for e in episodes))},
        "investigations": {"n": len(investigations), "by_state": dict(Counter(i["state"] for i in investigations))},
        "decisions": {"n": len(decisions), "by_decision": dict(Counter(d["decision"] for d in decisions)),
                      "by_reason": dict(Counter(d["reason"] for d in decisions if d["reason"]))},
        "reviews": {"n": len(reviews), "by_label": dict(labels), "misses_reported": misses},
        "precision": _rate(tp, tp + fp),
        "recall_vs_reported": _rate(tp, tp + misses),
        "investigation_acceptance": _rate(sum(1 for i in investigations if i["state"] == "accepted"),
                                          len(investigations)),
        "action_helpful": _rate(verdicts["helpful"], sum(verdicts.values())),
        "not_recurred_after_action": _rate(sum(1 for d in decisions if d["decision"] == "applied" and d["outcome"]
                                               and d["outcome"].get("outcome") == "not_recurred"),
                                           sum(1 for d in decisions if d["decision"] == "applied" and d["outcome"])),
        "outcomes": dict(outcomes),
        "applied_actions": applied,
        "note": "read-only; sample counts shown with every rate; no thresholds or policy are tuned from this",
    }
