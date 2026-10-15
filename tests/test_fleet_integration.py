"""Phase 28 (FLEET-11/12): serial IntegrationWriter over immutable lane candidates (temp Git + real Store)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_fleet_graph import task
from test_fleet_lanes import JOB, World, controller, lease_of, seed

from steward import fleet_integration as fi
from steward import fleet_lease as fl
from steward import fleet_runtime as rt
from steward.fabric import Frame
from steward.fleet_store import FleetStore
from steward.runner_exec import SubprocessRunner
from steward.store import Store

TWO = [task("t1", owns=["src/a/"]), task("t2", owns=["src/b/"])]
CHAIN = [task("t1", owns=["src/a/"]), task("t2", owns=["src/b/"], depends_on=["t1"])]


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def commit_in(path, files: dict, msg="work"):
    for rel, text in files.items():
        f = Path(path) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    git(path, "add", "-A")
    git(path, "-c", "user.name=W", "-c", "user.email=w@x", "commit", "-q", "-m", msg)
    return git(path, "rev-parse", "HEAD")


class Env:
    def __init__(self, tmp_path, home, tasks, **kw):
        self.store, self.repo, _ = seed(tmp_path, home, tasks, max_lanes=2, **kw)
        self.ctl, self.world = controller(tmp_path, self.store, self.repo)
        self.writer = fi.IntegrationWriter(self.store, self.repo, SubprocessRunner(), base_dir=tmp_path / "wts",
                                           base_sha=self.ctl.base_sha())
        self.ctl.integration = self.writer
        self.fs = FleetStore(self.store)
        self.seq = 0
        self.wts = {}

    def admit(self):
        for r in self.ctl.admit(JOB):
            if r["status"] == "started":
                self.wts[r["task"]] = Path(r["worktree"])

    def finish(self, task_id, files, *, evidence="ev:1", stop=True, base=None):
        """Worker commits in its lane, sends a v2 `done` frame naming the candidate, lane stops (confirmed)."""
        lane = rt.lane_id_for(task_id)
        if base:
            git(self.wts[task_id], "merge", "--ff-only", base)
        sha = commit_in(self.wts[task_id], files)
        lease = lease_of(self.store, lane)
        self.seq += 1
        fr = Frame(v=2, seq=self.seq, job=JOB, type="done", body={"candidate_sha": sha, "evidence_ref": evidence},
                   task=task_id, lane=lane, attempt=lease["attempt_no"], sender=f"shim:{lane}", epoch=lease["epoch"])
        assert self.ctl.accept_frame(JOB, fr).accepted
        if stop:
            self.ctl.tick(JOB)
        return sha

    def head(self):
        return git(self.writer.integration_path(JOB), "rev-parse", "HEAD")


@pytest.fixture
def env2(steward_home, tmp_path):
    e = Env(tmp_path, steward_home, TWO)
    e.admit()
    return e


# ------------------------------------------------------------------------------------------ tracer

def test_two_independent_candidates_integrate_serially_with_exact_ledger(env2):
    e = env2
    s2 = e.finish("t2", {"src/b/b.py": "b\n"})
    s1 = e.finish("t1", {"src/a/a.py": "a\n"})
    base = e.ctl.base_sha()
    res = e.writer.integrate(JOB)
    assert [r["task_id"] for r in res] == ["t1", "t2"]  # topological order then task id, not arrival order
    assert all(r["outcome"] == "integrated" for r in res)
    hist = e.fs.integration_history(JOB)
    assert [h["task_id"] for h in hist] == ["t1", "t2"]
    assert hist[0]["before_sha"] == base and hist[0]["input_sha"] == s1
    assert hist[1]["before_sha"] == hist[0]["output_sha"] and hist[1]["input_sha"] == s2
    assert hist[1]["output_sha"] == e.head()
    assert hist[0]["changed_paths"] == ["src/a/a.py"] and hist[1]["changed_paths"] == ["src/b/b.py"]
    h = hist[0]
    assert (h["lane_id"], h["attempt_no"], h["epoch"], h["evidence_ref"], h["writer_epoch"]) == (
        "lane-t1", 1, 1, "ev:1", 1)
    assert git(e.writer.integration_path(JOB), "rev-parse", "--abbrev-ref", "HEAD") == f"steward/{JOB}"
    tree = git(e.writer.integration_path(JOB), "ls-files")
    assert "src/a/a.py" in tree and "src/b/b.py" in tree
    # source branches preserved
    assert git(e.repo, "rev-parse", f"steward-lanes/{JOB}/lane-t1") == s1
    assert git(e.repo, "rev-parse", f"steward-lanes/{JOB}/lane-t2") == s2
    assert fl.get(e.store, JOB, fl.INTEGRATION)["state"] == "stopped"
    assert git(e.repo, "status", "--porcelain") == ""


def test_replay_returns_recorded_receipt_without_second_commit(env2):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n"})
    first = e.writer.integrate(JOB)[0]
    head, n = e.head(), len(e.fs.integration_history(JOB))
    again = e.writer.integrate_candidate(JOB, "t1")
    assert again["replayed"] and again["output_sha"] == first["output_sha"]
    assert e.head() == head and len(e.fs.integration_history(JOB)) == n
    # a fresh Store handle + fresh writer (process restart) is also a replay
    store2 = Store(e.store.db_path.parent)
    w2 = fi.IntegrationWriter(store2, e.repo, SubprocessRunner(), base_dir=e.writer.base_dir, base_sha=e.writer.base_sha)
    assert w2.integrate_candidate(JOB, "t1")["replayed"] and e.head() == head


def test_store_rejects_duplicate_terminal_key_atomically(env2):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n"})
    r = e.writer.integrate(JOB)[0]
    cand = e.fs.latest_candidate(JOB, "t1")
    row, fresh = e.fs.record_integration_attempt(
        JOB, idem_key=fi.idem_key(JOB, cand), cand=cand, before_sha="x", output_sha="y", changed_paths=[],
        conflict_paths=[], outcome="integrated", reason="dup", writer_epoch=9)
    assert not fresh and row["output_sha"] == r["output_sha"]
    with pytest.raises(Exception, match="insert-only"):
        e.store._conn.execute("UPDATE fleet_integration_attempts SET outcome='x'")


def test_runner_routes_lane_candidate_event_to_writer(env2):
    from steward.runner import Runner
    e = env2
    sha = commit_in(e.wts["t1"], {"src/a/a.py": "a\n"})
    r = object.__new__(Runner)
    r.store, r.integration_writer = e.store, None
    assert r.route_lane_candidate(JOB, "t1", candidate_sha=sha, evidence_ref="e")["status"] == "ignored"
    r.integration_writer = e.writer
    e.world.alive.clear()
    out = r.route_lane_candidate(JOB, "t1", candidate_sha=sha, evidence_ref="e")
    assert out["status"] == "admitted"
    assert out["integrated"][0]["outcome"] == "blocked"  # lane still running: stop not confirmed
    assert e.fs.integration_history(JOB)[0]["reason"] == "lane_stop_unconfirmed"


def test_topological_order_is_stable():
    from steward import fleet_graph as fg
    g = fg.compile_graph({"schema_version": 1, "plan_blob_id": "a" * 40, "tasks": [
        task("c", depends_on=["a"], owns=["c/"]), task("b", owns=["b/"]), task("a", owns=["a/"])]},
        committed_plan_blob_id="a" * 40)
    assert fi.topo_order(g) == ["a", "b", "c"]


# --------------------------------------------------------------------------------------- admission / dependencies

def test_admission_rejects_forged_or_foreign_candidates(env2):
    e = env2
    sha = commit_in(e.wts["t1"], {"src/a/a.py": "a\n"})
    other = commit_in(e.wts["t2"], {"src/b/b.py": "b\n"})
    w = e.writer
    assert w.submit_candidate(JOB, "t1", candidate_sha=sha, evidence_ref="")["reason"] == "missing_evidence_ref"
    assert w.submit_candidate(JOB, "t1", candidate_sha="zz", evidence_ref="e")["reason"] == "not_a_commit_sha"
    assert w.submit_candidate(JOB, "t1", candidate_sha="0" * 40, evidence_ref="e")["reason"] == "not_a_commit"
    assert w.submit_candidate(JOB, "t1", candidate_sha=other, evidence_ref="e")["reason"] == "not_lane_branch_tip"
    assert w.submit_candidate(JOB, "nope", candidate_sha=sha, evidence_ref="e")["reason"] == "unknown_task"
    assert e.fs.candidates(JOB) == []
    assert w.submit_candidate(JOB, "t1", candidate_sha=sha, evidence_ref="e")["status"] == "admitted"


def test_dependent_gated_on_integration_not_mere_finish(steward_home, tmp_path):
    e = Env(tmp_path, steward_home, CHAIN)
    e.admit()
    assert list(e.wts) == ["t1"]
    e.finish("t1", {"src/a/a.py": "a\n"})
    assert fl.task_states(e.store, JOB)["t1"] == "finished"
    assert e.ctl.admit(JOB) == []  # finished but not integrated: t2 must wait (27-01 open issue)
    assert e.writer.integrate(JOB)[0]["outcome"] == "integrated"
    started = e.ctl.admit(JOB)
    assert [r["task"] for r in started] == ["t2"]


def test_dependent_must_be_based_on_integrated_dependency(steward_home, tmp_path):
    e = Env(tmp_path, steward_home, CHAIN)
    e.admit()
    e.finish("t1", {"src/a/a.py": "a\n"})
    out = e.writer.integrate(JOB)[0]["output_sha"]
    e.admit()
    # lane t2 was cut from the pinned base, not an integration SHA containing t1
    e.finish("t2", {"src/b/b.py": "b\n"})
    r = e.writer.integrate_candidate(JOB, "t2")
    assert r["outcome"] == "blocked" and r["reason"] == "candidate_not_based_on_dependencies"
    assert e.head() == out


def test_dependent_based_on_integration_sha_integrates(steward_home, tmp_path):
    e = Env(tmp_path, steward_home, CHAIN)
    e.admit()
    e.finish("t1", {"src/a/a.py": "a\n"})
    out = e.writer.integrate(JOB)[0]["output_sha"]
    e.admit()
    e.finish("t2", {"src/b/b.py": "b\n"}, base=out)
    res = e.writer.integrate(JOB)
    assert [r["task_id"] for r in res] == ["t1", "t2"] and res[1]["outcome"] == "integrated"
    assert res[1]["before_sha"] == out and res[1]["changed_paths"] == ["src/b/b.py"]


# --------------------------------------------------------------------------- blocked before the branch changes

def blocked(e, task_id, reason):
    before = e.head() if e.writer.integration_path(JOB).exists() else None
    r = e.writer.integrate_candidate(JOB, task_id)
    assert r["outcome"] == "blocked" and r["reason"].startswith(reason), r
    if before:
        assert e.head() == before
    return r


def test_stale_graph_revision_blocks(env2):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n"})
    from steward import fleet_graph as fg
    from test_fleet_graph import BLOB, manifest
    g = fg.compile_graph(manifest(*TWO), committed_plan_blob_id=BLOB, goal="goal")
    e.fs.record_graph(JOB, 2, g)
    assert e.fs.set_current_graph(JOB, 2, expected_version=1)
    blocked(e, "t1", "stale_graph_revision")
    assert not e.writer.integration_path(JOB).exists() or e.head() == e.ctl.base_sha()


def test_changed_candidate_commit_blocks(env2):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n"})
    commit_in(e.wts["t1"], {"src/a/more.py": "late\n"})  # lane branch moved after admission
    blocked(e, "t1", "candidate_commit_changed")


def test_unconfirmed_lane_stop_blocks(env2):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n"}, stop=False)
    assert lease_of(e.store, "lane-t1")["state"] == "running"
    blocked(e, "t1", "lane_stop_unconfirmed")


def test_uncertain_lane_keeps_writer_fenced(env2):
    e = env2
    e.world.kill_on_stop = False
    e.finish("t1", {"src/a/a.py": "a\n"})
    assert lease_of(e.store, "lane-t1")["state"] == "uncertain"
    blocked(e, "t1", "writer_unavailable")
    assert fl.get(e.store, JOB, fl.INTEGRATION) is None


def test_out_of_claim_diff_blocks(env2):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n", "src/b/steal.py": "x\n"})
    r = blocked(e, "t1", "out_of_claim_paths")
    assert "src/b/steal.py" in r["reason"] and r["changed_paths"] == ["src/a/a.py", "src/b/steal.py"]
    assert e.fs.integrated_tasks(JOB) == {}


def test_dirty_integration_tree_blocks(env2):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n"})
    e.writer._ensure_worktree(JOB)
    (e.writer.integration_path(JOB) / "stray.txt").write_text("dirty")
    blocked(e, "t1", "dirty_integration_tree")
    assert (e.writer.integration_path(JOB) / "stray.txt").exists()  # never cleaned/reset


def test_stale_integration_head_blocks(env2):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n"})
    e.writer._ensure_worktree(JOB)
    commit_in(e.writer.integration_path(JOB), {"README.out": "someone else\n"})
    blocked(e, "t1", "stale_integration_head")


def test_blocked_attempts_are_recorded_and_retryable(env2):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n"})
    e.writer._ensure_worktree(JOB)
    stray = e.writer.integration_path(JOB) / "stray.txt"
    stray.write_text("dirty")
    blocked(e, "t1", "dirty_integration_tree")
    assert [h["outcome"] for h in e.fs.integration_history(JOB)] == ["blocked"]
    assert rt.integration_holds(e.store, JOB) == {"t1": "dirty_integration_tree"}
    stray.unlink()
    assert e.writer.integrate_candidate(JOB, "t1")["outcome"] == "integrated"
    assert rt.integration_holds(e.store, JOB) == {}


# ---------------------------------------------------------------------------------------------------- conflicts

def test_conflict_is_recorded_and_inputs_preserved(steward_home, tmp_path):
    shared = [task("t1", owns=["src/"]), task("t2", owns=["src/"], depends_on=[])]
    e = Env(tmp_path, steward_home, shared)
    e.admit()  # overlapping claims serialize: t2 starts only after t1 is integrated, but from the pinned base
    s1 = e.finish("t1", {"src/same.py": "one\n"})
    first = e.writer.integrate(JOB)[0]
    e.admit()
    s2 = e.finish("t2", {"src/same.py": "two\n"})
    res = [first, *e.writer.integrate(JOB)[1:]]
    assert [r["outcome"] for r in res] == ["integrated", "conflict"]
    c = e.fs.integration_history(JOB)[-1]
    assert c["conflict_paths"] == ["src/same.py"] and c["output_sha"] is None and c["input_sha"] == s2
    assert git(e.repo, "rev-parse", f"steward-lanes/{JOB}/lane-t1") == s1
    assert git(e.repo, "rev-parse", f"steward-lanes/{JOB}/lane-t2") == s2
    path = e.writer.integration_path(JOB)
    assert git(path, "status", "--porcelain") == "" and e.head() == res[0]["output_sha"]
    assert rt.integration_holds(e.store, JOB) == {"t2": "merge_conflict"}
    # the same conflicted candidate replays its stored conflict (no retry loop)
    assert e.writer.integrate_candidate(JOB, "t2")["replayed"]


# ------------------------------------------------------------------------------------- fencing / restart

def test_held_or_uncertain_writer_blocks_and_needs_confirmed_stop(env2):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n"})
    fl.acquire_integration(e.store, JOB, holder="crashed-writer")  # prior writer never released (process died)
    r = e.writer.integrate_candidate(JOB, "t1")
    assert r["outcome"] == "blocked" and r["reason"].startswith("writer_unavailable")
    assert e.fs.integrated_tasks(JOB) == {}
    # unconfirmed probe: stays fenced
    assert e.writer.recover_writer(JOB, lambda j: rt.UNKNOWN)["status"] == "fenced"
    assert e.writer.integrate_candidate(JOB, "t1")["outcome"] == "blocked"
    assert fl.get(e.store, JOB, fl.INTEGRATION)["state"] == "uncertain"
    # only a confirmed-dead probe frees it
    assert e.writer.recover_writer(JOB, lambda j: rt.DEAD)["status"] == "freed"
    assert e.writer.integrate_candidate(JOB, "t1")["outcome"] == "integrated"


def test_recover_dead_writer_then_integrate(env2):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n"})
    fl.acquire_integration(e.store, JOB, holder="crashed-writer")
    assert e.writer.recover_writer(JOB, lambda j: rt.DEAD)["status"] == "freed"
    assert e.writer.integrate(JOB)[0]["outcome"] == "integrated"
    assert fl.get(e.store, JOB, fl.INTEGRATION)["epoch"] == 2


def test_crash_between_merge_and_ledger_is_adopted_not_duplicated(env2):
    e = env2
    sha = e.finish("t1", {"src/a/a.py": "a\n"})
    path = e.writer._ensure_worktree(JOB)
    git(path, "-c", "user.name=S", "-c", "user.email=s@x", "merge", "--no-ff", "-q", "-m", "crashed", sha)
    merged = e.head()
    r = e.writer.integrate_candidate(JOB, "t1")
    assert r["outcome"] == "integrated" and r["output_sha"] == merged and r["reason"] == "adopted_unrecorded_merge"
    assert e.head() == merged


def test_writer_exception_leaves_branch_fenced(env2, monkeypatch):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n"})
    monkeypatch.setattr(e.writer, "_dirty", lambda p: (_ for _ in ()).throw(OSError("boom")))
    r = e.writer.integrate_candidate(JOB, "t1")
    assert r["outcome"] == "blocked" and "writer_exception" in r["reason"]
    assert fl.get(e.store, JOB, fl.INTEGRATION)["state"] == "uncertain"
    monkeypatch.undo()
    assert e.writer.integrate_candidate(JOB, "t1")["reason"].startswith("writer_unavailable")


def test_stale_lane_attempt_after_recovery_blocks(env2):
    e = env2
    e.finish("t1", {"src/a/a.py": "a\n"})
    lane = "lane-t1"
    lease = fl.get(e.store, JOB, lane)
    fl.acquire_lane(e.store, JOB, lane, max_lanes=2)  # a newer attempt (epoch 2) supersedes the candidate
    assert lease["epoch"] == 1
    blocked(e, "t1", "stale_lane_attempt")
