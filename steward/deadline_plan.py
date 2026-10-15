"""The worker's deadline plan (v2.2, DL-05, DL-12): a transient committed artifact, validated against the owner's commitment.

The worker commits `.steward/deadline/<job8>/PLAN.json` early, keeps working, and removes the file in a cleanup commit
before it reports done (the candidate's final diff must not contain it; closeout sends it back otherwise). Steward reads
the plan only from committed HEAD, never the dirty tree, validates it against the current commitment revision and the
allowed adaptations, and adopts the last valid version into kv `deadline_plan` with its exact source SHA and raw text
(the store keeps it after the cleanup commit removes the file). A malformed, stale or unauthorized revision is
rejected once per SHA+reason and the previous valid plan stays in force.

Every adopted milestone target becomes an immutable slot record in kv `deadline_targets`, keyed exactly
`(commitment_rev, milestone_id, target_at_utc)`; a revision that moves a target adds a slot and keeps the old one
(Phase 22 delivers check-ins for each). No valid plan by `min(15 min, 10 % of the window)` adopts a labelled fallback.
The worker can never touch the commitment itself (`steward.deadline.revise` is human-only).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from steward import deadline
from steward.models import Judgment
from steward.store import Store

PLAN_VERSION = 1
K_PLAN = "deadline_plan"
K_TARGETS = "deadline_targets"
K_SEEN = "deadline_plan_seen"
CANDIDATE = "candidate"
ADAPTATIONS = ("reorder", "switch_approach", "stop_retry", "drop_optional")
STOP_RETRY_MIN_FAILURES = 3
RUNNER_PREDICATES = ("identical_calls_no_change", "repeated_failure_code")
PLAN_WINDOW_CAP_MIN = 15.0
PLAN_WINDOW_SHARE = 0.10


def verification_tail_min() -> float:
    from steward import eta

    return float(eta.TAIL_SINGLE_MIN)


def plan_path(job_id: str) -> str:
    return f".steward/deadline/{job_id[:8]}/PLAN.json"


def _utc(text) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo is not None else None


def _utc_text(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slot_key(commitment_rev: int, milestone_id: str, target_at_utc: str) -> str:
    return f"{commitment_rev}:{milestone_id}:{target_at_utc}"


# -- validation (pure) ----------------------------------------------------------------------------

def validate(raw: str, *, commitment: dict, now: datetime, tail_min: float | None = None) -> tuple[dict | None, list[str]]:
    """`raw` PLAN.json text -> (normalized plan, []) or (None, reasons). Pure: no store, no clock beyond `now`.

    Rejects malformed JSON, a stale `commitment_rev`, a missing/reclassified required item, an unauthorized drop
    (required or worker-proposed items), unknown adaptation kinds, a stop-retry without ≥3 identical failures or a
    runner predicate, bad dependencies or target ordering, a commit milestone without declared paths, and a candidate
    target later than `due − tail` while that bound is still in the future. A window already shorter than the tail
    is never a reason: the plan is adopted and marked `infeasible`. Unknown fields are ignored; any `met`/`status`
    prose is dropped (only code-verified evidence counts)."""
    tail = verification_tail_min() if tail_min is None else tail_min
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return None, [f"malformed_json: {str(exc)[:80]}"]
    if not isinstance(data, dict):
        return None, ["malformed_json: not an object"]
    reasons: list[str] = []
    if data.get("v") != PLAN_VERSION:
        reasons.append(f"unsupported_version: {data.get('v')!r}")
    if data.get("commitment_rev") != commitment.get("rev"):
        reasons.append(f"stale_commitment_rev: plan {data.get('commitment_rev')!r}, current {commitment.get('rev')}")
    owner_items = {i["id"]: i for i in commitment.get("items") or []}
    plan_items: dict[str, dict] = {}
    for raw_item in data.get("items") or []:
        if not isinstance(raw_item, dict) or not isinstance(raw_item.get("id"), str):
            reasons.append("bad_item: every item needs a string id")
            continue
        iid = raw_item["id"]
        cls = raw_item.get("class") or (owner_items.get(iid) or {}).get("class") or "required"
        if cls not in ("required", "optional", "proposed-optional"):
            reasons.append(f"bad_item_class:{iid}")
            continue
        owner = owner_items.get(iid)
        if owner is None and cls == "optional":
            cls = "proposed-optional"  # only the owner classifies optional scope
        if owner is not None and owner["class"] == "required" and cls != "required":
            reasons.append(f"reclassified_required:{iid}")
        ev = raw_item.get("evidence") if isinstance(raw_item.get("evidence"), dict) else {}
        plan_items[iid] = {
            "id": iid, "class": cls, "owner_class": owner["class"] if owner else None,
            "text": str(raw_item.get("text") or (owner or {}).get("text") or "")[:300],
            "evidence": {"paths": [str(p) for p in ev.get("paths") or [] if isinstance(p, str)],
                         "tests": [str(t) for t in ev.get("tests") or [] if isinstance(t, str)]},
        }
    dropped = [d for d in data.get("dropped") or [] if isinstance(d, str)]
    adaptations = []
    for a in data.get("adaptations") or []:
        if not isinstance(a, dict):
            reasons.append("bad_adaptation")
            continue
        kind = a.get("kind")
        if kind not in ADAPTATIONS:
            reasons.append(f"unauthorized_adaptation:{kind}")
            continue
        if kind == "stop_retry":
            failures = a.get("failures") if isinstance(a.get("failures"), int) else 0
            if failures < STOP_RETRY_MIN_FAILURES and a.get("predicate") not in RUNNER_PREDICATES:
                reasons.append("stop_retry_unproven: needs >=3 identical failures or a runner predicate")
        if kind == "drop_optional":
            dropped += [d for d in a.get("items") or [] if isinstance(d, str)]
        adaptations.append({k: a.get(k) for k in ("kind", "detail", "reason", "items", "failures", "predicate", "at")
                            if a.get(k) is not None})
    for iid in sorted(set(dropped)):
        owner = owner_items.get(iid)
        if owner is None or owner["class"] != "optional":
            reasons.append(f"unauthorized_drop:{iid}")
    for iid, owner in owner_items.items():
        if owner["class"] == "required" and iid not in plan_items:
            reasons.append(f"missing_required_item:{iid}")
    milestones = []
    seen_ids: set[str] = set()
    for m in data.get("milestones") or []:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            reasons.append("bad_milestone: every milestone needs a string id")
            continue
        mid = m["id"]
        if mid in seen_ids:
            reasons.append(f"duplicate_milestone:{mid}")
            continue
        seen_ids.add(mid)
        target = _utc(m.get("target_at"))
        if target is None:
            reasons.append(f"bad_target:{mid} (ISO time with an offset)")
            continue
        evidence = m.get("evidence") or ("verify" if mid == CANDIDATE else "commit")
        if mid == CANDIDATE and evidence != "verify":
            reasons.append("candidate_evidence_must_be_verify")
        if mid != CANDIDATE and not (evidence == "commit" or str(evidence).startswith("units:")):
            reasons.append(f"bad_evidence:{mid}")
        paths = [str(p) for p in m.get("paths") or [] if isinstance(p, str)]
        if evidence == "commit" and not paths:
            reasons.append(f"commit_evidence_needs_paths:{mid}")
        items = [i for i in m.get("items") or [] if isinstance(i, str)]
        for iid in items:
            if iid not in plan_items and iid not in owner_items:
                reasons.append(f"unknown_item_in_milestone:{mid}:{iid}")
        milestones.append({"id": mid, "title": str(m.get("title") or mid)[:120], "items": items,
                           "target_at_utc": _utc_text(target), "evidence": str(evidence), "paths": paths,
                           "tests": [str(t) for t in m.get("tests") or [] if isinstance(t, str)],
                           "depends_on": [d for d in m.get("depends_on") or [] if isinstance(d, str)]})
    cands = [m for m in milestones if m["id"] == CANDIDATE]
    if not cands:
        reasons.append("missing_candidate_milestone")
    by_id = {m["id"]: m for m in milestones}
    for m in milestones:
        for dep in m["depends_on"]:
            if dep not in by_id:
                reasons.append(f"bad_dependency:{m['id']}->{dep}")
            elif by_id[dep]["target_at_utc"] > m["target_at_utc"]:
                reasons.append(f"dependency_after_dependent:{dep}->{m['id']}")
    if _has_cycle(by_id):
        reasons.append("dependency_cycle")
    covered = {i for m in milestones for i in m["items"]}
    for iid, item in plan_items.items():
        if item["class"] != "optional" and iid not in dropped and iid not in covered and cands:
            cands[0]["items"].append(iid)  # the candidate covers everything required that no milestone names
    infeasible = False
    due = _utc(commitment.get("due_at_utc"))
    if cands and due is not None:
        cand_t = _utc(cands[0]["target_at_utc"])
        limit = due - timedelta(minutes=tail)
        for m in milestones:
            if m["id"] != CANDIDATE and _utc(m["target_at_utc"]) > cand_t:
                reasons.append(f"target_order:{m['id']} after candidate")
        if limit > now:
            if cand_t > limit:
                reasons.append(f"beyond_deadline: candidate {cands[0]['target_at_utc']} > due - {tail:g} min")
        else:
            infeasible = True  # a window shorter than the tail: adopt, report infeasibility, never refuse work
    if reasons:
        return None, sorted(set(reasons))
    tradeoffs = [str(t)[:300] for t in data.get("tradeoffs") or [] if isinstance(t, (str, int, float))]
    return {"v": PLAN_VERSION, "commitment_rev": commitment["rev"], "approach": str(data.get("approach") or "")[:500],
            "items": list(plan_items.values()), "milestones": milestones, "adaptations": adaptations,
            "dropped": sorted(set(dropped)), "tradeoffs": tradeoffs, "infeasible": infeasible}, []


def _has_cycle(by_id: dict) -> bool:
    state: dict[str, int] = {}

    def visit(mid: str) -> bool:
        if state.get(mid) == 1:
            return True
        if state.get(mid) == 2 or mid not in by_id:
            return False
        state[mid] = 1
        if any(visit(d) for d in by_id[mid]["depends_on"]):
            return True
        state[mid] = 2
        return False

    return any(visit(m) for m in by_id)


def fallback_plan(commitment: dict, *, start: datetime, now: datetime, tail_min: float | None = None) -> dict:
    """A labelled code fallback (no worker plan by the plan window): a 50 % checkpoint plus `candidate` at
    `due − tail` (or now + tail when the window is already shorter). Every required item rides the candidate."""
    tail = verification_tail_min() if tail_min is None else tail_min
    due = _utc(commitment["due_at_utc"])
    limit = due - timedelta(minutes=tail)
    infeasible = limit <= now
    cand = limit if not infeasible else now + timedelta(minutes=tail)
    mid_t = start + (due - start) / 2
    required = [i["id"] for i in commitment.get("items") or [] if i["class"] == "required"]
    milestones = []
    if now < mid_t < cand:
        milestones.append({"id": "F50", "title": "halfway checkpoint (fallback)", "items": [],
                           "target_at_utc": _utc_text(mid_t), "evidence": "time", "paths": [], "tests": [],
                           "depends_on": []})
    milestones.append({"id": CANDIDATE, "title": "review-ready candidate", "items": required,
                       "target_at_utc": _utc_text(cand), "evidence": "verify", "paths": [], "tests": [],
                       "depends_on": []})
    return {"v": PLAN_VERSION, "commitment_rev": commitment["rev"], "approach": "(fallback: no worker plan)",
            "items": [{"id": i["id"], "class": i["class"], "owner_class": i["class"], "text": i["text"],
                       "evidence": {"paths": [], "tests": []}} for i in commitment.get("items") or []],
            "milestones": milestones, "adaptations": [], "dropped": [], "tradeoffs": [], "infeasible": infeasible}


# -- store (adoption is the only writer) -----------------------------------------------------------

def plan_doc(store: Store, job_id: str) -> dict:
    doc = store.kv_get(job_id, K_PLAN)
    return doc if isinstance(doc, dict) else {"current": None, "history": [], "rejections": {}}


def current_plan(store: Store, job_id: str) -> dict | None:
    return plan_doc(store, job_id).get("current")


def targets(store: Store, job_id: str) -> list[dict]:
    rows = store.kv_get(job_id, K_TARGETS)
    return list(rows) if isinstance(rows, list) else []


def _snapshot_targets(tx, job_id: str, plan: dict, plan_rev: int, at: str) -> list[dict]:
    rows = tx.kv_get(job_id, K_TARGETS) or []
    keys = {r["key"] for r in rows}
    added = []
    for m in plan["milestones"]:
        key = slot_key(plan["commitment_rev"], m["id"], m["target_at_utc"])
        if key in keys:
            continue  # an unchanged target reuses its slot; plan revision is metadata only
        rec = {"key": key, "commitment_rev": plan["commitment_rev"], "milestone_id": m["id"],
               "target_at_utc": m["target_at_utc"], "title": m["title"], "items": list(m["items"]),
               "evidence": m["evidence"], "created_by_plan_rev": plan_rev, "source": plan.get("source", "worker"),
               "recorded_at": at}
        rows.append(rec)
        keys.add(key)
        added.append(rec)
    tx.kv_set(job_id, K_TARGETS, rows)
    return added


def adopt(store: Store, job_id: str, plan: dict, *, sha: str, raw: str, source: str, at: str,
          rule: str = "deadline_plan_valid") -> dict | None:
    """Adopt `plan` as the job's current plan (one IMMEDIATE transaction): a monotonic plan_rev, the exact raw
    text and SHA kept in history, and every target snapshotted as a slot before anything can supersede it.
    Idempotent per `(sha, commitment_rev, source)`: a racing sweep that already adopted it changes nothing."""
    with store.immediate() as tx:
        doc = tx.kv_get(job_id, K_PLAN) or {"current": None, "history": [], "rejections": {}}
        for h in doc["history"]:
            if h["sha"] == sha and h["commitment_rev"] == plan["commitment_rev"] and h["source"] == source:
                return None
        plan_rev = len(doc["history"]) + 1
        current = {**plan, "plan_rev": plan_rev, "sha": sha, "source": source, "adopted_at": at}
        prev = doc.get("current")
        doc["history"].append({"plan_rev": plan_rev, "sha": sha, "commitment_rev": plan["commitment_rev"],
                               "source": source, "adopted_at": at, "raw": raw})
        doc["current"] = current
        tx.kv_set(job_id, K_PLAN, doc)
        added = _snapshot_targets(tx, job_id, current, plan_rev, at)
        moved = []
        if prev:
            old = {m["id"]: m["target_at_utc"] for m in prev.get("milestones") or []}
            moved = [{"milestone": m["id"], "from": old[m["id"]], "to": m["target_at_utc"]}
                     for m in current["milestones"] if m["id"] in old and old[m["id"]] != m["target_at_utc"]]
        tx.append_event(job_id, what="deadline_plan_adopted",
                        evidence={"plan_rev": plan_rev, "sha": sha, "source": source,
                                  "commitment_rev": plan["commitment_rev"], "infeasible": plan.get("infeasible", False),
                                  "milestones": [{"id": m["id"], "target_at_utc": m["target_at_utc"]}
                                                 for m in current["milestones"]],
                                  "targets_moved": moved, "slots_added": [a["key"] for a in added],
                                  "adaptations": current.get("adaptations", []), "dropped": current.get("dropped", []),
                                  "tradeoffs": current.get("tradeoffs", [])},
                        rule=rule, judgment=Judgment.code)
        for a in current.get("adaptations") or []:
            if prev is None or a not in (prev.get("adaptations") or []):
                tx.append_event(job_id, what="deadline_adaptation",
                                evidence={**a, "plan_rev": plan_rev, "sha": sha, "provenance": "worker plan (validated)"},
                                rule="deadline_adaptation_allowed", judgment=Judgment.code)
        for iid in current.get("dropped") or []:
            if prev is None or iid not in (prev.get("dropped") or []):
                tx.append_event(job_id, what="deadline_scope_dropped",
                                evidence={"item": iid, "plan_rev": plan_rev, "sha": sha, "class": "optional"},
                                rule="deadline_owner_optional_only", judgment=Judgment.code)
    return current


def reject(store: Store, job_id: str, *, sha: str, reasons: list[str], at: str) -> list[str]:
    """Record a rejected plan revision: one `deadline_plan_rejected` + `deadline_guardrail` per (sha, reason).
    The previous valid plan stays current. Returns the reasons newly recorded."""
    new: list[str] = []
    with store.immediate() as tx:
        doc = tx.kv_get(job_id, K_PLAN) or {"current": None, "history": [], "rejections": {}}
        for reason in reasons:
            key = f"{sha}:{reason.split(':', 1)[0]}"
            if key in doc["rejections"]:
                continue
            doc["rejections"][key] = at
            new.append(reason)
        if not new:
            return []
        tx.kv_set(job_id, K_PLAN, doc)
        kept = (doc.get("current") or {}).get("plan_rev")
        tx.append_event(job_id, what="deadline_plan_rejected",
                        evidence={"sha": sha, "reasons": new, "kept_plan_rev": kept},
                        rule="deadline_plan_validation", judgment=Judgment.code)
        for reason in new:
            tx.append_event(job_id, what="deadline_guardrail",
                            evidence={"sha": sha, "reason": reason, "kept_plan_rev": kept},
                            rule="deadline_plan_validation", judgment=Judgment.code)
    return new


# -- reading committed HEAD --------------------------------------------------------------------------

def read_committed(worktree, runner, job_id: str) -> tuple[str, str | None]:
    """`(head_sha, raw_text|None)` from committed HEAD only. `head_sha` is "" when there is no readable HEAD."""
    res = runner.run(["git", "rev-parse", "HEAD"], cwd=worktree)
    head = (res.stdout or "").strip() if res.exit_code == 0 else ""
    if not head:
        return "", None
    shown = runner.run(["git", "show", f"{head}:{plan_path(job_id)}"], cwd=worktree)
    return head, (shown.stdout if shown.exit_code == 0 else None)


def commitment_start(store: Store, job_id: str) -> datetime | None:
    """When the deadline clock started: rev 1's human write instant (the instant its due time resolved at)."""
    doc = deadline.commitment(store, job_id)
    if not doc:
        return None
    first = doc["revisions"][0]
    return _utc(first.get("resolved_at")) or _utc(first.get("at"))


def plan_window_min(commitment: dict, start: datetime) -> float:
    due = _utc(commitment["due_at_utc"])
    window = max(0.0, (due - start).total_seconds() / 60.0)
    return min(PLAN_WINDOW_CAP_MIN, PLAN_WINDOW_SHARE * window)


def refresh(store: Store, job_id: str, *, worktree, runner, now: datetime, start: datetime | None = None) -> dict:
    """One plan step for the sweep: read committed HEAD when it moved (or the commitment revision changed), adopt or
    reject; adopt the fallback once the plan window passes with no valid plan. Returns what happened."""
    rec = deadline.current(store, job_id)
    if rec is None:
        return {"skipped": "no deadline"}
    out: dict = {}
    head, raw = ("", None)
    if worktree is not None and runner is not None:
        try:
            head, raw = read_committed(worktree, runner, job_id)
        except Exception as exc:  # noqa: BLE001 - a git failure is a visible fallback, never a crash
            out["error"] = str(exc)[:200]
    seen = store.kv_get(job_id, K_SEEN) or {}
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if head and (seen.get("head") != head or seen.get("rev") != rec["rev"]):
        store.kv_set(job_id, K_SEEN, {"head": head, "rev": rec["rev"]})
        if raw is not None:
            plan, reasons = validate(raw, commitment=rec, now=now)
            if plan is not None:
                out["adopted"] = adopt(store, job_id, plan, sha=head, raw=raw, source="worker", at=at)
            else:
                out["rejected"] = reject(store, job_id, sha=head, reasons=reasons, at=at)
    cur = current_plan(store, job_id)
    has_valid = cur is not None and cur.get("commitment_rev") == rec["rev"] and cur.get("source") == "worker"
    if not has_valid:
        begin = start or commitment_start(store, job_id) or now
        if now >= begin + timedelta(minutes=plan_window_min(rec, begin)):
            fb_sha = f"fallback:rev{rec['rev']}"
            if not any(h["sha"] == fb_sha for h in plan_doc(store, job_id)["history"]) and \
                    (cur is None or cur.get("commitment_rev") != rec["rev"]):
                plan = fallback_plan(rec, start=begin, now=now)
                adopted = adopt(store, job_id, {**plan, "source": "fallback"}, sha=fb_sha, raw="", source="fallback",
                                at=at, rule="deadline_plan_fallback")
                if adopted is not None:
                    store.append_event(job_id, what="deadline_plan_missing",
                                       evidence={"commitment_rev": rec["rev"], "plan_window_min":
                                                 round(plan_window_min(rec, begin), 1),
                                                 "fallback_plan_rev": adopted["plan_rev"]},
                                       rule="deadline_plan_window", judgment=Judgment.code)
                    out["fallback"] = adopted
    return out


def in_final_diff(worktree, runner, base: str, head: str, job_id: str) -> bool:
    """True when the candidate's final diff (`base...head`) still carries the transient plan file."""
    if not base or not head:
        return False
    res = runner.run(["git", "diff", "--name-only", f"{base}...{head}", "--", plan_path(job_id)], cwd=worktree)
    return res.exit_code == 0 and bool((res.stdout or "").strip())


# -- brief ---------------------------------------------------------------------------------------------

def brief_lines(store: Store, job_id: str) -> list[str]:
    """The worker brief's DEADLINE section (DL-05): due time, revision, item classes, plan/evidence/candidate rules
    and the bounded adaptations. Empty when the job has no deadline in force."""
    rec = deadline.current(store, job_id)
    if rec is None:
        return []
    path = plan_path(job_id)
    items = "; ".join(f"{i['id']} [{i['class']}] {i['text']}" for i in rec.get("items") or [])
    return [
        f"Due {deadline.fmt_local(rec['due_at'])} ({rec['due_at_utc']}), commitment rev {rec['rev']}. The due time, "
        "core outcome, acceptance and item classes are the owner's; you cannot change them.",
        f"Items: {items}",
        f"First, within a few minutes and without waiting for anyone, commit {path} (JSON: v=1, commitment_rev={rec['rev']}, "
        "approach, items [{id, class, evidence: {paths, tests}}], milestones [{id, title, items, target_at ISO with offset, "
        "evidence: commit|units:<label>|verify, paths, depends_on}], adaptations, dropped, tradeoffs). The last "
        f"milestone is `candidate` (evidence verify) no later than due minus {verification_tail_min():g} min. Then keep working.",
        "A milestone counts only from code-checked evidence: commit milestones need their declared paths changed at a "
        "commit; the candidate needs, for every required item, its declared paths changed and its declared tests "
        "passing at the exact candidate commit. A commit subject or a claim of done proves nothing.",
        "Allowed without asking: reorder independent work, switch to a reversible approach inside this worktree, stop a "
        "retry after 3 identical failures, drop an item the owner classified optional. Never drop or reclassify a "
        "required item; items you propose as optional stay required until the owner classifies them. If the deadline "
        "looks infeasible, write tradeoffs in the plan and keep doing required work.",
        f"Before you report done, remove {path} in a cleanup commit: the final diff must not contain it.",
        "A deadline notice naming an older commitment rev than this one is void.",
    ]
