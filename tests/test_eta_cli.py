"""Priors at closeout, `steward eta`, and the one efficiency switch (phase 18-05, G4, G19)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from test_eta_watch import Clock, a7_world, events, watcher  # noqa: E402
from ruling_support import StubLead, finish as finish_ticks, patch_green_verification  # noqa: E402
from test_runner import _Clock, _Factory, _contracted_job, _fake_diagnoser, _git_runner  # noqa: E402
from steward import cli, eta, eta_prior  # noqa: E402
from steward import efficiency_watch as ew  # noqa: E402
from steward.runner import Runner  # noqa: E402
from steward.store import Store  # noqa: E402
from steward.worker import WorkerObservation  # noqa: E402


def _finish(store, job, monkeypatch):
    patch_green_verification(monkeypatch)  # a verification in which checks ran, so the ruling flow is exercised
    factory = _Factory(scripts=[[WorkerObservation(lifecycle="idle", output_tail="all good\nSTEWARD-DONE",
                                                   pid_alive=True, ts="t")]])
    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.job_lead = StubLead(store)
    runner.start(job.job_id)
    return finish_ticks(runner, job.job_id)


def test_closeout_rebuilds_priors(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _contracted_job(store)
    calls = []
    real = eta_prior.rebuild
    monkeypatch.setattr(eta_prior, "rebuild", lambda st, **kw: (calls.append(st), real(st, **kw))[1])
    report = _finish(store, job, monkeypatch)
    assert report.state == "ACCEPTED" and len(calls) == 1
    data = json.loads((steward_home / eta_prior.PRIORS_FILE).read_text())
    [row] = [r for r in data["rows"] if r["job"] == job.job_id]
    assert row["kind"] == "other" and row["minutes"] >= 0


def test_a_rebuild_failure_never_affects_closeout(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _contracted_job(store)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(eta_prior, "rebuild", boom)
    report = _finish(store, job, monkeypatch)
    assert report.action == "closeout" and report.state == "ACCEPTED"
    [ev] = [e for e in store.events(job.job_id) if e.what == "eta_error"]
    assert "disk full" in ev.evidence["error"] and ev.rule == "eta"


# -- steward eta -----------------------------------------------------------------------------------------------


def _with_eta(tmp_path, monkeypatch, *, records=None):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    if records:
        store.kv_set(job.job_id, "eta_jev", records)
        store.kv_set(job.job_id, "jev_status", "ok:ok")
    w, _ = watcher(store, proj, clock, tmp_path)
    w.step(job.job_id)
    monkeypatch.setattr(cli.time, "time", lambda: clock.t + 12)
    return store, job


def test_cli_eta_prints_the_estimate(tmp_path, monkeypatch, capsys):
    store, job = _with_eta(tmp_path, monkeypatch)
    assert cli.main(["eta", job.job_id]) == 0
    out = capsys.readouterr().out
    stored = store.kv_get(job.job_id, ew.STATE_KEY)["eta"]
    assert eta.render(stored, cli.time.time())["text"] in out
    assert "eta ~" in out and "units 3/7" in out and "from cycle times" in out
    assert "source units" in out and "units 3/7 (planning)" in out and "cycle mean" in out and "sd " in out
    assert "plan_ahead yes" in out and "(no Jev)" in out
    assert "prior gsd-build: 3 peers, median" in out and "updated 12s ago" in out


def test_cli_eta_shows_jev_labels(tmp_path, monkeypatch, capsys):
    rows = [{"at": i, "answers": {"unit": {"choice": "16-04", "confidence": 1.0},
                                  "position": {"choice": "late", "confidence": 0.9}}} for i in (1, 2, 3)]
    store, job = _with_eta(tmp_path, monkeypatch, records=rows)
    assert cli.main(["eta", job.job_id]) == 0
    out = capsys.readouterr().out
    assert "jev position=late, unit=16-04" in out and "(no Jev)" not in out


def test_cli_eta_json(tmp_path, monkeypatch, capsys):
    store, job = _with_eta(tmp_path, monkeypatch)
    assert cli.main(["eta", job.job_id, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    stored = store.kv_get(job.job_id, ew.STATE_KEY)["eta"]
    assert out["raw"] == stored and out["eta"] == eta.api_view(stored, cli.time.time())
    assert out["prior"]["kind"] == "gsd-build" and out["prior"]["peers"] == 3
    assert set(out["eta"]) >= {"p10_min", "p50_min", "p90_min", "shown", "units_done", "units_total", "unit_label",
                               "source", "stage", "overdue_min", "updated_at"}


def test_cli_eta_no_data_off_and_unknown(steward_home, capsys):
    store = Store(steward_home)
    job = _contracted_job(store)
    assert cli.main(["eta", job.job_id]) == 0
    assert capsys.readouterr().out.strip() == "eta: no data yet"
    store.kv_set(job.job_id, ew.SWITCH_KEY, "off")
    assert cli.main(["eta", job.job_id]) == 0
    assert capsys.readouterr().out.startswith("eta: off")
    assert cli.main(["eta", "nope"]) == 1


def test_cli_eta_codex(tmp_path, monkeypatch, capsys):
    store, job, proj, d, clock = a7_world(tmp_path, monkeypatch)
    store.kv_set(job.job_id, "worker_kind", "codex")
    w, _ = watcher(store, proj, clock, tmp_path)
    w.step(job.job_id)
    assert cli.main(["eta", job.job_id]) == 0
    assert capsys.readouterr().out.strip() == "eta n/a (codex transcript)"


def test_cli_eta_is_read_only(tmp_path, monkeypatch, capsys):
    store, job = _with_eta(tmp_path, monkeypatch)
    before = (len(store.events(job.job_id)), store.kv_get(job.job_id, ew.STATE_KEY), store.kv_get(job.job_id, "eta_notify"))
    assert cli.main(["eta", job.job_id]) == 0 and cli.main(["eta", job.job_id, "--json"]) == 0
    capsys.readouterr()
    assert (len(store.events(job.job_id)), store.kv_get(job.job_id, ew.STATE_KEY), store.kv_get(job.job_id, "eta_notify")) == before


def test_cli_eta_notify(steward_home, capsys):
    store = Store(steward_home)
    job = _contracted_job(store)
    assert cli.main(["eta", job.job_id, "notify", "overdue"]) == 0
    assert store.kv_get(job.job_id, "eta_notify") == "overdue"
    [ev] = events(store, job, "eta_notify_set")
    assert ev.evidence == {"value": "overdue", "previous": "off"} and ev.judgment.value == "human"
    assert cli.main(["eta", job.job_id, "notify", "off"]) == 0
    assert store.kv_get(job.job_id, "eta_notify") is None
    assert [e.evidence for e in events(store, job, "eta_notify_set")][-1] == {"value": "off", "previous": "overdue"}
    capsys.readouterr()
    for bad in (["notify"], ["notify", "always"], ["notify", "overdue", "off"]):
        assert cli.main(["eta", job.job_id, *bad]) == 1
    assert "overdue|off" in capsys.readouterr().err
    assert len(events(store, job, "eta_notify_set")) == 2 and store.kv_get(job.job_id, "eta_notify") is None
    assert cli.main(["eta", "nope", "notify", "overdue"]) == 1


# -- the one switch ----------------------------------------------------------------------------------------------


def _toggled(store, job):
    return [(e.what, e.evidence, e.rule, e.judgment.value) for e in events(store, job, "supervisor_toggled")]


def test_cli_efficiency_off_on(steward_home, tmp_path, monkeypatch):
    store, job, proj, d, clock = a7_world(tmp_path / "x", monkeypatch)
    other = _contracted_job(store, goal="other job")
    assert cli.main(["efficiency", job.job_id, "off"]) == 0
    assert cli.main(["supervisor", other.job_id, "efficiency", "off"]) == 0
    assert store.kv_get(job.job_id, ew.SWITCH_KEY) == store.kv_get(other.job_id, ew.SWITCH_KEY) == "off"
    [a] = _toggled(store, job)
    [b] = _toggled(store, other)
    assert a[1:] == b[1:] and a[1]["role"] == "efficiency" and a[1]["state"] == "off"
    w, spend = watcher(store, proj, clock, tmp_path / "x")
    w.step(job.job_id)
    assert "eta" not in store.kv_get(job.job_id, ew.STATE_KEY)
    assert cli.main(["efficiency", job.job_id, "on"]) == 0
    assert cli.main(["supervisor", other.job_id, "efficiency", "shadow"]) == 0
    assert store.kv_get(job.job_id, ew.SWITCH_KEY) == store.kv_get(other.job_id, ew.SWITCH_KEY) == "shadow"
    spend.t += 61
    clock.t += 61
    w.step(job.job_id)
    assert store.kv_get(job.job_id, ew.STATE_KEY)["eta"]["source"] == "units"
