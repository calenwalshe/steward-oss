#!/usr/bin/env python3
"""Run a scratch-only owner-surface qualification against an explicitly declared disposable local target.

No production defaults exist. The target must be under ~/.cache/steward-ownership-proof/targets, contain the
`.steward-disposable` declaration, and be listed with this exact local host in a supplied allowlist. The required
owner-supplied reference is an audit annotation only; the script cannot verify approval. This harness does not contact services, launch
workers, or change the target repository; it creates an isolated Store/job and reads owner status/notices/receipts.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROOF = Path.home() / ".cache" / "steward-ownership-proof"
TARGETS = PROOF / "targets"
RUNS = PROOF / "runs"


class Refused(ValueError):
    pass


def validate_target(host: str, target: Path, allowlist_path: Path, owner_authorization: str) -> tuple[str, Path]:
    current_host = socket.gethostname()
    if not owner_authorization.strip():
        raise Refused("a nonempty operator audit reference is required")
    if host != current_host:
        raise Refused(f"this harness only runs on its current local host ({current_host})")
    allowlist_file = Path(allowlist_path).resolve()
    try:
        allowlist_file.relative_to(PROOF.resolve())
    except ValueError as exc:
        raise Refused(f"allowlist must be stored under {PROOF}") from exc
    allowlist = json.loads(allowlist_file.read_text())
    if not isinstance(allowlist, dict) or not isinstance(allowlist.get("hosts"), list) or not isinstance(
            allowlist.get("targets"), list):
        raise Refused("allowlist must contain host and target lists")
    if host not in allowlist.get("hosts", []):
        raise Refused("host is absent from the explicit allowlist")
    resolved = Path(target).resolve()
    try:
        resolved.relative_to(TARGETS.resolve())
    except ValueError as exc:
        raise Refused(f"target must be under the declared disposable root {TARGETS}") from exc
    approved = {str(Path(value).resolve()) for value in allowlist.get("targets", [])}
    if str(resolved) not in approved:
        raise Refused("target is absent from the explicit allowlist")
    marker = resolved / ".steward-disposable"
    if not marker.is_file() or marker.read_text().strip() != "DECLARED DISPOSABLE TARGET":
        raise Refused("target lacks the disposable-target declaration")
    if not (resolved / ".git").exists():
        raise Refused("declared target must be a local git repository")
    return current_host, resolved


class Recorder:
    def __init__(self):
        self.notices: list[dict] = []

    def send(self, event):
        self.notices.append({"kind": getattr(event, "kind", "unknown"), "title": getattr(event, "title", ""),
                             "body": getattr(event, "body", "")})
        return True


def qualify(host: str, target: Path, allowlist: Path, owner_authorization: str) -> dict:
    checked_host, checked_target = validate_target(host, target, allowlist, owner_authorization)
    run_id = "ownership-proof-" + uuid.uuid4().hex[:12]
    run_home = RUNS / run_id
    run_home.mkdir(parents=True, exist_ok=False)
    config_dir = run_home / "config"
    config_dir.mkdir()
    os.environ["STEWARD_HOME"] = str(run_home / "steward-home")
    os.environ["STEWARD_CONFIG_DIR"] = str(config_dir)
    for key in [key for key in os.environ if key.startswith("HERDR_")]:
        os.environ.pop(key, None)
    for key in ("META_API_KEY", "TYPESAFE_API_KEY", "TELEGRAM_BOT_TOKEN"):
        os.environ.pop(key, None)
    from steward import blockers, decision_trace, jobview, ownership
    from steward.actor import Actor, ActorRef
    from steward.contract import Contract, store_contract
    from steward.models import JobState, Judgment
    from steward.store import Store

    store = Store(run_home / "store")
    goal = "qualification scratch job: technical park stays visible without asking its owner"
    job = store.create_job(goal, str(checked_target))
    store_contract(store, job.job_id, Contract(goal=goal, done_when=["the scratch job reaches its test finish line"]))
    store.set_state(job.job_id, JobState.RUNNING, what="worker attempt started", evidence={"attempt_no": 1},
                    rule="qualification_harness", judgment=Judgment.code)
    basis_seq = store.events(job.job_id)[-1].seq
    blocker, _ = blockers.raise_blocker(store, job.job_id, kind="worker_parked_unfinished", subject=run_id,
                                        claim="scratch worker is parked pending technical review",
                                        evidence=[f"run:{run_id}"], origin_actor=ActorRef(Actor.WORKER, run_id),
                                        now=time.time(), origin_rule="qualification_harness", basis_seq=basis_seq)
    notice = Recorder()
    reconciled = ownership.reconcile_job(store, job.job_id, now=time.time(), notifier=notice)
    owner_view = ownership.owner_view(store, job.job_id)
    events = jobview.event_dicts(store.events(job.job_id))
    owner_reason = jobview.reason([], store.get_job(job.job_id).state.value, events,
                                  ownership=asdict(owner_view))
    intents = [dict(row) for row in store._conn.execute("SELECT intent_key,kind,state,reason FROM action_intents "
                                                        "WHERE job_id=? ORDER BY updated_epoch", (job.job_id,))]
    decisions = decision_trace.trace(store, job.job_id)
    receipts = [dict(row) for row in store._conn.execute("SELECT milestone,revision,created_epoch FROM delivery_receipts "
                                                         "WHERE job_id=? ORDER BY seq", (job.job_id,))]
    result = {"schema": "ownership_live_qualification_v1", "run_id": run_id, "host": checked_host,
              "target": str(checked_target), "operator_asserted_reference": owner_authorization[:120],
              "job_id": job.job_id, "blocker_id": blocker.blocker_id,
              "owner_visible": {"ownership_status": owner_view.status, "ownership_text": owner_view.text,
                                 "reason": owner_reason},
              "notices": notice.notices, "action_receipts": {"intents": intents,
              "decision_records": decisions, "delivery_receipts": receipts},
              "result": "passed" if reconciled.status == "awaiting_technical_review" and
              owner_view.status == "awaiting_technical_review" and not notice.notices else "failed",
              "limitations": ["scratch Store/job only", "no live services or worker execution",
                              "operator reference is not independently verified owner authorization",
                              "does not qualify untested production targets"]}
    path = run_home / "receipt.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    store.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="exact local hostname")
    parser.add_argument("--target", required=True, type=Path, help="declared disposable local git target")
    parser.add_argument("--allowlist", required=True, type=Path, help="JSON containing exact hosts and targets")
    parser.add_argument("--owner-authorization", required=True,
                        help="operator audit annotation; approval is not independently verified")
    args = parser.parse_args(argv)
    try:
        result = qualify(args.host, args.target, args.allowlist, args.owner_authorization)
    except (Refused, OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
