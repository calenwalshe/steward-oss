"""`scripts/fleet_live_proof.py` judges a proof by the job's own record, never by the runner's exit status
(2026-10-06: muse job b27abe20 reached NEEDS_HUMAN with both lanes stopped and the driver returned 0 on the state
alone; that run happened to be a real success - integrated, verified, awaiting acceptance - but a failed job would
have passed the same way)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from steward import fleet_graph as fg
from steward import fleet_registry as fr
from steward.fleet_store import FleetStore
from steward.models import Judgment
from steward.store import Store

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("fleet_live_proof", ROOT / "scripts" / "fleet_live_proof.py")
proof = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proof)

FINAL = "a4443db03674e6cb69539f1cb80121d9db318ce0"


def _job(steward_home, tmp_path, job_id="PROOF"):
    store = Store(steward_home)
    repo = tmp_path / "repo"
    repo.mkdir()
    store.create_job("fleet proof", str(repo), job_id=job_id)
    fs = FleetStore(store)
    roster = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [
        {"id": "muse-worker", "provider": "meta", "runtime": "muse", "model": "muse-spark-1.3-contributor",
         "roles": ["worker"], "capabilities": ["edit"], "strengths": [], "enabled": True, "label": "m"},
        {"id": "lead", "provider": "openai", "runtime": "codex", "model": "gpt-6-astra", "roles": ["tech_lead"],
         "capabilities": ["plan"], "strengths": [], "enabled": True, "label": "l"}]})
    for runtime in ("muse", "codex"):
        fs.record_qualification(runtime, "test", ["edit", "plan"], qualified=True, available=True)
    graph = fg.compile_graph({"schema_version": 1, "plan_blob_id": "a" * 40, "tasks": [
        {"id": t, "objective": t, "done_when": ["pytest"], "depends_on": [], "owns": [f"calc/{t}.py"],
         "required": True, "priority": 1} for t in ("add", "mul")]}, committed_plan_blob_id="a" * 40, goal="g")
    fs.configure_job(roster, graph, job_id=job_id, pins={}, policy="default", tech_lead="lead",
                     limits={"max_lanes": 2})
    return store, fs


def _state(store, job_id, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state, job_id))


def _lane(fs, job_id, task, *, runtime="muse", finished=True, release="task_finished", integrate=True,
          before="b" * 40, output=None):
    lane = f"lane-{task}"
    fs.record_lane(job_id, lane, task_id=task)
    fs.record_attempt(job_id, lane, 1, provider="meta", runtime=runtime, model="muse-spark-1.3-contributor",
                      adapter_version="1")
    ev = dict(actor="controller", rule="lane_lease", judgment="code", lane_id=lane, attempt_no=1)
    fs.append_event(job_id, what="lane_started", evidence={"epoch": 1}, **ev)
    if finished:
        fs.append_event(job_id, what="task finished", evidence={"epoch": 1, "task": task}, **ev)
    if release:
        fs.append_event(job_id, what="lane lease released", evidence={"epoch": 1, "evidence": {"reason": release}},
                        **ev)
    if integrate:
        with fs.store._conn:
            fs.store._conn.execute(
                "INSERT INTO fleet_integration_attempts (job_id, idem_key, task_id, graph_rev, lane_id, attempt_no,"
                " epoch, candidate_sha, evidence_ref, before_sha, input_sha, output_sha, changed_paths_json,"
                " conflict_paths_json, outcome, reason, writer_epoch, rule, created_at)"
                " VALUES (?,?,?,1,?,1,1,?,?,?,?,?,'[]','[]','integrated','ok',1,'serial_integration_writer','now')",
                (job_id, f"idem-{task}", task, lane, "c" * 40, "ref", before, "c" * 40, output or FINAL))


def _closeout(store, fs, job_id, *, passed_at=FINAL, owner_kinds=("acceptance_decision",)):
    fs.append_event(job_id, what="fleet candidate recorded", actor="fleet_closeout", rule="fleet_combined_closeout",
                    judgment="code", evidence={"final_sha": FINAL, "base_sha": "b" * 40})
    if passed_at:
        store.append_event(job_id, what="checks passed at exact commit", evidence={"commit_sha": passed_at},
                           rule="checks_pass_at_sha", judgment=Judgment.code)
    store.append_event(job_id, what="ownership_status", evidence={"status": "needs_owner",
                                                                  "owner_kinds": list(owner_kinds)},
                       rule="ownership", judgment=Judgment.code)


def test_observed_success_integrated_verified_awaiting_acceptance_passes(steward_home, tmp_path):
    store, fs = _job(steward_home, tmp_path)
    _lane(fs, "PROOF", "mul", output="2" * 40)
    _lane(fs, "PROOF", "add", before="2" * 40)
    _closeout(store, fs, "PROOF")
    _state(store, "PROOF", "NEEDS_HUMAN")
    ok, lines = proof.assess(store, "PROOF", ["muse"])
    assert ok, lines
    assert lines[0] == "proof verdict: PASS"
    assert any("lane-add: runtime=muse" in ln and "finished=True" in ln for ln in lines)


def test_needs_human_with_stopped_lanes_but_no_integration_or_verification_fails(steward_home, tmp_path):
    """The observed shape (NEEDS_HUMAN, both lanes `stopped`) without the evidence that makes it a success."""
    store, fs = _job(steward_home, tmp_path)
    _lane(fs, "PROOF", "add", finished=False, release="worker_error", integrate=False)
    _lane(fs, "PROOF", "mul", finished=False, release="start_failed: boom", integrate=False)
    store.append_event("PROOF", what="ownership_status", evidence={"owner_kinds": ["fleet_task_cannot_complete"]},
                       rule="ownership", judgment=Judgment.code)
    _state(store, "PROOF", "NEEDS_HUMAN")
    ok, lines = proof.assess(store, "PROOF", ["muse"])
    assert not ok
    verdict = lines[0]
    for needle in ("lane-add: no `task finished`", "lease released for 'worker_error'", "task add: never integrated",
                   "task mul: never integrated", "no combined fleet candidate recorded",
                   "needs the owner for ['fleet_task_cannot_complete']"):
        assert needle in verdict


def test_timeout_fails_even_when_the_work_so_far_looks_fine(steward_home, tmp_path):
    store, fs = _job(steward_home, tmp_path)
    _lane(fs, "PROOF", "add")
    _state(store, "PROOF", "RUNNING")
    ok, lines = proof.assess(store, "PROOF", ["muse"])
    assert not ok and "timeout: job still RUNNING" in lines[0]
    _lane(fs, "PROOF", "mul")
    _closeout(store, fs, "PROOF")
    _state(store, "PROOF", "NEEDS_HUMAN")
    ok, lines = proof.assess(store, "PROOF", ["muse"], timed_out=True)
    assert not ok and "timeout" in lines[0]


@pytest.mark.parametrize("change, needle", [
    ({"passed_at": "d" * 40}, "checks did not pass at the combined candidate"),
    ({"passed_at": None}, "checks did not pass at the combined candidate"),
    ({"owner_kinds": ("acceptance_decision", "integration_blocked")}, "not only the acceptance decision"),
])
def test_needs_human_for_anything_but_a_verified_acceptance_fails(steward_home, tmp_path, change, needle):
    store, fs = _job(steward_home, tmp_path)
    _lane(fs, "PROOF", "add")
    _lane(fs, "PROOF", "mul")
    _closeout(store, fs, "PROOF", **change)
    _state(store, "PROOF", "NEEDS_HUMAN")
    ok, lines = proof.assess(store, "PROOF", ["muse"])
    assert not ok and needle in lines[0]


def test_a_lane_on_another_runtime_than_requested_fails(steward_home, tmp_path):
    store, fs = _job(steward_home, tmp_path)
    _lane(fs, "PROOF", "add", runtime="codex")
    _lane(fs, "PROOF", "mul")
    _closeout(store, fs, "PROOF")
    _state(store, "PROOF", "NEEDS_HUMAN")
    ok, lines = proof.assess(store, "PROOF", ["muse"])
    assert not ok and "lane-add: ran on 'codex', not one of ['muse']" in lines[0]


def test_stopped_or_failed_end_states_fail(steward_home, tmp_path):
    store, fs = _job(steward_home, tmp_path)
    _lane(fs, "PROOF", "add")
    _lane(fs, "PROOF", "mul")
    _closeout(store, fs, "PROOF")
    for state in ("STOPPED", "FAILED"):
        _state(store, "PROOF", state)
        ok, lines = proof.assess(store, "PROOF", ["muse"])
        assert not ok and f"job ended {state}" in lines[0]


def test_guard_refuses_a_proof_root_that_is_or_contains_the_live_home(tmp_path, monkeypatch):
    monkeypatch.setattr(proof, "PROOF", proof.LIVE_HOME)
    monkeypatch.setenv("STEWARD_HOME", str(proof.LIVE_HOME / "x"))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(proof.LIVE_HOME / "y"))
    with pytest.raises(SystemExit, match="live Steward home"):
        proof.guard()
    monkeypatch.setattr(proof, "PROOF", tmp_path / "root")
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path / "elsewhere"))
    with pytest.raises(SystemExit, match="not under"):
        proof.guard()


def test_advisor_uses_the_validator_schema_and_only_pays_for_gated_triggers(tmp_path):
    """Live 2026-10-06: every `new --fleet-plan` tech-lead answer was `bad_field: expected_effect` because the
    services advisor had its own prompt. It now sends `proposal_prompt` and its answer parses."""
    from steward.fleet_services import FleetModelAdvisor
    from steward.fleet_tech_lead import parse_proposal
    from steward.runner_exec import CommandResult

    observed = {"contract": "c", "graph_rev": 1, "registry_hash": "h", "steer_rev": 0}
    answer = {"proposal_id": "p1", "trigger_id": "trg-1", "observed": observed, "evidence_refs": ["fact:1"],
              "rationale": "r", "expected_effect": {"lanes": "steady"},
              "actions": [{"type": "diagnose", "key": "k1", "target": None, "params": {"question": "why?"}}]}
    prompts = []

    class Runner:
        def run(self, argv, cwd, *, timeout=None, env=None):
            prompts.append(argv[-1])
            out = Path(argv[argv.index("--output-last-message") + 1])
            out.write_text("Here you go:\n```json\n" + json.dumps(answer) + "\n```\n")
            return CommandResult(list(argv), str(cwd), 0, "", "", 0.0)

    lead = FleetModelAdvisor("codex", "gpt-6-astra", Runner(), tmp_path, gated_only=True)
    assert lead({"trigger_id": "trg-1", "observed": observed, "events": [{"ref": "event:4"}]}) is None
    assert prompts == []   # an ungated material event costs nothing
    raw = lead({"trigger_id": "trg-1", "observed": observed, "events": [{"ref": "fact:1"}]})
    assert parse_proposal(raw).proposal_id == "p1"
    assert "expected_effect (small object)" in prompts[0] and "actions (1..5" in prompts[0]
    assert "empty actions array" not in prompts[0]
