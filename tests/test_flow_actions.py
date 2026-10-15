"""v2.3 Flow bounded action (phase 25): finite allowlist, existing guarded mechanisms, auditable receipts."""

from __future__ import annotations

import pytest

from steward import flow_actions as fa
from steward.models import JobState, Judgment
from steward.steering import Steering, _is_non_human, provenance_judgment
from steward.store import Store

REFS = ["turn:7", "event:12"]


def _job(store, state=JobState.RUNNING):
    from test_runner import _contracted_job

    job = _contracted_job(store, "fix the parser")
    if state == JobState.RUNNING:
        store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    return job.job_id


def _episode(store, job_id, *, label="retry_or_rebuild", conf=0.9, mode="active", now=1000.0, rev=None):
    rec, _ = store.record_flow_episode(
        job_id, episode_id=f"flow-{label}-{now}", label=label, confidence=conf, detector_version="flow-v1", mode=mode,
        snapshot={"label": label}, source_refs=list(REFS),
        job_revision=store.get_revision(job_id) if rev is None else rev, head_sha="abc", now=now,
        dedupe_window_s=900.0)
    return rec


def _investigation(store, ep, action="nudge", instruction="Install the missing dependency before rebuilding.",
                   refs=("turn:7",), state="accepted"):
    store.claim_flow_investigation(ep["episode_id"], ep["job_id"], owner="t", now=1.0, model="m")
    result = {"root_cause": "rebuild loop", "evidence_refs": list(refs), "recommended_action": action,
              "instruction": instruction} if state == "accepted" else None
    return store.finish_flow_investigation(ep["episode_id"], state, result=result,
                                           reason=None if state == "accepted" else "invalid_json")


# -- task 1: allowlist and nonhuman origin ----------------------------------------------------------------------


def test_flow_origin_is_registered_nonhuman_and_model_informed():
    assert _is_non_human("flow")
    assert provenance_judgment("flow") == Judgment.model_informed_code_authorized


def test_flow_origin_can_only_nudge_or_focus_and_never_urgently(steward_home):
    store = Store(steward_home)
    job_id = _job(store)
    steering = Steering(store)
    for verb in ("goal", "avoid", "authority", "priority"):
        with pytest.raises(ValueError):
            steering.record(job_id, verb, "x", origin="flow")
    with pytest.raises(ValueError):
        steering.record(job_id, "nudge", "x", origin="flow", urgent=True)
    assert steering.record(job_id, "nudge", "x", origin="flow").accepted


def test_flow_steer_is_fenced_by_any_unacknowledged_instruction(steward_home):
    store = Store(steward_home)
    job_id = _job(store)
    steering = Steering(store)
    steering.record(job_id, "nudge", "human says hi", origin="cli")
    r = steering.record(job_id, "nudge", "flow says hi", origin="flow")
    assert not r.accepted and "pending_unacked" in r.detail
    steering.mark_acknowledged(job_id, 1, "acknowledged")
    assert steering.record(job_id, "nudge", "flow says hi", origin="flow", request_id="flow-2").accepted


def test_flow_steer_never_counts_as_human_corroboration_or_outranks_a_human():
    from steward import watcher_authority as wa

    assert wa.source_of("flow") == "watcher" and wa.outranks("cli", "flow") and not wa.outranks("flow", "cli")


def test_allowlist_is_finite_and_maps_to_existing_mechanisms():
    assert fa.MECHANISMS == {"no_action": None, "nudge": "steering", "focus": "steering",
                             "watcher_review": "watcher_trust", "recovery": "recovery_predicate",
                             "escalate_human": "human_review"}
    for reserved in ("stop", "cancel", "permission", "accept", "acceptance", "merge", "release", "publish", "push",
                     "deploy", "contact", "email", "message", "approve"):
        assert reserved in fa.RESERVED and reserved not in fa.MECHANISMS


@pytest.mark.parametrize("action,kind,reason", [
    ("merge", "reject", "reserved_human_decision"),
    ("permission", "reject", "reserved_human_decision"),
    ("contact", "reject", "reserved_human_decision"),
    ("rm_rf", "reject", "unknown_action"),
    ("no_action", "no_action", "investigator_recommended_no_action"),
    ("nudge", "map", None),
])
def test_validate_rejects_unknown_and_reserved_actions(steward_home, action, kind, reason):
    store = Store(steward_home)
    job_id = _job(store)
    ep = _episode(store, job_id)
    inv = _investigation(store, ep, action="nudge")
    inv = {**inv, "result": {**inv["result"], "recommended_action": action}}
    d = fa.validate(store, ep, inv, mode="active", now=2000.0)
    assert d.kind == kind and (reason is None or d.reason == reason)
    if kind == "map":
        assert d.mechanism == "steering"


@pytest.mark.parametrize("setup,reason", [
    ("shadow_mode", "mode_not_active"),
    ("shadow_episode", "episode_not_active"),
    ("weak", "weak_evidence"),
    ("invented", "evidence_not_in_episode"),
    ("not_accepted", "investigation_not_accepted"),
    ("stale", "stale_revision"),
    ("not_running", "job_not_running"),
])
def test_validate_defers_or_rejects_unsafe_inputs_with_a_reason(steward_home, setup, reason):
    store = Store(steward_home)
    job_id = _job(store, JobState.CREATED if setup == "not_running" else JobState.RUNNING)
    ep = _episode(store, job_id, conf=0.71 if setup == "weak" else 0.9,
                  mode="shadow" if setup == "shadow_episode" else "active")
    inv = _investigation(store, ep, refs=("event:999",) if setup == "invented" else ("turn:7",),
                         state="rejected" if setup == "not_accepted" else "accepted")
    if setup == "stale":
        Steering(store).record(job_id, "nudge", "a human changed course", origin="cli")
    d = fa.validate(store, ep, inv, mode="shadow" if setup == "shadow_mode" else "active", now=2000.0)
    assert d.kind in ("reject", "defer") and d.reason == reason


# -- task 2: route through existing authority controls --------------------------------------------------------


def _process(store, ep, inv, mode="active", now=2000.0):
    return fa.process(store, ep, inv, mode=mode, now=now)


def test_nudge_goes_through_revisioned_idempotent_steering(steward_home):
    store = Store(steward_home)
    job_id = _job(store)
    ep = _episode(store, job_id)
    inv = _investigation(store, ep)
    rec = _process(store, ep, inv)
    assert rec["decision"] == "applied" and rec["mechanism"] == "steering"
    [ins] = [i for i in Steering(store).all_instructions(job_id) if i["origin"] == "flow"]
    assert ins["verb"] == "nudge" and ins["text"].startswith("[flow] ") and not ins.get("urgent")
    assert rec["receipt"]["request_id"] == f"flow-{ep['episode_id']}" and rec["receipt"]["accepted"]
    ev = [e for e in store.events(job_id) if e.what == "nudge instruction recorded"][-1]
    assert ev.judgment == Judgment.model_informed_code_authorized
    # duplicate processing: no second decision, no second steer
    assert _process(store, ep, inv) is None
    assert len([i for i in Steering(store).all_instructions(job_id) if i["origin"] == "flow"]) == 1


def test_human_steer_after_detection_wins(steward_home):
    store = Store(steward_home)
    job_id = _job(store)
    ep = _episode(store, job_id)
    inv = _investigation(store, ep)
    Steering(store).record(job_id, "nudge", "human: do it my way", origin="moshi")
    rec = _process(store, ep, inv)
    assert rec["decision"] == "deferred" and rec["reason"] == "stale_revision"
    assert not [i for i in Steering(store).all_instructions(job_id) if i["origin"] == "flow"]


def test_steer_pending_ack_fence_defers(steward_home, monkeypatch):
    store = Store(steward_home)
    job_id = _job(store)
    Steering(store).record(job_id, "nudge", "earlier steer", origin="trajectory")
    ep = _episode(store, job_id)  # captured after the steer: revision is current, but the steer is unacked
    rec = _process(store, ep, _investigation(store, ep))
    assert rec["decision"] == "deferred" and rec["reason"] == "pending_unacked"


def test_cooldown_bounds_flow_actions_per_job(steward_home):
    store = Store(steward_home)
    job_id = _job(store)
    ep1 = _episode(store, job_id, label="retry_or_rebuild", now=1000.0)
    assert _process(store, ep1, _investigation(store, ep1), now=1000.0)["decision"] == "applied"
    Steering(store).mark_acknowledged(job_id, Steering(store).all_instructions(job_id)[-1]["rev"], "acknowledged")
    ep2 = _episode(store, job_id, label="wrong_direction", now=1100.0)
    rec = _process(store, ep2, _investigation(store, ep2), now=1100.0)
    assert rec["decision"] == "deferred" and rec["reason"] == "cooldown"


def _watcher(store, job_id, tier):
    from steward import custom_watchers, watcher_authority as wa

    w = custom_watchers.add_watcher(store, job_id, {"name": "drift", "ask": "is it drifting?", "every": "5m",
                                                     "may": ["escalate", "nudge"]})
    rec = wa.init_record(store, job_id, w)
    rec["tier"] = tier
    wa._put(store, job_id, "drift", rec)
    return w


@pytest.mark.parametrize("tier,quarantined,decision,reason", [
    (1, False, "applied", None),
    (0, False, "deferred", "no_trusted_watcher"),
    (2, True, "deferred", "watcher_quarantined"),
])
def test_watcher_review_uses_the_trust_gates(steward_home, tier, quarantined, decision, reason):
    from steward import watcher_authority as wa

    store = Store(steward_home)
    job_id = _job(store)
    _watcher(store, job_id, tier)
    if quarantined:
        store.kv_set(job_id, wa.QUARANTINE_KEY, {"name": "x"})
    ep = _episode(store, job_id)
    rec = _process(store, ep, _investigation(store, ep, action="watcher_review", instruction=""))
    assert rec["decision"] == decision and rec["reason"] == reason
    assert bool(store.kv_get(job_id, "watcher_run_now:drift")) == (decision == "applied")


def test_watcher_review_without_watchers_is_a_no_op_defer(steward_home):
    store = Store(steward_home)
    job_id = _job(store)
    ep = _episode(store, job_id)
    rec = _process(store, ep, _investigation(store, ep, action="watcher_review", instruction=""))
    assert rec["decision"] == "deferred" and rec["reason"] == "no_trusted_watcher"


@pytest.mark.parametrize("cls,used,reason", [
    ("progressing", 0, "runner_predicate_not_met"),
    ("suspect_stall", 2, "recovery_budget_exhausted"),
    ("suspect_stall", 0, "recovery_qualified_runner_owns"),
    ("dead", 1, "recovery_qualified_runner_owns"),
])
def test_recovery_requires_the_independent_runner_predicate_and_budget(steward_home, cls, used, reason):
    store = Store(steward_home)
    job_id = _job(store)
    store.kv_set(job_id, "last_class", cls)
    store.kv_set(job_id, "recoveries_used", used)
    ep = _episode(store, job_id)
    rec = _process(store, ep, _investigation(store, ep, action="recovery", instruction=""))
    assert rec["decision"] == "deferred" and rec["reason"] == reason
    assert store.kv_get(job_id, "recoveries_used") == used  # Flow never spends a recovery itself
    assert not [e for e in store.events(job_id) if e.what.startswith(("worker replacement", "recovery escalated"))]


def test_escalate_human_is_recorded_for_review_and_decides_nothing(steward_home):
    from steward import approvals

    store = Store(steward_home)
    job_id = _job(store)
    ep = _episode(store, job_id)
    rec = _process(store, ep, _investigation(store, ep, action="escalate_human", instruction=""))
    assert rec["decision"] == "escalated" and rec["reason"] == "human_review_requested"
    assert approvals.list_approvals(store, job_id) == [] and store.get_job(job_id).state == JobState.RUNNING


@pytest.mark.parametrize("action", sorted(fa.RESERVED))
def test_every_reserved_action_is_rejected_without_mutation(steward_home, action):
    from steward import approvals

    store = Store(steward_home)
    job_id = _job(store)
    ep = _episode(store, job_id)
    inv = _investigation(store, ep)
    inv = {**inv, "result": {**inv["result"], "recommended_action": action}}
    before = (Steering(store).all_instructions(job_id), store.get_revision(job_id), store.get_job(job_id).state)
    rec = _process(store, ep, inv)
    assert rec["decision"] == "rejected" and rec["reason"] == "reserved_human_decision"
    assert (Steering(store).all_instructions(job_id), store.get_revision(job_id), store.get_job(job_id).state) == before
    assert approvals.list_approvals(store, job_id) == []


def test_dispatch_failure_defers_and_never_raises(steward_home, monkeypatch):
    store = Store(steward_home)
    job_id = _job(store)
    ep = _episode(store, job_id)
    monkeypatch.setattr(fa.Steering, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    rec = _process(store, ep, _investigation(store, ep))
    assert rec["decision"] == "deferred" and rec["reason"] == "error:RuntimeError"


# -- task 2: wiring on the observer thread ----------------------------------------------------------------------


def _wired(steward_home, flow_mode, responses_labels, inv_text=None):
    import json as _json

    from test_flow_watch import FAKE_KEY, AllActions, ScriptedTransport, _flow_resp, _sig

    from steward import config, jev_observer
    from steward import flow_investigator as fi
    from steward.jev_watch import JevWatch

    store = Store(steward_home)
    job_id = _job(store)
    store.kv_set(job_id, "supervisor_jev", "shadow")
    store.kv_set(job_id, config.FLOW_SWITCH_KEY, flow_mode)
    now = [1000.0]
    obs = jev_observer.JevObserver(transport=ScriptedTransport([_flow_resp(x) for x in responses_labels], now),
                                   enabled=True, api_key_fn=lambda: FAKE_KEY, clock=lambda: now[0])
    actions = AllActions()
    w = JevWatch(store, obs, actions, lambda: now[0],
                 activity_fn=lambda s, j: {"turns": [{"i": 7, "ts": "t", "kind": "error", "text": "build failed"}]})
    text = inv_text or _json.dumps({"root_cause": "rebuild loop", "evidence_refs": ["turn:7"],
                                    "recommended_action": "nudge", "instruction": "fix the dependency first"})

    class Inv:
        calls = 0

        def investigate(self, prompt):
            Inv.calls += 1
            return fi.RawAnswer(text, 0.01)

    w.flow_investigator, w.flow_executor = Inv(), (lambda fn: fn())
    for _ in responses_labels:
        w.step(job_id, _sig(), force=True)
        now[0] += 10.0
    return store, job_id, actions, Inv


def test_active_flow_applies_one_allowlisted_steer_end_to_end(steward_home, monkeypatch):
    store, job_id, actions, inv = _wired(steward_home, "active", ["retry_or_rebuild"] * 3)
    flow_steers = [i for i in Steering(store).all_instructions(job_id) if i["origin"] == "flow"]
    assert len(flow_steers) == 1 and inv.calls == 1 and actions.calls == []
    [d] = store.flow_decisions(job_id)
    assert d["decision"] == "applied" and d["mechanism"] == "steering"


def test_shadow_flow_never_decides_or_acts(steward_home):
    store, job_id, actions, inv = _wired(steward_home, "shadow", ["retry_or_rebuild"] * 3)
    assert store.flow_decisions(job_id) == [] and inv.calls == 0 and actions.calls == []
    assert not [i for i in Steering(store).all_instructions(job_id) if i["origin"] == "flow"]


def test_controller_outage_never_breaks_the_observer(steward_home, monkeypatch):
    monkeypatch.setattr(fa, "process", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    store, job_id, actions, inv = _wired(steward_home, "active", ["retry_or_rebuild"] * 2)
    assert any(e.what == "flow_unavailable" for e in store.events(job_id))
    assert store.get_job(job_id).state == JobState.RUNNING


# -- task 3: receipts, adjudication, outcomes, evaluation and replay -------------------------------------------


def test_applied_action_has_linked_proposal_decision_and_receipt_events(steward_home):
    store = Store(steward_home)
    job_id = _job(store)
    ep = _episode(store, job_id)
    rec = _process(store, ep, _investigation(store, ep))
    evs = {e.what: e for e in store.events(job_id) if e.what.startswith("flow_")}
    assert evs["flow_proposal"].evidence["recommended_action"] == "nudge"
    assert evs["flow_decision"].evidence["decision"] == "applied" and evs["flow_decision"].seq == rec["event_seq"]
    assert evs["flow_action_receipt"].evidence["decision_seq"] == rec["event_seq"]
    assert evs["flow_action_receipt"].evidence["receipt"]["request_id"] == f"flow-{ep['episode_id']}"


def test_adjudication_is_human_only_and_never_tunes_anything(steward_home):
    from steward import watcher_authority as wa

    store = Store(steward_home)
    job_id = _job(store)
    ep = _episode(store, job_id)
    with pytest.raises(wa.AuthorityError):
        fa.adjudicate(store, ep["episode_id"], "true_positive", actor="flow")
    with pytest.raises(wa.AuthorityError):
        fa.report_miss(store, job_id, "wrong_direction", actor="watcher:drift")
    with pytest.raises(ValueError):
        fa.adjudicate(store, ep["episode_id"], "great", actor="cli")
    before = (fa.ACTION_MIN_CONFIDENCE, fa.COOLDOWN_S, dict(fa.MECHANISMS))
    r = fa.adjudicate(store, ep["episode_id"], "false_positive", actor="cli", action_verdict="harmful", note="nope")
    assert r["label"] == "false_positive" and r["actor"] == "cli"
    r = fa.adjudicate(store, ep["episode_id"], "true_positive", actor="moshi")  # latest human verdict wins
    assert store.flow_review(ep["episode_id"])["label"] == "true_positive"
    assert [e.judgment for e in store.events(job_id) if e.what == "flow_adjudicated"] == [Judgment.human] * 2
    assert (fa.ACTION_MIN_CONFIDENCE, fa.COOLDOWN_S, dict(fa.MECHANISMS)) == before


def test_outcome_recorded_once_after_the_window(steward_home):
    store = Store(steward_home)
    job_id = _job(store)
    ep = _episode(store, job_id, now=1000.0)
    _process(store, ep, _investigation(store, ep), now=1000.0)
    assert fa.record_outcomes(store, job_id, 1000.0 + 60) == []
    store.record_flow_episode(job_id, episode_id="flow-later", label="retry_or_rebuild", confidence=0.9,
                              detector_version="flow-v1", mode="active", snapshot={}, source_refs=[], job_revision=0,
                              head_sha=None, now=1000.0 + 1200, dedupe_window_s=0.0)
    [d] = fa.record_outcomes(store, job_id, 1000.0 + fa.OUTCOME_WINDOW_S + 1)
    assert d["outcome"]["outcome"] == "recurred" and d["outcome"]["recurred_episodes"] == ["flow-later"]
    assert fa.record_outcomes(store, job_id, 1e9) == []
    assert len([e for e in store.events(job_id) if e.what == "flow_outcome"]) == 1


def test_api_exposes_decisions_reviews_and_report(steward_home):
    from steward.api import StewardAPI

    store = Store(steward_home)
    job_id = _job(store)
    ep = _episode(store, job_id)
    _process(store, ep, _investigation(store, ep))
    api = StewardAPI(store)
    api.adjudicate_flow(ep["episode_id"], "true_positive", origin="cli", action_verdict="helpful")
    [view] = api.get_flow(job_id)["episodes"]
    assert view["decision"]["decision"] == "applied" and view["review"]["label"] == "true_positive"
    report = api.flow_report()
    assert report["precision"] == {"value": 1.0, "num": 1, "den": 1} and report["episodes"]["n"] == 1


def _replayed(tmp_path_factory, n):
    import flow_replay

    out = flow_replay.run(tmp_path_factory.mktemp(f"replay{n}"), flow_replay.standard_scenarios())
    return out, flow_replay.normalize(out["store"], out["jobs"])


def test_replay_is_deterministic_across_three_runs(tmp_path_factory):
    import flow_replay

    digests = {flow_replay.digest(_replayed(tmp_path_factory, i)[1]) for i in range(3)}
    assert len(digests) == 1


def test_replay_covers_every_class_and_boundary(tmp_path_factory):
    import json as _json

    import flow_replay

    out, rec = _replayed(tmp_path_factory, 9)
    store = out["store"]
    assert out["legacy_routes"] == []  # Flow never reached a Jev route, doctor, watcher or diagnosis request
    dec = {k: v["decisions"] for k, v in rec.items()}
    assert dec["wrong_direction"] == [("focus", "applied", None, "not_recurred")]
    assert rec["wrong_direction"]["flow_steers"][0][0] == "focus" and len(rec["wrong_direction"]["episodes"]) == 1
    assert dec["self_correction"] == [("no_action", "no_action", "investigator_recommended_no_action", "not_recurred")]
    assert dec["supervisor_correction"] == [("escalate_human", "escalated", "human_review_requested", "not_recurred")]
    assert dec["retry_or_rebuild"] == [("watcher_review", "deferred", "no_trusted_watcher", "not_recurred")]
    assert dec["blocked_handoff"] == [("recovery", "deferred", "runner_predicate_not_met", "not_recurred")]
    assert rec["supervision_churn"]["investigations"] == [("failed", "provider_exit_1")]
    assert rec["supervision_churn"]["decisions"] == []
    assert rec["miss"]["episodes"] == [] and rec["miss"]["events"] == ["flow_miss_reported"]
    assert len(rec["duplicate_and_restart"]["episodes"]) == 1 and len(rec["duplicate_and_restart"]["flow_steers"]) == 1
    assert rec["invented_evidence"]["investigations"] == [("rejected", "invented_evidence")]
    assert rec["prohibited_merge"]["investigations"] == [("rejected", "unknown_action")]
    assert rec["prohibited_permission"]["investigations"] == [("rejected", "unknown_action")]
    assert rec["malformed_model"]["investigations"] == [("rejected", "invalid_json")]
    assert rec["model_outage"]["investigations"] == [("failed", "provider_error:TimeoutExpired")]
    assert dec["human_overrides"] == [("nudge", "deferred", "stale_revision", "not_recurred")]
    assert rec["human_overrides"]["flow_steers"] == []
    assert rec["shadow_only"]["episodes"] == [("wrong_direction", "shadow")]
    assert rec["shadow_only"]["investigations"] == [] and rec["shadow_only"]["decisions"] == []
    assert rec["off"]["episodes"] == [] and rec["off"]["events"] == []
    for name in ("prohibited_merge", "prohibited_permission", "invented_evidence", "malformed_model", "model_outage"):
        assert rec[name]["flow_steers"] == [] and rec[name]["decisions"] == []
    assert all(v["state"] == "RUNNING" for v in rec.values())  # ordinary work continued everywhere
    blob = _json.dumps([store.flow_episodes(j) for j in out["jobs"].values()]) + _json.dumps(
        [[e.evidence for e in store.events(j)] for j in out["jobs"].values()]) + _json.dumps(rec)
    for leaked in (flow_replay.SECRET, "hunter2hunter2"):
        assert leaked not in blob


def test_replay_metrics_report_denominators(tmp_path_factory):
    out, _ = _replayed(tmp_path_factory, 7)
    report = fa.evaluate(out["store"], list(out["jobs"].values()))
    # reviewed: 5 true positives in 4+1 scenarios + 1 false positive; one unclear; one reported miss
    assert report["precision"] == {"value": 5 / 6, "num": 5, "den": 6}
    assert report["recall_vs_reported"] == {"value": 5 / 6, "num": 5, "den": 6}
    assert report["reviews"]["misses_reported"] == 1 and report["reviews"]["by_label"]["unclear"] == 1
    assert report["action_helpful"]["den"] == 3 and report["action_helpful"]["num"] == 1
    assert report["investigations"]["n"] == report["investigation_acceptance"]["den"]
    assert report["applied_actions"] == report["not_recurred_after_action"]["den"]
    assert "snapshot" not in str(report) and "turns" not in str(report)
