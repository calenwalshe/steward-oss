"""A locked database never kills the runner (2026-10-07): `database is locked` raised from a tick
(watcher_authority.observe_tick -> kv_set) escaped run_forever twice and left the runner pane a bare shell."""
import sqlite3
from types import SimpleNamespace

import pytest

from steward.runner import Runner, _is_db_lock_error


class _Store:
    def __init__(self):
        self.events = []

    def append_event(self, job_id, **kw):
        self.events.append(kw["what"])


def _stub(outcomes):
    """A Runner stand-in whose tick() raises/returns `outcomes` in order."""
    seq = iter(outcomes)

    def tick(job_id):
        out = next(seq)
        if isinstance(out, BaseException):
            raise out
        return out

    return SimpleNamespace(tick=tick, store=_Store())


def test_locked_tick_is_retried_with_backoff_then_recorded():
    report = SimpleNamespace(state="RUNNING")
    stub = _stub([sqlite3.OperationalError("database is locked"), sqlite3.OperationalError("database is busy"), report])
    slept = []
    assert Runner._tick_surviving_lock(stub, "job-1", slept.append) is report
    assert slept == [1.0, 2.0]
    assert stub.store.events == ["tick_db_lock_retried"]


def test_clean_tick_records_nothing():
    report = SimpleNamespace(state="RUNNING")
    stub = _stub([report])
    assert Runner._tick_surviving_lock(stub, "job-1", lambda s: None) is report
    assert stub.store.events == []


def test_backoff_caps_at_the_last_delay():
    report = SimpleNamespace(state="RUNNING")
    stub = _stub([sqlite3.OperationalError("database is locked")] * 7 + [report])
    slept = []
    Runner._tick_surviving_lock(stub, "job-1", slept.append)
    assert slept == [1.0, 2.0, 5.0, 10.0, 30.0, 30.0, 30.0]


def test_other_database_errors_still_propagate():
    stub = _stub([sqlite3.OperationalError("no such table: jobs")])
    with pytest.raises(sqlite3.OperationalError):
        Runner._tick_surviving_lock(stub, "job-1", lambda s: None)


def test_lock_classifier():
    assert _is_db_lock_error(sqlite3.OperationalError("database is locked"))
    assert not _is_db_lock_error(sqlite3.OperationalError("disk I/O error"))


def test_run_forever_ticks_through_the_lock_survivor():
    import inspect

    src = inspect.getsource(Runner.run_forever)
    assert "self.tick(job_id)" not in src and src.count("self._tick_surviving_lock(job_id, sleep_fn)") == 2
