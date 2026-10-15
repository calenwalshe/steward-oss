"""Shared isolated fixtures for the continuous-job-ownership tests (Plan 1). Shapes are sanitized from incident
evidence (8025f70f Tracks park, a8996d99 escalation loop, d22d6633 recovery approval). Never production state,
never a real notifier, never a real model."""
from __future__ import annotations

import json
import threading
import time

from steward import fleet_registry as fr
from steward.actor import Actor, ActorRef
from steward.contract import Contract, store_contract
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

ASTRA = fr.RosterEntry(id="astra-tech-lead", provider="openai", runtime="codex", model="gpt-6-astra",
                       roles=("tech_lead",), capabilities=("plan",), strengths=(), enabled=True, label="Astra",
                       priority=1)
FABLE = fr.RosterEntry(id="fable-tech-lead", provider="anthropic", runtime="claude-fabric", model="claude-fable-5-1",
                       roles=("tech_lead",), capabilities=("plan",), strengths=(), enabled=True, label="Fable",
                       priority=2)
CONTROLLER = ActorRef(Actor.CONTROLLER, "ownership")
LEAD = ActorRef(Actor.TECH_LEAD, "astra-tech-lead")
OWNER = ActorRef(Actor.OWNER, "cli")
JEV = ActorRef(Actor.JEV, "jev")
OPERATOR = ActorRef(Actor.OPERATOR_AGENT, "claude-code")

TRACKS_LAST_SAID = ("I did not reach the finish line, and I'm not printing STEWARD-DONE. Step 3 hit a STOP rule on "
                    "liveness... Waiting for Sam to check the unit.")


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> float:
        self.t += s
        return self.t


class Notes:
    """Recording phone notifier (`send`) and operational sender (`send_text`); never `tg`."""

    def __init__(self):
        self.events: list = []
        self.texts: list[str] = []

    def send(self, event) -> bool:
        self.events.append(event)
        return True

    def send_text(self, text: str) -> bool:
        self.texts.append(text)
        return True

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


def git_ok(head: str = "sha-1") -> FakeRunner:
    def respond(argv, cwd):
        out = head + "\n" if argv[:2] == ["git", "rev-parse"] else "base-0\n" if argv[:2] == ["git", "merge-base"] else ""
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)
    return FakeRunner(respond)


def contracted(store: Store, goal: str = "fix it. Finish line: `pytest -q`") -> str:
    job = store.create_job(goal, "/repo")
    store_contract(store, job.job_id, Contract(goal=goal, done_when=["done"]))
    (store.home / "worktrees" / job.job_id).mkdir(parents=True, exist_ok=True)
    return job.job_id


def _run_then_finish(store: Store, jid: str, *, marker_rule: str, marker: str) -> None:
    store.set_state(jid, JobState.RUNNING, what="worker dispatch started", evidence={}, rule="worker_dispatch",
                    judgment=Judgment.code)
    store.set_state(jid, JobState.WORKER_FINISHED, what="worker reported done", evidence={"marker": marker},
                    rule=marker_rule, judgment=Judgment.code)


def park_tracks(store: Store) -> str:
    """8025f70f (2026-10-04): the worker said it was NOT done and went idle; closeout parked it as an acceptance
    hold with two technical reasons."""
    jid = contracted(store, "Circuit Tracks Phase 2 finish. Finish line: verify --phase 2 passes")
    store.set_state(jid, JobState.RUNNING, what="worker dispatch started", evidence={}, rule="worker_dispatch",
                    judgment=Judgment.code)
    store.append_event(jid, what="idle_after_final_message",
                       evidence={"idle_s": 400, "last_said": TRACKS_LAST_SAID, "said_at": "t", "class": "idle"},
                       rule="idle_after_final_message", judgment=Judgment.code)
    store.set_state(jid, JobState.WORKER_FINISHED, what="worker idle after its final message",
                    evidence={"marker": "idle_after_final_message"}, rule="idle_after_final_message",
                    judgment=Judgment.code)
    store.set_state(jid, JobState.NEEDS_HUMAN, what="checks passed; acceptance unresolved",
                    evidence={"reasons": ["1 baseline failure(s) still failing: ['t::a']",
                                          "test-weakening detected: ['removed_test_function']"]},
                    rule="acceptance_unresolved", judgment=Judgment.code)
    return jid


def park_done(store: Store, reasons: list[str] | None = None, *, goal: str = "fix it. Finish line: `pytest -q`") -> str:
    """STEWARD-DONE + checks passed + closeout acceptance hold with the given reasons (default: human mode)."""
    jid = contracted(store, goal)
    _run_then_finish(store, jid, marker_rule="steward_done_marker", marker="STEWARD-DONE")
    store.set_state(jid, JobState.NEEDS_HUMAN, what="checks passed; acceptance unresolved",
                    evidence={"reasons": reasons if reasons is not None else
                              ["checks passed — Sam accepts (acceptance mode: human)"]},
                    rule="acceptance_unresolved", judgment=Judgment.code)
    return jid


def park_rule(store: Store, rule: str, what: str, evidence: dict | None = None) -> str:
    """A job parked by one runner/closeout escalation site (`rule`, `what` exactly as that site writes them)."""
    jid = contracted(store)
    store.set_state(jid, JobState.RUNNING, what="worker dispatch started", evidence={}, rule="worker_dispatch",
                    judgment=Judgment.code)
    if rule in ("verify_rounds_exhausted", "bg_verify_unverified"):
        store.set_state(jid, JobState.WORKER_FINISHED, what="worker reported done", evidence={"marker": "STEWARD-DONE"},
                        rule="steward_done_marker", judgment=Judgment.code)
    store.set_state(jid, JobState.NEEDS_HUMAN, what=what, evidence=evidence or {}, rule=rule, judgment=Judgment.code)
    return jid


class ScriptedLead:
    """A tech-lead invoker: per lead id, a list of answers (str JSON, Exception, or "block")."""

    def __init__(self, plan: dict[str, list]):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.calls: list[tuple[str, str]] = []
        self.release = threading.Event()

    def __call__(self, entry, prompt, timeout_s):
        self.calls.append((entry.id, prompt))
        b = self.plan.get(entry.id, []).pop(0) if self.plan.get(entry.id) else "block"
        if b == "block":
            self.release.wait(30)
            raise TimeoutError("released")
        if isinstance(b, Exception):
            raise b
        return b


def proposal(*actions: dict, confidence: float = 0.9, pid: str | None = None, strategy: str = "") -> str:
    return json.dumps({"proposal_id": pid or f"p-{time.time_ns()}", "trigger_id": "t", "confidence": confidence,
                       "judgment": False, "signature": "sig", "plan_item": "", "strategy": strategy,
                       "actions": list(actions)})


def wait_review(lead, job_id: str, timeout: float = 5.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        bg = lead._reviews.get(job_id)
        if bg is None or bg.done():
            return
        time.sleep(0.01)


class SyncBg:
    """A synchronous `start_bg` for JobTechLead: the call runs at once; the next `on_event` finishes it."""

    def __init__(self, fn):
        self._r, self._e = None, None
        try:
            self._r = fn()
        except BaseException as exc:  # noqa: BLE001 - delivered by result()
            self._e = exc

    def done(self) -> bool:
        return True

    def result(self):
        if self._e is not None:
            raise self._e
        return self._r


class FakeLead:
    """The JobTechLead surface the sweep uses (`state`, `on_event`), recording calls."""

    def __init__(self):
        self.calls: list[dict] = []
        self._state: dict = {}

    def state(self, job_id):
        return self._state.get(job_id, {})

    def on_event(self, job_id, *, sweep=False, reason="", blocker_ids=None):
        self.calls.append({"job": job_id, "sweep": sweep, "reason": reason, "blocker_ids": list(blocker_ids or [])})
        if blocker_ids:
            self._state[job_id] = {"in_flight": {"lead": "astra-tech-lead", "blocker_ids": list(blocker_ids)}}
        return {"status": "review_started"}


def control_lead(store: Store, plan, *, send_back_fn=None, policy=None, start_bg=SyncBg, **wire_kw):
    """The control pane's JobTechLead over `store`, with every roster entry available and a fake git."""
    from steward import sendback
    from steward import tech_lead_policy as tpol
    from steward.runner import wire_control_lead

    scripted = plan if isinstance(plan, ScriptedLead) else ScriptedLead(plan)

    def _sb(job_id, text, origin, request_id, judgment):
        return sendback.send_back(store, job_id, text, origin=origin, request_id=request_id, judgment=judgment)

    lead = wire_control_lead(store, [ASTRA, FABLE], send_back_fn=send_back_fn or _sb,
                             policy_fn=lambda: policy or tpol.Policy(), runner_exec=git_ok(), invoke=scripted,
                             start_bg=start_bg, **wire_kw)
    lead.available = lambda e: None
    return lead, scripted
