"""Issue O: slow write-transaction log + one retry of the code-fact step on 'database is locked'."""
import json
import sqlite3
from types import SimpleNamespace

from steward import code_facts, runner as runner_mod
from steward.store import SLOW_WRITE_LOG, Store


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_slow_write_transaction_logged_with_call_site(tmp_path):
    store = Store(tmp_path / "home")
    clock = _Clock()
    store._write_clock = clock
    with store.immediate():
        clock.t += 6.5
    lines = (store.home / SLOW_WRITE_LOG).read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["what"] == "slow_write_transaction" and rec["duration_s"] == 6.5 and rec["pid"]
    assert 1 <= len(rec["stack"]) <= 6
    assert rec["stack"][0].startswith("test_issue_o_db_locked:test_slow_write_transaction_logged_with_call_site:")


def test_fast_write_transaction_not_logged(tmp_path):
    store = Store(tmp_path / "home")
    clock = _Clock()
    store._write_clock = clock
    with store.immediate():
        clock.t += 1.0
    assert not (store.home / SLOW_WRITE_LOG).exists()


def _fake_runner(store):
    return SimpleNamespace(store=store, arbiter=None, _fleet_wired=lambda j: False)


def _job(store):
    return store.create_job("g", "r").job_id


def _errors(store, job_id):
    return [e for e in store.events(job_id) if e.what == "code_fact_error"]


def test_code_facts_step_retries_once_then_succeeds(tmp_path, monkeypatch):
    store = Store(tmp_path / "home")
    job_id = _job(store)
    calls = []

    def step(*a):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return []

    monkeypatch.setattr(code_facts, "step", step)
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)
    runner_mod.Runner._code_facts_step(_fake_runner(store), job_id)
    assert len(calls) == 2 and _errors(store, job_id) == []


def test_code_facts_step_records_error_only_after_second_failure(tmp_path, monkeypatch):
    store = Store(tmp_path / "home")
    job_id = _job(store)
    calls = []

    def step(*a):
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(code_facts, "step", step)
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)
    runner_mod.Runner._code_facts_step(_fake_runner(store), job_id)
    assert len(calls) == 2
    errs = _errors(store, job_id)
    assert len(errs) == 1 and errs[0].evidence == {"error": "database is locked"}


def test_other_errors_are_not_retried(tmp_path, monkeypatch):
    store = Store(tmp_path / "home")
    job_id = _job(store)
    calls = []

    def step(*a):
        calls.append(1)
        raise ValueError("boom")

    monkeypatch.setattr(code_facts, "step", step)
    runner_mod.Runner._code_facts_step(_fake_runner(store), job_id)
    assert len(calls) == 1 and len(_errors(store, job_id)) == 1
