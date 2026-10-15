"""Plan 2 Task 11: every byte sent to Jev passes one guard (spec §13 canary masking, diagnostic-vs-egress boundary)."""
from __future__ import annotations

import json

import pytest

from steward import auto_interrupt, egress, jev_observer, masking, sanitize
from steward.jev_observer import FakeTransport, JevObserver, TransportResponse
from steward.store import Store
from authority_fixtures import CANARY, owner_job

OK = TransportResponse(200, json.dumps({"answers": {
    "triage": {"choice": "ok", "confidence": 0.9, "probabilities": {"ok": 0.9}},
    "risk": {"choice": "low", "confidence": 0.9}, "lane_triage": {"choice": "ok", "confidence": 0.9},
    "activity": {"choice": sorted(auto_interrupt.ACTIVITY)[0], "confidence": 0.9}}}).encode())


@pytest.fixture(autouse=True)
def _canary():
    sanitize.register_canary(CANARY)
    yield
    masking.clear()


def _obs(fake):
    return JevObserver(fake, enabled=True, api_key_fn=lambda: "test-key")


def _sent(fake):
    return b"".join(c["body"] for c in fake.calls).decode("utf-8")


def test_every_jev_lane_is_scrubbed_at_the_seam(steward_home, monkeypatch):
    s = Store(steward_home)
    jid = owner_job(s)
    monkeypatch.setattr(jev_observer.exceptions, "use", lambda *a, **k: None)
    monkeypatch.setattr(jev_observer.exceptions, "load", lambda: {"supervisor-model": {}})
    fake = FakeTransport(OK)
    obs = _obs(fake)
    obs.observe(s, jid, {"note": f"sig {CANARY}"}, f"tail Bearer {CANARY} password=pw-123")
    obs.ask_risk(s, jid, f"curl -H 'Authorization: Bearer {CANARY}' x", "rule")
    obs.observe_lane(s, jid, "lane-a", {"note": CANARY})
    auto_interrupt.jev_activity(obs, s, jid, {"calls": [{"name": "Bash", "input": {"command": f"echo {CANARY}"}}],
                                              "since": 0})
    assert len(fake.calls) == 4
    blob = _sent(fake)
    assert CANARY not in blob and "pw-123" not in blob and sanitize.leaks(blob) == []


def test_transport_assigned_later_is_still_guarded(steward_home):
    obs = _obs(FakeTransport(OK))
    replacement = FakeTransport(OK)
    obs.transport = replacement  # what tests/flow_replay.py does
    assert isinstance(obs.transport, egress.GuardedTransport) and obs.transport.inner is replacement
    assert obs.transport.calls is replacement.calls  # attribute access delegates to the inner transport


def test_unknown_schema_and_leaks_are_refused_before_any_byte_is_sent():
    fake = FakeTransport(OK)
    g = egress.GuardedTransport(fake)
    with pytest.raises(egress.EgressRefused) as e1:
        g.post("u", json.dumps({"state": {"schema_version": "mystery"}}).encode(), {}, 1.0)
    assert e1.value.reason == "unknown_lane"
    with pytest.raises(egress.EgressRefused):
        g.post("u", b"not json", {}, 1.0)
    assert fake.calls == []


def test_oversized_bodies_are_refused_not_truncated():
    fake = FakeTransport(OK)
    g = egress.GuardedTransport(fake)
    body = {"state": {"schema_version": "steward_jev_v2", "output_tail": "x" * 70000}}
    with pytest.raises(egress.EgressRefused) as e:
        g.post("u", json.dumps(body).encode(), {}, 1.0)
    assert e.value.reason.startswith("too_large") and fake.calls == []


def test_clean_bodies_pass_byte_identical():
    fake = FakeTransport(OK)
    raw = json.dumps({"model": "m", "state": {"schema_version": "steward_jev_lane_v1", "signals": {"a": 1}}}).encode()
    egress.GuardedTransport(fake).post("u", raw, {}, 1.0)
    assert fake.calls[0]["body"] == raw


def test_observation_envelope_is_unchanged(steward_home, monkeypatch):
    s = Store(steward_home)
    jid = owner_job(s)
    monkeypatch.setattr(jev_observer.exceptions, "use", lambda *a, **k: None)
    monkeypatch.setattr(jev_observer.exceptions, "load", lambda: {"supervisor-model": {}})
    fake = FakeTransport(OK)
    _obs(fake).observe(s, jid, {}, "z" * 5000)
    assert len(json.loads(fake.calls[0]["body"])["state"]["output_tail"]) == jev_observer.MAX_STATE_CHARS == 1800


def test_refusal_is_recorded_and_observe_still_never_raises(steward_home, monkeypatch):
    s = Store(steward_home)
    jid = owner_job(s)
    monkeypatch.setattr(jev_observer.exceptions, "use", lambda *a, **k: None)
    monkeypatch.setattr(jev_observer.exceptions, "load", lambda: {"supervisor-model": {}})
    monkeypatch.setattr(egress, "LANE_MAX_BYTES", {**egress.LANE_MAX_BYTES, "observe": 10})
    fake = FakeTransport(OK)
    assert _obs(fake).observe(s, jid, {}, "tail") is None
    ev = [e for e in s.events(jid) if e.what == "jev_egress_refused"]
    assert fake.calls == [] and ev and ev[0].evidence["lane"] == "observe"


def test_on_sent_sees_only_guarded_bytes():
    seen = []
    fake = FakeTransport(OK)
    g = egress.GuardedTransport(fake, on_sent=lambda lane, body, resp, latency: seen.append((lane, body)))
    g.post("u", json.dumps({"state": {"schema_version": "steward_jev_v2", "output_tail": f"t {CANARY}"}}).encode(),
           {}, 1.0)
    assert seen and seen[0][0] == "observe" and CANARY.encode() not in seen[0][1]


def test_canary_never_leaves_any_capture_or_egress_path(steward_home, monkeypatch):
    """Review Focus 5: one registered value through worker output, a held command, a diagnostic read, a decision
    record and a lead packet — never stored or sent in the clear."""
    from steward import decision_trace, diagnostics
    from steward.actor import Actor, ActorRef
    from authority_fixtures import grant, ssh_runner
    from ownership_fixtures import CONTROLLER

    s = Store(steward_home)
    jid = owner_job(s)
    monkeypatch.setattr(jev_observer.exceptions, "use", lambda *a, **k: None)
    monkeypatch.setattr(jev_observer.exceptions, "load", lambda: {"supervisor-model": {}})
    fake = FakeTransport(OK)
    _obs(fake).observe(s, jid, {}, f"output {CANARY}")
    _obs(fake).ask_risk(s, jid, f"echo {CANARY}", "rule")
    grant(s, jid, "remote_read:pi-post")
    diagnostics.run_read(s, jid, target="pi-post", command="uptime", purpose=f"p {CANARY}",
                         requested_by=ActorRef(Actor.TECH_LEAD, "astra-tech-lead"),
                         runner_exec=ssh_runner({"uptime": (0, f"up {CANARY}")}), now=1.0)
    decision_trace.record(s, job_id=jid, actor=CONTROLLER, stage="signal", decision=f"d {CANARY}", now=2.0)
    dump = "\n".join(str(tuple(r)) for t in ("events", "kv", "diagnostic_reads", "decision_records")
                     for r in s._conn.execute(f"SELECT * FROM {t}"))
    assert CANARY not in dump and CANARY not in _sent(fake)
    assert CANARY not in json.dumps(diagnostics.packet_section(s, jid))
