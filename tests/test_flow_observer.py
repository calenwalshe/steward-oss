"""v2.3 Flow rider on the Jev observer (phase 23): typed contract, rider budget, mode and episode building."""

from __future__ import annotations

import json

from steward.jev_observer import FakeTransport, JevObserver, TransportResponse
from steward.store import Store
from test_jev_observer import _eta_questions, _happy_response


# -- v2.3 Flow rider (phase 23) ----------------------------------------------------------------


def _flow_response(flow=None, triage="ok", extra=None):
    answers = {"triage": {"choice": triage, "confidence": 0.9, "probabilities": {triage: 0.9}}}
    if flow is not None:
        answers["flow"] = flow
    answers.update(extra or {})
    return TransportResponse(status_code=200, body=json.dumps({"answers": answers}).encode())


def test_flow_labels_are_finite_and_friction_excludes_smooth_and_uncertain():
    from steward import jev_observer

    assert jev_observer.FLOW_LABELS == ("smooth", "wrong_direction", "self_correction", "supervisor_correction",
                                        "retry_or_rebuild", "blocked_handoff", "supervision_churn", "uncertain")
    assert set(jev_observer.FLOW_FRICTION) == set(jev_observer.FLOW_LABELS) - {"smooth", "uncertain"}
    q = jev_observer.flow_question()
    assert q["type"] == "choice" and tuple(q["criteria"]) == jev_observer.FLOW_LABELS
    assert "untrusted" in q["instructions"]


def test_parse_flow_accepts_only_finite_label_and_unit_confidence():
    from steward.jev_observer import FLOW_DETECTOR_VERSION, parse_flow

    assert parse_flow({"choice": "retry_or_rebuild", "confidence": 0.82}) == {
        "label": "retry_or_rebuild", "confidence": 0.82, "detector_version": FLOW_DETECTOR_VERSION}
    for bad in (None, "retry", [], {}, {"choice": "rm -rf /", "confidence": 0.9},
                {"choice": "smooth", "confidence": 1.4}, {"choice": "smooth", "confidence": "high"},
                {"choice": "smooth", "confidence": True}, {"confidence": 0.9}):
        assert parse_flow(bad) is None


def test_flow_question_rides_the_one_triage_request(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(_flow_response({"choice": "wrong_direction", "confidence": 0.9}))
    result = JevObserver(transport=transport, enabled=True).observe(
        store, job.job_id, {}, "", extra_questions=_eta_questions(), flow=True)
    assert len(transport.calls) == 1
    qs = json.loads(transport.calls[0]["body"])["questions"]
    assert set(qs) == {"triage", "flow"} | set(_eta_questions())
    assert result["choice"] == "ok" and result["flow"]["label"] == "wrong_direction"
    assert "flow" not in result["eta"]


def test_flow_off_leaves_the_request_body_unchanged(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    t1, t2 = FakeTransport(_happy_response()), FakeTransport(_happy_response())
    JevObserver(transport=t1, enabled=True).observe(store, job.job_id, {}, "", extra_questions=_eta_questions())
    JevObserver(transport=t2, enabled=True).observe(store, job.job_id, {}, "", extra_questions=_eta_questions(),
                                                    flow=False)
    assert t1.calls[0]["body"] == t2.calls[0]["body"]
    assert "flow" not in json.loads(t1.calls[0]["body"])["questions"]


def test_malformed_or_missing_flow_answer_never_costs_triage_or_eta(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    for flow in (None, {"choice": "nuke", "confidence": 0.9}, "garbage", {"choice": "smooth", "confidence": 9}):
        resp = _flow_response(flow, extra={"near_done": {"choice": "no", "confidence": 0.95}})
        result = JevObserver(transport=FakeTransport(resp), enabled=True).observe(
            store, job.job_id, {}, "", extra_questions=_eta_questions(), flow=True)
        assert result["choice"] == "ok" and result["flow"] is None
        assert result["eta"] == {"near_done": {"choice": "no", "confidence": 0.95}}
    assert [e for e in store.events(job.job_id) if e.what == "jev_unavailable"] == []


def test_flow_answer_never_lands_in_the_jev_shadow_event(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    JevObserver(transport=FakeTransport(_flow_response({"choice": "self_correction", "confidence": 0.9})),
                enabled=True).observe(store, job.job_id, {}, "", flow=True)
    ev = [e for e in store.events(job.job_id) if e.what == "jev_shadow"][0]
    assert "flow" not in ev.evidence and ev.evidence["authority"] == "none"


def test_rider_budget_is_eight_with_one_reserved_flow_slot():
    from steward import jev_observer

    assert jev_observer.MAX_EXTRA_QUESTIONS == 7  # the ETA + deadline budget is unchanged
    assert jev_observer.FLOW_RIDER_SLOTS == 1 and jev_observer.MAX_RIDERS == 8


def test_flow_rider_trims_extras_beyond_the_local_cap(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    extra = {f"q{i}": {"type": "choice", "instructions": "x", "criteria": {"a": "a"}} for i in range(9)}
    transport = FakeTransport(_flow_response(None))
    JevObserver(transport=transport, enabled=True).observe(store, job.job_id, {}, "", extra_questions=extra, flow=True)
    qs = json.loads(transport.calls[0]["body"])["questions"]
    assert "flow" in qs and "triage" in qs and len(qs) - 1 == 8


# -- Flow mode (independent of the JEV observer mode) ------------------------------------------


def test_flow_mode_defaults_off_and_is_independent_of_jev_mode(steward_home, tmp_path, monkeypatch):
    from steward import config

    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("STEWARD_FLOW_MODE", raising=False)
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    store.kv_set(job.job_id, "supervisor_jev", "active")
    assert config.flow_mode(store, job.job_id) == "off"
    (tmp_path / "steward.toml").write_text('[flow]\nmode = "shadow"\n[jev]\nmode = "off"\n')
    assert config.flow_settings()["mode"] == "shadow" and config.flow_mode(store, job.job_id) == "shadow"
    store.kv_set(job.job_id, config.FLOW_SWITCH_KEY, "active")
    assert config.flow_mode(store, job.job_id) == "active"
    store.kv_set(job.job_id, config.FLOW_SWITCH_KEY, "bogus")
    assert config.flow_mode(store, job.job_id) == "shadow"
    monkeypatch.setenv("STEWARD_FLOW_MODE", "nonsense")
    assert config.flow_settings()["mode"] == "off"
    monkeypatch.setenv("STEWARD_FLOW_MODE", "active")
    assert config.flow_settings()["mode"] == "active"
    (tmp_path / "steward.toml").write_text("not = [valid toml")
    monkeypatch.delenv("STEWARD_FLOW_MODE")
    assert config.flow_settings()["mode"] == "off"


# -- Flow episodes from an observation (phase 23 task 2) -----------------------------------------


def _ctx():
    return {"goal": "fix the parser", "state": "RUNNING", "hb": "working", "idle_s": 12, "last": "tool",
            "turns": [["said", "rebuilding again"], ["tool", "Bash: npm run build"], ["error", "build failed"]],
            "steers": [{"rev": 2, "st": "acknowledged", "age_s": 30}], "verdicts": [], "perms": [],
            "perm_wait_s": None, "eta": "units 1/3 done"}


def test_episode_id_is_stable_and_versioned():
    from steward import jev_observer as jo

    a = jo.episode_id("job", "flow-v1", "retry_or_rebuild", ["turn:3", "event:9"])
    assert a == jo.episode_id("job", "flow-v1", "retry_or_rebuild", ["turn:3", "event:9"])
    assert a != jo.episode_id("job", "flow-v2", "retry_or_rebuild", ["turn:3", "event:9"])
    assert a != jo.episode_id("job", "flow-v1", "retry_or_rebuild", ["turn:4", "event:9"])
    assert a.startswith("flow-") and len(a) <= 40


def test_snapshot_keeps_only_bounded_cleaned_context():
    from steward import jev_observer as jo

    ctx = _ctx()
    ctx["turns"] = [["said", "x" * 300 + " password=hunter22"]] * 9
    snap = jo.build_snapshot(ctx, {"label": "retry_or_rebuild", "confidence": 0.9})
    blob = json.dumps(snap, separators=(",", ":"))
    assert len(blob) <= jo.FLOW_SNAPSHOT_CHARS and "hunter22" not in blob
    assert set(snap) <= {"label", "confidence", "goal", "hb", "idle_s", "last", "turns", "steers", "verdicts", "perms"}
    assert "eta" not in snap


def test_record_episode_persists_once_for_qualifying_friction(steward_home):
    from steward import jev_observer as jo

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    flow = {"label": "retry_or_rebuild", "confidence": 0.9, "detector_version": jo.FLOW_DETECTOR_VERSION}
    rec, created = jo.record_episode(store, job.job_id, flow, _ctx(), ["turn:3"], mode="shadow", now=10.0,
                                     head_sha="abc")
    assert created and rec["snapshot"]["turns"][0] == ["said", "rebuilding again"]
    assert rec["job_revision"] == store.get_revision(job.job_id) and rec["head_sha"] == "abc"
    assert jo.record_episode(store, job.job_id, flow, _ctx(), ["turn:3"], mode="shadow", now=11.0)[1] is False


def test_record_episode_noops_for_smooth_uncertain_low_or_missing(steward_home):
    from steward import jev_observer as jo

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    for flow in (None, {"label": "smooth", "confidence": 0.99}, {"label": "uncertain", "confidence": 0.99},
                 {"label": "wrong_direction", "confidence": 0.5}, {"label": "bogus", "confidence": 0.9}):
        assert jo.record_episode(store, job.job_id, flow, _ctx(), ["turn:1"], mode="shadow", now=1.0) is None
    assert store.flow_episodes(job.job_id) == []
