import json

from steward.jev_observer import FakeTransport, JevObserver, TransportResponse
from steward.models import JobState, Judgment
from steward.store import Store


def _happy_response(choice="ok"):
    payload = {
        "model": "jev-latest",
        "answers": {
            "triage": {
                "choice": choice,
                "confidence": 0.83,
                "probabilities": {"ok": 0.83, "stalled": 0.17},
            }
        },
    }
    return TransportResponse(status_code=200, body=json.dumps(payload).encode())


def test_observe_happy_path_records_jev_shadow_event_with_authority_none(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(_happy_response("hung_call"))
    observer = JevObserver(transport=transport, enabled=True)

    result = observer.observe(store, job.job_id, {"same_error_count": 3}, "some output")

    assert result["choice"] == "hung_call"
    assert result["confidence"] == 0.83
    assert result["heuristic_class"] == "hung_call"
    events = [e for e in store.events(job.job_id) if e.what == "jev_shadow"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.model
    assert events[0].rule == "shadow_mode"
    assert events[0].evidence["authority"] == "none"

    call = transport.calls[0]
    assert call["url"] == "https://api.typesafe.ai/v1/systemone"
    assert call["headers"]["Authorization"] == "Bearer test-key"
    body = json.loads(call["body"])
    assert body["state"]["schema_version"] == "steward_jev_v2"
    assert "triage" in body["questions"]
    assert body["model"] == "jev-latest"


def test_observe_uses_typesafe_model_env_override(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-experimental")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(_happy_response())
    observer = JevObserver(transport=transport, enabled=True)

    observer.observe(store, job.job_id, {}, "tail")

    body = json.loads(transport.calls[0]["body"])
    assert body["model"] == "jev-experimental"


def test_observe_redacts_output_tail_and_caps_length(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(_happy_response())
    observer = JevObserver(transport=transport, enabled=True)

    long_tail = "x" * 3000 + "Bearer sk-abcdefghijklmnop"
    observer.observe(store, job.job_id, {}, long_tail)

    body = json.loads(transport.calls[0]["body"])
    tail = body["state"]["output_tail"]
    assert len(tail) <= 1800
    assert "sk-abcdefghijklmnop" not in tail


def test_observe_http_500_records_jev_unavailable_and_returns_none(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(TransportResponse(status_code=500, body=b"boom"))
    observer = JevObserver(transport=transport, enabled=True)

    result = observer.observe(store, job.job_id, {}, "tail")

    assert result is None
    events = [e for e in store.events(job.job_id) if e.what == "jev_unavailable"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.code
    assert events[0].rule == "jev_shadow_unavailable"
    assert "http_error_500" in events[0].evidence["code"]


def test_observe_missing_api_key_records_jev_unavailable_and_returns_none(steward_home, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(_happy_response())
    observer = JevObserver(transport=transport, enabled=True)

    result = observer.observe(store, job.job_id, {}, "tail")

    assert result is None
    events = [e for e in store.events(job.job_id) if e.what == "jev_unavailable"]
    assert events[0].evidence["code"] == "no_api_key"
    assert transport.calls == []  # never even attempted the request


def test_observe_transport_raises_records_jev_unavailable_and_never_raises(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(ConnectionError("network down"))
    observer = JevObserver(transport=transport, enabled=True)

    result = observer.observe(store, job.job_id, {}, "tail")  # must not raise

    assert result is None
    events = [e for e in store.events(job.job_id) if e.what == "jev_unavailable"]
    assert len(events) == 1
    assert "ConnectionError" in events[0].evidence["code"]


def test_observe_malformed_response_records_jev_unavailable(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(TransportResponse(status_code=200, body=b"not json"))
    observer = JevObserver(transport=transport, enabled=True)

    result = observer.observe(store, job.job_id, {}, "tail")

    assert result is None
    events = [e for e in store.events(job.job_id) if e.what == "jev_unavailable"]
    assert len(events) == 1


def test_observe_disabled_records_jev_unavailable_and_never_calls_transport(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(_happy_response())
    observer = JevObserver(transport=transport, enabled=False)

    result = observer.observe(store, job.job_id, {}, "tail")

    assert result is None
    assert transport.calls == []
    events = [e for e in store.events(job.job_id) if e.what == "jev_unavailable"]
    assert events[0].evidence["code"] == "disabled"


def test_observe_uses_supervisor_model_exception_and_records_it(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(_happy_response())
    observer = JevObserver(transport=transport, enabled=True)

    observer.observe(store, job.job_id, {}, "tail")

    used = [e for e in store.events(job.job_id) if e.what == "exception_used" and e.evidence.get("name") == "supervisor-model"]
    assert len(used) == 1
    assert used[0].evidence["role"] == "jev"


def test_observe_unregistered_supervisor_model_fails_closed_via_jev_unavailable(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    registry_path = tmp_path / "no-supervisor-model.toml"
    registry_path.write_text('[git]\ndirection = "out"\npurpose = "x"\naudit = "event"\n')
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(registry_path))
    transport = FakeTransport(_happy_response())
    observer = JevObserver(transport=transport, enabled=True)

    result = observer.observe(store, job.job_id, {}, "tail")

    assert result is None
    assert transport.calls == []
    events = [e for e in store.events(job.job_id) if e.what == "jev_unavailable"]
    assert len(events) == 1
    assert events[0].evidence["code"] == "supervisor_model_unregistered"
    violations = [e for e in store.events(job.job_id) if e.what == "exception_violation"]
    assert len(violations) == 1


def test_observe_never_changes_job_state(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(TransportResponse(status_code=500, body=b""))
    observer = JevObserver(transport=transport, enabled=True)

    before = store.get_job(job.job_id).state
    observer.observe(store, job.job_id, {}, "tail")
    after = store.get_job(job.job_id).state

    assert before == after == JobState.CREATED


# -- extra (ETA) questions ride the triage body (phase 18-04) ----------------------------------------


def _eta_questions():
    from steward import eta_jev

    return eta_jev.questions({"source": "units", "kind": "gsd-build", "units_done": 3, "units_total": 7,
                              "open_units": ["16-04", "16-05"]})


def _eta_response(answers):
    payload = {"answers": {"triage": {"choice": "ok", "confidence": 0.9, "probabilities": {"ok": 0.9}}}}
    payload["answers"].update({k: {"choice": c, "confidence": conf, "probabilities": {c: conf}} for k, (c, conf) in answers.items()})
    return TransportResponse(status_code=200, body=json.dumps(payload).encode())


def test_invalid_eta_answer_is_dropped_not_unavailable(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(_eta_response({
        "unit": ("16-09", 0.9),           # not among the criteria
        "position": ("mid", 1.4),         # confidence out of range
        "scope": ("same", 0.98),          # valid
        "fraction": ("40-60", "high"),    # confidence not a number
        "near_done": ("no", 0.95),        # valid
    }))
    observer = JevObserver(transport=transport, enabled=True)

    result = observer.observe(store, job.job_id, {}, "", force=True, extra_questions=_eta_questions())

    assert result["choice"] == "ok"
    assert result["eta"] == {"scope": {"choice": "same", "confidence": 0.98}, "near_done": {"choice": "no", "confidence": 0.95}}
    assert store.kv_get(job.job_id, "jev_status") == "ok:ok"
    assert [e for e in store.events(job.job_id) if e.what == "jev_unavailable"] == []


def test_missing_eta_answers_leave_the_triage_intact(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    observer = JevObserver(transport=FakeTransport(_happy_response()), enabled=True)
    result = observer.observe(store, job.job_id, {}, "", extra_questions=_eta_questions())
    assert result["choice"] == "ok" and result["eta"] == {}
    assert jev_observer_stats(store, job)["eta_calls"] == 1


def jev_observer_stats(store, job):
    from steward import jev_observer

    return jev_observer.get_stats(store, job.job_id)


def test_record_ok_event_excludes_eta(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    observer = JevObserver(transport=FakeTransport(_eta_response({"scope": ("same", 0.98)})), enabled=True)
    observer.observe(store, job.job_id, {}, "", extra_questions=_eta_questions())
    events = [e for e in store.events(job.job_id) if e.what == "jev_shadow"]
    assert len(events) == 1 and "eta" not in events[0].evidence
    assert events[0].evidence["authority"] == "none"


def test_extra_questions_join_the_triage_dict_and_never_replace_it(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(_happy_response())
    observer = JevObserver(transport=transport, enabled=True)
    observer.observe(store, job.job_id, {}, "", extra_questions={**_eta_questions(), "triage": {"type": "choice", "criteria": {"x": "y"}}})
    qs = json.loads(transport.calls[0]["body"])["questions"]
    assert "ok" in qs["triage"]["criteria"] and "x" not in qs["triage"]["criteria"] and "unit" in qs


def test_eta_call_stats_count_failed_eta_calls_and_body_size(steward_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    transport = FakeTransport(TransportResponse(status_code=500, body=b""))
    observer = JevObserver(transport=transport, enabled=True)
    assert observer.observe(store, job.job_id, {}, "", extra_questions=_eta_questions()) is None
    st = jev_observer_stats(store, job)
    assert st["eta_calls"] == 1 and st["eta_body_chars"] == len(transport.calls[0]["body"]) and st["unavailable"] == 1
    observer.observe(store, job.job_id, {}, "")  # a plain call does not count as an ETA call
    assert jev_observer_stats(store, job)["eta_calls"] == 1


# -- v2.7 (SRD-04): the stop question ---------------------------------------------------------------------


def _stop_obs(choice, conf=0.9):
    import json as _json
    from steward.jev_observer import FakeTransport as _FT, TransportResponse as _TR
    body = _json.dumps({"answers": {"stop": {"choice": choice, "confidence": conf, "probabilities": {}}}}).encode()
    fake = _FT(_TR(200, body))
    return JevObserver(fake, enabled=True, api_key_fn=lambda: "test-key"), fake


def test_ask_stop_returns_label_and_redacts_the_message(steward_home, monkeypatch):
    from steward import jev_observer
    monkeypatch.setattr(jev_observer.exceptions, "use", lambda *a, **k: None)
    monkeypatch.setattr(jev_observer.exceptions, "load", lambda: {"supervisor-model": {}})
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    obs, fake = _stop_obs("asking")
    got = obs.ask_stop(store, job.job_id, "which db? password=hunter2", {"failed": ["no_new_commit"]})
    assert got == {"choice": "asking", "confidence": 0.9}
    assert b"hunter2" not in fake.calls[0]["body"] and b"no_new_commit" in fake.calls[0]["body"]


def test_ask_stop_rejects_unknown_choice_and_disabled(steward_home):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    obs, _ = _stop_obs("finished")
    assert obs.ask_stop(store, job.job_id, "x", {}) is None
    off, fake = _stop_obs("asking")
    off.enabled = False
    assert off.ask_stop(store, job.job_id, "x", {}) is None and fake.calls == []
