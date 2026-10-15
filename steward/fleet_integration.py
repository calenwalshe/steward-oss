"""Exclusive serial integration of immutable lane candidates (Phase 28, FLEET-11/12).

Only a Store-recorded candidate (committed SHA + evidence reference) can enter the single
`IntegrationWriter`. Before any Git write it re-validates the current graph revision, candidate commit
identity, lane attempt/epoch with a confirmed process-tree stop, integrated dependencies, declared
ownership of the exact changed paths, a clean integration tree and the ledger's expected before-SHA, all
under Phase 27's exclusive integration-writer lease. Candidates apply in stable topological order then
task id. Every outcome is appended to `fleet_integration_attempts` with exact before/input/output SHAs.

The integration worktree is `<base_dir>/<job_id>` on branch `steward/<job_id>`; lanes never write it.
Lane branches and worktrees are never touched (no stash, reset or rewrite of source or lane trees); a
conflicting merge is aborted inside the integration worktree only. The integration SHA is internal: it is
not the human review candidate until Phase 28-02 verifies it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path

from steward import fleet_graph as fg
from steward import fleet_lease as fl
from steward import fleet_runtime as rt
from steward.fleet_store import FleetStore
from steward.store import Store

_SHA = re.compile(r"^[0-9a-f]{40}$")
_IDENT = ["-c", "user.name=Steward Integration", "-c", "user.email=steward-integration@localhost"]


def idem_key(job_id: str, cand: dict) -> str:
    body = "|".join(str(x) for x in (job_id, cand["task_id"], cand["graph_rev"], cand["lane_id"],
                                     cand["attempt_no"], cand["epoch"], cand["candidate_sha"]))
    return hashlib.sha256(body.encode()).hexdigest()


def topo_order(graph: fg.Graph) -> list[str]:
    """Stable topological order: repeatedly take the smallest task id whose dependencies are placed."""
    deps = {t.id: set(t.depends_on) for t in graph.tasks}
    placed: list[str] = []
    while len(placed) < len(deps):
        ready = sorted(t for t, d in deps.items() if t not in placed and d <= set(placed))
        if not ready:  # cycle (graph invalid): nothing further is orderable
            break
        placed.append(ready[0])
    return placed


def path_owned(path: str, claims: tuple[str, ...]) -> bool:
    for c in claims:
        c = c.rstrip("/")
        if path == c or path.startswith(c + "/"):
            return True
        if re.search(r"[*?\[]", c) and (fnmatch.fnmatchcase(path, c) or fnmatch.fnmatchcase(path, c + "/*")):
            return True
    return False


class IntegrationWriter:
    def __init__(self, store: Store, repo: Path | str, runner, *, base_dir: Path | str, base_sha: str,
                 holder: str = "integration-writer"):
        self.store = store
        self.fleet = FleetStore(store)
        self.repo = Path(repo)
        self.runner = runner
        self.base_dir = Path(base_dir)
        self.base_sha = base_sha
        self.holder = holder

    # -- git helpers (all through the injected runner) -------------------------------------------------------

    def _git(self, args, cwd=None):
        return self.runner.run(["git", *args], cwd=cwd or self.repo)

    def integration_path(self, job_id: str) -> Path:
        return self.base_dir / job_id

    def integration_branch(self, job_id: str) -> str:
        return f"steward/{job_id}"

    def _ensure_worktree(self, job_id: str) -> Path:
        path = self.integration_path(job_id)
        if path.exists():
            return path
        self.base_dir.mkdir(parents=True, exist_ok=True)
        res = self._git(["worktree", "add", "-b", self.integration_branch(job_id), str(path), self.base_sha])
        if res.exit_code != 0:
            raise RuntimeError(f"integration worktree failed: {res.stderr.strip()}")
        return path

    def _head(self, path: Path) -> str | None:
        res = self._git(["rev-parse", "HEAD"], path)
        return res.stdout.strip() if res.exit_code == 0 else None

    def _dirty(self, path: Path) -> bool:
        res = self._git(["status", "--porcelain"], path)
        return res.exit_code != 0 or bool(res.stdout.strip())

    def _names(self, args, cwd) -> list[str] | None:
        res = self._git(args, cwd)
        if res.exit_code != 0:
            return None
        return sorted(x for x in res.stdout.splitlines() if x.strip())

    # -- admission ---------------------------------------------------------------------------------------------

    def submit_candidate(self, job_id: str, task_id: str, *, candidate_sha: str, evidence_ref: str) -> dict:
        """Admit a lane's claimed candidate into the Store. Returns {status: admitted|rejected, reason}.
        The worker's `done` claim carries nothing authoritative: lane/attempt/epoch come from the Store lease,
        the commit identity from Git."""
        view = self.fleet.graph_view(job_id)
        if not view or view["mode"] != "fleet":
            return {"status": "rejected", "reason": "no_fleet_graph"}
        if task_id not in {t["id"] for t in view["tasks"]}:
            return {"status": "rejected", "reason": "unknown_task"}
        if not (evidence_ref or "").strip():
            return {"status": "rejected", "reason": "missing_evidence_ref"}
        if not _SHA.match(candidate_sha or ""):
            return {"status": "rejected", "reason": "not_a_commit_sha"}
        lane = self.store._conn.execute("SELECT * FROM fleet_lanes WHERE job_id=? AND task_id=?",
                                        (job_id, task_id)).fetchone()
        if lane is None:
            return {"status": "rejected", "reason": "no_lane_for_task"}
        lease = fl.get(self.store, job_id, lane["lane_id"])
        if lease is None:
            return {"status": "rejected", "reason": "no_lane_attempt"}
        kind = self._git(["cat-file", "-t", candidate_sha])
        if kind.exit_code != 0 or kind.stdout.strip() != "commit":
            return {"status": "rejected", "reason": "not_a_commit"}
        tip = self._git(["rev-parse", f"refs/heads/{lane['branch']}"])
        if tip.exit_code != 0 or tip.stdout.strip() != candidate_sha:
            return {"status": "rejected", "reason": "not_lane_branch_tip"}
        self.fleet.record_candidate(job_id, task_id, graph_rev=view["graph_rev"], lane_id=lane["lane_id"],
                                    attempt_no=lease["attempt_no"], epoch=lease["epoch"],
                                    candidate_sha=candidate_sha, evidence_ref=evidence_ref)
        rt.require_integration(self.store, job_id)
        return {"status": "admitted", "reason": "ok"}

    # -- queue -------------------------------------------------------------------------------------------------

    def integrate(self, job_id: str) -> list[dict]:
        """Apply every ready candidate serially, stable topological order then task id. Stops at the first
        blocked or conflicted candidate (the queue does not advance past it)."""
        view = self.fleet.graph_view(job_id)
        if not view or view["mode"] != "fleet":
            return []
        graph = fg.Graph(tasks=[fg.Task(id=t["id"], objective=t["objective"], done_when=tuple(t["done_when"]),
                                        depends_on=tuple(t["depends_on"]), owns=tuple(t["owns"]),
                                        required=t["required"], priority=t["priority"]) for t in view["tasks"]],
                         implicit=view["implicit"])
        results = []
        for task_id in topo_order(graph):
            cand = self.fleet.latest_candidate(job_id, task_id)
            if cand is None:
                continue
            integrated = self.fleet.integrated_tasks(job_id)
            if any(d not in integrated for d in next(t for t in graph.tasks if t.id == task_id).depends_on):
                continue  # dependency has no integrated result yet; it is not this task's turn
            res = self.integrate_candidate(job_id, task_id)
            results.append(res)
            if res["outcome"] != "integrated":
                break
        return results

    def integrate_candidate(self, job_id: str, task_id: str) -> dict:
        cand = self.fleet.latest_candidate(job_id, task_id)
        if cand is None:
            return {"outcome": "blocked", "reason": "no_candidate", "task": task_id}
        key = idem_key(job_id, cand)
        prior = self.fleet.terminal_receipt(job_id, key)
        if prior is not None:
            return {**prior, "replayed": True}
        try:
            lease = fl.acquire_integration(self.store, job_id, holder=self.holder)
        except fl.LeaseHeld as exc:
            return self._record(job_id, key, cand, None, None, [], [], "blocked", f"writer_unavailable: {exc}", None)
        epoch = lease["epoch"]
        confirmed, evidence = True, {"reason": "integration_attempt_complete"}
        try:
            result = self._apply(job_id, key, cand, epoch)
        except Exception as exc:  # noqa: BLE001 - Git may be half-applied: stay fenced until confirmed
            confirmed, evidence = False, {"reason": "writer_exception", "error": str(exc)[:200], "uncertain": True}
            result = self._record(job_id, key, cand, None, None, [], [], "blocked", f"writer_exception: {exc}"[:200],
                                  epoch)
        fl.release_integration(self.store, job_id, epoch, confirmed=confirmed, evidence=evidence)
        if result["outcome"] == "integrated":
            rt.clear_hold(self.store, job_id, task_id)
        else:
            rt.hold_downstream(self.store, job_id, task_id, result["reason"])
        return result

    def _record(self, job_id, key, cand, before, output, changed, conflicts, outcome, reason, writer_epoch) -> dict:
        row, fresh = self.fleet.record_integration_attempt(
            job_id, idem_key=key, cand=cand, before_sha=before, output_sha=output, changed_paths=changed,
            conflict_paths=conflicts, outcome=outcome, reason=reason, writer_epoch=writer_epoch)
        return {**row, "replayed": not fresh}

    def _apply(self, job_id: str, key: str, cand: dict, epoch: int) -> dict:
        sha, task_id = cand["candidate_sha"], cand["task_id"]

        def block(reason, before=None, changed=()):
            return self._record(job_id, key, cand, before, None, list(changed), [], "blocked", reason, epoch)

        # -- revision / identity / lane / dependencies (no Git write, no worktree needed) ----------------------
        cur = self.fleet.current_graph(job_id)
        view = self.fleet.graph_view(job_id)
        if cur is None or cur[0] != cand["graph_rev"] or not view or view["mode"] != "fleet":
            return block("stale_graph_revision")
        task = next((t for t in view["tasks"] if t["id"] == task_id), None)
        if task is None:
            return block("task_not_in_graph")
        lane = self.store._conn.execute("SELECT * FROM fleet_lanes WHERE job_id=? AND lane_id=?",
                                        (job_id, cand["lane_id"])).fetchone()
        tip = self._git(["rev-parse", f"refs/heads/{lane['branch']}"]) if lane else None
        if lane is None or tip.exit_code != 0 or tip.stdout.strip() != sha:
            return block("candidate_commit_changed")
        kind = self._git(["cat-file", "-t", sha])
        if kind.exit_code != 0 or kind.stdout.strip() != "commit":
            return block("candidate_commit_changed")
        lease = fl.get(self.store, job_id, cand["lane_id"])
        if lease is None or lease["epoch"] != cand["epoch"] or lease["attempt_no"] != cand["attempt_no"]:
            return block("stale_lane_attempt")
        if lease["state"] != "stopped" or not lease.get("stop_evidence"):
            return block("lane_stop_unconfirmed")
        integrated = self.fleet.integrated_tasks(job_id)
        if any(d not in integrated for d in task["depends_on"]):
            return block("dependency_not_integrated")

        # -- integration tree state ---------------------------------------------------------------------------
        path = self._ensure_worktree(job_id)
        expected = self.fleet.integration_head(job_id) or self.base_sha
        head = self._head(path)
        if self._dirty(path):
            return block("dirty_integration_tree", head)
        if head != expected:
            adopted = self._adopt_unrecorded(job_id, path, expected, head, cand, key, epoch)
            if adopted is not None:
                return adopted
            return block("stale_integration_head", head)
        for d in task["depends_on"]:
            anc = self._git(["merge-base", "--is-ancestor", integrated[d], sha])
            if anc.exit_code != 0:
                return block("candidate_not_based_on_dependencies", head)

        # -- exact diff vs. declared ownership ----------------------------------------------------------------
        mb = self._git(["merge-base", sha, head], path)
        if mb.exit_code != 0:
            return block("no_merge_base", head)
        changed = self._names(["diff", "--name-only", mb.stdout.strip(), sha], path)
        if changed is None:
            return block("diff_failed", head)
        claims = tuple(task["owns"])
        if claims and not view.get("implicit") and not any(fg.is_global_claim(c) for c in claims):
            outside = [p for p in changed if not path_owned(p, claims)]
            if outside:
                return block("out_of_claim_paths: " + ",".join(outside[:20]), head, changed)

        # -- writer fence immediately before the write --------------------------------------------------------
        mine = fl.get(self.store, job_id, fl.INTEGRATION)
        if not mine or mine["epoch"] != epoch or mine["state"] != "running":
            return block("writer_fenced", head, changed)
        if self._head(path) != expected:
            return block("stale_integration_head", self._head(path), changed)

        res = self._git([*_IDENT, "merge", "--no-ff", "--no-edit", "-m",
                         f"steward: integrate {task_id} ({sha[:12]})", sha], path)
        if res.exit_code != 0:
            conflicts = self._names(["diff", "--name-only", "--diff-filter=U"], path) or []
            self._git(["merge", "--abort"], path)
            if self._dirty(path) or self._head(path) != expected:
                raise RuntimeError("integration tree not restored after conflict")
            return self._record(job_id, key, cand, expected, None, changed, conflicts or changed, "conflict",
                                "merge_conflict", epoch)
        out = self._head(path)
        if self._dirty(path) or not out:
            raise RuntimeError("integration tree dirty after merge")
        actual = self._names(["diff", "--name-only", expected, out], path) or []
        if claims and not view.get("implicit") and not any(fg.is_global_claim(c) for c in claims) and any(
                not path_owned(p, claims) for p in actual):
            raise RuntimeError("merge result exceeds declared ownership")
        return self._record(job_id, key, cand, expected, out, actual, [], "integrated", "ok", epoch)

    def _adopt_unrecorded(self, job_id, path, expected, head, cand, key, epoch) -> dict | None:
        """Crash recovery: Git advanced by exactly this candidate's merge but the ledger row was never
        written. Adopt it (idempotent) instead of writing a second commit."""
        if not head:
            return None
        res = self._git(["rev-list", "--parents", "-n", "1", head], path)
        parts = res.stdout.split() if res.exit_code == 0 else []
        if len(parts) == 3 and parts[1] == expected and parts[2] == cand["candidate_sha"]:
            actual = self._names(["diff", "--name-only", expected, head], path) or []
            return self._record(job_id, key, cand, expected, head, actual, [], "integrated",
                                "adopted_unrecorded_merge", epoch)
        return None

    # -- writer recovery -----------------------------------------------------------------------------------------

    def recover_writer(self, job_id: str, probe) -> dict:
        """Reacquire only with confirmed-stop evidence. `probe(job_id)` returns rt.DEAD only when the prior
        writer's process is observed gone; anything else leaves the integration branch fenced."""
        lease = fl.get(self.store, job_id, fl.INTEGRATION)
        if lease is None or lease["state"] not in fl.HELD:
            return {"status": "free"}
        status = probe(job_id)
        if status == rt.DEAD:
            fl.release_integration(self.store, job_id, lease["epoch"], confirmed=True,
                                   evidence={"reason": "writer_probe_dead", "probe": status})
            return {"status": "freed", "epoch": lease["epoch"]}
        fl.release_integration(self.store, job_id, lease["epoch"], confirmed=False,
                               evidence={"reason": "writer_unconfirmed", "probe": status, "uncertain": True})
        return {"status": "fenced", "epoch": lease["epoch"], "probe": status}
