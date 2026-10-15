"""Phase 28-02 (FLEET-13/14): combined final-SHA closeout, one human candidate, bounded repair (temp Git + real Store)."""

from __future__ import annotations

import pytest
from test_fleet_graph import task
from test_fleet_integration import Env, git
from test_fleet_lanes import JOB

from steward import closeout, fleet_closeout as fc
from steward import fleet_lease as fl
from steward.api import StewardAPI
from steward.discovery import Discovery, DiscoveredCommand
from steward.models import JobState, Judgment
from steward.runner_exec import SubprocessRunner
from steward.verify import Verification
from steward.weakening import Finding

TWO = [task("t1", owns=["src/a/"]), task("t2", owns=["src/b/"])]
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def disc(line):
    return lambda wt: Discovery(commands=[DiscoveredCommand(line=line, source="t", kind="check", reason="")])


class Ready:
    """Two required tasks, both integrated, lanes stopped, job WORKER_FINISHED, baseline recorded."""

    def __init__(self, tmp_path, home, *, check="true", partial=False):
        self.e = Env(tmp_path, home, TWO)
        e = self.e
        e.admit()
        e.finish("t1", {"src/a/a.py": "a\n"})
        if not partial:
            e.finish("t2", {"src/b/b.py": "b\n"})
        else:
            e.admit()
        e.writer.integrate(JOB)
        e.store.add_check(JOB, commit_sha=e.ctl.base_sha(), command=check, exit_code=0, results={}, label="baseline")
        for st, what in ((JobState.CONTRACTED, "c"), (JobState.RUNNING, "r"), (JobState.WORKER_FINISHED, "w")):
            e.store.set_state(JOB, st, what=what, evidence={}, rule="t", judgment=Judgment.code)
        self.fco = self.make(check)

    def make(self, line, **kw):
        e = self.e
        return fc.FleetCloseout(e.store, SubprocessRunner(), base_dir=e.writer.base_dir, base_sha=e.ctl.base_sha(),
                                discover=disc(line), **kw)

    def state(self):
        return self.e.store.get_job(JOB).state


@pytest.fixture
def ready(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")  # legacy auto-accept must NOT apply to a fleet candidate
    return Ready(tmp_path, steward_home)


# ------------------------------------------------------------------------------------------------ tracer

def test_passing_final_sha_creates_one_candidate_and_waits_for_human_despite_auto_accept(ready):
    r, e = ready, ready.e
    assert closeout.acceptance_mode() == "auto"
    res = r.fco.run(JOB)
    head = e.head()
    assert res["status"] == "candidate" and res["state"] == "NEEDS_HUMAN" and r.state() == JobState.NEEDS_HUMAN
    cand = fc.final_candidate(e.store, JOB)
    assert cand["final_sha"] == head == cand["verified_sha"] == e.fs.integration_head(JOB)
    assert cand["base_sha"] == e.ctl.base_sha()
    assert {t["task"] for t in cand["tasks"]} == {"t1", "t2"} and all(t["evidence_ref"] for t in cand["tasks"])
    assert cand["checks"] == [{"command": "true", "exit_code": 0, "passed": True}]
    assert cand["diff"]["files"] == ["src/a/a.py", "src/b/b.py"] and "a.py" in cand["diff"]["stat"]
    assert cand["weakening"] == [] and cand["baseline"]["new_failures"] == []
    assert set(cand["lane_shas"]) == {"t1", "t2"}
    assert len(fc.candidates(e.store, JOB)) == 1


def test_rerun_after_candidate_does_not_duplicate(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")
    r = Ready(tmp_path, steward_home)
    r.fco.run(JOB)
    r.e.store.set_state(JOB, JobState.RUNNING, what="again", evidence={}, rule="t", judgment=Judgment.code)
    r.e.store.set_state(JOB, JobState.WORKER_FINISHED, what="again", evidence={}, rule="t", judgment=Judgment.code)
    assert r.fco.run(JOB)["status"] == "candidate"
    assert len(fc.candidates(r.e.store, JOB)) == 1


def test_api_projects_single_candidate_and_only_human_accepts(ready):
    r, e = ready, ready.e
    assert StewardAPI(e.store).get_verification(JOB)["fleet_candidate"]["candidate"] is None
    r.fco.run(JOB)
    v = StewardAPI(e.store).get_verification(JOB)
    fcand = v["fleet_candidate"]
    assert fcand["final_sha"] == e.head() and v["commit_sha"] == e.head() and v["verified"] is True
    assert fcand["diff"]["files"] and fcand["baseline"] and fcand["checks"]
    with pytest.raises(Exception):  # code cannot take NEEDS_HUMAN -> ACCEPTED
        e.store.set_state(JOB, JobState.ACCEPTED, what="x", evidence={}, rule="t", judgment=Judgment.code)
    assert closeout.accept(e.store, JOB) == JobState.ACCEPTED
    assert e.store.get_job(JOB).state == JobState.ACCEPTED


def test_legacy_job_gets_no_fleet_projection(steward_home):
    from steward.store import Store
    store = Store(steward_home)
    store.create_job("g", "/repo", job_id="legacy1")
    assert "fleet_candidate" not in StewardAPI(store).get_verification("legacy1")


# ------------------------------------------------------------------------------------------------ blocking

def no_candidate(r):
    assert fc.final_candidate(r.e.store, JOB) is None


def test_missing_required_task_blocks(steward_home, tmp_path):
    r = Ready(tmp_path, steward_home, partial=True)
    res = r.fco.run(JOB)
    assert res["status"] == "waiting" and any(x.startswith("required_task_not_integrated: t2") for x in res["reasons"])
    no_candidate(r)
    assert r.state() == JobState.WORKER_FINISHED


def test_unstopped_lane_and_live_writer_block(ready):
    r, e = ready, ready.e
    lease = fl.get(e.store, JOB, "lane-t1")
    e.store.kv_set(JOB, "lease:lane-t1", {**lease, "state": "running"})
    res = r.fco.run(JOB)
    assert any(x.startswith("lane_not_stopped: lane-t1") for x in res["reasons"])
    e.store.kv_set(JOB, "lease:lane-t1", lease)
    w = fl.acquire_integration(e.store, JOB, holder="x")
    res = r.fco.run(JOB)
    assert any(x.startswith("integration_writer_live") for x in res["reasons"])
    fl.release_integration(e.store, JOB, w["epoch"], confirmed=True, evidence={"ok": 1})
    no_candidate(r)


def test_unacknowledged_steering_blocks(ready):
    r, e = ready, ready.e
    e.store.kv_set(JOB, "fleet_receipts", {"req:lane-t1": {"stage": "delivered", "lane": "lane-t1"}})
    res = r.fco.run(JOB)
    assert any(x.startswith("steering_unacknowledged") for x in res["reasons"])
    e.store.kv_set(JOB, "fleet_receipts", {"req:lane-t1": {"stage": "acknowledged", "lane": "lane-t1"}})
    assert r.fco.run(JOB)["status"] == "candidate"


def test_dirty_integration_tree_blocks_and_is_left_alone(ready):
    r, e = ready, ready.e
    stray = e.writer.integration_path(JOB) / "stray.txt"
    stray.write_text("x")
    res = r.fco.run(JOB)
    assert "dirty_integration_tree" in res["reasons"] and stray.exists()
    no_candidate(r)


def test_stale_head_blocks(ready):
    r, e = ready, ready.e
    p = e.writer.integration_path(JOB)
    (p / "extra.txt").write_text("x")
    git(p, "add", "-A")
    git(p, "-c", "user.name=W", "-c", "user.email=w@x", "commit", "-q", "-m", "sneaky")
    res = r.fco.run(JOB)
    assert any(x.startswith("stale_integration_head") for x in res["reasons"])
    no_candidate(r)


def test_stale_task_evidence_blocks(ready):
    r, e = ready, ready.e
    e.fs.record_candidate(JOB, "t1", graph_rev=1, lane_id="lane-t1", attempt_no=1, epoch=1,
                          candidate_sha="f" * 40, evidence_ref="newer")
    res = r.fco.run(JOB)
    assert any(x.startswith("stale_task_evidence: t1") for x in res["reasons"])
    no_candidate(r)


def test_new_failure_blocks_candidate(ready):
    r = ready
    res = r.make("false").run(JOB)
    assert res["status"] == "repair" and any(x.startswith("new_failures") for x in res["reasons"])
    no_candidate(r)


def test_unverified_command_blocks(ready):
    r = ready
    broken = lambda *a, **k: Verification(commit_sha=r.e.head(), broken=[{"command": "x", "exit": 127, "reason": "nf"}])
    res = r.make("true", verify=broken).run(JOB)
    assert any(x.startswith("unverified_checks") for x in res["reasons"])
    no_candidate(r)


def test_weakening_blocks_candidate(ready):
    r = ready
    detect = lambda wt, base, runner: [Finding(path="tests/t.py", kind="assert_removed", detail="d")]
    res = r.make("true", detect=detect).run(JOB)
    assert any(x.startswith("test_weakening") for x in res["reasons"])
    no_candidate(r)


def test_out_of_claim_change_blocks(ready):
    r, e = ready, ready.e
    # a baseline older than the declared claims (the empty tree) makes every repo file look like a change
    fco = fc.FleetCloseout(e.store, SubprocessRunner(), base_dir=e.writer.base_dir, base_sha=EMPTY_TREE,
                           discover=disc("true"), detect=lambda *a: [])
    fco._base = lambda job_id, head: EMPTY_TREE
    res = fco.run(JOB)
    assert any(x.startswith("out_of_claim_paths") for x in res["reasons"])
    no_candidate(r)


# --------------------------------------------------------------------------------------- repair routing

def test_repair_is_bounded_single_and_waits_for_fenced_writer(ready):
    r, e = ready, ready.e
    bad = r.make("false")
    w = fl.acquire_integration(e.store, JOB, holder="x")
    res = bad.run(JOB)
    assert res["status"] == "waiting" and r.state() == JobState.WORKER_FINISHED  # writer not fenced: no repair yet
    fl.release_integration(e.store, JOB, w["epoch"], confirmed=True, evidence={"ok": 1})
    lanes_before = {t: e.fs.latest_candidate(JOB, t)["candidate_sha"] for t in ("t1", "t2")}
    res = bad.run(JOB)
    assert res["status"] == "repair" and r.state() == JobState.RUNNING
    ev = [x for x in e.fs.fleet_events(JOB) if x["what"] == fc.REPAIR_WHAT]
    assert len(ev) == 1 and '"round": 1' in ev[0]["evidence"]
    assert lanes_before == {t: e.fs.latest_candidate(JOB, t)["candidate_sha"] for t in ("t1", "t2")}
    # while a repair is in flight a second one is never routed
    e.store.set_state(JOB, JobState.WORKER_FINISHED, what="w", evidence={}, rule="t", judgment=Judgment.code)
    assert bad.run(JOB)["status"] == "waiting"
    assert len([x for x in e.fs.fleet_events(JOB) if x["what"] == fc.REPAIR_WHAT]) == 1
    bad.repair_finished(JOB)
    assert bad.run(JOB)["status"] == "repair"  # round 2
    e.store.set_state(JOB, JobState.WORKER_FINISHED, what="w", evidence={}, rule="t", judgment=Judgment.code)
    bad.repair_finished(JOB)
    res = bad.run(JOB)
    assert res["status"] == "exhausted" and r.state() == JobState.NEEDS_HUMAN
    no_candidate(r)


def test_repair_then_pass_rereports_combined_candidate(ready):
    r = ready
    assert r.make("false").run(JOB)["status"] == "repair"
    r.e.store.set_state(JOB, JobState.WORKER_FINISHED, what="w", evidence={}, rule="t", judgment=Judgment.code)
    r.fco.repair_finished(JOB)
    assert r.fco.run(JOB)["status"] == "candidate"


# --------------------------------------------------------------------------------------------- runner

def test_runner_fleet_job_uses_combined_closeout_and_fails_closed_without_coordinator(ready, monkeypatch):
    from steward import heartbeat
    from steward.runner import Runner
    r, e = ready, ready.e
    rn = object.__new__(Runner)
    rn.store, rn.fleet_closeout = e.store, None
    rn._notify_closeout_result = lambda *a: None
    rep = rn._run_closeout(JOB, heartbeat.Class.DONE if hasattr(heartbeat.Class, "DONE") else list(heartbeat.Class)[0])
    assert rep.state == "needs_human" or rep.state == JobState.NEEDS_HUMAN.value
    no_candidate(r)


def test_explicit_fleet_start_never_creates_a_legacy_worker(ready, monkeypatch):
    from steward import runner as runner_module
    from steward.contract import Contract
    from steward.runner import Runner
    r, e = ready, ready.e
    monkeypatch.setattr(runner_module, "load_contract", lambda store, job_id: Contract(
        goal="fleet goal", done_when=["required work is complete"]))
    e.store.kv_set(JOB, "fleet_mode_requested", True)
    called = []
    rn = object.__new__(Runner)
    rn.store = e.store
    rn.trajectory = None
    rn.fleet_controller = None
    rn.worker_factory = lambda ref: called.append(ref)
    rn.clock = lambda: 1.0
    rn._is_fleet_job = lambda job_id: True
    rn.start(JOB)
    assert called == []
    assert e.store.get_job(JOB).state == JobState.NEEDS_HUMAN


def test_runner_delegates_to_coordinator(steward_home, tmp_path, monkeypatch):
    from steward import heartbeat
    from steward.runner import Runner
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "auto")
    r = Ready(tmp_path, steward_home)
    rn = object.__new__(Runner)
    rn.store, rn.fleet_closeout = r.e.store, r.fco
    rn._notify_closeout_result = lambda *a: None
    rep = rn._run_closeout(JOB, list(heartbeat.Class)[0])
    assert rep.detail.startswith("fleet closeout candidate") and rep.state == JobState.NEEDS_HUMAN.value
    assert fc.final_candidate(r.e.store, JOB)["final_sha"] == r.e.head()
