# tests/pause_support.py
"""Test support for pause review (spec 2026-10-13): a fabric runner whose worker yields once, a real JobTechLead on a
scripted lead, and readers for pause facts, verdicts, steers and events. Not a test file (no test_ prefix)."""
from __future__ import annotations

import dataclasses
import json

from ownership_fixtures import SyncBg
from test_runner_fabric import BASE, NEW, _FabricFactory, _contracted_job, _fake_diagnoser, _frame, _obs, _yield_git
from test_tech_lead_no_clog import ASTRA, FABLE

from steward import fleet_tech_lead as tl
from steward import pause_checkpoint
from steward import tech_lead_actions as tla
from steward import tech_lead_policy as tpol
from steward.contract import Unit, load_contract
from steward.fleet_runtime import Arbiter
from steward.models import JobState, Judgment
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner
from steward.steering import Steering
from steward.store import Store

__all__ = ["BASE", "NEW", "ASTRA", "FABLE", "Unit"]
DEFAULT_SAID = "STEWARD-PAUSE unit=u1 next=u2 add the parser"


class FakeClock:
    def __init__(self, t: float = 1_800_000_000.0):  # a real-looking epoch: the idle guard ignores synthetic clocks
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> float:
        self.t += s
        return self.t


class Spend:
    def __init__(self, worker_usd: float, supervision_usd: float):
        self.worker_usd, self.supervision_usd = worker_usd, supervision_usd


def set_pause(store, job_id, mode="unit", units=None):
    contract = dataclasses.replace(load_contract(store, job_id), pause=mode, units=list(units or []))
    store.kv_set(job_id, "contract", dataclasses.asdict(contract))


def pause_runner(steward_home, *, mode="unit", said=DEFAULT_SAID, git=None, frames=None, lifecycle="idle",
                 scripts=None, clock=None, units=None, jev=None):
    """A started fabric runner on a pause job. By default its worker ends one turn (done frame at NEW, clean tree)."""
    store = Store(steward_home)
    job = _contracted_job(store)
    set_pause(store, job.job_id, mode, units)
    clock = clock or FakeClock()
    if scripts is None:
        frames = frames if frames is not None else (_frame(9, "done", {"sha": NEW}),)
        scripts = [[_obs(lifecycle=lifecycle, events=tuple(frames))]]
    factory = _FabricFactory(scripts=scripts)
    runner = Runner(store, factory, git or _yield_git(), _fake_diagnoser(), clock=clock, jev_observer=jev,
                    activity_fn=lambda st, jid: {"turns": [{"kind": "said", "text": said,
                                                            "ts": "2026-10-13T00:00:00Z"}]})
    runner.start(job.job_id)
    return store, job, runner, factory, clock


def mutable_git(box: dict):
    """A git fake whose HEAD / last subject / porcelain the test changes between ticks."""
    def respond(argv, cwd):
        def ok(out=""):
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=out, stderr="", duration_s=0.0)
        if argv[:2] == ["git", "merge-base"]:
            return ok(box.get("base", BASE) + "\n")
        if argv[:2] == ["git", "rev-parse"]:
            return ok(box["head"] + "\n")
        if argv[:2] == ["git", "log"]:
            return ok(box.get("subject", "") + "\n")
        if argv[:2] == ["git", "status"]:
            return ok(box.get("porcelain", ""))
        return ok()
    return FakeRunner(respond)


def rule(outcome="continue", **params):
    """A lead's answer: ONE rule_pause action."""
    p = {"outcome": outcome, "reason": params.pop("reason", "checked against the finish line"), **params}
    if outcome == "continue":
        p.setdefault("next_unit", {"id": "u2", "title": "add the parser", "finish_line": "pytest -q tests/test_p.py"})
    if outcome == "send_back":
        p.setdefault("text", "the parser test is missing")
    if outcome == "ask_owner":
        p.setdefault("decision", "Ship without the migration?")
    return json.dumps({"proposal_id": "pr-1", "trigger_id": "t", "confidence": 0.9, "signature": "pause",
                       "actions": [{"type": "rule_pause", "params": p}]})


class Leads:
    """`invoke(entry, prompt, timeout_s)` answering from a per-lead script (a string or an Exception); records prompts."""

    def __init__(self, plan: dict):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, entry, prompt, timeout_s):
        self.prompts.append((entry.id, prompt))
        script = self.plan.get(entry.id) or []
        if not script:
            raise TimeoutError("no answer scripted")
        answer = script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def wire_pause_lead(runner, plan, *, leads=(ASTRA, FABLE), notes=None):
    """A real JobTechLead on the runner (synchronous reviews, every lead available)."""
    notes = notes if notes is not None else []
    invoke = Leads(plan)
    ex = tla.ActionExecutor(runner.store, runner_exec=runner.runner_exec, notify_fn=lambda *a: notes.append(a),
                            clock=runner.clock)
    lead = tl.JobTechLead(runner.store, list(leads), ex, arbiter=runner.arbiter, invoke=invoke,
                          available=lambda e: None, policy_fn=lambda: tpol.Policy(), clock=runner.clock,
                          start_bg=SyncBg)
    lead.spend_report_fn = lambda: Spend(0.0, 0.0)
    runner.job_lead = lead
    return lead, invoke


def pause_lead_env(steward_home, plan, *, units=("u1", "u2"), clock=None, leads=(ASTRA, FABLE)):
    """A RUNNING pause job and a JobTechLead driven directly (no runner)."""
    store = Store(steward_home)
    job = _contracted_job(store)
    set_pause(store, job.job_id, "unit", [Unit(u, f"title {u}") for u in units])
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    (store.home / "worktrees" / job.job_id).mkdir(parents=True)
    clock = clock or FakeClock()
    notes: list = []
    arb = Arbiter(store, clock)
    ex = tla.ActionExecutor(store, runner_exec=_yield_git(), notify_fn=lambda *a: notes.append(a), clock=clock)
    invoke = Leads(plan)
    lead = tl.JobTechLead(store, list(leads), ex, arbiter=arb, invoke=invoke, available=lambda e: None,
                          policy_fn=lambda: tpol.Policy(), clock=clock, start_bg=SyncBg)
    lead.spend_report_fn = lambda: Spend(0.0, 0.0)
    return store, job.job_id, lead, arb, invoke, clock, notes


def prime_pause(store, job_id, arb, clock, **data):
    """What the runner records at a pause: the checkpoint and one `worker_paused` Arbiter fact."""
    cp = pause_checkpoint.load(store, job_id)
    fact = {"seq": 9, "sha": NEW, "yield_lane": "CANDIDATE", "failed": [], "label": None, "unit_id": "u1",
            "unit_matched": True, "done_claim": False, "next_unit": "u2", "next_title": "title u2",
            "said": "STEWARD-PAUSE unit=u1 next=u2 title u2", "pause_kind": "pause",
            "pause_seq": cp["pause_seq"] + 1, "unacked": [], **data}
    pause_checkpoint.update(store, job_id, lambda c: c.update(
        pause_seq=fact["pause_seq"], last_pause_fact=fact, last_pause_sha=fact["sha"], last_pause_at=clock(),
        awaiting_review=True, review_requested_at=clock(), done_claim=fact["done_claim"],
        unit_id=c["unit_id"] or fact["unit_id"]))
    arb.submit(job_id, "pause", "worker_paused", **fact)
    return fact


def pause_facts(store, job_id, kind="worker_paused"):
    doc = store.kv_get(job_id, "fleet_arbiter") or {}
    return [f for f in doc.get("facts", []) if f["source"] == "pause" and f["kind"] == kind]


def verdicts(store, job_id):
    return [e.evidence for e in store.events(job_id) if e.what == "pause_verdict"]


def steers(store, job_id, origin=None):
    return [i for i in Steering(store).all_instructions(job_id) if origin is None or i["origin"] == origin]


def events(store, job_id, what):
    return [e for e in store.events(job_id) if e.what == what]
