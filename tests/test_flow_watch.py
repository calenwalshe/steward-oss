"""v2.3 Flow through the Jev watch (phases 23-24): strict record-only shadow, active investigation gate."""

from __future__ import annotations

import json

from steward import config as config_module
from steward import masking
from steward.jev_observer import JevObserver, TransportResponse
from steward.jev_watch import JevWatch
from steward.models import JobState
from steward.steering import Steering
from steward.store import Store
from test_jev_watch import FAKE_KEY, FakeActions, ScriptedTransport, _running, _sig, _watch, keyed  # noqa: F401


# -- v2.3 Flow: strict record-only shadow (phase 23) -----------------------------------------------------------

_JEV_BOOKKEEPING = {"jev_stats", "jev_status", "jev_streak", "jev_exception_logged_at", "jev_cooldown",
                    "jev_diagnosis_requested_at", "supervisor_jev", config_module.FLOW_SWITCH_KEY}


def _flow_resp(label="retry_or_rebuild", conf=0.9, triage="ok", tconf=0.9, flow_raw=None):
    answers = {"triage": {"choice": triage, "confidence": tconf, "probabilities": {triage: tconf}}}
    if flow_raw is not None:
        answers["flow"] = flow_raw
    elif label is not None:
        answers["flow"] = {"choice": label, "confidence": conf, "probabilities": {label: conf}}
    return TransportResponse(status_code=200, body=json.dumps({"answers": answers}).encode())


class AllActions(FakeActions):
    """Every route the Jev gate can take, recorded: Flow must reach none of them."""

    def finished(self, job_id):
        self.calls.append(("finished", job_id))

    def done_unproven(self, job_id):
        self.calls.append(("done_unproven", job_id))

    def request_diagnosis(self, job_id, evidence):
        self.calls.append(("request_diagnosis", evidence["choice"]))
        return "diag"


def _kv_dump(store, job_id):
    rows = store._conn.execute("SELECT key, value_json FROM kv WHERE job_id = ?", (job_id,)).fetchall()
    return {r["key"]: r["value_json"] for r in rows if r["key"] not in _JEV_BOOKKEEPING}


def _flow_watch(store, responses, *, flow="shadow", jev="shadow", now=None, turns=None):
    job_id = store.list_jobs()[0].job_id
    store.kv_set(job_id, config_module.FLOW_SWITCH_KEY, flow)
    turns = turns if turns is not None else [{"i": 7, "ts": "2026-10-02T00:00:00Z", "kind": "error",
                                              "text": "build failed again"}]
    w, t, actions, now = _watch(store, responses, mode=jev, actions=AllActions(), now=now,
                                activity=lambda s, j: {"turns": turns})
    return w, t, actions, now, job_id


def _step(w, job_id, now, n=1):
    for _ in range(n):
        w.step(job_id, _sig(), force=True)
        now[0] += 10.0


def test_flow_off_asks_nothing_and_records_nothing(steward_home, keyed):
    store = Store(steward_home)
    _running(store)
    w, t, actions, now, job_id = _flow_watch(store, [_flow_resp()], flow="off")
    _step(w, job_id, now, 3)
    assert all("flow" not in c["questions"] for c in t.calls)
    assert store.flow_episodes(job_id) == [] and actions.calls == []


def test_flow_shadow_records_one_episode_and_has_zero_side_effects(steward_home, keyed):
    store = Store(steward_home)
    _running(store)
    w, t, actions, now, job_id = _flow_watch(store, [_flow_resp("retry_or_rebuild")])
    before = _kv_dump(store, job_id)
    instructions_before = Steering(store).all_instructions(job_id)
    _step(w, job_id, now, 5)
    assert len(t.calls) == 5 and all("flow" in c["questions"] for c in t.calls)
    eps = store.flow_episodes(job_id)
    assert len(eps) == 1 and eps[0]["label"] == "retry_or_rebuild" and eps[0]["mode"] == "shadow"
    assert "turn:7" in eps[0]["source_refs"]
    assert actions.calls == []
    assert Steering(store).all_instructions(job_id) == instructions_before
    assert _kv_dump(store, job_id) == before
    whats = {e.what for e in store.events(job_id)}
    assert not whats & {"jev_flag", "jev_escalated", "jev_diagnosis_requested", "flow_investigation_started"}
    assert store.get_job(job_id).state == JobState.RUNNING


def test_flow_shadow_with_active_jev_still_never_routes_flow(steward_home, keyed):
    store = Store(steward_home)
    _running(store)
    w, t, actions, now, job_id = _flow_watch(store, [_flow_resp("supervision_churn")], jev="active")
    _step(w, job_id, now, 4)
    assert len(store.flow_episodes(job_id)) == 1 and actions.calls == []


def test_flow_noops_for_smooth_uncertain_missing_malformed_and_low(steward_home, keyed):
    store = Store(steward_home)
    _running(store)
    responses = [_flow_resp("smooth"), _flow_resp("uncertain"), _flow_resp(None), _flow_resp(flow_raw="junk"),
                 _flow_resp(flow_raw={"choice": "wrong_direction", "confidence": 3}), _flow_resp("wrong_direction", 0.4),
                 _flow_resp(flow_raw={"choice": "delete_repo", "confidence": 0.99})]
    w, t, actions, now, job_id = _flow_watch(store, responses)
    _step(w, job_id, now, len(responses))
    assert store.flow_episodes(job_id) == [] and actions.calls == []
    assert not [e for e in store.events(job_id) if e.what.startswith("flow_")]


def test_flow_transport_failure_is_a_no_op(steward_home, keyed):
    store = Store(steward_home)
    _running(store)
    w, t, actions, now, job_id = _flow_watch(store, [ConnectionError("down"),
                                                     TransportResponse(status_code=503, body=b"")])
    _step(w, job_id, now, 2)
    assert store.flow_episodes(job_id) == [] and actions.calls == []


def test_flow_jev_off_means_no_request_and_no_flow(steward_home, keyed):
    store = Store(steward_home)
    _running(store)
    w, t, actions, now, job_id = _flow_watch(store, [_flow_resp()], jev="off")
    _step(w, job_id, now, 3)
    assert t.calls == [] and store.flow_episodes(job_id) == []


def test_flow_does_not_change_baseline_triage(steward_home, keyed):
    """The same triage answers produce the same Jev events and routes with Flow off and Flow shadow."""
    script = [("stalled", 0.9), ("stalled", 0.9), ("looping", 0.85), ("looping", 0.85), ("ok", 0.9)]
    outcomes = []
    for flow in ("off", "shadow"):
        home = steward_home / flow
        home.mkdir()
        store = Store(home)
        _running(store)
        responses = [_flow_resp("wrong_direction", triage=c, tconf=k) for c, k in script]
        w, t, actions, now, job_id = _flow_watch(store, responses, flow=flow, jev="active")
        _step(w, job_id, now, len(script))
        events = [(e.what, e.evidence.get("choice")) for e in store.events(job_id)
                  if e.what.startswith("jev_")]
        outcomes.append((events, [(c[0], "<job>" if c[1] == job_id else c[1]) for c in actions.calls]))
    assert outcomes[0] == outcomes[1]
    assert ("request_diagnosis", "stalled") in outcomes[0][1]


def test_flow_dedupes_across_restart(steward_home, keyed):
    store = Store(steward_home)
    _running(store)
    w, t, actions, now, job_id = _flow_watch(store, [_flow_resp("blocked_handoff")])
    _step(w, job_id, now)
    store2 = Store(steward_home)
    obs = JevObserver(transport=ScriptedTransport([_flow_resp("blocked_handoff")]), enabled=True,
                      api_key_fn=lambda: FAKE_KEY, clock=lambda: now[0])
    w2 = JevWatch(store2, obs, AllActions(), lambda: now[0],
                  activity_fn=lambda s, j: {"turns": [{"i": 8, "ts": "x", "kind": "said", "text": "waiting"}]})
    _step(w2, job_id, now, 3)
    assert len(store2.flow_episodes(job_id)) == 1


def test_flow_episode_is_redacted(steward_home, keyed):
    store = Store(steward_home)
    _running(store)
    masking.register(["literal-flow-key-424242"])
    try:
        turns = [{"i": 1, "ts": "x", "kind": "said",
                  "text": "retrying with API_KEY=hunter2hunter2 sk-abcdefghijklmnopqr literal-flow-key-424242"}]
        w, t, actions, now, job_id = _flow_watch(store, [_flow_resp("retry_or_rebuild")], turns=turns)
        _step(w, job_id, now)
        blob = json.dumps(store.flow_episodes(job_id)) + json.dumps([e.evidence for e in store.events(job_id)])
        for leaked in ("hunter2hunter2", "sk-abcdefghijklmnopqr", "literal-flow-key-424242"):
            assert leaked not in blob
        assert len(store.flow_episodes(job_id)) == 1
    finally:
        masking.clear()


def test_flow_store_failure_never_breaks_the_observer(steward_home, keyed, monkeypatch):
    store = Store(steward_home)
    _running(store)
    w, t, actions, now, job_id = _flow_watch(store, [_flow_resp("retry_or_rebuild")])
    monkeypatch.setattr(store, "record_flow_episode", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    result = w.step(job_id, _sig(), force=True)
    assert result is not None and result["choice"] == "ok" and actions.calls == []


def test_api_exposes_flow_episodes(steward_home, keyed):
    from steward.api import StewardAPI

    store = Store(steward_home)
    _running(store)
    w, t, actions, now, job_id = _flow_watch(store, [_flow_resp("self_correction")])
    _step(w, job_id, now)
    view = StewardAPI(store).get_flow(job_id)
    assert view["mode"] == "shadow" and [e["label"] for e in view["episodes"]] == ["self_correction"]
    assert StewardAPI(store).get_flow("nope") is None


# -- v2.3 Flow: active-mode investigation gate (phase 24) ------------------------------------------------------


class _Inv:
    def __init__(self, text=None, exc=None):
        from steward import flow_investigator as fi

        self.fi, self.exc, self.prompts = fi, exc, []
        self.text = text or json.dumps({"root_cause": "rebuild loop", "evidence_refs": ["turn:7"],
                                        "recommended_action": "nudge", "instruction": "fix the dep first"})

    def investigate(self, prompt):
        self.prompts.append(prompt)
        if self.exc:
            raise self.exc
        return self.fi.RawAnswer(self.text, 0.01)


def _active_watch(store, responses, inv, *, flow="active", jev="shadow", executor=None):
    w, t, actions, now, job_id = _flow_watch(store, responses, flow=flow, jev=jev)
    w.flow_investigator = inv
    w.flow_executor = executor or (lambda fn: fn())
    return w, t, actions, now, job_id


def test_active_investigates_each_episode_once(steward_home, keyed):
    store = Store(steward_home)
    _running(store)
    inv = _Inv()
    w, t, actions, now, job_id = _active_watch(store, [_flow_resp("retry_or_rebuild")], inv)
    _step(w, job_id, now, 4)
    assert len(inv.prompts) == 1
    [rec] = store.flow_investigations(job_id)
    assert rec["state"] == "accepted" and rec["result"]["recommended_action"] == "nudge"
    # the investigator itself acts on nothing; since phase 25 only the controller's one decision may steer
    assert actions.calls == [] and len(store.flow_decisions(job_id)) == 1
    assert {i["origin"] for i in Steering(store).all_instructions(job_id)} <= {"flow"}


def test_shadow_and_off_never_call_the_investigator(steward_home, keyed):
    for flow in ("shadow", "off"):
        home = steward_home / flow
        home.mkdir()
        store = Store(home)
        _running(store)
        inv = _Inv()
        w, t, actions, now, job_id = _active_watch(store, [_flow_resp("wrong_direction")], inv, flow=flow)
        _step(w, job_id, now, 3)
        assert inv.prompts == [] and store.flow_investigations(job_id) == []


def test_shadow_episode_is_never_investigated_after_switching_to_active(steward_home, keyed):
    store = Store(steward_home)
    _running(store)
    inv = _Inv()
    w, t, actions, now, job_id = _active_watch(store, [_flow_resp("wrong_direction")], inv, flow="shadow")
    _step(w, job_id, now)
    store.kv_set(job_id, config_module.FLOW_SWITCH_KEY, "active")
    _step(w, job_id, now, 2)  # same label inside the window: the shadow episode stands, record-only
    assert inv.prompts == [] and store.flow_episodes(job_id)[0]["mode"] == "shadow"


def test_investigator_failure_never_stalls_the_observer(steward_home, keyed):
    from steward import flow_investigator as fi

    store = Store(steward_home)
    _running(store)
    inv = _Inv(exc=fi.Unavailable("provider_exit_124"))
    w, t, actions, now, job_id = _active_watch(store, [_flow_resp("blocked_handoff")], inv)
    results = [w.step(job_id, _sig(), force=True) for _ in range(3)]
    assert all(r is not None and r["choice"] == "ok" for r in results)
    assert store.flow_investigations(job_id)[0]["state"] == "failed" and len(inv.prompts) == 1


def test_executor_failure_is_recorded_not_raised(steward_home, keyed):
    store = Store(steward_home)
    _running(store)

    def broken(fn):
        raise RuntimeError("no threads")

    w, t, actions, now, job_id = _active_watch(store, [_flow_resp("blocked_handoff")], _Inv(), executor=broken)
    assert w.step(job_id, _sig(), force=True)["choice"] == "ok"
    assert any(e.what == "flow_unavailable" for e in store.events(job_id))


def test_default_executor_runs_off_the_observer_thread(steward_home, keyed):
    import threading

    from steward import flow_investigator as fi

    store = Store(steward_home)
    _running(store)
    gate, seen = threading.Event(), []

    class Slow(_Inv):
        def investigate(self, prompt):
            seen.append(threading.current_thread().name)
            gate.wait(5)
            return super().investigate(prompt)

    w, t, actions, now, job_id = _flow_watch(store, [_flow_resp("retry_or_rebuild")], flow="active")
    w.flow_investigator = Slow()
    assert w.flow_executor is fi.run_in_thread
    assert w.step(job_id, _sig(), force=True)["choice"] == "ok"  # returned while the model is still "thinking"
    gate.set()
    for _ in range(100):
        rec = store.flow_investigations(job_id)
        if rec and rec[0]["state"] != "running":
            break
        threading.Event().wait(0.02)
    assert rec[0]["state"] == "accepted" and seen == ["steward-flow-investigation"]


def test_restart_continues_a_justified_interrupted_attempt_once(steward_home, keyed):
    store = Store(steward_home)
    _running(store)
    inv = _Inv()
    w, t, actions, now, job_id = _active_watch(store, [_flow_resp("retry_or_rebuild")], inv, executor=lambda fn: None)
    _step(w, job_id, now)  # claimed, but the "process" dies before the model answers
    ep = store.flow_episodes(job_id)[0]
    store.claim_flow_investigation(ep["episode_id"], job_id, owner="dead", now=1.0, model=None)
    w2, *_ = _active_watch(Store(steward_home), [_flow_resp("retry_or_rebuild")], inv)
    _step(w2, job_id, now)
    # §7 permits a bounded continuation from an interrupted checkpoint when a
    # fresh observer can make progress; the uncertain attempt itself is never
    # treated as a verdict or used to act.
    assert store.flow_investigation(ep["episode_id"])["state"] == "accepted"
    assert len(inv.prompts) == 1
