"""Deadline schedule status (v2.2, DL-07, DL-08, DL-18): an honest forecast against the owner's commitment, beside the ETA.

`assess(snapshot, now)` is pure: rules over a frozen store snapshot and a clock, worst wins, every reason names its rule
and evidence (source design §7.2 with the v2.2 evidence caps). With no adopted worker plan and no affirmative progress
the sign is `At risk · estimating` — never On track merely because no risk was detected. On track needs an adopted
worker plan AND affirmative progress (a verified milestone, or a units-grade ETA whose P90 fits). Unlikely needs strong
evidence (units-grade timing or observed misses against the clock); priors, peers, Jev labels and fallback plans never
produce it. Worsening applies at once; improving needs a met milestone or two clean assessments ≥ 10 min apart, at most
once per 30 min; a human commitment revision resets and recomputes, labelled as the owner's change.

`sweep(...)` is the only writer: the runner tick (before its state early-returns) and the control pane's periodic tick
call it. It adopts plans, records milestone met/missed facts and the status in Store transactions (exactly once across
racing sweeps), and never raises: an error becomes one `deadline_error` per distinct message and the last good status
stays. Reads use `project(...)`, a pure projection of the persisted state plus display-only clock text: no events, no
kv, no hysteresis changes, no notifications. The ETA is read from kv `efficiency_state.eta`, never written.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from steward import deadline, deadline_evidence, deadline_outbox, deadline_plan, owner
from steward import machine
from steward.models import JobState, Judgment
from steward.store import Store

K_STATUS = "deadline_status"
K_FACTS = "deadline_milestones"
K_ERRORS = "deadline_errors"
ORDER = {"on_track": 0, "at_risk": 1, "unlikely": 2}
LABEL = {"on_track": "On track", "at_risk": "At risk", "unlikely": "Unlikely"}
ASSESS_EVERY_S = 60.0
ETA_STALE_S = 600.0
CLEAN_GAP_S = 600.0        # two clean assessments at least 10 min apart
IMPROVE_EVERY_S = 1800.0   # at most one improvement per 30 min
PROGRESS_RISK_S = 600.0    # R3: a stall class held for 10 min
WARNING_COOLDOWN_S = 900.0  # 15 min between At risk warnings; Unlikely bypasses
GRACE_MIN = 5.0
GRACE_SHARE = 0.10
P50_SOURCES = ("units", "units+trend")
RANGE_SOURCES = ("prior", "peers", "class-rate")
STALL_CLASSES = ("suspect_stall", "dead")


def _utc(text) -> datetime | None:
    return deadline_plan._utc(text)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_span(minutes: float) -> str:
    minutes = max(0, int(round(minutes)))
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}" if h else f"{m}m"


def grace_min(milestones: list[dict], index: int, start: datetime | None) -> float:
    """max(5 min, 10 % of the gap to the previous target (or the job start))."""
    target = _utc(milestones[index]["target_at_utc"])
    prev = _utc(milestones[index - 1]["target_at_utc"]) if index > 0 else start
    gap = (target - prev).total_seconds() / 60.0 if prev and target and target > prev else 0.0
    return max(GRACE_MIN, GRACE_SHARE * gap)


# -- snapshot + pure assessment -------------------------------------------------------------------------------

def snapshot(store: Store, job_id: str) -> dict:
    """A frozen copy of everything `assess` reads (plain dicts only)."""
    from steward import efficiency_watch

    job = store.get_job(job_id)
    eff = store.kv_get(job_id, efficiency_watch.STATE_KEY)
    hb = [e for e in store.events(job_id) if e.what == "heartbeat class changed"]
    return {
        "job_state": job.state.value if job else None,
        "commitment": deadline.current(store, job_id),
        "start": _iso(deadline_plan.commitment_start(store, job_id)) if deadline_plan.commitment_start(store, job_id)
        else None,
        "plan": deadline_plan.current_plan(store, job_id),
        "facts": store.kv_get(job_id, K_FACTS) or {"met": {}, "missed": {}},
        "eta": eff.get("eta") if isinstance(eff, dict) and isinstance(eff.get("eta"), dict) else None,
        "last_class": store.kv_get(job_id, "last_class"),
        "class_since": hb[-1].ts if hb else None,
        "verdict": store.kv_get(job_id, deadline_evidence.K_VERDICT),
        "prev": store.kv_get(job_id, K_STATUS),
    }


def _eta_grade(eta: dict | None, now: datetime) -> tuple[str, dict | None]:
    if not isinstance(eta, dict):
        return "none", None
    at = eta.get("at")
    if not isinstance(at, (int, float)) or now.timestamp() - float(at) > ETA_STALE_S:
        return "stale", None
    src = eta.get("source")
    shown = eta.get("shown") or {}
    if src in P50_SOURCES and int(eta.get("units_done") or 0) >= 1 and shown.get("p50_end") is not None:
        return "p50", eta
    if src in RANGE_SOURCES and shown.get("p10_end") is not None:
        return "range", eta
    return "none", None


def assess(snap: dict, now: datetime) -> dict:
    """Pure: `{status, label, basis, reasons:[{rule, evidence}], affirmative, phase}` for the snapshot at `now`."""
    rec = snap["commitment"]
    due = _utc(rec["due_at_utc"])
    plan = snap.get("plan") or None
    facts = snap.get("facts") or {"met": {}, "missed": {}}
    met, missed = facts.get("met") or {}, facts.get("missed") or {}
    start = _utc(snap.get("start")) or now
    tail = deadline_plan.verification_tail_min()
    strong: list[dict] = []
    risk: list[dict] = []
    weak: list[dict] = []
    worker_plan = bool(plan) and plan.get("source") == "worker" and plan.get("commitment_rev") == rec["rev"]
    milestones = (plan or {}).get("milestones") or []
    evidence_ms = [m for m in milestones if m["id"] != deadline_plan.CANDIDATE and m.get("evidence") not in ("time",)]
    verdict = snap.get("verdict") or {}
    ready = bool(verdict.get("ready")) and snap.get("job_state") in (JobState.NEEDS_HUMAN.value,
                                                                    JobState.ACCEPTED.value)
    grade, eta = _eta_grade(snap.get("eta"), now)
    shown = (eta or {}).get("shown") or {}
    due_s = due.timestamp()
    if grade == "p50":
        if shown.get("p10_end") is not None and float(shown["p10_end"]) > due_s:
            strong.append({"rule": "U1", "evidence": f"even the ETA's optimistic end (P10) is after the due time "
                                                     f"(source {eta.get('source')}, {eta.get('units_done')} units done)"})
        elif shown.get("p90_end") is not None and float(shown["p90_end"]) > due_s:
            risk.append({"rule": "R2", "evidence": f"ETA P90 is after the due time (source {eta.get('source')})"})
    elif grade == "range" and float(shown["p10_end"]) > due_s:
        weak.append({"rule": "R2", "evidence": f"comparable jobs' fastest end is after the due time "
                                               f"(source {eta.get('source')}; early signal)"})
    fin = (eta or {}).get("fin_stage") or ""
    if fin.startswith("fixing") and (eta or {}).get("fin_p90_end") and float(eta["fin_p90_end"]) > due_s:
        strong.append({"rule": "U4", "evidence": f"finishing stage '{fin}' runs past the due time"})
    if now >= due - timedelta(minutes=tail) and snap.get("job_state") not in (JobState.WORKER_FINISHED.value,):
        unmet = [m["id"] for m in evidence_ms if m["id"] not in met]
        if unmet and not ready:
            strong.append({"rule": "U2", "evidence": f"inside the {tail:g}-min verification tail with milestone(s) "
                                                     f"{', '.join(unmet)} unmet"})
    open_misses = sorted((v for v in missed.values() if v.get("milestone_id") not in met),
                         key=lambda v: v["target_at_utc"])
    if open_misses:
        risk.append({"rule": "R1", "evidence": "; ".join(
            f"{v['milestone_id']} passed {v['target_at_utc']} + {v.get('grace_min', GRACE_MIN):g} min grace without "
            "evidence" for v in open_misses[:3])})
    if len(missed) >= 2 and open_misses:
        last = open_misses[-1]
        gap = max(GRACE_MIN, float(last.get("gap_min") or 0.0))
        if now > _utc(last["target_at_utc"]) + timedelta(minutes=gap):
            strong.append({"rule": "U3", "evidence": f"{len(missed)} milestones missed and {last['milestone_id']} still "
                                                     f"unmet a full milestone gap after its target"})
    since = _utc(snap.get("class_since"))
    if snap.get("last_class") in STALL_CLASSES and since and (now - since).total_seconds() >= PROGRESS_RISK_S:
        risk.append({"rule": "R3", "evidence": f"heartbeat {snap['last_class']} for "
                                               f"{_fmt_span((now - since).total_seconds() / 60)}"})
    window_min = (due - start).total_seconds() / 60.0
    if (plan or {}).get("infeasible") or window_min < tail:
        risk.append({"rule": "R4", "evidence": f"window {_fmt_span(window_min)} is shorter than the {tail:g}-min "
                                               "verification tail plus work (infeasible as committed)"})
    if plan and plan.get("source") == "fallback" and (risk or weak):
        risk.append({"rule": "R5", "evidence": "no valid worker plan (fallback in force) and a progress/timing risk"})
    affirmative = []
    if worker_plan:
        affirmative += [f"{mid} met at {f['met_at']} ({f['evidence'].get('kind')})"
                        for mid, f in met.items() if any(m["id"] == mid for m in milestones)]
        if grade == "p50" and shown.get("p90_end") is not None and float(shown["p90_end"]) <= due_s:
            affirmative.append(f"ETA from {eta.get('units_done')} completed unit(s): P90 fits the due time")
    if ready:
        affirmative.append(f"candidate ready at {str(verdict.get('sha'))[:12]}")
    if strong:
        status, basis, reasons = "unlikely", "evidence", strong + risk + weak
    elif risk or weak:
        status, reasons = "at_risk", risk + weak
        basis = "evidence" if risk else "weak"
    elif affirmative:
        status, basis, reasons = "on_track", "evidence", [{"rule": "OK", "evidence": a} for a in affirmative]
    else:
        status, basis = "at_risk", "none"
        reasons = [{"rule": "E0", "evidence": "estimating: " + ("no adopted worker plan yet" if not worker_plan
                                                                else "no progress evidence yet")}]
    if grade in ("none", "stale") and basis != "none":
        basis = f"{basis} (milestones only: ETA {grade})"
    label = LABEL[status] + (" · estimating" if basis == "none" else " (early signal)" if basis == "weak" else "")
    phase = "candidate_ready" if ready else ("passed" if now >= due else "running")
    return {"status": status, "label": label, "basis": basis, "reasons": reasons, "affirmative": bool(affirmative),
            "phase": phase, "commitment_rev": rec["rev"], "plan_rev": (plan or {}).get("plan_rev")}


def apply_hysteresis(prev: dict | None, cand: dict, now: datetime, *, met_since: bool) -> tuple[dict, str | None]:
    """Pure: the status to persist and the transition cause (None = unchanged)."""
    at = _iso(now)
    if prev is None or prev.get("commitment_rev") != cand["commitment_rev"]:
        cause = "initial" if prev is None else f"commitment revised by {owner.name()} (rev {cand['commitment_rev']})"
        return {**cand, "since": at, "last_assessed_at": at, "clean": [], "last_improved_at": None}, cause
    out = {**prev, "last_assessed_at": at, "phase": cand["phase"], "plan_rev": cand["plan_rev"]}
    if ORDER[cand["status"]] > ORDER[prev["status"]]:
        return {**cand, "since": at, "last_assessed_at": at, "clean": [], "last_improved_at":
                prev.get("last_improved_at")}, "worsened"
    if ORDER[cand["status"]] == ORDER[prev["status"]]:
        out.update({k: cand[k] for k in ("reasons", "basis", "label", "affirmative")})
        out["clean"] = []
        return out, None
    clean = [c for c in prev.get("clean") or []] + [at]
    spaced = len(clean) >= 2 and (_utc(clean[-1]) - _utc(clean[0])).total_seconds() >= CLEAN_GAP_S
    last_imp = _utc(prev.get("last_improved_at"))
    rate_ok = last_imp is None or (now - last_imp).total_seconds() >= IMPROVE_EVERY_S
    if (met_since or spaced) and rate_ok:
        return {**cand, "since": at, "last_assessed_at": at, "clean": [], "last_improved_at": at}, \
            ("improved: milestone met" if met_since else "improved: two clean assessments")
    out["clean"] = clean[-2:] if spaced else clean
    return out, None


# -- the one writer -----------------------------------------------------------------------------------------

def _record_error(store: Store, job_id: str, exc: Exception) -> None:
    msg = f"{type(exc).__name__}: {str(exc)[:160]}"
    try:
        with store.immediate() as tx:
            seen = tx.kv_get(job_id, K_ERRORS) or []
            if msg in seen:
                return
            tx.kv_set(job_id, K_ERRORS, (seen + [msg])[-20:])
            tx.append_event(job_id, what="deadline_error", evidence={"error": msg, "kept": "last good status"},
                            rule="deadline_supervisor_exception", judgment=Judgment.code)
    except Exception:  # noqa: BLE001 - the error path itself never raises
        pass


def _record_facts(store: Store, job_id: str, plan: dict, rec: dict, now: datetime, *, hist, structure) -> bool:
    """Milestone met (first evidence) and missed (target + grace without evidence) facts, exactly once each."""
    changed = False
    start = deadline_plan.commitment_start(store, job_id)
    milestones = plan.get("milestones") or []
    found = {}
    for m in milestones:
        if m["id"] == deadline_plan.CANDIDATE or m.get("evidence") == "time":
            continue
        ev = deadline_evidence.milestone_evidence(m, hist=hist, structure=structure) if hist is not None or \
            structure is not None else None
        if ev:
            found[m["id"]] = ev
    verdict = store.kv_get(job_id, deadline_evidence.K_VERDICT) or {}
    active = deadline.mode(store, job_id) == "active"
    with store.immediate() as tx:
        facts = tx.kv_get(job_id, K_FACTS) or {"met": {}, "missed": {}}
        for i, m in enumerate(milestones):
            mid = m["id"]
            target = _utc(m["target_at_utc"])
            grace = grace_min(milestones, i, start)
            if mid in found and mid not in facts["met"]:
                ev = found[mid]
                late = _utc(ev["met_at"]) > target + timedelta(minutes=grace)
                facts["met"][mid] = {**ev, "plan_rev": plan.get("plan_rev"), "recorded_at": _iso(now),
                                     "target_at_utc": m["target_at_utc"], "late": late}
                tx.append_event(job_id, what="deadline_milestone_met",
                                evidence={"milestone": mid, "sha": ev["met_sha"], "met_at": ev["met_at"],
                                          "evidence": ev["evidence"], "target_at_utc": m["target_at_utc"],
                                          "late": late, "plan_rev": plan.get("plan_rev"),
                                          "commitment_rev": rec["rev"]},
                                rule="deadline_milestone_evidence", judgment=Judgment.code)
                changed = True
            if m.get("evidence") == "time":
                continue
            key = deadline_plan.slot_key(plan["commitment_rev"], mid, m["target_at_utc"])
            proven = (mid in facts["met"] and _utc(facts["met"][mid]["met_at"]) <= target + timedelta(minutes=grace))
            if mid == deadline_plan.CANDIDATE:
                proven = bool(verdict.get("ready"))
            if now >= target + timedelta(minutes=grace) and not proven and key not in facts["missed"]:
                prev_t = _utc(milestones[i - 1]["target_at_utc"]) if i else start
                facts["missed"][key] = {"milestone_id": mid, "target_at_utc": m["target_at_utc"], "grace_min": grace,
                                        "gap_min": ((target - prev_t).total_seconds() / 60.0) if prev_t else grace,
                                        "missed_at": _iso(now), "plan_rev": plan.get("plan_rev")}
                draft_key = None
                if active:
                    draft = deadline_outbox.make_draft(
                        store, job_id, key=f"miss:{key}", kind="miss", now=now,
                        title=f"\u23f1 {mid} missed", detail={"slot": key, "milestone": mid},
                        body=f"{mid} passed its {deadline_outbox._hhmm(m['target_at_utc'])} target + "
                             f"{grace:.0f} min grace with no evidence. Work continues.")
                    draft_key = draft["key"] if tx.insert_draft(job_id, draft) else None
                tx.append_event(job_id, what="deadline_milestone_missed",
                                evidence={"milestone": mid, "slot": key, "target_at_utc": m["target_at_utc"],
                                          "grace_min": round(grace, 1), "plan_rev": plan.get("plan_rev"),
                                          "commitment_rev": rec["rev"], "draft": draft_key},
                                rule="deadline_target_plus_grace", judgment=Judgment.code)
                changed = True
        due = _utc(rec["due_at_utc"])
        passed_key = f"passed:{rec['rev']}"
        if now >= due and not verdict.get("ready") and not facts.get(passed_key):
            facts[passed_key] = _iso(now)
            tx.append_event(job_id, what="deadline_passed",
                            evidence={"commitment_rev": rec["rev"], "due_at_utc": rec["due_at_utc"],
                                      "work": "continues on authorized required items; due time unchanged"},
                            rule="deadline_due_time", judgment=Judgment.code)
            changed = True
        if changed:
            tx.kv_set(job_id, K_FACTS, facts)
    return changed


def _structure(store, job_id, runner, plan):
    if not any(str(m.get("evidence", "")).startswith("units:") for m in (plan or {}).get("milestones") or []):
        return None
    from steward import eta_structure

    return eta_structure.read(store, job_id, runner)


def sweep(store: Store, job_id: str, *, now: datetime, runner=None, worktree=None, force: bool = False) -> dict | None:
    """One supervision step for a deadline job (runner tick or control-pane tick). Never raises."""
    try:
        rec = deadline.current(store, job_id)
        if rec is None or deadline.mode(store, job_id) == "off":
            return None
        job = store.get_job(job_id)
        if job is not None and machine.is_terminal(job.state):
            # a stopped/released job: only catch up the slots that came due while it was live; no new assessment
            deadline_outbox.sweep(store, job_id, now, until=min(now, _utc(job.updated_at) or now))
            return store.kv_get(job_id, K_STATUS)
        seen_rev = store.get_revision(job_id)  # the steering revision this assessment saw (stale-steer fence)
        out = deadline_plan.refresh(store, job_id, worktree=worktree, runner=runner, now=now)
        plan = deadline_plan.current_plan(store, job_id)
        changed = bool(out.get("adopted") or out.get("fallback"))
        if out.get("fallback") or out.get("rejected"):
            deadline.supervisor_steer(store, job_id, expected_revision=seen_rev,
                                      reason=("plan_missing" if out.get("fallback") else
                                                             "plan_rejected:" + out["rejected"][0].split(":")[0]),
                                      text=("commit your deadline plan (PLAN.json) now and keep working" if
                                            out.get("fallback") else "your last deadline plan revision was rejected: "
                                            + "; ".join(out["rejected"])[:200] + "; the previous plan stays in force"))
        if plan is not None:
            hist = None
            if worktree is not None and runner is not None:
                head = runner.run(["git", "rev-parse", "HEAD"], cwd=worktree)
                head_sha = (head.stdout or "").strip() if head.exit_code == 0 else ""
                if head_sha:
                    job = store.get_job(job_id)
                    from steward import questions

                    base = questions._scan_base(store, job_id, job, head_sha, runner)
                    hist = deadline_evidence.history(worktree, runner, base, head_sha)
            changed = _record_facts(store, job_id, plan, rec, now, hist=hist,
                                    structure=_structure(store, job_id, runner, plan)) or changed
        deadline_outbox.sweep(store, job_id, now)  # slots: recorded locally once (active) or marked shadow
        candidate_step(store, job_id, now)
        prev = store.kv_get(job_id, K_STATUS)
        last = _utc((prev or {}).get("last_assessed_at"))
        if (not force and not changed and prev and prev.get("commitment_rev") == rec["rev"] and last
                and (now - last).total_seconds() < ASSESS_EVERY_S):
            return prev
        new = _persist_status(store, job_id, now)
        if new.get("_worsened"):
            deadline.supervisor_steer(store, job_id, reason=f"worsened:{new['status']}:{new['since']}",
                                      expected_revision=seen_rev,
                                      text=f"deadline status is now {new['label']}: "
                                           f"{(new['reasons'] or [{}])[0].get('evidence', '')[:160]}. Keep doing "
                                           "required work; if infeasible, write tradeoffs in the plan.")
        return new
    except Exception as exc:  # noqa: BLE001 - supervision never stops a tick or ordinary dispatch
        _record_error(store, job_id, exc)
        return None


def _persist_status(store: Store, job_id: str, now: datetime) -> dict:
    snap = snapshot(store, job_id)
    cand = assess(snap, now)
    active = deadline.mode(store, job_id) == "active"
    with store.immediate() as tx:
        prev = tx.kv_get(job_id, K_STATUS)
        facts = tx.kv_get(job_id, K_FACTS) or {"met": {}, "missed": {}}
        since = _utc((prev or {}).get("since"))
        met_since = any(_utc(f.get("recorded_at")) and since and _utc(f["recorded_at"]) >= since
                        for f in (facts.get("met") or {}).values())
        new, cause = apply_hysteresis(prev, cand, now, met_since=met_since)
        tx.kv_set(job_id, K_STATUS, new)
        worse = prev is not None and ORDER[new["status"]] > ORDER[prev["status"]]
        if cause is not None and (prev is None or prev.get("status") != new["status"] or
                                  prev.get("commitment_rev") != new["commitment_rev"]):
            warning = _warning(store, tx, job_id, prev, new, now) if worse and active else None
            ev = tx.append_event(job_id, what="deadline_status_changed",
                                 evidence={"from": (prev or {}).get("status"), "to": new["status"],
                                           "label": new["label"], "reasons": new["reasons"], "basis": new["basis"],
                                           "cause": cause, "commitment_rev": new["commitment_rev"],
                                           "plan_rev": new.get("plan_rev"), "warning": warning},
                                 rule="deadline_status_rules", judgment=Judgment.code)
            if warning and warning.get("draft"):
                tx.conn.execute("UPDATE deadline_outbox SET event_seq = ? WHERE draft_key = ?", (ev.seq,
                                                                                               warning["draft"]))
    return {**new, "_worsened": bool(worse and active)}


def _warning(store: Store, tx, job_id: str, prev: dict, new: dict, now: datetime) -> dict:
    """One local warning draft per worsening (DL-11): a 15-min cooldown between At risk warnings; a change into
    Unlikely is never delayed. The status event is recorded either way."""
    last = tx.conn.execute("SELECT created_at FROM deadline_outbox WHERE job_id = ? AND kind = 'warning' ORDER BY "
                           "created_at DESC LIMIT 1", (job_id,)).fetchone()
    if last and new["status"] != "unlikely" and \
            (now - _utc(last["created_at"])).total_seconds() < WARNING_COOLDOWN_S:
        return {"draft": None, "suppressed": "cooldown (15 min); the status change is still recorded"}
    why = (new["reasons"] or [{}])[0]
    draft = deadline_outbox.make_draft(
        store, job_id, key=f"warn:{new['commitment_rev']}:{new['status']}:{new['since']}", kind="warning", now=now,
        title=f"\u23f1 {new['label']} (was {LABEL[prev['status']]})",
        body=f"{why.get('rule', '')}: {why.get('evidence', '')}", detail={"from": prev["status"], "to": new["status"],
                                                                        "reasons": new["reasons"]})
    return {"draft": draft["key"] if tx.insert_draft(job_id, draft) else None}


def sweep_all(store: Store, runner=None, *, now: datetime | None = None) -> int:
    """The control pane's periodic backstop: every job with a deadline in force, any state (paused/needs-human too)."""
    import json

    now = now or deadline.utcnow()
    n = 0
    rows = store._conn.execute("SELECT job_id, value_json FROM kv WHERE key = ?", (deadline.K_COMMIT,)).fetchall()
    for row in rows:
        try:
            doc = json.loads(row["value_json"])
        except ValueError:
            continue
        if deadline._current_of(doc) is None:
            continue
        wt = store.home / "worktrees" / row["job_id"]
        sweep(store, row["job_id"], now=now, runner=runner, worktree=wt if wt.is_dir() else None)
        n += 1
    return n


# -- pure read projection ----------------------------------------------------------------------------------

def project(store: Store, job_id: str, now: datetime | None = None) -> dict | None:
    """The canonical deadline object for every read surface (API/MCP get_job, CLI, pane, phone row). Pure: reads
    persisted state only; `now` affects display text (time left / overrun banner) and nothing else."""
    base = deadline.view(store, job_id)
    if base is None:
        return None
    now = now or deadline.utcnow()
    status = store.kv_get(job_id, K_STATUS)
    plan = deadline_plan.current_plan(store, job_id)
    facts = store.kv_get(job_id, K_FACTS) or {"met": {}, "missed": {}}
    verdict = store.kv_get(job_id, deadline_evidence.K_VERDICT)
    slots = deadline_plan.targets(store, job_id)
    out = {**base, "status": None, "sign": None, "reasons": [], "basis": None, "banner": None, "plan": None,
           "milestones": [], "slots": [{k: s.get(k) for k in ("key", "milestone_id", "target_at_utc",
                                                               "created_by_plan_rev", "source")} for s in slots],
           "candidate": None, "next_target": None, "last_target": None, "left": None}
    if verdict:
        out["candidate"] = {"sha": verdict.get("sha"), "ready": verdict.get("ready"), "reasons": verdict.get("reasons"),
                            "items": [{k: r.get(k) for k in ("item", "class", "outcome", "reasons", "paths", "tests",
                                                             "results")} for r in verdict.get("items") or []],
                            "optional_dropped": verdict.get("optional_dropped", [])}
    if not base["active"]:
        return out
    if plan:
        out["plan"] = {k: plan.get(k) for k in ("plan_rev", "sha", "source", "commitment_rev", "infeasible",
                                                 "adaptations", "dropped", "tradeoffs", "approach")}
        for m in plan.get("milestones") or []:
            met = (facts.get("met") or {}).get(m["id"])
            missed = [v for v in (facts.get("missed") or {}).values() if v["milestone_id"] == m["id"]
                      and v["target_at_utc"] == m["target_at_utc"]]
            out["milestones"].append({"id": m["id"], "title": m["title"], "target_at_utc": m["target_at_utc"],
                                      "evidence": m.get("evidence"), "met_at": met.get("met_at") if met else None,
                                      "met_sha": met.get("met_sha") if met else None, "late": (met or {}).get("late"),
                                      "missed": bool(missed)})
        upcoming = sorted(m["target_at_utc"] for m in out["milestones"] if _utc(m["target_at_utc"]) > now)
        past = sorted(m["target_at_utc"] for m in out["milestones"] if _utc(m["target_at_utc"]) <= now)
        nxt = next((m for m in out["milestones"] if upcoming and m["target_at_utc"] == upcoming[0]), None)
        lst = next((m for m in out["milestones"] if past and m["target_at_utc"] == past[-1]), None)
        out["next_target"] = {"id": nxt["id"], "target_at_utc": nxt["target_at_utc"]} if nxt else None
        out["last_target"] = {"id": lst["id"], "target_at_utc": lst["target_at_utc"], "met": bool(lst["met_at"]),
                              "missed": lst["missed"]} if lst else None
    due = _utc(base["due_at_utc"])
    if status:
        out.update(status=status.get("status"), sign=status.get("label"), reasons=status.get("reasons") or [],
                   basis=status.get("basis"), assessed_at=status.get("last_assessed_at"))
    else:
        out.update(status="at_risk", sign="At risk · estimating", basis="none",
                   reasons=[{"rule": "E0", "evidence": "estimating: not assessed yet"}])
    out["notices"] = deadline_outbox.outbox_view(store, job_id)
    out["jev_labels"] = {k: v for k, v in (store.kv_get(job_id, deadline.K_JEV) or {}).items() if k != "records"} or None
    ready = bool((verdict or {}).get("ready"))
    if now >= due and not ready:
        out["banner"] = f"Deadline passed · {_fmt_span((now - due).total_seconds() / 60)} over"
    elif ready:
        out["banner"] = f"Candidate ready · waiting for {owner.poss()} merge decision"
    else:
        out["left"] = _fmt_span((due - now).total_seconds() / 60) + " left"
    return out


def line(store: Store, job_id: str, now: datetime | None = None) -> str | None:
    """One short line (CLI status/explain, pane, phone row): sign or banner, reason, last/next target."""
    p = project(store, job_id, now)
    if p is None:
        return None
    if not p["active"]:
        return f"deadline cleared (rev {p['rev']}) · merge: human only"
    head = p["banner"] or f"{p['sign']} · due {p['due_local']} (rev {p['rev']}) · {p['left']}"
    why = (p["reasons"] or [{}])[0].get("evidence") if p["reasons"] else None
    parts = [head]
    if why and not p["banner"]:
        parts.append(why[:90])
    if p.get("next_target"):
        parts.append(f"next {p['next_target']['id']} {p['next_target']['target_at_utc'][11:16]}Z")
    return " · ".join(parts)


def render(store: Store, job_id: str, now: datetime | None = None) -> str:
    """`steward deadline JOB`: the commitment, sign and reasons, plan, milestones and every slot (pure read)."""
    p = project(store, job_id, now)
    if p is None:
        return f"{job_id[:8]}: no deadline"
    lines = [deadline.render(store, job_id)]
    if not p["active"]:
        return "\n".join(lines)
    lines.append(f"status: {p['banner'] or p['sign']} (basis {p['basis']})" + (f" · {p['left']}" if p["left"] else ""))
    lines += [f"  {r['rule']}: {r['evidence']}" for r in p["reasons"][:6]]
    plan = p["plan"]
    if plan:
        lines.append(f"plan: rev {plan['plan_rev']} ({plan['source']}{', infeasible window' if plan['infeasible'] else ''})"
                     f" from {str(plan['sha'])[:12]}")
        for m in p["milestones"]:
            state = (f"met {m['met_at']}{' (late)' if m['late'] else ''}" if m["met_at"] else
                     ("MISSED" if m["missed"] else "open"))
            lines.append(f"  {m['id']:<10} target {m['target_at_utc']} [{m['evidence']}] {state} — {m['title']}")
        for a in plan.get("adaptations") or []:
            lines.append(f"  adaptation: {a.get('kind')} {a.get('detail', '')} ({a.get('reason', '')})")
        if plan.get("dropped"):
            lines.append(f"  dropped (owner-optional): {', '.join(plan['dropped'])}")
        for t in plan.get("tradeoffs") or []:
            lines.append(f"  tradeoff (worker-planned): {t}")
    else:
        lines.append("plan: none adopted yet (fallback after the plan window)")
    if p["slots"]:
        lines.append("target slots (kept across plan revisions):")
        lines += [f"  {s['key']} (plan rev {s['created_by_plan_rev']}, {s['source']})" for s in p["slots"]]
    if p["candidate"]:
        c = p["candidate"]
        lines.append(f"candidate {str(c['sha'])[:12]}: {'READY' if c['ready'] else 'acceptance unresolved'}")
        lines += [f"  {r['item']} [{r['class']}] {r['outcome']}" + (f" — {'; '.join(r['reasons'])}" if r.get("reasons")
                                                                     else "") for r in c["items"]]
    return "\n".join(lines)


# -- candidate report and calibration (DL-14, DL-15) ----------------------------------------------------------

CALIBRATION_FILE = "deadline-calibration.json"


def report(store: Store, job_id: str) -> dict | None:
    """Everything the owner needs for the merge decision, from stored facts only (pure read)."""
    from steward import questions as questions_module

    base = deadline.view(store, job_id)
    if base is None:
        return None
    verdict = store.kv_get(job_id, deadline_evidence.K_VERDICT) or {}
    facts = store.kv_get(job_id, K_FACTS) or {"met": {}, "missed": {}}
    plan = deadline_plan.current_plan(store, job_id) or {}
    events = store.events(job_id)
    checks = next((e.evidence for e in reversed(events) if e.rule == "checks_pass_at_sha"), None)
    acceptance = next(({"what": e.what, "reasons": e.evidence.get("reasons")} for e in reversed(events)
                       if e.rule == "acceptance_unresolved"), None)
    due = _utc((deadline._current_of(deadline.commitment(store, job_id)) or {}).get("due_at_utc"))
    ready_at = _utc(verdict.get("at")) if verdict.get("ready") else None
    slots_out = []
    for s in deadline_plan.targets(store, job_id):
        met = (facts.get("met") or {}).get(s["milestone_id"])
        slots_out.append({"slot": s["key"], "milestone": s["milestone_id"], "target_at_utc": s["target_at_utc"],
                          "plan_rev": s.get("created_by_plan_rev"), "met_at": (met or {}).get("met_at"),
                          "missed": s["key"] in (facts.get("missed") or {})})
    try:
        qsum = questions_module.summary(questions_module.load(store, job_id))
    except Exception:  # noqa: BLE001
        qsum = None
    return {
        "job_id": job_id, "commitment_revisions": base["revisions"], "due_at_utc": base["due_at_utc"],
        "candidate_sha": verdict.get("sha"), "ready": bool(verdict.get("ready")),
        "acceptance": "candidate ready" if verdict.get("ready") else "acceptance unresolved",
        "unresolved": verdict.get("reasons") or [],
        "items": [{k: r.get(k) for k in ("item", "class", "sha", "paths", "tests", "results", "outcome", "reasons")}
                  for r in verdict.get("items") or []],
        "checks": checks, "closeout": acceptance,
        "milestones": slots_out, "plan_revisions": len(deadline_plan.plan_doc(store, job_id)["history"]),
        "adaptations": [e.evidence for e in events if e.what == "deadline_adaptation"],
        "optional_dropped": verdict.get("optional_dropped") or plan.get("dropped") or [],
        "tradeoffs": plan.get("tradeoffs") or [],
        "open_questions": qsum,
        "on_time": (ready_at <= due) if (ready_at and due) else None,
        "late_by_min": round((ready_at - due).total_seconds() / 60, 1) if ready_at and due and ready_at > due else 0,
        "merge": f"{owner.poss()} decision: `steward accept`, then `steward incorporate` (never automatic)",
        "jev_labels": store.kv_get(job_id, "deadline_jev_labels"),
    }


def render_report(rep: dict | None) -> str:
    if rep is None:
        return "no deadline"
    lines = [f"candidate {str(rep['candidate_sha'])[:12] if rep['candidate_sha'] else '(none yet)'}: {rep['acceptance']}"
             + ("" if rep["on_time"] is None else (" · on time" if rep["on_time"] else f" · late by {rep['late_by_min']} min")),
             "required/optional items:"]
    for it in rep["items"]:
        lines.append(f"  {it['item']} [{it['class']}] {it['outcome']} at {str(it.get('sha'))[:12]}: paths "
                     f"{it.get('paths')} tests {it.get('results')}" + (f" — {'; '.join(it['reasons'])}" if it.get("reasons")
                                                                       else ""))
    lines += [f"unresolved: {u}" for u in rep["unresolved"]]
    lines.append(f"checks: {rep['checks']}")
    lines.append("milestones (planned vs actual):")
    lines += [f"  {m['slot']} met {m['met_at'] or '-'}{' MISSED' if m['missed'] else ''}" for m in rep["milestones"]]
    lines.append("commitment revisions: " + "; ".join(f"rev {r['rev']} {r['action']} by {r['set_by']} ({r['reason']})"
                                                       for r in rep["commitment_revisions"]))
    if rep["adaptations"]:
        lines.append("adaptations: " + "; ".join(f"{a.get('kind')} {a.get('detail', '')}" for a in rep["adaptations"]))
    if rep["optional_dropped"]:
        lines.append(f"optional dropped: {', '.join(rep['optional_dropped'])}")
    lines.append(f"merge: {rep['merge']}")
    return "\n".join(lines)


def _calibration_path(store: Store):
    return store.home / CALIBRATION_FILE


def load_calibration(store: Store) -> list[dict]:
    try:
        rows = json.loads(_calibration_path(store).read_text())
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []  # missing or corrupt: start empty, never crash


def record_calibration(store: Store, job_id: str, *, outcome: str) -> dict | None:
    """One descriptive row per finished deadline job (atomic write; keyed by job, so re-recording replaces it).
    Display only: thresholds change only by a human config revision (A-DL-2)."""
    rec = deadline._current_of(deadline.commitment(store, job_id))
    start = deadline_plan.commitment_start(store, job_id)
    if rec is None or start is None:
        return None
    facts = store.kv_get(job_id, K_FACTS) or {"met": {}}
    plan = deadline_plan.current_plan(store, job_id) or {}
    verdict = store.kv_get(job_id, deadline_evidence.K_VERDICT) or {}

    def off(text):
        dt = _utc(text)
        return round((dt - start).total_seconds() / 60, 1) if dt else None

    from steward import eta_prior

    job = store.get_job(job_id)
    row = {"job": job_id, "kind": eta_prior.job_kind(job.goal) if job else "other",
           "jev_kind": (store.kv_get(job_id, "deadline_jev_labels") or {}).get("kind"),
           "window_min": off(rec["due_at_utc"]),
           "milestones": [{"id": m["id"], "planned_offset_min": off(m["target_at_utc"]),
                           "actual_offset_min": off(((facts.get("met") or {}).get(m["id"]) or {}).get("met_at"))}
                          for m in plan.get("milestones") or []],
           "candidate_at_offset_min": off(verdict.get("at")) if verdict.get("ready") else None, "outcome": outcome}
    rows = [r for r in load_calibration(store) if r.get("job") != job_id] + [row]
    path = _calibration_path(store)
    tmp = path.with_suffix(f".{os.getpid()}.{os.urandom(4).hex()}.tmp")
    tmp.write_text(json.dumps(rows, indent=1, sort_keys=True))
    os.replace(tmp, path)
    return row


def calibration_summary(store: Store, kind: str | None = None) -> dict:
    """Sample size and observed ranges only (no factor is applied anywhere)."""
    rows = [r for r in load_calibration(store) if kind is None or r.get("kind") == kind]
    ratios = []
    for r in rows:
        for m in r.get("milestones") or []:
            if m.get("planned_offset_min") and m.get("actual_offset_min") is not None:
                ratios.append(m["actual_offset_min"] / m["planned_offset_min"])
    return {"n_jobs": len(rows), "n_milestones": len(ratios),
            "observed_ratio_range": [round(min(ratios), 2), round(max(ratios), 2)] if ratios else None,
            "note": "descriptive only; thresholds change only by a human config revision"}


def candidate_step(store: Store, job_id: str, now: datetime) -> dict | None:
    """When the candidate is READY and the job waits for the owner: one local report draft (active mode) and the
    calibration row. Exactly once per candidate SHA."""
    verdict = store.kv_get(job_id, deadline_evidence.K_VERDICT) or {}
    job = store.get_job(job_id)
    if not verdict.get("ready") or job is None or job.state not in (JobState.NEEDS_HUMAN, JobState.ACCEPTED):
        return None
    with store.immediate() as tx:  # one report per candidate SHA across racing sweeps
        if tx.kv_get(job_id, "deadline_candidate_reported") == verdict.get("sha"):
            return None
        tx.kv_set(job_id, "deadline_candidate_reported", verdict.get("sha"))
    rep = report(store, job_id)
    try:
        record_calibration(store, job_id, outcome="met" if rep.get("on_time") else "late")
    except Exception as exc:  # noqa: BLE001 - calibration is descriptive; the report must still be recorded
        _record_error(store, job_id, exc)
    evidence = {"sha": verdict.get("sha"), "on_time": rep.get("on_time"), "late_by_min": rep.get("late_by_min"),
                "items": [{"item": i["item"], "outcome": i["outcome"]} for i in rep["items"]],
                "optional_dropped": rep["optional_dropped"], "merge": "waiting for the owner"}
    if deadline.mode(store, job_id) == "active":
        draft = deadline_outbox.make_draft(
            store, job_id, key=f"candidate:{verdict.get('sha')}", kind="candidate", now=now,
            title="⏱ candidate ready",
            body=(f"Every required item proven at {str(verdict.get('sha'))[:8]}; "
                  f"{'on time' if rep.get('on_time') else 'late'}. Review, then accept/incorporate yourself."),
            detail={"sha": verdict.get("sha")})
        store.append_event_with_outbox(job_id, what="deadline_candidate_report", evidence=evidence,
                                       rule="deadline_candidate_report", judgment=Judgment.code, draft=draft)
    else:
        store.append_event(job_id, what="deadline_candidate_report", evidence=evidence,
                           rule="deadline_candidate_report", judgment=Judgment.code)
    return rep
