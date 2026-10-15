"""SUP-08: a Jev outage (any `jev_unavailable`, e.g. HTTP 402) alerts the owner once per outage, then a recovery
notice when Jev answers again; status shows it. Never real Telegram from tests: the notifier is a recording stub."""

from __future__ import annotations

from steward import jev_watch, notify, supervisors
from steward.jev_observer import TransportResponse
from steward.store import Store
from test_jev_watch import _resp, _running, _sig, _watch

BILLING = TransportResponse(status_code=402, body=b'{"error_type":"billing_error"}')


class Recorder:
    def __init__(self, ok=True):
        self.sent: list[str] = []
        self.ok = ok

    def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


def _outage_watch(store, responses, rec, now=None):
    w, t, acts, now = _watch(store, responses, now=now)
    w.outage = jev_watch.OutageAlert(store, rec, clock=lambda: now[0])
    return w, now


def test_one_alert_per_outage_then_one_recovery(steward_home):
    store = Store(steward_home)
    job = _running(store)
    rec = Recorder()
    w, now = _outage_watch(store, [BILLING, BILLING, BILLING, _resp("ok"), _resp("ok")], rec)
    for _ in range(3):
        w.step(job.job_id, _sig(), force=True)
        now[0] += 11
    assert len(rec.sent) == 1 and "Jev unavailable" in rec.sent[0] and "http_error_402" in rec.sent[0]
    w.step(job.job_id, _sig(), force=True)
    w.step(job.job_id, _sig(), force=True)
    assert len(rec.sent) == 2 and "Jev is back" in rec.sent[1]
    whats = [e.what for e in store.events(job.job_id)]
    assert whats.count("jev_outage_alert") == 1 and whats.count("jev_outage_recovered") == 1


def test_outage_is_deduped_across_jobs(steward_home):
    store = Store(steward_home)
    a, b = _running(store, "a"), _running(store, "b")
    rec = Recorder()
    w, now = _outage_watch(store, [BILLING], rec)
    store.kv_set(b.job_id, "supervisor_jev", "shadow")
    w.step(a.job_id, _sig(), force=True)
    w.step(b.job_id, _sig(), force=True)
    assert len(rec.sent) == 1


def test_undelivered_alert_is_retried_not_dropped(steward_home):
    store = Store(steward_home)
    job = _running(store)
    rec = Recorder(ok=False)
    w, now = _outage_watch(store, [BILLING], rec)
    w.step(job.job_id, _sig(), force=True)
    now[0] += 11
    w.step(job.job_id, _sig(), force=True)
    assert len(rec.sent) == 1  # throttled
    now[0] += jev_watch.OutageAlert.RETRY_S
    rec.ok = True
    w.step(job.job_id, _sig(), force=True)
    assert len(rec.sent) == 2
    assert store.kv_get(jev_watch.OutageAlert.KV_JOB, jev_watch.OutageAlert.KEY)["alert_delivered"] is True


def test_disabled_jev_is_not_an_outage(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _running(store)
    rec = Recorder()
    w, now = _outage_watch(store, [_resp("ok")], rec)
    w.observer.enabled = False
    w.step(job.job_id, _sig(), force=False)
    w.observer.observe(store, job.job_id, {}, "", force=False)  # records unavailable:disabled
    w.outage.check(job.job_id, None)
    assert rec.sent == []


def test_status_shows_unavailable_since(steward_home):
    store = Store(steward_home)
    job = _running(store)
    rec = Recorder()
    w, now = _outage_watch(store, [BILLING], rec, now=[1_760_000_000.0])
    w.step(job.job_id, _sig(), force=True)
    jev_row = next(r for r in supervisors.supervisor_rows(store, job.job_id) if r["role"] == "jev")
    assert any(p.startswith("unavailable since") for p in jev_row["parts"])


def test_production_notifier_is_tg_send(steward_home):
    from steward.runner_exec import CommandResult, FakeRunner

    fr = FakeRunner(lambda argv, cwd: CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout="", stderr="",
                                                     duration_s=0.0))
    assert notify.tg_send_text("hello", runner=fr) is True
    assert fr.calls[0]["argv"] == ["tg", "send", "hello"]
    fr1 = FakeRunner(lambda argv, cwd: CommandResult(argv=argv, cwd=cwd, exit_code=1, stdout="", stderr="",
                                                      duration_s=0.0))
    assert notify.tg_send_text("hello", runner=fr1) is False  # exit 1 = not delivered, never swallowed


def test_runner_default_watch_has_an_outage_alert(steward_home):
    from test_stability import ScriptedRunner, _runner

    store = Store(steward_home)
    runner = _runner(store, ScriptedRunner(), [[]])
    assert isinstance(runner.jev_watch.outage, jev_watch.OutageAlert)


def test_an_unconfigured_jev_is_not_an_outage(steward_home):
    """No API key / unregistered supervisor-model: Jev was never configured here (status says so); no alert."""
    store = Store(steward_home)
    job = _running(store)
    rec = Recorder()
    w, now = _outage_watch(store, [_resp("ok")], rec)
    w.observer.api_key_fn = lambda: ""
    w.step(job.job_id, _sig(), force=True)
    assert store.kv_get(job.job_id, "jev_status") == "unavailable:no_api_key" and rec.sent == []
