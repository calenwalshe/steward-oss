"""Read-only replay and measurement of recorded Jev decisions; labels preserve provenance."""
from __future__ import annotations

import hashlib
import json
import random
import statistics

from steward import jev_observer, jev_watch
from steward.store import Store

LIVE_GATE = {"confidence": jev_observer.FLAG_CONFIDENCE, "streak": jev_watch.STREAK_N,
             "cooldown_s": jev_watch.COOLDOWN_S}
AUTO_WINDOW_S = 600.0
ORDER_PERMUTATIONS = 3
SOURCES = ("code_outcome", "independent_model", "owner_correction")
CAVEAT = ("Replay reproduces recorded gate decisions under the recorded inputs; it does not establish that live Jev "
          "inference is deterministic or correct, nor what a whole live job would have done.")


class LabelError(ValueError): pass


def label(store: Store, receipt_id: str, label: str, *, source: str, actor, uncertainty="", evidence=(),
          question="", now) -> str:
    from steward.actor import Actor, is_owner
    if source == "code_outcome":
        from steward import jev_outcomes
        try:
            return jev_outcomes.label(store, receipt_id, label, source=source, actor=actor, uncertainty=uncertainty,
                                      evidence=evidence, question=question, now=now)
        except (KeyError, ValueError) as exc:
            raise LabelError(str(exc)) from exc
    if source not in SOURCES: raise LabelError(f"unknown label source {source!r}")
    if source == "owner_correction" and not is_owner(actor):
        raise LabelError("only the owner's own correction is an owner_correction label")
    if source == "independent_model" and actor.kind != Actor.INDEPENDENT_REVIEWER:
        raise LabelError("independent_model labels come from an independent reviewer")
    if source == "code_outcome" and actor.kind != Actor.CONTROLLER:
        raise LabelError("code_outcome labels come from the controller")
    r = store._conn.execute("SELECT lane FROM judgment_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
    if r is None: raise LabelError(f"unknown receipt {receipt_id}")
    aid = "lbl-" + hashlib.sha256(f"{receipt_id}|{source}|{actor.id}|{label}|{now}".encode()).hexdigest()[:12]
    with store.immediate() as tx:
        tx.conn.execute("INSERT INTO eval_labels (label_id,receipt_id,lane,label,source,actor_kind,actor_id,"
                        "uncertainty,evidence_json,created_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (aid, receipt_id, r["lane"], str(label)[:40], source, actor.kind.value, actor.id,
                         str(uncertainty)[:80], json.dumps([str(e)[:120] for e in evidence][:8]), now))
    return aid


def _epoch(ts):
    from datetime import datetime
    try: return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError): return None


def auto_label(store: Store, *, now: float) -> int:
    from steward.actor import Actor, ActorRef
    from steward import judgment_ledger
    from steward import jev_outcomes
    existing = {r[0] for r in store._conn.execute("SELECT receipt_id FROM eval_labels WHERE source='code_outcome'")}
    count = 0
    for r in judgment_ledger.receipts(store, lane="triage"):
        if r["status"] != "ok" or r["receipt_id"] in existing or r["created_epoch"] > now - AUTO_WINDOW_S: continue
        truth = None; ref = None
        for e in store.events(r["job_id"]):
            ts = _epoch(e.ts)
            if ts is None or not r["created_epoch"] <= ts <= r["created_epoch"] + AUTO_WINDOW_S: continue
            if e.what == "worker reported done" or ((e.rule or "").endswith("_marker") and (e.evidence or {}).get("marker")):
                truth = "finished"
            elif e.what == "worker replacement decided" and (e.evidence or {}).get("cls") == "dead": truth = "stalled"
            if truth: ref = f"seq:{e.seq}"; break
        if truth:
            # Route every automatic code label through the strict receipt/event/time/value validator.
            response = r.get("response") or {}
            answers = response.get("answers") or {}
            question = next(iter(answers)) if len(answers) == 1 else ""
            if len(answers) > 1 or not ref:
                continue
            try:
                jev_outcomes.label(store, r["receipt_id"], truth, source="code_outcome",
                                   actor=ActorRef(Actor.CONTROLLER, "judgment_eval"), evidence=[ref],
                                   question=question, now=now)
            except ValueError:
                continue
            count += 1
    return count


def _primary(r, lane):
    answers = ((r.get("response") or {}).get("answers") or {})
    a = answers.get(lane) or (next(iter(answers.values()), {}) if answers else {})
    return a.get("choice"), float(a.get("confidence") or 0)


def replay_gate(store: Store, *, lane: str, gate: dict, receipts=None) -> dict:
    from steward import judgment_ledger
    rows = receipts if receipts is not None else judgment_ledger.receipts(store, lane=lane)
    groups = {}
    for r in rows:
        if r["status"] == "ok": groups.setdefault(r["job_id"], []).append(r)
    flags, choices, flagged = 0, {}, []
    for rs in groups.values():
        streak, n, cooldown = None, 0, {}
        for r in rs:
            choice, conf = _primary(r, lane)
            if choice in (None, "ok") or conf < float(gate["confidence"]): streak, n = None, 0; continue
            n = n + 1 if choice == streak else 1; streak = choice
            if n < int(gate["streak"]) or r["created_epoch"] - cooldown.get(choice, float("-inf")) < gate["cooldown_s"]: continue
            cooldown[choice] = r["created_epoch"]; streak, n = None, 0
            flags += 1; choices[choice] = choices.get(choice, 0) + 1; flagged.append(r["receipt_id"])
    return {"flags": flags, "by_choice": choices, "flagged": flagged, "gate": dict(gate)}


def _labels(store, lane=None):
    sql, args = "SELECT * FROM eval_labels", []
    if lane: sql, args = sql + " WHERE lane=?", [lane]
    out = {}
    for r in store._conn.execute(sql + " ORDER BY created_epoch", args).fetchall(): out.setdefault(r["receipt_id"], []).append(dict(r))
    return out


def _truth(labels):
    for source in ("owner_correction", "code_outcome", "independent_model"):
        values = [r for r in labels if r["source"] == source and r["label"] != "unresolved"]
        if values: return values[-1]["label"], source
    return None, None


def _score(store, lane, gate):
    from steward import judgment_ledger
    rows = judgment_ledger.receipts(store, lane=lane); flagged = set(replay_gate(store, lane=lane, gate=gate, receipts=rows)["flagged"])
    labels = _labels(store, lane); tp = fp = misses = total = 0
    for r in rows:
        truth, _ = _truth(labels.get(r["receipt_id"], []))
        if truth is None: continue
        total += 1; choice, _ = _primary(r, lane)
        if r["receipt_id"] in flagged:
            if truth == choice: tp += 1
            else: fp += 1
        elif truth != "ok": misses += 1
    return {"true_wakes": tp, "false_wakes": fp, "misses": misses, "labelled": total,
            "coverage": total / len(rows) if rows else None}


def sweep_thresholds(store, *, lane, confidences=(.6, .7, .8, .9), streaks=(1, 2, 3)):
    rows = []
    for c in confidences:
        for s in streaks:
            gate = {**LIVE_GATE, "confidence": c, "streak": s}
            rows.append({"confidence": c, "streak": s, "flags": replay_gate(store, lane=lane, gate=gate)["flags"],
                         **_score(store, lane, gate)})
    return rows


def order_sensitivity(answer_fn, pack_questions, states, *, permutations=ORDER_PERMUTATIONS):
    rnd = random.Random(0); changed = 0
    for state in states:
        seen = set()
        for _ in range(max(1, permutations)):
            qs = json.loads(json.dumps(pack_questions))
            for q in qs.values():
                items = list((q.get("criteria") or {}).items()); rnd.shuffle(items); q["criteria"] = dict(items)
            seen.add(answer_fn(qs, state))
        changed += len(seen) > 1
    return {"states": len(states), "permutations": permutations,
            "changed_fraction": changed / len(states) if states else None}


def report(store, *, lane=None):
    from steward import judgment_ledger
    lanes = [lane] if lane else sorted(judgment_ledger.status(store)["by_lane"])
    out = {}; all_labels = _labels(store)
    for ln in lanes:
        rows = judgment_ledger.receipts(store, lane=ln); statuses = {}
        for r in rows: statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        counts = {s: 0 for s in SOURCES}; unresolved = ground = 0
        for r in rows:
            lbs = all_labels.get(r["receipt_id"], [])
            for lb in lbs: counts[lb["source"]] += 1
            ground += any(lb["source"] == "owner_correction" for lb in lbs); unresolved += not lbs
        lat = sorted(int(r["latency_ms"] or 0) for r in rows)
        out[ln] = {"receipts": len(rows), "by_status": statuses, "labels": {**counts, "unresolved": unresolved},
                   "ground_truth_labels": ground, "latency_ms": {"p50": statistics.median(lat) if lat else None,
                   "p90": lat[int(.9 * (len(lat)-1))] if lat else None}, "cost": "unknown", "gate_live": dict(LIVE_GATE),
                   **_score(store, ln, LIVE_GATE)}
    return {"lanes": out, "caveat": CAVEAT, "status": judgment_ledger.status(store)}
