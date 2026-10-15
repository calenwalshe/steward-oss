"""Combined-SHA closeout for a fleet job (Phase 28, FLEET-13/14).

The only thing a fleet job may present for human review is the final integration SHA recorded by the
Phase 28-01 ledger. `FleetCloseout.run` refuses to look at lane-local checks or worker claims: it requires
every required graph task to have an integrated, current, evidenced ledger row, every lane process
confirmed stopped, every steering receipt acknowledged and no live integration/repair writer; then it runs
the existing baseline-relative `verify` and `weakening.detect` at exactly that SHA in the integration
worktree. Only a fully passing run appends ONE immutable `fleet candidate recorded` event (per final SHA)
and routes the job to the human (`NEEDS_HUMAN`) through `closeout.closeout`, regardless of the legacy
auto-accept setting. Any other outcome blocks candidate creation, records an actionable reason and, for
repairable failures only, requests at most one bounded repair after the integration writer is fenced.

No Git write happens here (status/diff reads and the injected check commands only).
"""

from __future__ import annotations

import json
from pathlib import Path

from steward import closeout as closeout_module
from steward import fleet_graph as fg
from steward import fleet_lease as fl
from steward import fleet_runtime as rt
from steward import weakening
from steward.discovery import discover as discover_repo
from steward.fleet_integration import path_owned
from steward.fleet_store import FleetStore
from steward.models import JobState, Judgment
from steward.store import Store
from steward.verify import verify as verify_worker

CANDIDATE_WHAT = "fleet candidate recorded"
BLOCKED_WHAT = "fleet closeout blocked"
REPAIR_WHAT = "fleet repair requested"
MAX_REPAIRS = 2
_REPAIR_ACTIVE = "fleet_repair_active"
_REPAIR_ROUNDS = "fleet_repair_rounds"
_LAST_BLOCK = "fleet_closeout_last_block"
# Reasons that more waiting resolves (nothing to repair yet); everything else is a repairable failure.
_WAITING = ("required_task_not_integrated", "no_integration", "lane_not_stopped", "integration_writer_live",
            "steering_unacknowledged", "repair_writer_live", "integration_worktree_missing")


def is_fleet(store: Store, job_id: str) -> bool:
    """True for a job with an explicit (non-implicit) fleet graph; legacy and implicit-graph jobs are not."""
    row = rt.fleet_graph_row(store, job_id)
    return row is not None and not row["implicit"]


def candidates(store: Store, job_id: str) -> list[dict]:
    out = []
    for ev in FleetStore(store).fleet_events(job_id):
        if ev["what"] == CANDIDATE_WHAT:
            out.append(json.loads(ev["evidence"]))
    return out


def projection(store: Store, job_id: str) -> dict | None:
    """Read-only: None for a legacy job; {"candidate": None} until a combined closeout passed; else the candidate."""
    if not is_fleet(store, job_id):
        return None
    cand = final_candidate(store, job_id)
    return {"candidate": None, "final_sha": None} if cand is None else {"candidate": True, **cand}


def candidate_lines(fleet: dict) -> list[str]:
    """The fleet candidate block every read surface prints (one candidate, one diff, one evidence bundle)."""
    if not fleet.get("candidate"):
        return ["fleet candidate: none (combined verification has not passed)"]
    out = [f"fleet candidate: {fleet['final_sha']} (base {fleet['base_sha']})"]
    out += [f"  {line}" for line in (fleet["diff"].get("stat") or "").splitlines()]
    out += [f"  task {t['task']}: {t['candidate_sha'][:12]} evidence {t['evidence_ref']}" for t in fleet["tasks"]]
    out += [f"  check {'ok' if c['passed'] else 'FAIL'}: {c['command']}" for c in fleet["checks"]]
    b = fleet["baseline"]
    out.append(f"  baseline: new {len(b['new_failures'])}, fixed {len(b['fixed'])}, "
               f"preexisting {len(b['preexisting_failures'])}")
    out.append(f"  weakening: {', '.join(fleet['weakening']) or 'none'}")
    return out


def final_candidate(store: Store, job_id: str) -> dict | None:
    """The latest persisted human-review candidate (None until a combined closeout passed)."""
    found = candidates(store, job_id)
    return found[-1] if found else None


class FleetCloseout:
    def __init__(self, store: Store, runner, *, base_dir: Path | str, base_sha: str | None = None,
                 discover=discover_repo, verify=verify_worker, detect=weakening.detect, max_repairs: int = MAX_REPAIRS):
        self.store = store
        self.fleet = FleetStore(store)
        self.runner = runner
        self.base_dir = Path(base_dir)
        self.base_sha = base_sha
        self.discover = discover
        self.verify = verify
        self.detect = detect
        self.max_repairs = max_repairs

    # -- helpers ----------------------------------------------------------------------------------------------

    def worktree(self, job_id: str) -> Path:
        return self.base_dir / job_id

    def _git(self, args, cwd):
        return self.runner.run(["git", *args], cwd=cwd)

    def _base(self, job_id: str, head: str | None) -> str | None:
        rows = self.store.get_checks(job_id, label="baseline")
        return rows[0]["commit_sha"] if rows else self.base_sha

    # -- structural gate (no verification yet) ----------------------------------------------------------------

    def gate(self, job_id: str) -> tuple[list[str], dict]:
        """Reasons the final SHA cannot yet be verified, plus the evidence gathered. Empty reasons = clear."""
        reasons: list[str] = []
        view = self.fleet.graph_view(job_id)
        if not view or view["mode"] != "fleet" or view["implicit"]:
            return ["not_a_fleet_job"], {}
        tasks = [t for t in view["tasks"] if t["required"]]
        integrated = self.fleet.integrated_tasks(job_id)
        rows = {r["task_id"]: r for r in self.fleet.integration_history(job_id) if r["outcome"] == "integrated"}
        head = self.fleet.integration_head(job_id)
        if head is None:
            reasons.append("no_integration: nothing integrated yet")
        evidence_tasks = []
        for t in tasks:
            tid = t["id"]
            row = rows.get(tid)
            if tid not in integrated or row is None:
                reasons.append(f"required_task_not_integrated: {tid}")
                continue
            latest = self.fleet.latest_candidate(job_id, tid)
            if row["graph_rev"] != view["graph_rev"]:
                reasons.append(f"stale_task_evidence: {tid} integrated at graph revision {row['graph_rev']}, "
                               f"current is {view['graph_rev']}")
            elif latest is None or latest["candidate_sha"] != row["candidate_sha"]:
                reasons.append(f"stale_task_evidence: {tid} has a newer candidate than its integrated one")
            if not (row["evidence_ref"] or "").strip():
                reasons.append(f"missing_required_proof: {tid}")
            evidence_tasks.append({"task": tid, "lane": row["lane_id"], "attempt": row["attempt_no"],
                                   "candidate_sha": row["candidate_sha"], "evidence_ref": row["evidence_ref"],
                                   "output_sha": row["output_sha"], "done_when": t["done_when"],
                                   "changed_paths": row["changed_paths"]})
        lanes = self.store._conn.execute("SELECT lane_id FROM fleet_lanes WHERE job_id=? ORDER BY lane_id",
                                         (job_id,)).fetchall()
        for lane in lanes:
            lease = fl.get(self.store, job_id, lane["lane_id"])
            if lease is None or lease["state"] != "stopped" or not lease.get("stop_evidence"):
                reasons.append(f"lane_not_stopped: {lane['lane_id']}")
        writer = fl.get(self.store, job_id, fl.INTEGRATION)
        if writer is not None and writer["state"] in fl.HELD:
            reasons.append(f"integration_writer_live: state {writer['state']}")
        if self.store.kv_get(job_id, _REPAIR_ACTIVE, False):
            reasons.append("repair_writer_live: a bounded repair is still in flight")
        for key, receipt in sorted(self.store.kv_get(job_id, "fleet_receipts", {}).items()):
            if receipt.get("stage") != "acknowledged":
                reasons.append(f"steering_unacknowledged: {key} ({receipt.get('stage')})")
        path = self.worktree(job_id)
        info = {"head": head, "tasks": evidence_tasks, "graph_rev": view["graph_rev"], "view": view}
        if not path.is_dir():
            reasons.append("integration_worktree_missing")
            return reasons, info
        wt_head = self._git(["rev-parse", "HEAD"], path)
        status = self._git(["status", "--porcelain"], path)
        if status.exit_code != 0 or status.stdout.strip():
            reasons.append("dirty_integration_tree")
        if head and wt_head.stdout.strip() != head:
            reasons.append(f"stale_integration_head: worktree {wt_head.stdout.strip()[:12]} != ledger {head[:12]}")
        return reasons, info

    # -- run ---------------------------------------------------------------------------------------------------

    def run(self, job_id: str, contract=None) -> dict:
        """Gate, verify at the exact final SHA, then persist one candidate or block. Returns
        {status: candidate|blocked|repair|exhausted|waiting, reasons, state}."""
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(f"unknown job {job_id}")
        reasons, info = self.gate(job_id)
        if reasons:
            return self._blocked(job_id, reasons, info)
        head, path = info["head"], self.worktree(job_id)
        base = self._base(job_id, head)
        discovery = self.discover(path)
        v = self.verify(self.store, job_id, path, discovery, self.runner)
        if v.dirty:
            reasons.append("dirty_integration_tree")
        if v.commit_sha != head:
            reasons.append(f"verified_sha_mismatch: {v.commit_sha[:12]} != {head[:12]}")
        if v.unverified:
            reasons.append(f"unverified_checks: {v.unverified_reason}")
        if v.new_failures:
            reasons.append(f"new_failures: {v.new_failures[:10]}")
        findings = []
        if not v.dirty and base:
            findings = self.detect(path, base, self.runner)
            if findings:
                reasons.append(f"test_weakening: {[f.kind for f in findings]}")
        diff_names: list[str] = []
        if base:
            res = self._git(["diff", "--name-only", f"{base}..{head}"], path)
            diff_names = sorted(x for x in res.stdout.splitlines() if x.strip()) if res.exit_code == 0 else []
            claims = [c for t in info["view"]["tasks"] for c in t["owns"]]
            open_claims = (not claims or any(fg.is_global_claim(c) for c in claims)
                           or any(not t["owns"] for t in info["view"]["tasks"]))
            if not open_claims:
                outside = [p for p in diff_names if not path_owned(p, tuple(claims))]
                if outside:
                    reasons.append("out_of_claim_paths: " + ",".join(outside[:20]))
        else:
            reasons.append("no_baseline_sha")
        if reasons:
            return self._blocked(job_id, reasons, info, verification=v)

        cand = self._persist_candidate(job_id, head, base, info, v, findings, diff_names, path)
        self.store.kv_set(job_id, _REPAIR_ACTIVE, False)
        self.store.kv_set(job_id, _LAST_BLOCK, None)
        if job.state == JobState.WORKER_FINISHED:
            state = closeout_module.closeout(self.store, job_id, v, findings, contract)
        else:
            state = job.state  # candidate persisted for review; the state machine only closes out from WORKER_FINISHED
        return {"status": "candidate", "reasons": [], "state": state.value, "candidate": cand}

    def _persist_candidate(self, job_id, head, base, info, v, findings, diff_names, path) -> dict:
        for existing in candidates(self.store, job_id):
            if existing["final_sha"] == head:
                return existing  # exactly one record per final SHA
        stat = self._git(["diff", "--stat", f"{base}..{head}"], path).stdout.strip()
        lanes = {t["task"]: t["candidate_sha"] for t in info["tasks"]}
        cand = {
            "final_sha": head, "base_sha": base, "graph_rev": info["graph_rev"],
            "diff": {"stat": stat, "files": diff_names},
            "tasks": info["tasks"], "lane_shas": lanes,
            "baseline": {"base_sha": base, "new_failures": list(v.new_failures), "fixed": list(v.fixed),
                         "preexisting_failures": list(v.preexisting_failures), "docs_only": v.docs_only},
            "weakening": [f"{f.kind}: {f.path}" for f in findings],
            "checks": [{"command": r.command, "exit_code": r.exit_code, "passed": r.exit_code == 0}
                       for r in v.results],
            "verified_sha": v.commit_sha,
            "ledger": [{"task": r["task_id"], "before": r["before_sha"], "output": r["output_sha"]}
                       for r in self.fleet.integration_history(job_id) if r["outcome"] == "integrated"],
        }
        self.fleet.append_event(job_id, what=CANDIDATE_WHAT, actor="fleet_closeout", rule="fleet_combined_closeout",
                                judgment=Judgment.code, evidence=cand, graph_rev=info["graph_rev"])
        return cand

    # -- blocking + bounded repair -----------------------------------------------------------------------------

    def _blocked(self, job_id: str, reasons: list[str], info: dict, verification=None) -> dict:
        waiting = all(r.split(":")[0] in _WAITING for r in reasons)
        sources = {c["task_id"]: c["candidate_sha"] for c in self.fleet.candidates(job_id)}
        marker = sorted(reasons)
        if self.store.kv_get(job_id, _LAST_BLOCK) != marker:  # one event per distinct block, not per tick
            self.store.kv_set(job_id, _LAST_BLOCK, marker)
            self.fleet.append_event(job_id, what=BLOCKED_WHAT, actor="fleet_closeout", rule="fleet_combined_closeout",
                                    judgment=Judgment.code, evidence={
                                        "reasons": reasons, "head": info.get("head"), "lane_shas": sources,
                                        "waiting": waiting})
        job = self.store.get_job(job_id)
        if waiting or job.state != JobState.WORKER_FINISHED:
            return {"status": "waiting" if waiting else "blocked", "reasons": reasons, "state": job.state.value}
        writer = fl.get(self.store, job_id, fl.INTEGRATION)
        if writer is not None and writer["state"] in fl.HELD:
            return {"status": "waiting", "reasons": reasons + ["repair_deferred: integration writer not fenced"],
                    "state": job.state.value}
        rounds = self.store.kv_get(job_id, _REPAIR_ROUNDS, 0) + 1
        self.store.kv_set(job_id, _REPAIR_ROUNDS, rounds)
        if rounds > self.max_repairs:
            self.store.set_state(job_id, JobState.NEEDS_HUMAN, what="fleet repair budget exhausted",
                                 evidence={"reasons": reasons, "rounds": rounds}, rule="fleet_repair_exhausted",
                                 judgment=Judgment.code)
            return {"status": "exhausted", "reasons": reasons, "state": JobState.NEEDS_HUMAN.value}
        affected = sorted(t["task"] for t in info.get("tasks", [])) or []
        self.store.kv_set(job_id, _REPAIR_ACTIVE, True)  # at most one repair writer at a time
        self.fleet.append_event(job_id, what=REPAIR_WHAT, actor="fleet_closeout", rule="fleet_bounded_repair",
                                judgment=Judgment.code,
                                evidence={"round": rounds, "max": self.max_repairs, "reasons": reasons,
                                          "affected_tasks": affected, "source_lane_shas": sources,
                                          "rerun": "combined verification at the new final SHA"})
        self.store.set_state(job_id, JobState.RUNNING, what="fleet closeout blocked; bounded repair requested",
                             evidence={"reasons": reasons, "round": rounds}, rule="fleet_bounded_repair",
                             judgment=Judgment.code)
        return {"status": "repair", "reasons": reasons, "state": JobState.RUNNING.value}

    def repair_finished(self, job_id: str) -> None:
        """The repair writer is done (fenced/stopped); the next closeout may verify the new final SHA."""
        self.store.kv_set(job_id, _REPAIR_ACTIVE, False)
