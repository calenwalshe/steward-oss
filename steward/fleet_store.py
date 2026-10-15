"""Normalized fleet records over `Store` (Phase 26, FLEET-02/03/05).

A thin layer: it uses the Store's connection and never changes legacy tables or
behavior. History rows are insert-only (enforced by triggers in `steward.store`).
Legacy jobs with no fleet records project as one implicit legacy task whose
model and cost are explicitly `unknown` - nothing is fabricated.
"""

from __future__ import annotations

import hashlib
import json

from steward import fleet_graph as fg
from steward import masking
from steward import fleet_registry as fr
from steward.store import Store, now_iso


class FleetStore:
    def __init__(self, store: Store, *, version_probe=None):
        self.store = store
        self._c = store._conn
        # runtime -> installed CLI version now (FLEET-27). Late-bound so tests can replace the module function.
        self._version_probe = version_probe

    def current_version(self, runtime: str) -> str | None:
        if self._version_probe is not None:
            return self._version_probe(runtime)
        from steward import fleet_versions
        return fleet_versions.current_version(runtime)

    # -- roster + qualification -------------------------------------------

    def record_roster(self, roster: fr.Roster) -> str:
        with self._c:
            self._c.execute(
                "INSERT OR IGNORE INTO fleet_roster_revisions"
                " (roster_hash, revision, schema_version, canonical_json, recorded_at) VALUES (?,?,?,?,?)",
                (roster.hash, roster.revision, roster.schema_version, roster.canonical, now_iso()),
            )
        return roster.hash

    def latest_roster(self) -> fr.Roster | None:
        row = self._c.execute(
            "SELECT canonical_json FROM fleet_roster_revisions ORDER BY rowid DESC LIMIT 1").fetchone()
        return fr.validate_roster(json.loads(row["canonical_json"])) if row else None

    def get_roster(self, roster_hash: str) -> fr.Roster | None:
        row = self._c.execute("SELECT canonical_json FROM fleet_roster_revisions WHERE roster_hash=?",
                              (roster_hash,)).fetchone()
        return fr.validate_roster(json.loads(row["canonical_json"])) if row else None

    def record_qualification(self, runtime: str, adapter_version: str, capabilities, *,
                             qualified: bool, available: bool, evidence: str = "", cli_version: str | None = None) -> int:
        """Explicit adapter qualification record (never inferred from CLI detection or roster presence)."""
        caps = sorted(set(capabilities))
        if not set(caps) <= fr.CAPABILITIES:
            raise fr.RosterError("bad_capabilities", ",".join(caps))
        with self._c:
            n = self._c.execute("SELECT COALESCE(MAX(qual_rev),0)+1 FROM fleet_qualifications WHERE runtime=?",
                                (runtime,)).fetchone()[0]
            self._c.execute(
                "INSERT INTO fleet_qualifications (runtime, qual_rev, adapter_version, capabilities_json, qualified,"
                " available, evidence, recorded_at, cli_version) VALUES (?,?,?,?,?,?,?,?,?)",
                (runtime, n, adapter_version, json.dumps(caps), int(qualified), int(available),
                 evidence, now_iso(), cli_version))
        return n

    def qualification(self, runtime: str) -> dict | None:
        row = self._c.execute("SELECT * FROM fleet_qualifications WHERE runtime=? ORDER BY qual_rev DESC LIMIT 1",
                              (runtime,)).fetchone()
        if row is None:
            return None
        return {"runtime": runtime, "qual_rev": row["qual_rev"], "adapter_version": row["adapter_version"],
                "capabilities": json.loads(row["capabilities_json"]),
                "qualified": bool(row["qualified"]), "available": bool(row["available"]),
                "cli_version": row["cli_version"]}

    def qualified_version(self, runtime: str) -> str | None:
        """The CLI version the runtime's latest capability observations were recorded against (None: unrecorded)."""
        r = self._c.execute("SELECT cli_version FROM fleet_runtime_capabilities WHERE runtime=? ORDER BY seq DESC"
                            " LIMIT 1", (runtime,)).fetchone()
        return None if r is None else r["cli_version"]

    def stale(self, runtime: str | None) -> str | None:
        """Why the runtime's qualification no longer describes the installed CLI (FLEET-27), else None. A record
        without a version predates version capture and is not version-governed."""
        if not runtime:
            return None
        was = self.qualified_version(runtime)
        if was is None:
            return None
        now = self.current_version(runtime)
        if now == was:
            return None
        return f"stale_version: qualified on {was!r}, installed CLI reports {now!r}; run `steward fleet qualify {runtime}`"

    def dispatchable(self, entry: fr.RosterEntry) -> bool:
        """Enabled AND runnable in production for every role it claims (`fr.PRODUCTION_RUNTIMES`) AND explicitly
        qualified for every claimed capability AND currently available AND qualified on the CLI version installed
        now."""
        q = self.qualification(entry.runtime)
        return bool(entry.enabled and not fr.production_gap(entry) and q and q["qualified"] and q["available"]
                    and set(entry.capabilities) <= set(q["capabilities"]) and self.stale(entry.runtime) is None)

    def roster_view(self, roster: fr.Roster | None = None) -> list[dict]:
        roster = roster or self.latest_roster()
        if roster is None:
            return []
        rows = []
        for e in roster.entries:
            q = self.qualification(e.runtime)
            rows.append({
                "id": e.id, "label": e.label, "provider": e.provider, "runtime": e.runtime, "model": e.model,
                "roles": list(e.roles), "capabilities": list(e.capabilities), "strengths": list(e.strengths),
                "enabled": e.enabled, "qualified": bool(q and q["qualified"]),
                "available": bool(q and q["available"]), "dispatchable": self.dispatchable(e),
                "no_production_adapter": fr.production_gap(e),
                "stale": self.stale(e.runtime), "qualified_version": self.qualified_version(e.runtime),
                "runtime_capabilities": sorted(self.qualified_capabilities(e.runtime)),
                "cost": "unknown", "roster_revision": roster.revision, "roster_hash": roster.hash,
            })
        return rows

    # -- job snapshots (FLEET-03) ------------------------------------------------

    def snapshot_job(self, roster: fr.Roster, *, job_id: str, pins: dict, policy: str, tech_lead: str | None,
                     limits: dict, reason: str = "") -> dict:
        """Freeze registry revision/hash, eligible IDs, pins, policy, tech lead and hard limits for a job.

        Insert-only: a second snapshot for the same job, or any later roster edit, cannot change it.
        Eligibility = enabled and explicitly qualified/available at snapshot time.
        """
        if self.store.get_job(job_id) is None:
            raise KeyError(f"unknown job {job_id}")
        if self._c.execute("SELECT 1 FROM fleet_jobs WHERE job_id=?", (job_id,)).fetchone():
            raise fr.RosterError("snapshot_exists", job_id)
        self.record_roster(roster)
        eligible = sorted(e.id for e in roster.entries if self.dispatchable(e))
        for lane, rid in sorted(pins.items()):
            if rid not in eligible:
                raise fr.RosterError("pin_not_eligible", f"{lane}={rid}")
        if tech_lead is not None:
            e = roster.get(tech_lead)
            if e is None or "tech_lead" not in e.roles or tech_lead not in eligible:
                raise fr.RosterError("tech_lead_not_eligible", tech_lead)
        snap = {
            "registry_hash": roster.hash, "registry_revision": roster.revision, "eligible": eligible,
            "pins": dict(sorted(pins.items())), "policy": policy, "tech_lead": tech_lead,
            "limits": dict(sorted(limits.items())), "reason": reason,
            "qualifications": {r: self.qualification(r) for r in sorted({e.runtime for e in roster.entries})},
        }
        body = json.dumps(snap, sort_keys=True, separators=(",", ":"))
        with self._c:
            self._c.execute(
                "INSERT INTO fleet_jobs (job_id, mode, registry_hash, registry_revision, snapshot_json,"
                " snapshot_hash, created_at) VALUES (?,?,?,?,?,?,?)",
                (job_id, "fleet", roster.hash, roster.revision, body,
                 hashlib.sha256(body.encode()).hexdigest(), now_iso()))
        return snap

    def configure_job(self, roster: fr.Roster, graph: fg.Graph, *, job_id: str, pins: dict, policy: str,
                      tech_lead: str | None, limits: dict, reason: str = "") -> dict:
        """Atomically freeze a validated explicit graph and roster snapshot for one job.

        Unlike the lower-level import helpers, this is the supported creation seam:
        roster revision, snapshot, graph revision and current-graph pointer become
        visible together or not at all. No implicit/fallback graph is dispatchable.
        """
        if graph.implicit or graph.errors or not graph.tasks or not graph.plan_blob_id:
            raise ValueError("fleet job requires a valid explicit graph bound to a committed plan blob")
        if not isinstance(pins, dict) or not isinstance(limits, dict) or not policy:
            raise fr.RosterError("bad_snapshot")
        task_ids = {t.id for t in graph.tasks}
        valid_lanes = {f"lane-{task_id}" for task_id in task_ids}
        if any(lane not in valid_lanes for lane in pins):
            raise fr.RosterError("pin_unknown_lane")
        if self.store.get_job(job_id) is None:
            raise KeyError(f"unknown job {job_id}")

        with self.store.immediate() as tx:
            conn = tx.conn
            if conn.execute("SELECT 1 FROM fleet_jobs WHERE job_id=?", (job_id,)).fetchone():
                raise fr.RosterError("snapshot_exists", job_id)
            eligible = []
            qualifications = {}
            for entry in roster.entries:
                row = conn.execute("SELECT * FROM fleet_qualifications WHERE runtime=? ORDER BY qual_rev DESC LIMIT 1",
                                   (entry.runtime,)).fetchone()
                q = None if row is None else {
                    "runtime": entry.runtime, "qual_rev": row["qual_rev"], "adapter_version": row["adapter_version"],
                    "capabilities": json.loads(row["capabilities_json"]), "qualified": bool(row["qualified"]),
                    "available": bool(row["available"]),
                }
                qualifications[entry.runtime] = q
                if (entry.enabled and not fr.production_gap(entry) and q and q["qualified"] and q["available"]
                        and set(entry.capabilities) <= set(q["capabilities"])):
                    eligible.append(entry.id)
            eligible.sort()
            eligible_workers = {e.id for e in roster.entries if "worker" in e.roles and e.id in eligible}
            if not eligible_workers:
                raise fr.RosterError("no_eligible_worker")
            for lane, roster_id in sorted(pins.items()):
                if roster_id not in eligible_workers:
                    raise fr.RosterError("pin_not_eligible", f"{lane}={roster_id}")
            if tech_lead is not None:
                entry = roster.get(tech_lead)
                if entry is None or "tech_lead" not in entry.roles or tech_lead not in eligible:
                    raise fr.RosterError("tech_lead_not_eligible", tech_lead)

            conn.execute(
                "INSERT OR IGNORE INTO fleet_roster_revisions"
                " (roster_hash, revision, schema_version, canonical_json, recorded_at) VALUES (?,?,?,?,?)",
                (roster.hash, roster.revision, roster.schema_version, roster.canonical, now_iso()),
            )
            snap = {"registry_hash": roster.hash, "registry_revision": roster.revision, "eligible": eligible,
                    "pins": dict(sorted(pins.items())), "policy": policy, "tech_lead": tech_lead,
                    "limits": dict(sorted(limits.items())), "reason": reason,
                    "qualifications": {k: qualifications[k] for k in sorted(qualifications)}}
            body = json.dumps(snap, sort_keys=True, separators=(",", ":"))
            conn.execute(
                "INSERT INTO fleet_jobs (job_id, mode, registry_hash, registry_revision, snapshot_json,"
                " snapshot_hash, created_at) VALUES (?,?,?,?,?,?,?)",
                (job_id, "fleet", roster.hash, roster.revision, body, hashlib.sha256(body.encode()).hexdigest(), now_iso()),
            )
            conn.execute(
                "INSERT INTO fleet_graph_revisions VALUES (?,?,?,?,?,?,?,?)",
                (job_id, 1, graph.plan_blob_id, graph.graph_hash, 0, "", "[]", now_iso()),
            )
            for ordinal, task in enumerate(graph.tasks):
                conn.execute(
                    "INSERT INTO fleet_tasks (job_id, graph_rev, task_id, ordinal, parent_task, objective,"
                    " done_when_json, required, priority, evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (job_id, 1, task.id, ordinal, task.parent, task.objective, json.dumps(list(task.done_when)),
                     int(task.required), task.priority, json.dumps(list(task.evidence))),
                )
                for dependency in task.depends_on:
                    conn.execute("INSERT INTO fleet_task_deps VALUES (?,?,?,?)", (job_id, 1, task.id, dependency))
                for claim in task.owns:
                    conn.execute("INSERT INTO fleet_task_claims VALUES (?,?,?,?)", (job_id, 1, task.id, claim))
            conn.execute("INSERT INTO fleet_job_state VALUES (?,?,1)", (job_id, 1))
            tx.kv_set(job_id, "fleet_capability_gate", True)
        return snap

    def get_snapshot(self, job_id: str) -> dict | None:
        row = self._c.execute("SELECT snapshot_json, snapshot_hash FROM fleet_jobs WHERE job_id=?",
                              (job_id,)).fetchone()
        return None if row is None else {**json.loads(row["snapshot_json"]), "snapshot_hash": row["snapshot_hash"]}

    def record_assignment(self, job_id: str, lane_id: str, *, roster_id: str, actor: str, reason: str,
                          policy_rule: str) -> int:
        """Insert-only assignment revision. The model must be eligible under the job's frozen snapshot;
        later disabling it does not erase the row."""
        snap = self.get_snapshot(job_id)
        if snap is None:
            raise fr.RosterError("no_snapshot", job_id)
        roster = self.get_roster(snap["registry_hash"])
        entry = roster.get(roster_id) if roster else None
        if entry is None or roster_id not in snap["eligible"]:
            raise fr.RosterError("not_eligible", roster_id)
        with self._c:
            n = self._c.execute("SELECT COALESCE(MAX(assignment_rev),0)+1 FROM fleet_assignments"
                                " WHERE job_id=? AND lane_id=?", (job_id, lane_id)).fetchone()[0]
            self._c.execute("INSERT INTO fleet_assignments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (job_id, lane_id, n, roster.hash, roster_id, entry.provider, entry.runtime,
                             entry.model, actor, reason, policy_rule, now_iso()))
        return n

    def assignments(self, job_id: str, lane_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM fleet_assignments WHERE job_id=?" + (" AND lane_id=?" if lane_id else "")
        rows = self._c.execute(q + " ORDER BY lane_id, assignment_rev", (job_id, lane_id) if lane_id else (job_id,))
        return [dict(r) for r in rows]

    # -- graphs ---------------------------------------------------------------

    def record_graph(self, job_id: str, graph_rev: int, g: fg.Graph, *, replaces: dict | None = None) -> None:
        """Insert an immutable graph revision. Required work may not disappear from the previous revision.
        `replaces` ({task_id: [child ids]}, Phase 29 split) lets a required task be replaced only by children
        that are present and required, so a split can never drop required work."""
        prev = self._c.execute("SELECT MAX(graph_rev) FROM fleet_graph_revisions WHERE job_id=? AND graph_rev<?",
                               (job_id, graph_rev)).fetchone()[0]
        if prev is not None and not g.implicit:
            now = {t.id: t for t in g.tasks}
            for row in self._c.execute("SELECT task_id FROM fleet_tasks WHERE job_id=? AND graph_rev=? AND required=1",
                                       (job_id, prev)):
                kids = (replaces or {}).get(row["task_id"])
                if kids and row["task_id"] not in now and all(k in now and now[k].required for k in kids):
                    continue
                if row["task_id"] not in now or not now[row["task_id"]].required:
                    raise ValueError(f"required task {row['task_id']} dropped or reclassified")
        with self._c:
            self._c.execute(
                "INSERT INTO fleet_graph_revisions VALUES (?,?,?,?,?,?,?,?)",
                (job_id, graph_rev, g.plan_blob_id, g.graph_hash, int(g.implicit), g.fallback_reason,
                 json.dumps(g.errors), now_iso()))
            for i, t in enumerate(g.tasks):
                self._c.execute(
                    "INSERT INTO fleet_tasks (job_id, graph_rev, task_id, ordinal, parent_task, objective,"
                    " done_when_json, required, priority, evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (job_id, graph_rev, t.id, i, t.parent, t.objective, json.dumps(list(t.done_when)),
                     int(t.required), t.priority, json.dumps(list(t.evidence))))
                for d in t.depends_on:
                    self._c.execute("INSERT INTO fleet_task_deps VALUES (?,?,?,?)", (job_id, graph_rev, t.id, d))
                for p in t.owns:
                    self._c.execute("INSERT INTO fleet_task_claims VALUES (?,?,?,?)", (job_id, graph_rev, t.id, p))

    def set_current_graph(self, job_id: str, graph_rev: int, *, expected_version: int) -> bool:
        """Compare-and-swap the mutable current-graph projection. expected_version 0 means 'none yet'."""
        with self._c:
            if expected_version == 0:
                cur = self._c.execute("INSERT OR IGNORE INTO fleet_job_state VALUES (?,?,1)", (job_id, graph_rev))
            else:
                cur = self._c.execute("UPDATE fleet_job_state SET graph_rev=?, version=version+1"
                                      " WHERE job_id=? AND version=?", (graph_rev, job_id, expected_version))
        return cur.rowcount == 1

    def current_graph(self, job_id: str) -> tuple[int, int] | None:
        r = self._c.execute("SELECT graph_rev, version FROM fleet_job_state WHERE job_id=?", (job_id,)).fetchone()
        return None if r is None else (r["graph_rev"], r["version"])

    # -- lane / attempt / integration record shapes (storage only; no scheduling) -----

    def record_lane(self, job_id: str, lane_id: str, *, task_id: str, worktree_path: str | None = None,
                    branch: str | None = None) -> None:
        with self._c:
            self._c.execute("INSERT INTO fleet_lanes (job_id, lane_id, task_id, worktree_path, branch)"
                            " VALUES (?,?,?,?,?)", (job_id, lane_id, task_id, worktree_path, branch))

    def record_attempt(self, job_id: str, lane_id: str, attempt_no: int, *, provider: str, runtime: str,
                       model: str, adapter_version: str, herdr_ref: str | None = None) -> None:
        with self._c:
            self._c.execute(
                "INSERT INTO fleet_attempts (job_id, lane_id, attempt_no, provider, runtime, model, adapter_version,"
                " herdr_ref, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, lane_id, attempt_no, provider, runtime, model, adapter_version, herdr_ref, now_iso()))

    def record_integration_batch(self, job_id: str, batch_id: str, *, graph_rev: int, inputs: list) -> None:
        with self._c:
            self._c.execute("INSERT INTO fleet_integration_batches (job_id, batch_id, graph_rev, inputs_json,"
                            " created_at) VALUES (?,?,?,?,?)", (job_id, batch_id, graph_rev, json.dumps(inputs), now_iso()))

    # -- append-only fleet event history --------------------------------------------

    def append_event(self, job_id: str, *, what: str, actor: str, rule: str, judgment, evidence: dict | None = None,
                     task_id=None, lane_id=None, attempt_no=None, graph_rev=None, assignment_rev=None,
                     registry_hash=None) -> int:
        masking.arm_job(self.store, job_id)
        with self._c:
            cur = self._c.execute(
                "INSERT INTO fleet_events (job_id, ts, what, task_id, lane_id, attempt_no, graph_rev, assignment_rev,"
                " registry_hash, actor, rule, judgment, evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, now_iso(), masking.mask(what), task_id, lane_id, attempt_no, graph_rev, assignment_rev,
                 registry_hash, actor, rule, getattr(judgment, "value", judgment),
                 json.dumps(masking.mask_obj(evidence or {}))))
        return cur.lastrowid

    def fleet_events(self, job_id: str) -> list[dict]:
        return [dict(r) for r in self._c.execute("SELECT * FROM fleet_events WHERE job_id=? ORDER BY seq", (job_id,))]

    def graph_view(self, job_id: str) -> dict | None:
        """Fleet graph if one was recorded; else the truthful legacy projection; None if no such job."""
        job = self.store.get_job(job_id)
        if job is None:
            return None
        rev = self._c.execute("SELECT * FROM fleet_graph_revisions WHERE job_id=? ORDER BY graph_rev DESC LIMIT 1",
                              (job_id,)).fetchone()
        if rev is None:
            return {"job_id": job_id, "mode": "legacy", "graph_rev": None, "graph_hash": None,
                    "reason": "legacy job: no fleet records; implicit single task/lane, model and cost unknown",
                    "tasks": [{"id": fg.IMPLICIT_ID, "objective": job.goal, "lane": "legacy", "model": "unknown",
                               "cost": "unknown", "depends_on": [], "owns": [], "required": True,
                               "status": "legacy"}]}
        r = rev["graph_rev"]
        tasks = []
        for t in self._c.execute("SELECT * FROM fleet_tasks WHERE job_id=? AND graph_rev=? ORDER BY ordinal",
                                 (job_id, r)):
            deps = [x["depends_on"] for x in self._c.execute(
                "SELECT depends_on FROM fleet_task_deps WHERE job_id=? AND graph_rev=? AND task_id=?"
                " ORDER BY depends_on", (job_id, r, t["task_id"]))]
            owns = [x["path"] for x in self._c.execute(
                "SELECT path FROM fleet_task_claims WHERE job_id=? AND graph_rev=? AND task_id=? ORDER BY path",
                (job_id, r, t["task_id"]))]
            tasks.append({"id": t["task_id"], "objective": t["objective"], "depends_on": deps, "owns": owns,
                          "required": bool(t["required"]), "priority": t["priority"], "status": t["status"],
                          "done_when": json.loads(t["done_when_json"])})
        return {"job_id": job_id, "mode": "fleet", "graph_rev": r, "graph_hash": rev["graph_hash"],
                "implicit": bool(rev["implicit"]), "reason": rev["fallback_reason"],
                "errors": json.loads(rev["errors_json"]), "tasks": tasks}

    def lanes_view(self, job_id: str) -> list[dict]:
        """Read-only: each lane's task, worktree, branch, lease state/epoch and its attempts (model as started)."""
        from steward import fleet_lease as fl

        out = []
        usage = self.lane_usage(job_id)
        for r in self._c.execute("SELECT * FROM fleet_lanes WHERE job_id=? ORDER BY lane_id", (job_id,)):
            lease = fl.get(self.store, job_id, r["lane_id"]) or {}
            attempts = [{"attempt": a["attempt_no"], "provider": a["provider"], "runtime": a["runtime"],
                         "model": a["model"], "ref": a["herdr_ref"], "started_at": a["started_at"]}
                        for a in self._c.execute("SELECT * FROM fleet_attempts WHERE job_id=? AND lane_id=?"
                                                 " ORDER BY attempt_no", (job_id, r["lane_id"]))]
            on = self.lane_runtime(job_id, r["lane_id"]) or {}
            out.append({"lane": r["lane_id"], "task": r["task_id"], "worktree": r["worktree_path"],
                        "branch": r["branch"], "state": lease.get("state", "declared"), "epoch": lease.get("epoch"),
                        "attempts": attempts, "runtime": on.get("runtime"), "model": on.get("model"),
                        "usage": usage.get(r["lane_id"], {"tokens": None, "cost_usd": None})})
        return out

    def lane_usage(self, job_id: str) -> dict:
        """lane -> {tokens, cost_usd} summed over its attempts' recorded usage; a figure no attempt reported stays
        None (unknown), never 0 (D-08)."""
        out: dict = {}
        for e in self._c.execute("SELECT lane_id, evidence FROM fleet_events WHERE job_id=? AND what='lane_usage'"
                                 " ORDER BY seq", (job_id,)):
            u = (json.loads(e["evidence"]) or {}).get("usage") or {}
            cur = out.setdefault(e["lane_id"], {"tokens": None, "cost_usd": None})
            for k in ("tokens", "cost_usd"):
                if u.get(k) is not None:
                    cur[k] = (cur[k] or 0) + u[k]
        return out

    # -- candidates + integration ledger (Phase 28, FLEET-11/12) -----------------------------------------------

    def record_candidate(self, job_id: str, task_id: str, *, graph_rev: int, lane_id: str, attempt_no: int,
                         epoch: int, candidate_sha: str, evidence_ref: str) -> bool:
        """Insert-only immutable candidate admission record. Re-recording the same (task, sha) is a no-op."""
        with self._c:
            cur = self._c.execute(
                "INSERT OR IGNORE INTO fleet_candidates (job_id, task_id, graph_rev, lane_id, attempt_no, epoch,"
                " candidate_sha, evidence_ref, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, task_id, graph_rev, lane_id, attempt_no, epoch, candidate_sha, evidence_ref, now_iso()))
        return cur.rowcount == 1

    def candidates(self, job_id: str) -> list[dict]:
        return [dict(r) for r in self._c.execute(
            "SELECT * FROM fleet_candidates WHERE job_id=? ORDER BY seq", (job_id,))]

    def latest_candidate(self, job_id: str, task_id: str) -> dict | None:
        r = self._c.execute("SELECT * FROM fleet_candidates WHERE job_id=? AND task_id=? ORDER BY seq DESC LIMIT 1",
                            (job_id, task_id)).fetchone()
        return None if r is None else dict(r)

    @staticmethod
    def _attempt_row(r) -> dict:
        d = dict(r)
        d["changed_paths"] = json.loads(d.pop("changed_paths_json"))
        d["conflict_paths"] = json.loads(d.pop("conflict_paths_json"))
        return d

    def integration_history(self, job_id: str) -> list[dict]:
        return [self._attempt_row(r) for r in self._c.execute(
            "SELECT * FROM fleet_integration_attempts WHERE job_id=? ORDER BY seq", (job_id,))]

    def terminal_receipt(self, job_id: str, idem_key: str) -> dict | None:
        r = self._c.execute(
            "SELECT * FROM fleet_integration_attempts WHERE job_id=? AND idem_key=?"
            " AND outcome IN ('integrated','conflict') ORDER BY seq LIMIT 1", (job_id, idem_key)).fetchone()
        return None if r is None else self._attempt_row(r)

    def integrated_tasks(self, job_id: str) -> dict[str, str]:
        """task_id -> output SHA of its integrated attempt."""
        return {r["task_id"]: r["output_sha"] for r in self._c.execute(
            "SELECT task_id, output_sha FROM fleet_integration_attempts WHERE job_id=? AND outcome='integrated'"
            " ORDER BY seq", (job_id,))}

    def integration_head(self, job_id: str) -> str | None:
        """Output SHA of the latest integrated attempt (the ledger's view of the integration branch tip)."""
        r = self._c.execute("SELECT output_sha FROM fleet_integration_attempts WHERE job_id=?"
                            " AND outcome='integrated' ORDER BY seq DESC LIMIT 1", (job_id,)).fetchone()
        return None if r is None else r["output_sha"]

    def record_integration_attempt(self, job_id: str, *, idem_key: str, cand: dict, before_sha: str | None,
                                   output_sha: str | None, changed_paths, conflict_paths, outcome: str,
                                   reason: str, writer_epoch: int | None,
                                   rule: str = "serial_integration_writer") -> tuple[dict, bool]:
        """Atomically append the attempt row and its fleet event in one IMMEDIATE transaction. A terminal
        outcome for an existing idempotency key returns the stored row and writes nothing
        (returns (row, False)); otherwise (row, True)."""
        masking.arm_job(self.store, job_id)
        with self.store.immediate() as tx:
            if outcome in ("integrated", "conflict"):
                prior = tx.conn.execute(
                    "SELECT * FROM fleet_integration_attempts WHERE job_id=? AND idem_key=?"
                    " AND outcome IN ('integrated','conflict') ORDER BY seq LIMIT 1", (job_id, idem_key)).fetchone()
                if prior is not None:
                    return self._attempt_row(prior), False
            cur = tx.conn.execute(
                "INSERT INTO fleet_integration_attempts (job_id, idem_key, task_id, graph_rev, lane_id, attempt_no,"
                " epoch, candidate_sha, evidence_ref, before_sha, input_sha, output_sha, changed_paths_json,"
                " conflict_paths_json, outcome, reason, writer_epoch, rule, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, idem_key, cand["task_id"], cand["graph_rev"], cand["lane_id"], cand["attempt_no"],
                 cand["epoch"], cand["candidate_sha"], cand["evidence_ref"], before_sha, cand["candidate_sha"],
                 output_sha, json.dumps(sorted(changed_paths)), json.dumps(sorted(conflict_paths)), outcome,
                 masking.mask(reason), writer_epoch, rule, now_iso()))
            tx.conn.execute(
                "INSERT INTO fleet_events (job_id, ts, what, task_id, lane_id, attempt_no, graph_rev, actor, rule,"
                " judgment, evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, now_iso(), f"integration {outcome}", cand["task_id"], cand["lane_id"], cand["attempt_no"],
                 cand["graph_rev"], "integration_writer", rule, "code",
                 json.dumps(masking.mask_obj({"before": before_sha, "input": cand["candidate_sha"],
                                              "output": output_sha, "reason": reason,
                                              "writer_epoch": writer_epoch, "idem_key": idem_key}))))
            row = tx.conn.execute("SELECT * FROM fleet_integration_attempts WHERE seq=?", (cur.lastrowid,)).fetchone()
        return self._attempt_row(row), True

    # -- runtime capability qualification (Phase 29, FLEET-18) -----------------------------------------------

    def record_runtime_capability(self, runtime: str, capability: str, *, qualified: bool, adapter_version: str,
                                  evidence: str, source: str = "probe", cli_version: str | None = None) -> int:
        """Insert-only observation for ONE capability of a runtime. Never inferred from the roster or CLI presence."""
        if capability not in fr.RUNTIME_CAPABILITIES:
            raise fr.RosterError("bad_runtime_capability", capability)
        with self._c:
            cur = self._c.execute(
                "INSERT INTO fleet_runtime_capabilities (runtime, capability, qualified, adapter_version, evidence,"
                " source, observed_at, cli_version) VALUES (?,?,?,?,?,?,?,?)",
                (runtime, capability, int(qualified), adapter_version, masking.mask(str(evidence))[:500], source,
                 now_iso(), cli_version))
        return cur.lastrowid

    def runtime_capabilities(self, runtime: str) -> dict:
        """capability -> latest observation {qualified, adapter_version, evidence, source, observed_at}."""
        out: dict = {}
        for r in self._c.execute("SELECT * FROM fleet_runtime_capabilities WHERE runtime=? ORDER BY seq", (runtime,)):
            out[r["capability"]] = {"qualified": bool(r["qualified"]), "adapter_version": r["adapter_version"],
                                    "evidence": r["evidence"], "source": r["source"], "observed_at": r["observed_at"],
                                    "cli_version": r["cli_version"]}
        return out

    def qualified_capabilities(self, runtime: str) -> set:
        return {c for c, o in self.runtime_capabilities(runtime).items() if o["qualified"]}

    def runtime_can(self, runtime: str | None, action: str) -> tuple[bool, list[str]]:
        """(allowed, missing) for one controller operation on one runtime; no runtime or no evidence -> not allowed
        when the operation needs any capability."""
        have = self.qualified_capabilities(runtime) if runtime else set()
        missing = fr.missing_capabilities(have, action)
        if missing or not fr.ACTION_REQUIREMENTS.get(action, True):
            return not missing, missing
        if self.stale(runtime):  # evidence about another CLI build proves nothing about this one (FLEET-27)
            return False, ["stale_version"]
        return True, []

    def lane_runtime(self, job_id: str, lane_id: str) -> dict | None:
        """Exact provider/runtime/model of the lane's latest attempt, else of its latest assignment."""
        r = self._c.execute("SELECT provider, runtime, model FROM fleet_attempts WHERE job_id=? AND lane_id=?"
                            " ORDER BY attempt_no DESC LIMIT 1", (job_id, lane_id)).fetchone()
        if r is None:
            rows = self.assignments(job_id, lane_id)
            r = rows[-1] if rows else None
        return None if r is None else {"provider": r["provider"], "runtime": r["runtime"], "model": r["model"]}

    # -- tech-lead proposal receipts (Phase 29, FLEET-15..17) ---------------------------------------------------

    def record_proposal(self, job_id: str, proposal_id: str, *, trigger_id: str, outcome: str, reason: str,
                        bindings: dict, actions: list, rule: str = "tech_lead_arbiter") -> tuple[dict, bool]:
        """One insert-only receipt per proposal id. A replay returns the stored receipt and writes nothing."""
        masking.arm_job(self.store, job_id)
        with self.store.immediate() as tx:
            prior = tx.conn.execute("SELECT * FROM fleet_proposals WHERE job_id=? AND proposal_id=?",
                                    (job_id, proposal_id)).fetchone()
            if prior is not None:
                return self._proposal_row(prior), False
            tx.conn.execute(
                "INSERT INTO fleet_proposals (job_id, proposal_id, trigger_id, outcome, reason, bindings_json,"
                " actions_json, rule, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, proposal_id, trigger_id, outcome, masking.mask(reason)[:300],
                 json.dumps(masking.mask_obj(bindings)), json.dumps(masking.mask_obj(actions)), rule, now_iso()))
            row = tx.conn.execute("SELECT * FROM fleet_proposals WHERE job_id=? AND proposal_id=?",
                                  (job_id, proposal_id)).fetchone()
        return self._proposal_row(row), True

    @staticmethod
    def _proposal_row(r) -> dict:
        d = dict(r)
        d["bindings"] = json.loads(d.pop("bindings_json"))
        d["actions"] = json.loads(d.pop("actions_json"))
        return d

    def proposal(self, job_id: str, proposal_id: str) -> dict | None:
        r = self._c.execute("SELECT * FROM fleet_proposals WHERE job_id=? AND proposal_id=?",
                            (job_id, proposal_id)).fetchone()
        return None if r is None else self._proposal_row(r)

    def proposals(self, job_id: str) -> list[dict]:
        return [self._proposal_row(r) for r in self._c.execute(
            "SELECT * FROM fleet_proposals WHERE job_id=? ORDER BY seq", (job_id,))]

    # -- append-only outcomes + human verdicts (Phase 29-02, FLEET-20) -----------------------------------------------

    _OUTCOME_BIND = ("task_id", "provider", "runtime", "model", "candidate_sha", "integration_sha")

    def record_outcome(self, job_id: str, lane_id: str, attempt_no: int, *, task_id: str, graph_rev=None,
                       assignment_rev=None, registry_hash=None, task_class=None, repository=None,
                       candidate_sha=None, integration_sha=None, duration_s=None, usage_source="unknown",
                       usage_confidence="none", tokens=None, cost_usd=None, conflicts=(), failures=(),
                       recovery=None, rework=None, termination=None, evidence_ref=None) -> tuple[dict, bool]:
        """Insert-only outcome of ONE attempt, bound to its exact runtime/model, candidate and integration SHAs.
        Unknown measurements stay None. A second record for the same attempt returns the stored row when its
        bindings agree and raises on any mismatch (never an update)."""
        att = self._c.execute("SELECT * FROM fleet_attempts WHERE job_id=? AND lane_id=? AND attempt_no=?",
                              (job_id, lane_id, attempt_no)).fetchone()
        if att is None:
            raise ValueError(f"outcome_unknown_attempt: {lane_id}#{attempt_no}")
        lane = self._c.execute("SELECT task_id FROM fleet_lanes WHERE job_id=? AND lane_id=?",
                               (job_id, lane_id)).fetchone()
        if lane is None or lane["task_id"] != task_id:
            raise ValueError("outcome_binding_mismatch: task")
        if candidate_sha is not None:
            c = self._c.execute("SELECT 1 FROM fleet_candidates WHERE job_id=? AND lane_id=? AND attempt_no=?"
                                " AND candidate_sha=?", (job_id, lane_id, attempt_no, candidate_sha)).fetchone()
            if c is None:
                raise ValueError("outcome_binding_mismatch: candidate_sha")
        if integration_sha is not None:
            i = self._c.execute("SELECT 1 FROM fleet_integration_attempts WHERE job_id=? AND lane_id=?"
                                " AND attempt_no=? AND outcome='integrated' AND output_sha=? AND candidate_sha=?",
                                (job_id, lane_id, attempt_no, integration_sha, candidate_sha)).fetchone()
            if i is None:
                raise ValueError("outcome_binding_mismatch: integration_sha")
        for name, v in (("tokens", tokens), ("cost_usd", cost_usd), ("duration_s", duration_s)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                raise ValueError(f"outcome_bad_measurement: {name}")
        masking.arm_job(self.store, job_id)
        row = {"task_id": task_id, "provider": att["provider"], "runtime": att["runtime"], "model": att["model"],
               "candidate_sha": candidate_sha, "integration_sha": integration_sha}
        with self.store.immediate() as tx:
            prior = tx.conn.execute("SELECT * FROM fleet_outcomes WHERE job_id=? AND lane_id=? AND attempt_no=?",
                                    (job_id, lane_id, attempt_no)).fetchone()
            if prior is not None:
                if any(prior[k] != row[k] for k in self._OUTCOME_BIND):
                    raise ValueError("outcome_binding_mismatch: duplicate")
                return self._outcome_row(prior), False
            cur = tx.conn.execute(
                "INSERT INTO fleet_outcomes (job_id, task_id, lane_id, attempt_no, assignment_rev, graph_rev,"
                " registry_hash, provider, runtime, model, adapter_version, task_class, repository, candidate_sha,"
                " integration_sha, duration_s, usage_source, usage_confidence, tokens, cost_usd, conflicts_json,"
                " failures_json, recovery_json, rework, termination, evidence_ref, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, task_id, lane_id, attempt_no, assignment_rev, graph_rev, registry_hash, att["provider"],
                 att["runtime"], att["model"], att["adapter_version"], task_class,
                 masking.mask(repository) if repository else None, candidate_sha, integration_sha, duration_s,
                 usage_source, usage_confidence, tokens, cost_usd,
                 json.dumps(masking.mask_obj(list(conflicts))[:20]), json.dumps(masking.mask_obj(list(failures))[:20]),
                 json.dumps(masking.mask_obj(recovery or {})), rework,
                 masking.mask(termination)[:200] if termination else None,
                 masking.mask(evidence_ref)[:200] if evidence_ref else None, now_iso()))
            out = tx.conn.execute("SELECT * FROM fleet_outcomes WHERE seq=?", (cur.lastrowid,)).fetchone()
        return self._outcome_row(out), True

    @staticmethod
    def _outcome_row(r) -> dict:
        d = dict(r)
        for k in ("conflicts", "failures", "recovery"):
            d[k] = json.loads(d.pop(k + "_json"))
        return d

    def outcomes(self, job_id: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM fleet_outcomes", ()
        if job_id is not None:
            q, a = q + " WHERE job_id=?", (job_id,)
        return [self._outcome_row(r) for r in self._c.execute(q + " ORDER BY seq", a)]

    HUMAN_VERDICTS = ("accepted", "rejected", "changes_requested", "abandoned")

    def record_human_outcome(self, job_id: str, *, final_sha: str, verdict: str, actor: str,
                             note: str = "") -> tuple[dict, bool]:
        """The owner's verdict, bound to the exact final candidate SHA of the job. A SHA that is not the job's
        persisted candidate, or a second differing verdict for the same SHA, is rejected (no update)."""
        from steward import fleet_closeout as fc
        if verdict not in self.HUMAN_VERDICTS:
            raise ValueError(f"human_outcome_bad_verdict: {verdict}")
        cand = fc.final_candidate(self.store, job_id)
        if cand is None or cand.get("final_sha") != final_sha:
            raise ValueError("human_outcome_binding_mismatch: final_sha")
        with self.store.immediate() as tx:
            prior = tx.conn.execute("SELECT * FROM fleet_human_outcomes WHERE job_id=? AND final_sha=?",
                                    (job_id, final_sha)).fetchone()
            if prior is not None:
                if prior["verdict"] != verdict:
                    raise ValueError("human_outcome_binding_mismatch: duplicate")
                return dict(prior), False
            tx.conn.execute("INSERT INTO fleet_human_outcomes (job_id, final_sha, verdict, actor, note, recorded_at)"
                            " VALUES (?,?,?,?,?,?)", (job_id, final_sha, verdict, actor,
                                                      masking.mask(note)[:300], now_iso()))
            row = tx.conn.execute("SELECT * FROM fleet_human_outcomes WHERE job_id=? AND final_sha=?",
                                  (job_id, final_sha)).fetchone()
        return dict(row), True

    def human_outcomes(self, job_id: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM fleet_human_outcomes", ()
        if job_id is not None:
            q, a = q + " WHERE job_id=?", (job_id,)
        return [dict(r) for r in self._c.execute(q + " ORDER BY seq", a)]

    # -- rollout evidence (Phase 29-02, FLEET-21): insert-only; written only by trusted operator code ---------------

    def record_rollout_evidence(self, kind: str, *, build_id: str | None = None, build_digest: str | None = None,
                                job_id: str | None = None, instance: dict | None = None,
                                payload: dict | None = None, expires_at: float | None = None) -> int:
        with self._c:
            cur = self._c.execute(
                "INSERT INTO fleet_rollout_evidence (kind, build_id, build_digest, job_id, instance_json,"
                " payload_json, expires_at, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                (kind, build_id, build_digest, job_id, json.dumps(masking.mask_obj(instance or {}), sort_keys=True),
                 json.dumps(masking.mask_obj(payload or {}), sort_keys=True), expires_at, now_iso()))
        return cur.lastrowid

    def rollout_evidence(self, kind: str | None = None, *, job_id: str | None = None) -> list[dict]:
        q, a, w = "SELECT * FROM fleet_rollout_evidence", [], []
        if kind:
            w.append("kind=?")
            a.append(kind)
        if job_id is not None:
            w.append("job_id=?")
            a.append(job_id)
        rows = self._c.execute(q + (" WHERE " + " AND ".join(w) if w else "") + " ORDER BY seq", a)
        out = []
        for r in rows:
            d = dict(r)
            d["instance"] = json.loads(d.pop("instance_json"))
            d["payload"] = json.loads(d.pop("payload_json"))
            out.append(d)
        return out
