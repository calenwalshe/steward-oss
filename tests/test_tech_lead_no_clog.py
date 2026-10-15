"""SUP-09 / SUP-13 / PRD D-08: the tech lead for every job, and no clogging.

At most one review in flight per job, with a time limit; facts arriving meanwhile queue and coalesce into the next
review; a hung review falls through Astra -> Fable -> NEEDS_YOU; Jev, the doctor and the code-fact floor never wait on
a review (a blocking fake advisor proves it). Legacy jobs keep their paths when nothing is wired."""

from __future__ import annotations

import json
import threading
import time

import pytest

from steward import approvals, code_facts
from steward import fleet_registry as fr
from steward import fleet_runtime as rt
from steward import fleet_tech_lead as tl
from steward import tech_lead_actions as tla
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.runner import wire_job_lead
from steward.store import Store
from test_runner import _contracted_job

ASTRA = fr.RosterEntry(id="astra-tech-lead", provider="openai", runtime="codex", model="gpt-6-astra",
                       roles=("tech_lead",), capabilities=("plan",), strengths=(), enabled=True, label="Astra",
                       priority=1)
FABLE = fr.RosterEntry(id="fable-tech-lead", provider="anthropic", runtime="claude-fabric", model="claude-fable-5-1",
                       roles=("tech_lead",), capabilities=("plan",), strengths=(), enabled=True, label="Fable",
                       priority=2)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def escalate_proposal(reason="look"):
    return json.dumps({"proposal_id": "p1", "trigger_id": "t", "confidence": 0.9, "signature": "sig",
                       "actions": [{"type": "escalate", "params": {"reason": reason}}]})


def send_back_proposal(text="fix the owner sweep"):
    return json.dumps({"proposal_id": "p2", "trigger_id": "t", "confidence": 0.95, "signature": "owner_sweep",
                       "actions": [{"type": "send_back", "params": {"text": text, "evidence": "t::a"}}]})


class Gate:
    """A controllable advisor: per lead, a list of behaviours: a string answer, an Exception, or 'block'."""

    def __init__(self, plan):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.calls: list[tuple[str, str]] = []
        self.release = threading.Event()

    def __call__(self, entry, prompt, timeout_s):
        self.calls.append((entry.id, prompt))
        b = self.plan[entry.id].pop(0) if self.plan[entry.id] else "block"
        if b == "block":
            self.release.wait(30)
            return send_back_proposal("late answer")
        if isinstance(b, Exception):
            raise b
        return b


def wait_done(lead, job_id, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        bg = lead._reviews.get(job_id)
        if bg is None or bg.done():
            return
        time.sleep(0.01)


@pytest.fixture
def env(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, "fix it. Finish line: `pytest -q tests/test_a.py`")
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    (store.home / "worktrees" / job.job_id).mkdir(parents=True)
    return store, job.job_id


def make_lead(store, gate, clock, *, timeout_s=300, leads=(ASTRA, FABLE)):
    arb = rt.Arbiter(store, clock)
    ex = tla.ActionExecutor(store, runner_exec=None, clock=clock)
    pol = tpol.Policy(review_timeout_s=timeout_s)
    lead = tl.JobTechLead(store, list(leads), ex, arbiter=arb, invoke=gate, available=lambda e: None,
                          policy_fn=lambda: pol, clock=clock)
    return lead, arb


def test_one_review_in_flight_and_facts_coalesce_into_the_next(env):
    store, jid = env
    clock = Clock()
    gate = Gate({"astra-tech-lead": ["block", send_back_proposal()], "fable-tech-lead": []})
    lead, arb = make_lead(store, gate, clock)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    assert lead.on_event(jid)["status"] == "review_started"
    arb.submit(jid, code_facts.SOURCE, "new_failures_repeated", key="b", test="t::a")
    arb.submit(jid, "jev", "thrash", confidence=0.9)
    assert lead.on_event(jid)["status"] == "review_in_flight"
    assert len(gate.calls) == 1  # never a second review while one is in flight
    gate.release.set()
    wait_done(lead, jid)
    assert lead.on_event(jid)["status"] == "acted"
    res = lead.on_event(jid)  # the queued facts: ONE coalesced review
    assert res["status"] == "review_started" and len(gate.calls) == 2
    started = [e for e in store.events(jid) if e.what == "tech_lead_review_started"]
    assert started[-1].evidence["facts"] == ["new_failures_repeated", "thrash"]
    assert started[-1].evidence["reasoning_effort"] == "medium"


def test_hung_review_falls_through_to_fable(env):
    store, jid = env
    clock = Clock()
    gate = Gate({"astra-tech-lead": ["block"], "fable-tech-lead": [send_back_proposal()]})
    lead, arb = make_lead(store, gate, clock, timeout_s=60)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    lead.on_event(jid)
    clock.t += 30
    assert lead.on_event(jid)["status"] == "review_in_flight"
    clock.t += 31  # past the 60 s limit
    res = lead.on_event(jid)
    assert res["status"] == "review_started" and res["lead"] == "fable-tech-lead"
    expired = [e for e in store.events(jid) if e.what == "tech_lead_review_expired"]
    assert expired and expired[0].evidence["lead"] == "astra-tech-lead"
    wait_done(lead, jid)
    assert lead.on_event(jid)["status"] == "acted"
    answered = next(e for e in store.events(jid) if e.what == "tech_lead_answered")
    assert answered.evidence["answered_by"] == "fable-tech-lead"
    assert answered.evidence["skipped"][0]["reason"].startswith("timeout")
    gate.release.set()


@pytest.mark.legacy_ownership_off  # Plan 1 G13: reviewer exhaustion requests strategy/outage handling, not NEEDS_YOU
def test_both_hung_escalates_needs_you_tech_lead_exhausted(env):
    store, jid = env
    clock = Clock()
    gate = Gate({"astra-tech-lead": ["block"], "fable-tech-lead": ["block"]})
    lead, arb = make_lead(store, gate, clock, timeout_s=60)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    lead.on_event(jid)
    clock.t += 61
    lead.on_event(jid)
    clock.t += 61
    assert lead.on_event(jid)["status"] == "exhausted"
    pend = [a for a in approvals.list_approvals(store, jid, status="pending") if a["kind"] == "tech_lead"]
    assert pend and "tech lead exhausted" in pend[0]["question"]
    assert any(e.what == "tech_lead_exhausted" for e in store.events(jid))
    gate.release.set()


def test_expired_review_is_itself_a_code_fact_but_not_a_new_review(env):
    store, jid = env
    clock = Clock()
    gate = Gate({"astra-tech-lead": ["block"], "fable-tech-lead": [send_back_proposal()]})
    lead, arb = make_lead(store, gate, clock, timeout_s=60)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    lead.on_event(jid)
    clock.t += 61
    lead.on_event(jid)
    wait_done(lead, jid)
    lead.on_event(jid)
    assert code_facts.step(store, jid, arb) == ["review_watchdog_expired"]
    assert lead.on_event(jid)["status"] == "skipped"  # bookkeeping fact: no review about a review
    gate.release.set()


def test_malformed_and_unavailable_fall_through(env):
    store, jid = env
    clock = Clock()
    gate = Gate({"astra-tech-lead": ["not json at all"], "fable-tech-lead": [send_back_proposal()]})
    lead, arb = make_lead(store, gate, clock)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    lead.on_event(jid)
    wait_done(lead, jid)
    res = lead.on_event(jid)
    assert res["status"] == "review_started" and res["lead"] == "fable-tech-lead"
    wait_done(lead, jid)
    assert lead.on_event(jid)["status"] == "acted"


def test_no_self_review_of_the_workers_own_model(env):
    store, jid = env
    store.kv_set(jid, "worker_model", "gpt-6-astra")
    clock = Clock()
    gate = Gate({"astra-tech-lead": [], "fable-tech-lead": [send_back_proposal()]})
    lead, arb = make_lead(store, gate, clock)
    lead.worker_model_fn = lambda j: store.kv_get(j, "worker_model")
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    assert lead.on_event(jid)["lead"] == "fable-tech-lead"


def test_packet_is_bounded_by_the_policy_token_budget(env):
    store, jid = env
    for i in range(300):
        store.append_event(jid, what="verification recorded", evidence={"new_failures": [f"t::x{i}" * 20]},
                           rule="r", judgment=Judgment.code)
    lead, _ = make_lead(store, Gate({}), Clock())
    pol = tpol.Policy(token_budget=2000)
    pkt = lead.packet(jid, [{"kind": "x"}], "trg", pol)
    assert len(json.dumps(pkt, default=str)) <= 2000 * 4 + 400
    assert "orientation" in pkt and pkt["facts"] == [{"kind": "x"}]


def test_packet_carries_the_redacted_worker_picture_and_stays_bounded(env, monkeypatch):
    from steward import worker_view

    store, jid = env
    monkeypatch.setattr(worker_view, "worker_context", lambda st, j: {
        "pane": "Allow Stew to run tool \"stew.get_recent_events\"?\n" + "x" * 20000,
        "transcript": [{"kind": "said", "text": f"t{i}"} for i in range(10)], "git": "## b\n M a.py"})
    lead, _ = make_lead(store, Gate({}), Clock())
    pkt = lead.packet(jid, [{"kind": "x"}], "trg", tpol.Policy(token_budget=2000))
    assert pkt["worker"]["git"] == "## b\n M a.py" and len(pkt["worker"]["transcript"]) == 4
    assert len(json.dumps(pkt, default=str)) <= 2000 * 4 + 400


# -- runner wiring: the tick never waits on a review --------------------------------------------------------


def test_runner_tick_keeps_jev_doctor_and_code_facts_running_during_a_review(steward_home):
    from test_finish_line_gate import TimeoutRunner
    from test_stability import _runner

    from steward.worker import WorkerObservation

    store = Store(steward_home)
    job = _contracted_job(store, "fix it")
    (store.home / "worktrees" / job.job_id).mkdir(parents=True)
    idle = WorkerObservation(lifecycle="idle", output_tail="working", pid_alive=True, ts="t")
    runner = _runner(store, TimeoutRunner(), [[idle] * 50])
    gate = Gate({"astra-tech-lead": ["block"], "fable-tech-lead": ["block"]})
    pol = tpol.Policy(review_timeout_s=3600)
    wire_job_lead(runner, [ASTRA, FABLE], invoke=gate, policy_fn=lambda: pol)
    runner.job_lead.available = lambda e: None
    jev_calls, hung_calls = [], []
    runner.jev_step = lambda jid: jev_calls.append(jid)
    real_hung = runner.hung_step
    runner.hung_step = lambda jid, **k: hung_calls.append(jid) or real_hung(jid, **k)
    runner.start(job.job_id)
    runner.job_fact(job.job_id, "jev", "thrash", confidence=0.9)
    t0 = time.time()
    runner.tick(job.job_id)          # starts the (blocking) review
    store.append_event(job.job_id, what="finish_check_unverified", evidence={"command": "c", "reason": "timed out"},
                       rule="finish_line_gate", judgment=Judgment.code)
    runner.tick(job.job_id)
    runner.tick(job.job_id)
    assert time.time() - t0 < 5      # the blocked review never held a tick
    assert len(jev_calls) >= 3 and len(hung_calls) >= 3
    kinds = [f["kind"] for f in store.kv_get(job.job_id, rt._ARBITER)["facts"]]
    assert "finish_check_unverified" in kinds  # the code-fact floor kept running
    assert runner.job_lead.state(job.job_id)["in_flight"]["lead"] == "astra-tech-lead"
    assert len(gate.calls) == 1
    gate.release.set()


def test_unwired_runner_is_legacy_unchanged(steward_home):
    from test_stability import ScriptedRunner, _runner

    store = Store(steward_home)
    job = _contracted_job(store, "fix it")
    runner = _runner(store, ScriptedRunner(), [[]])
    assert runner.job_lead is None
    runner.start(job.job_id)
    runner.tick(job.job_id)
    assert not [e for e in store.events(job.job_id) if e.what.startswith("tech_lead")]


def test_fleet_supervisor_still_skips_legacy_jobs_without_a_job_lead(env):
    store, jid = env
    sup = tl.TechLeadSupervisor(store, controller=None, advisor=None)
    assert sup.on_event(jid) == {"status": "skipped", "reason": "legacy_job"}


def test_steward_run_wires_the_ordered_tech_lead_from_the_roster(steward_home, tmp_path, monkeypatch):
    from test_stability import ScriptedRunner, _runner

    from steward import cli

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfg))
    store = Store(steward_home)
    job = _contracted_job(store, "fix it")
    runner = _runner(store, ScriptedRunner(), [[]])
    cli._wire_job_tech_lead(store, runner, job.job_id)
    assert runner.job_lead is None
    assert any(e.what == "tech_lead_unwired" for e in store.events(job.job_id))
    (cfg / "roster.toml").write_text('''schema_version = 1
revision = 1

[[entries]]
id = "fable-tech-lead"
provider = "anthropic"
runtime = "claude-fabric"
model = "claude-fable-5-1"
roles = ["tech_lead"]
capabilities = ["plan", "review"]
strengths = []
enabled = true
label = "Fable"
priority = 2

[[entries]]
id = "astra-tech-lead"
provider = "openai"
runtime = "codex"
model = "gpt-6-astra"
roles = ["tech_lead"]
capabilities = ["plan", "review"]
strengths = []
enabled = true
label = "Astra"
priority = 1
''')
    cli._wire_job_tech_lead(store, runner, job.job_id)
    assert [e.id for e in runner.job_lead.leads] == ["astra-tech-lead", "fable-tech-lead"]
    wired = next(e for e in store.events(job.job_id) if e.what == "tech_lead_wired")
    assert wired.evidence["leads"] == ["astra-tech-lead", "fable-tech-lead"] and wired.evidence["policy"] == "defaults"


def test_packet_carries_takeover_conditions_and_the_failing_test_source(env):
    store, jid = env
    wt = store.home / "worktrees" / jid
    (wt / "tests").mkdir()
    (wt / "tests" / "test_owner_sweep.py").write_text("def test_owner_name_never_in_package():\n    assert 0\n")
    for _ in range(2):
        store.append_event(jid, what="verification failed; returning to running with guidance",
                           evidence={"reason": "first failing test tests.test_owner_sweep::t"}, rule="r",
                           judgment=Judgment.code)
    lead, _ = make_lead(store, Gate({}), Clock())
    facts = [{"kind": "new_failures_repeated", "data": {"test": "tests.test_owner_sweep::test_owner_name_never_in_package"}}]
    pkt = lead.packet(jid, facts, "trg", tpol.Policy())
    assert pkt["takeover_indicated"]["indicated"] is True and "survived 2 send-backs" in pkt["takeover_indicated"]["why"][0]
    assert "test_owner_name_never_in_package" in pkt["failing_tests"]["tests/test_owner_sweep.py"]
    pkt2 = lead.packet(jid, [{"kind": "thrash"}], "trg", tpol.Policy(fix_rounds=3))
    assert pkt2["takeover_indicated"]["indicated"] is False


def test_steward_run_wires_the_notebook_and_playbook_memory(steward_home, tmp_path, monkeypatch):
    from test_stability import ScriptedRunner, _runner

    from steward import cli
    from steward.tech_lead_notebook import Memory

    cfg = tmp_path / "cfg2"
    cfg.mkdir()
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfg))
    (cfg / "roster.toml").write_text('schema_version = 1\nrevision = 1\n\n[[entries]]\nid = "fable-tech-lead"\n'
                                     'provider = "anthropic"\nruntime = "claude-fabric"\nmodel = "claude-fable-5-1"\n'
                                     'roles = ["tech_lead"]\ncapabilities = ["plan"]\nstrengths = []\nenabled = true\n'
                                     'label = "Fable"\npriority = 2\n')
    store = Store(steward_home)
    job = _contracted_job(store, "fix it")
    runner = _runner(store, ScriptedRunner(), [[]])
    cli._wire_job_tech_lead(store, runner, job.job_id)
    assert isinstance(runner.job_lead.memory, Memory)


def test_packet_names_the_files_the_job_changed(env):
    from steward.runner_exec import CommandResult, FakeRunner

    store, jid = env
    store.add_check(jid, commit_sha="base1", command="pytest", exit_code=0, results={}, label="baseline")
    fr = FakeRunner(lambda argv, cwd: CommandResult(argv=argv, cwd=cwd, exit_code=0,
                                                     stdout="pkg/fleet_runtime.py\nNOTES.md\n", stderr="",
                                                     duration_s=0.0))
    lead, _ = make_lead(store, Gate({}), Clock())
    lead.executor.runner_exec = fr
    pkt = lead.packet(jid, [{"kind": "x"}], "trg", tpol.Policy())
    assert pkt["changed_files"] == ["pkg/fleet_runtime.py", "NOTES.md"]
    assert fr.calls[0]["argv"] == ["git", "diff", "--name-only", "base1..HEAD"]


# -- v2.7 Phase 41: free sweeps, the advisory allowance and the hourly limit ----------------------------------------------


def test_a_sweep_with_no_facts_invokes_no_lead_and_records_a_snapshot(env):
    store, jid = env
    clock = Clock()
    gate = Gate({"astra-tech-lead": [send_back_proposal()], "fable-tech-lead": []})
    lead, _ = make_lead(store, gate, clock)
    store.kv_set(jid, "last_class", "progressing")
    lead.on_event(jid)
    clock.t += 20 * 60
    store.append_event(jid, what="verification recorded", evidence={}, rule="r", judgment=Judgment.code)
    lead.on_event(jid)
    clock.t += 11 * 60
    assert lead.on_event(jid)["status"] == "skipped"
    assert not gate.calls
    assert [e.evidence["model_calls"] for e in store.events(jid) if e.what == "tech_lead_sweep"] == [0]


def test_past_the_allowance_or_four_an_hour_non_completion_reviews_wait_silently(env):
    store, jid = env
    clock = Clock()
    gate = Gate({"astra-tech-lead": [send_back_proposal() for _ in range(6)], "fable-tech-lead": []})
    lead, arb = make_lead(store, gate, clock)
    lead.spend_report_fn = lambda: type("R", (), {"worker_usd": 2.0, "supervision_usd": 3.5})()  # allowance max(3, 1) = 3
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    res = lead.on_event(jid)
    assert res["status"] == "deferred" and res["reason"] == "allowance" and not gate.calls
    assert store.get_job(jid).state == JobState.RUNNING  # advisory: it never pauses the job
    lead.spend_report_fn = lambda: type("R", (), {"worker_usd": 0.0, "supervision_usd": 0.0})()
    from steward import supervision_allowance as sa
    sa.reset_cache()
    lead._set(jid, pending_facts=None, retry_at=None, review_starts=[clock.t - 5] * 4)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="b")
    assert lead.on_event(jid)["reason"] == "rate"


# Issue P (2026-10-06): the runner and the control pane each hold a JobTechLead on ONE store. The instance that did not
# start a review has no thread for it and used to declare it "lost (the runner restarted)" within seconds, 90x in a day.
def second_instance(store, gate, clock, *, timeout_s=300):
    lead2, _ = make_lead(store, gate, clock, timeout_s=timeout_s)
    return lead2


def test_another_instance_does_not_expire_a_live_review_it_does_not_own(env):
    store, jid = env
    clock = Clock()
    gate = Gate({"astra-tech-lead": ["block"], "fable-tech-lead": [send_back_proposal()]})
    lead, arb = make_lead(store, gate, clock)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    assert lead.on_event(jid)["lead"] == "astra-tech-lead"
    other = second_instance(store, gate, clock)
    clock.t += 5
    assert other.on_event(jid)["status"] == "review_in_flight"
    assert not [e for e in store.events(jid) if e.what == "tech_lead_review_expired"]
    gate.release.set()
    wait_done(lead, jid)
    assert lead.on_event(jid)["status"] == "acted"  # the owner still finishes it
    assert [e.evidence["answered_by"] for e in store.events(jid) if e.what == "tech_lead_answered"] == ["astra-tech-lead"]


def test_a_foreign_review_past_its_limit_still_expires_and_falls_to_fable(env):
    store, jid = env
    clock = Clock()
    gate = Gate({"astra-tech-lead": ["block"], "fable-tech-lead": [send_back_proposal()]})
    lead, arb = make_lead(store, gate, clock, timeout_s=60)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    lead.on_event(jid)
    other = second_instance(store, gate, clock, timeout_s=60)
    clock.t += 61
    res = other.on_event(jid)
    assert res["status"] == "review_started" and res["lead"] == "fable-tech-lead"
    exp = [e for e in store.events(jid) if e.what == "tech_lead_review_expired"]
    assert len(exp) == 1 and exp[0].evidence["why"].startswith("no answer within")
    gate.release.set()


def test_a_review_whose_owner_process_is_gone_is_lost(env):
    store, jid = env
    clock = Clock()
    gate = Gate({"astra-tech-lead": ["block"], "fable-tech-lead": [send_back_proposal()]})
    lead, arb = make_lead(store, gate, clock)
    arb.submit(jid, code_facts.SOURCE, "finish_check_unverified", key="a")
    lead.on_event(jid)
    st = store.kv_get(jid, tl.JOB_STATE)
    st["in_flight"]["owner_pid"] = 2 ** 22 + 12345  # no such process
    store.kv_set(jid, tl.JOB_STATE, st)
    other = second_instance(store, gate, clock)
    res = other.on_event(jid)
    assert res["status"] == "review_started" and res["lead"] == "fable-tech-lead"
    exp = [e for e in store.events(jid) if e.what == "tech_lead_review_expired"]
    assert exp[0].evidence["why"] == "review lost (the runner restarted)"
    gate.release.set()
