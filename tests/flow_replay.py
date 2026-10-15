"""Fake-clock Flow replay (v2.3 FLOW-10): scripted jobs through the real Store, JevObserver, JevWatch, Flow
investigator lifecycle and controller. Everything outside is injected: a fake clock, a scripted Jev transport, a
scripted investigator, a synchronous executor. No network, no model, no Herdr, no recipient.

Each `Scenario` is one RUNNING job: a list of Flow labels (one per 10 s tick), the investigator's raw answer (or an
exception) for each new episode, and optional human steps (a steer before the ticks, an adjudication, a reported
miss, a restart mid-run). `run()` replays every scenario, advances the clock past the outcome window, and returns the
normalized record plus `digest()` of it, so repeated runs must produce identical digests. Tests live in
`tests/test_flow_actions.py`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from steward import config, flow_actions, jev_observer, masking
from steward import flow_investigator as fi
from steward.jev_watch import JevWatch
from steward.models import JobState, Judgment
from steward.steering import Steering
from steward.store import Store

FAKE_KEY = "fake-typesafe-key-replay"
SECRET = "literal-replay-secret-31337"
START = 1_000_000.0


def answer(action="nudge", refs=("turn:7",), instruction=None, **extra) -> fi.RawAnswer:
    body = {"root_cause": f"replayed cause for {action}", "evidence_refs": list(refs), "recommended_action": action,
            "instruction": instruction if instruction is not None else
            ("Fix the missing dependency before rebuilding." if action in ("nudge", "focus") else "")}
    body.update(extra)
    return fi.RawAnswer(json.dumps(body), 0.02)


@dataclass
class Scenario:
    name: str
    labels: list
    answers: list = field(default_factory=list)
    flow_mode: str = "active"
    review: str | None = None  # true_positive | false_positive | unclear
    verdict: str | None = None
    miss: str | None = None
    human_steer_between: bool = False  # a human steers after detection, before the controller decides
    restart_after: int | None = None
    turn_text: str = "build failed again"


def _resp(label):
    answers = {"triage": {"choice": "ok", "confidence": 0.9, "probabilities": {"ok": 0.9}}}
    if label is not None:
        answers["flow"] = {"choice": label, "confidence": 0.92, "probabilities": {label: 0.92}}
    from steward.jev_observer import TransportResponse

    return TransportResponse(status_code=200, body=json.dumps({"answers": answers}).encode())


class _Transport:
    def __init__(self, labels):
        self.labels, self.calls = list(labels), 0

    def post(self, url, body, headers, timeout):
        label = self.labels[min(self.calls, len(self.labels) - 1)]
        self.calls += 1
        return _resp(label)


class _Investigator:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def investigate(self, prompt):
        a = self.answers[min(self.calls, len(self.answers) - 1)] if self.answers else answer()
        self.calls += 1
        if isinstance(a, Exception):
            raise a
        return a


class _NoActions:
    """The legacy Jev routes: a Flow replay must never reach any of them."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*a, **k):
            self.calls.append(name)
        return record


def _job(store, name):
    from test_runner import _contracted_job

    job = _contracted_job(store, f"replay {name}")
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    return job.job_id


def _watch(store, labels, inv, now, turn_text):
    obs = jev_observer.JevObserver(transport=_Transport(labels), enabled=True, api_key_fn=lambda: FAKE_KEY,
                                   clock=lambda: now[0])
    turns = [{"i": 7, "ts": "2026-10-02T00:00:00Z", "kind": "error", "text": turn_text}]
    actions = _NoActions()
    w = JevWatch(store, obs, actions, lambda: now[0], activity_fn=lambda s, j: {"turns": turns})
    w.flow_investigator, w.flow_executor = inv, (lambda fn: fn())
    return w, actions


def run(home, scenarios: list[Scenario]) -> dict:
    """Replay every scenario in its own job; return the normalized record (see `normalize`)."""
    masking.register([SECRET])
    try:
        store = Store(home)
        now = [START]
        out = {"jobs": {}, "legacy_routes": [], "investigator_calls": 0}
        for sc in scenarios:
            job_id = _job(store, sc.name)
            store.kv_set(job_id, "supervisor_jev", "shadow")
            store.kv_set(job_id, config.FLOW_SWITCH_KEY, sc.flow_mode)
            inv = _Investigator(sc.answers)
            w, actions = _watch(store, sc.labels, inv, now, sc.turn_text)
            queued: list = []
            if sc.human_steer_between:
                w.flow_executor = queued.append  # the investigation is still "running" when the human steers
            for i, _ in enumerate(sc.labels):
                if sc.human_steer_between and i == 1:
                    Steering(store).record(job_id, "nudge", "human: do it my way", origin="moshi")
                    Steering(store).mark_acknowledged(job_id, Steering(store).all_instructions(job_id)[-1]["rev"],
                                                      "acknowledged")
                    for fn in queued:
                        fn()
                if sc.restart_after is not None and i == sc.restart_after:
                    store = Store(home)  # a controller restart: new connection, new watch, same durable state
                    w, actions2 = _watch(store, sc.labels[i:], inv, now, sc.turn_text)
                    actions.calls += actions2.calls
                    actions = actions2
                w.step(job_id, None, force=True)
                now[0] += 10.0
            now[0] += flow_actions.OUTCOME_WINDOW_S + 1  # let outcomes come due, then one quiet tick records them
            w.observer.transport = _Transport(["smooth"])
            w.step(job_id, None, force=True)
            for ep in store.flow_episodes(job_id):
                if sc.review:
                    flow_actions.adjudicate(store, ep["episode_id"], sc.review, actor="cli", action_verdict=sc.verdict)
            if sc.miss:
                flow_actions.report_miss(store, job_id, sc.miss, actor="cli", note="seen by a human")
            out["jobs"][sc.name] = job_id
            out["legacy_routes"] += actions.calls
            out["investigator_calls"] += inv.calls
        out["store"] = store
        return out
    finally:
        masking.clear()


def normalize(store: Store, jobs: dict) -> dict:
    """Per scenario: Flow event kinds, episodes (label/mode), investigation states, decisions, outcomes, reviews,
    flow-origin steers. Ids, timestamps and paths are dropped."""
    rec = {}
    for name, job_id in sorted(jobs.items()):
        rec[name] = {
            "events": [e.what for e in store.events(job_id) if e.what.startswith("flow_")],
            "episodes": [(e["label"], e["mode"]) for e in store.flow_episodes(job_id)],
            "investigations": [(i["state"], i["reason"]) for i in store.flow_investigations(job_id)],
            "decisions": [(d["action"], d["decision"], d["reason"], (d["outcome"] or {}).get("outcome"))
                          for d in store.flow_decisions(job_id)],
            "reviews": [(r["label"], r["action_verdict"]) for r in store.flow_reviews(job_id)],
            "flow_steers": [(i["verb"], i["text"]) for i in Steering(store).all_instructions(job_id)
                            if i["origin"] == "flow"],
            "state": store.get_job(job_id).state.value,
        }
    return rec


def digest(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def standard_scenarios() -> list[Scenario]:
    """Representative friction, false positive, miss, duplicate, restart, masking, outage and prohibited actions."""
    return [
        Scenario("wrong_direction", ["wrong_direction"] * 3, [answer("focus")], review="true_positive",
                 verdict="helpful"),
        Scenario("self_correction", ["self_correction"], [answer("no_action")], review="true_positive"),
        Scenario("supervisor_correction", ["supervisor_correction"], [answer("escalate_human")],
                 review="true_positive"),
        Scenario("retry_or_rebuild", ["retry_or_rebuild", "smooth", "retry_or_rebuild"], [answer("watcher_review")],
                 review="true_positive"),
        Scenario("blocked_handoff", ["blocked_handoff"], [answer("recovery")], review="unclear"),
        Scenario("supervision_churn", ["supervision_churn"], [fi.Unavailable("provider_exit_1")]),
        Scenario("false_positive", ["wrong_direction"], [answer("nudge")], review="false_positive", verdict="harmful"),
        Scenario("miss", ["smooth", "uncertain", "smooth"], miss="blocked_handoff"),
        Scenario("duplicate_and_restart", ["retry_or_rebuild"] * 4, [answer("nudge")], restart_after=2,
                 review="true_positive", verdict="neutral"),
        Scenario("masking", ["wrong_direction"], [answer("nudge", instruction=f"use key {SECRET} then retry")],
                 turn_text=f"export API_KEY=hunter2hunter2 {SECRET}"),
        Scenario("invented_evidence", ["wrong_direction"], [answer("nudge", refs=("event:999999",))]),
        Scenario("prohibited_merge", ["wrong_direction"], [answer("merge")]),
        Scenario("prohibited_permission", ["wrong_direction"], [answer("permission")]),
        Scenario("malformed_model", ["wrong_direction"], [fi.RawAnswer("{not json")]),
        Scenario("model_outage", ["wrong_direction"], [fi.Unavailable("provider_error:TimeoutExpired")]),
        Scenario("human_overrides", ["wrong_direction"] * 2, [answer("nudge")], human_steer_between=True),
        Scenario("shadow_only", ["wrong_direction"] * 2, [answer("nudge")], flow_mode="shadow"),
        Scenario("off", ["wrong_direction"] * 2, [answer("nudge")], flow_mode="off"),
    ]
