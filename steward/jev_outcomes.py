"""Evidence-bound Jev lane outcome evaluation; this module reports evidence and never activates a lane."""
from __future__ import annotations

import json
from datetime import datetime
import uuid

from steward import judgment_ledger, masking
from steward.actor import Actor, ActorRef
from steward.models import Judgment
from steward.store import Store

_ALLOWED_SOURCES = {"code_outcome", "independent_model", "owner_correction"}
_LABEL_MAX = 120
_EVIDENCE_MAX = 8
REPORT_EVIDENCE_MAX = 100


def label(store: Store, receipt_id: str, value: str, *, source: str, actor: ActorRef,
          uncertainty: str = "", evidence=(), question: str = "", now: float) -> str:
    """Attach an append-only outcome label to one exact Jev receipt, retaining label provenance."""
    if source not in _ALLOWED_SOURCES:
        raise ValueError("unsupported evaluation label source")
    row = store._conn.execute("SELECT lane,job_id,response_json,created_epoch,decision_id FROM judgment_receipts WHERE receipt_id=?",
                              (receipt_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown Jev receipt {receipt_id}")
    value = str(value).strip()[:_LABEL_MAX]
    if not value:
        raise ValueError("evaluation label is required")
    if source == "owner_correction" and actor.kind != Actor.OWNER:
        raise ValueError("owner corrections require owner provenance")
    if source == "independent_model" and actor.kind in (Actor.OWNER, Actor.JEV):
        raise ValueError("independent model labels must identify an independent assessor")
    refs = [str(v)[:200] for v in evidence if str(v).strip()][:_EVIDENCE_MAX]
    if source == "code_outcome":
        if actor.kind != Actor.CONTROLLER:
            raise ValueError("code outcomes require trusted controller provenance")
        event_refs = [ref for ref in refs if ref.startswith("seq:")]
        if not event_refs:
            raise ValueError("code outcomes require a recorded event sequence reference")
        decision = store._conn.execute("SELECT run_id FROM decision_records WHERE decision_id=? AND job_id=?",
                                       (row["decision_id"], row["job_id"])).fetchone()
        expected_run = decision["run_id"] if decision else None
        if not expected_run:
            raise ValueError("code outcome receipt has no decision-run identity")
        for ref in event_refs:
            try:
                seq = int(ref.split(":", 1)[1])
            except ValueError as exc:
                raise ValueError("invalid code outcome event reference") from exc
            event = store._conn.execute("SELECT ts,what,evidence,rule FROM events WHERE seq=? AND job_id=?",
                                        (seq, row["job_id"])).fetchone()
            if event is None:
                raise ValueError("code outcome event reference is not part of this receipt's job")
            try:
                event_epoch = datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("code outcome event has no valid timestamp") from exc
            if event_epoch < float(row["created_epoch"]) or event_epoch > float(now):
                raise ValueError("code outcome event does not follow the referenced Jev receipt")
            try:
                observed = json.loads(event["evidence"] or "{}")
            except (TypeError, ValueError):
                observed = {}
            if not (observed.get("decision_id") == row["decision_id"] or observed.get("run_id") == expected_run):
                raise ValueError("code outcome event is not bound to this receipt's run or decision")
            derived = ("finished" if event["what"] == "worker reported done" or
                       (str(event["rule"] or "").endswith("_marker") and observed.get("marker")) else
                       "stalled" if event["what"] == "worker replacement decided" and observed.get("cls") == "dead"
                       else None)
            if derived is None or value != derived:
                raise ValueError("code outcome label does not match the referenced recorded outcome")
    try:
        response = json.loads(row["response_json"] or "{}")
    except (TypeError, ValueError):
        response = {}
    if not isinstance(response, dict):
        response = {}
    answers = response.get("answers") or {}
    if not isinstance(answers, dict):
        answers = {}
    question = str(question or "")[:120]
    if question and question not in answers:
        raise ValueError("label question is not present in the receipt response")
    label_id = "eval-" + uuid.uuid4().hex[:20]
    masking.arm_job(store, row["job_id"])
    with store._conn:
        store._conn.execute("INSERT INTO eval_labels(label_id,receipt_id,lane,label,question,source,actor_kind,actor_id,"
                            "uncertainty,evidence_json,created_epoch) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (label_id, receipt_id, row["lane"], value, question, source, actor.kind.value,
                             actor.id[:120] or "-", str(uncertainty)[:400], json.dumps(masking.mask_obj(refs)), now))
    return label_id


def report(store: Store, job_ids: list[str] | None = None) -> dict:
    """Summarize coverage and provenance by lane; keep unsupported metrics explicitly unknown."""
    receipts = judgment_ledger.receipts(store)
    allowed_jobs = set(job_ids) if job_ids is not None else None
    if allowed_jobs is not None:
        receipts = [r for r in receipts if r["job_id"] in allowed_jobs]
    by_receipt = {r["receipt_id"]: r for r in receipts}
    labels = store._conn.execute("SELECT * FROM eval_labels ORDER BY created_epoch,rowid").fetchall()
    if allowed_jobs is not None:
        labels = [l for l in labels if l["receipt_id"] in by_receipt]
    lanes: dict[str, dict] = {}
    for receipt in receipts:
        lane = lanes.setdefault(receipt["lane"], {"receipts": 0, "statuses": {}, "labels": {},
                                                   "matches": {}, "unlabeled": 0})
        lane["receipts"] += 1
        lane["statuses"][receipt["status"]] = lane["statuses"].get(receipt["status"], 0) + 1
    for row in labels:
        receipt = by_receipt.get(row["receipt_id"])
        if receipt is None:
            continue
        lane = lanes[receipt["lane"]]
        source = row["source"]
        lane["labels"][source] = lane["labels"].get(source, 0) + 1
        if source in ("code_outcome", "owner_correction") and row["label"] in ("miss", "missed", "false_wake"):
            category = "misses" if row["label"] in ("miss", "missed") else "false_wakes"
            categories = lane.setdefault("observed_outcomes", {}).setdefault(source, {})
            categories[category] = categories.get(category, 0) + 1
        if source in ("code_outcome", "owner_correction"):
            answers = (receipt.get("response") or {}).get("answers") or {}
            question = row["question"] or ""
            answer = answers.get(question) if question else (next(iter(answers.values())) if len(answers) == 1 else None)
            if isinstance(answer, dict):
                tally = lane["matches"].setdefault(source, {"compared": 0, "matching": 0})
                tally["compared"] += 1
                tally["matching"] += int(str(answer.get("choice")) == row["label"])
            elif len(answers) > 1:
                lane["unbound_question_labels"] = lane.get("unbound_question_labels", 0) + 1
    for lane, values in lanes.items():
        values["unlabeled"] = max(0, values["receipts"] - sum(values["labels"].values()))
        for source, tally in values["matches"].items():
            tally["agreement"] = tally["matching"] / tally["compared"] if tally["compared"] else None
        lane_receipts = {rid: r for rid, r in by_receipt.items() if r["lane"] == lane}
        lane_labels = [r for r in labels if r["receipt_id"] in lane_receipts]
        values["evaluation_evidence"] = [{"receipt_id": r["receipt_id"],
                                          "decision_id": lane_receipts[r["receipt_id"]]["decision_id"],
                                          "source": r["source"], "label": r["label"],
                                          "question": r["question"],
                                          "refs": json.loads(r["evidence_json"] or "[]")}
                                         for r in lane_labels[-REPORT_EVIDENCE_MAX:]]
        values["evaluation_evidence_omitted"] = max(0, len(lane_labels) - REPORT_EVIDENCE_MAX)
        values["activation_gate"] = {"promotion": "not_authorized_by_measurement",
                                      "rollback": "evidence_report_only",
                                      "requires_explicit_owner_activation": True}
    return {"schema": "jev_outcome_report_v1", "lanes": lanes,
            "option_order_sensitivity": "unknown_without_paired_order_experiment_labels",
            "worker_text_steering": "unknown_without_adversarial_pair_experiment_labels",
            "promotion": "not_authorized_by_measurement; separate owner-approved activation required",
            "rollback": "evidence_report_only; no automatic policy change",
            "limitations": ["independent_model labels are not human ground truth",
                            "unlabeled outcomes remain unknown", "no causal claim inferred from agreement"]}
