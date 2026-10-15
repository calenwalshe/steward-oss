"""The tech lead's per-job notebook (v2.6, PRD D-06, SUP-14) and the memory hooks the job tech lead calls.

Append-only: every attempt is a row (issue signature, evidence refs, plan item, action, strategy, outcome, acting
model); an outcome found later is a NEW row for the same signature, never an update. The signature is code, not the
model's: the normalized (fact kind, failing check / ask / file) of the review's primary fact, hashed.

Rules (PRD D-06): a signature seen before forces a different strategy - a proposal repeating a strategy that already
failed for it is rejected (and the next lead is asked); a third sighting escalates to the owner (tech lead exhausted).
`Memory` wires the notebook and the cross-job playbook (`steward.tech_lead_playbook`) into reviews: prior attempts
and the top playbook entries go into the packet; outcomes are resolved from the job's events (checks passing ->
succeeded; the same signature coming back -> failed; an owner steer right after an action -> overruled).
"""

from __future__ import annotations

import hashlib
import json
import time

from steward import actor as actor_module
from steward import tech_lead_playbook as playbook
from steward.models import Judgment
from steward.store import Store

ATTEMPT = "attempt"
EXHAUSTED_KEY = "tech_lead_exhausted_signatures"  # u3: signature -> {at, seq, state, blocker_id, skip_logged}
_OUTCOMES_OPEN = ("pending",)
_DETAIL_KEYS = ("test", "ask", "command", "file", "path")


def signature(facts: list[dict]) -> str:
    """`<kind>:<hash>` of the primary fact (the first that is not an outer-loop sweep)."""
    primary = next((f for f in facts if f.get("kind") not in (None, "sweep")), None)
    if primary is None:
        return "sweep:general"
    data = primary.get("data") or {}
    detail = next((str(data[k]) for k in _DETAIL_KEYS if data.get(k)), "")
    norm = " ".join(detail.lower().split())
    return f"{primary['kind']}:{hashlib.sha1(norm.encode()).hexdigest()[:10]}"


def append(store: Store, job_id: str, *, signature: str, action: str, strategy: str, outcome: str, model: str | None,
           lead: str | None, plan_item: str | None = None, evidence_refs=(), trigger_id: str | None = None,
           detail: str = "", kind: str | None = None) -> int:
    return store.notebook_append(job_id, {"signature": signature, "kind": kind, "action": action, "strategy": strategy,
                                          "outcome": outcome, "model": model, "lead": lead, "plan_item": plan_item,
                                          "evidence_refs": list(evidence_refs), "trigger_id": trigger_id,
                                          "detail": detail})


def entries(store: Store, job_id: str) -> list[dict]:
    return store.notebook_rows(job_id)


def lookup(store: Store, job_id: str, sig: str) -> list[dict]:
    """The attempts (not the outcome rows) recorded for a signature, oldest first."""
    return [r for r in store.notebook_rows(job_id, sig) if r["detail"] != "outcome"]


def _latest_outcomes(rows: list[dict]) -> dict[int, str]:
    """attempt seq -> its latest outcome (outcome rows name the attempt they resolve in `evidence_refs`)."""
    out = {r["seq"]: r["outcome"] for r in rows if r["detail"] != "outcome"}
    for r in rows:
        if r["detail"] == "outcome":
            for ref in r["evidence_refs"]:
                if str(ref).startswith("attempt:"):
                    out[int(str(ref).split(":", 1)[1])] = r["outcome"]
    return out


def set_outcome(store: Store, job_id: str, sig: str, outcome: str) -> list[dict]:
    """Resolve every still-pending attempt for `sig` with `outcome` (new rows). Returns the attempts resolved."""
    rows = store.notebook_rows(job_id, sig)
    latest = _latest_outcomes(rows)
    resolved = []
    for r in rows:
        if r["detail"] == "outcome" or latest.get(r["seq"]) not in _OUTCOMES_OPEN:
            continue
        append(store, job_id, signature=sig, action=r["action"], strategy=r["strategy"], outcome=outcome,
               model=r["model"], lead=r["lead"], plan_item=r["plan_item"], evidence_refs=[f"attempt:{r['seq']}"],
               trigger_id=r["trigger_id"], detail="outcome", kind=r["kind"])
        resolved.append(r)
    return resolved


def sightings(store: Store, job_id: str, sig: str) -> int:
    """How many reviews saw this signature (distinct triggers)."""
    return len({r["trigger_id"] for r in lookup(store, job_id, sig) if r["trigger_id"]})


def failed_strategies(store: Store, job_id: str, sig: str) -> set[str]:
    rows = store.notebook_rows(job_id, sig)
    latest = _latest_outcomes(rows)
    return {r["strategy"] for r in rows if r["detail"] != "outcome" and latest.get(r["seq"]) in ("failed", "overruled")}


def render(rows: list[dict]) -> str:
    if not rows:
        return "(no notebook entries)"
    out = []
    for r in rows:
        mark = "  ->" if r["detail"] == "outcome" else "-"
        out.append(f"{mark} {r['ts'][:19]} {r['signature']:<34} {r['action']:<10} strategy={r['strategy']:<12} "
                   f"outcome={r['outcome']:<10} model={r['model'] or '-'}"
                   + (f" plan={r['plan_item']}" if r["plan_item"] else "")
                   + (f" refs={','.join(map(str, r['evidence_refs']))}" if r["evidence_refs"] else ""))
    return "\n".join(out)


class Memory:
    """Notebook + playbook hooks for `JobTechLead` (every job)."""

    CURSOR = "tech_lead_memory_cursor"

    def __init__(self, store: Store, *, use_playbook: bool = True):
        self.store, self.use_playbook = store, use_playbook

    # -- packet ---------------------------------------------------------------------------------------------
    def packet_section(self, job_id: str, facts: list[dict], policy) -> dict:
        sig = signature(facts)
        prior = [{"action": r["action"], "strategy": r["strategy"], "model": r["model"], "lead": r["lead"],
                  "outcome": _latest_outcomes(self.store.notebook_rows(job_id, sig)).get(r["seq"], r["outcome"]),
                  "plan_item": r["plan_item"]} for r in lookup(self.store, job_id, sig)][-6:]
        out = {"notebook": {"signature": sig, "sightings": sightings(self.store, job_id, sig), "prior_attempts": prior,
                            "failed_strategies": sorted(failed_strategies(self.store, job_id, sig)),
                            "rule": "a signature seen before needs a different strategy; a third sighting escalates"}}
        if self.use_playbook:
            kinds = [k for k in dict.fromkeys(f.get("kind") for f in facts) if k and k != "sweep"]
            out["playbook"] = playbook.packet_entries(self.store, kinds, policy)
            out["prior_lessons"] = playbook.relevant_lessons(self.store, job_id, kinds)
            out["prior_lessons_rule"] = (
                "Evidence-backed advisory only. Check the cited assumptions against this job; current goal, grants, "
                "owner decisions and verification always control. A remedy is not authorization or proof.")
        return out

    # -- around an answer -----------------------------------------------------------------------------------
    def before_act(self, ctx, executor=None) -> dict | None:
        """None to proceed; {"reject": reason} for a repeated failed strategy; {"status": "escalated"} on a third
        sighting (the escalation is done here)."""
        sig = signature(ctx.facts)
        if sig == "sweep:general":
            return None
        trigger = getattr(ctx, "trigger_id", "") or ""
        seen_before = {r["trigger_id"] for r in lookup(self.store, ctx.job_id, sig) if r["trigger_id"] != trigger}
        if seen_before:  # the issue came back: whatever was tried for it did not hold
            for r in set_outcome(self.store, ctx.job_id, sig, "failed"):
                if self.use_playbook:
                    playbook.record_outcome(self.store, r["kind"], r["strategy"], success=False, job_id=ctx.job_id)
        if len(seen_before) >= 2:
            text = (f"tech lead exhausted: third sighting of {sig} (tried: "
                    f"{', '.join(sorted(failed_strategies(self.store, ctx.job_id, sig))) or '-'})")
            append(self.store, ctx.job_id, signature=sig, action="escalate", strategy="escalate", outcome="escalated",
                   model=ctx.model, lead=ctx.lead_id, plan_item=ctx.proposal.plan_item, trigger_id=trigger,
                   evidence_refs=[f"fact:{f.get('id')}" for f in ctx.facts if f.get("id")], detail=text)
            from steward import ownership
            if ownership.enabled():
                blocker_id = next(((f.get("data") or {}).get("blocker_id") for f in ctx.facts
                                   if (f.get("data") or {}).get("blocker_id")), None)
                now = executor.clock() if executor is not None else time.time()
                b = ownership.request_strategy_review(self.store, ctx.job_id, blocker_id=blocker_id, reason=text,
                                                      signature=sig, prefer_other_than=ctx.lead_id, now=now)
                if ownership.strategy_reviews_exhausted(self.store, ctx.job_id, b.blocker_id):
                    # u3: no strategy review is left for this signature — ask the owner once, stop paying for reviews
                    self._exhaust(ctx, executor, sig, text, b.blocker_id, now)
                    return {"status": "escalated", "signature": sig, "reason": text}
                return {"status": "strategy_review", "signature": sig, "reason": text}
            already = sig in (self.store.kv_get(ctx.job_id, EXHAUSTED_KEY) or {})
            self._exhaust(ctx, None, sig, text, "", executor.clock() if executor is not None else time.time())
            if executor is not None and not already:  # u3: once per exhaustion episode
                executor.escalate_text(ctx.job_id, text, kind="tech_lead_exhausted")
            return {"status": "escalated", "signature": sig, "reason": text}
        strategy = self.strategy(ctx.proposal)
        if seen_before and strategy in failed_strategies(self.store, ctx.job_id, sig):
            return {"reject": f"strategy {strategy!r} already failed for {sig}", "strategy": strategy,
                    "signature": sig}
        return None

    # -- exhausted signatures (u3) ------------------------------------------------------------------------------
    def _exhaust(self, ctx, executor, sig: str, text: str, blocker_id: str, now: float) -> None:
        """Mark `sig` exhausted and escalate it to the owner exactly once (per exhaustion episode)."""
        marks = dict(self.store.kv_get(ctx.job_id, EXHAUSTED_KEY) or {})
        if sig in marks:
            return
        job = self.store.get_job(ctx.job_id)
        last = self.store._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE job_id = ?",
                                        (ctx.job_id,)).fetchone()[0]
        marks[sig] = {"at": now, "seq": int(last), "state": job.state.value if job else None,
                      "blocker_id": blocker_id, "skip_logged": False}
        self.store.kv_set(ctx.job_id, EXHAUSTED_KEY, marks)
        if executor is None:
            return
        from steward.blockers import HumanRequest
        tried = sorted(failed_strategies(self.store, ctx.job_id, sig))
        facts = [f"{f.get('kind')}: {json.dumps(f.get('data') or {}, sort_keys=True)[:160]}" for f in ctx.facts
                 if f.get("kind")]
        request = HumanRequest(
            decision=f"{ctx.job_id[:8]}: {text}. The tech lead has no strategy left for {sig}; "
                     "say how to continue (approve = resume with your guidance, deny = stop).",
            why_authority_insufficient="every strategy the tech lead has for this issue failed and the strategy "
                                       "reviews are spent; another review would cost money and change nothing",
            evidence=tuple([f"signature:{sig}", *[f"tried:{t}" for t in tried], *facts][:6]),
            continuing="no further tech-lead reviews of this issue until the job changes or you answer",
            options=("continue", "stop"))
        executor.escalate_text(ctx.job_id, text, kind="tech_lead_exhausted", request=request, blocker_id=blocker_id,
                               lead_id=ctx.lead_id or "", subject=f"tech lead exhausted: {sig}")

    def exhausted_skip(self, job_id: str, facts: list[dict]) -> dict | None:
        """Before a review is paid for: the facts' signature is exhausted and nothing about the job changed since ->
        the skip result (logged once); a change (worker evidence, an owner act, a state change) re-opens it."""
        sig = signature(facts)
        marks = dict(self.store.kv_get(job_id, EXHAUSTED_KEY) or {})
        mark = marks.get(sig)
        if mark is None:
            return None
        if self._changed_since(job_id, mark):
            marks.pop(sig)
            self.store.kv_set(job_id, EXHAUSTED_KEY, marks)
            return None
        if not mark.get("skip_logged"):
            marks[sig] = {**mark, "skip_logged": True}
            self.store.kv_set(job_id, EXHAUSTED_KEY, marks)
            self.store.append_event(job_id, what="tech_lead_review_skipped_exhausted",
                                    evidence={"signature": sig, "blocker_id": mark.get("blocker_id"),
                                              "until": "the job changes or the owner answers"},
                                    rule="tech_lead_exhausted_once", judgment=Judgment.code)
        return {"status": "skipped", "reason": "signature_exhausted", "signature": sig}

    def _changed_since(self, job_id: str, mark: dict) -> bool:
        from steward import liveness
        job = self.store.get_job(job_id)
        if job is None or job.state.value != mark.get("state"):
            return True
        if liveness.worker_evidence_ts(self.store, job_id) > float(mark.get("at") or 0.0):
            return True
        row = self.store._conn.execute("SELECT 1 FROM events WHERE job_id = ? AND seq > ? AND judgment = ? LIMIT 1",
                                       (job_id, int(mark.get("seq") or 0), Judgment.human.value)).fetchone()
        return row is not None

    @staticmethod
    def strategy(proposal) -> str:
        return (proposal.strategy or "+".join(a.type for a in proposal.actions)).strip().lower()[:40]

    def after_act(self, ctx, results: list) -> None:
        sig = signature(ctx.facts)
        strategy = self.strategy(ctx.proposal)
        kind = sig.split(":", 1)[0]
        for a, res in zip(ctx.proposal.actions, results):
            outcome = (res or {}).get("outcome", "unknown") if isinstance(res, dict) else "unknown"
            row_outcome = "pending" if outcome in ("executed", "started") else outcome
            append(self.store, ctx.job_id, signature=sig, action=(res or {}).get("action") or a.type,
                   strategy=strategy, outcome=row_outcome, model=ctx.model, lead=ctx.lead_id,
                   plan_item=ctx.proposal.plan_item, trigger_id=getattr(ctx, "trigger_id", "") or "",
                   evidence_refs=[f"fact:{f.get('id')}" for f in ctx.facts if f.get("id")],
                   detail=str((res or {}).get("detail") or "")[:300], kind=kind)
            if self.use_playbook and row_outcome == "pending" and kind != "sweep":
                text = a.params.get("instructions") or a.params.get("text") or a.params.get("reason") or ""
                used = any(r["strategy"] == strategy for r in playbook.top(self.store, kind))
                playbook.record_attempt(self.store, kind, strategy, f"{strategy}: {text}", job_id=ctx.job_id)
                if used:
                    self.store.append_event(ctx.job_id, what="tech_lead_playbook_used",
                                            evidence={"kind": kind, "strategy": strategy, "signature": sig,
                                                      "lead": ctx.lead_id},
                                            rule="tech_lead_playbook", judgment=Judgment.code)

    # -- outcomes from the job's own events -------------------------------------------------------------------
    def observe(self, job_id: str) -> None:
        cursor = int(self.store.kv_get(job_id, self.CURSOR) or 0)
        events = self.store.events_since(job_id, cursor)
        if not events:
            return
        self.store.kv_set(job_id, self.CURSOR, events[-1].seq)
        acted = any(e.what == "tech_lead_action" for e in self.store.events_since(job_id, max(0, cursor - 200)))
        for e in events:
            if e.what == "checks passed at exact commit":
                for sig in {r["signature"] for r in entries(self.store, job_id)}:
                    for r in set_outcome(self.store, job_id, sig, "succeeded"):
                        if self.use_playbook and r["kind"] != "sweep":
                            playbook.record_outcome(self.store, r["kind"], r["strategy"], success=True, job_id=job_id)
            elif acted and e.what.endswith(" instruction recorded"):
                ev = e.evidence or {}
                origin = str(ev.get("origin") or "")
                if origin in ("tech_lead", "steward", "contract", "trajectory", "deadline", "flow", "diagnosis", "") \
                        or origin.startswith("watcher:"):
                    continue
                who = actor_module.classify(origin, ev.get("actor"))
                outcome = "overruled" if who.kind == actor_module.Actor.OWNER else "redirected_by_operator"
                for sig in {r["signature"] for r in entries(self.store, job_id)}:
                    for r in set_outcome(self.store, job_id, sig, outcome):
                        if outcome == "overruled" and self.use_playbook and r["kind"] != "sweep":
                            playbook.demote(self.store, r["kind"], r["strategy"])


_ = json  # rows carry JSON evidence refs (decoded by the store)
