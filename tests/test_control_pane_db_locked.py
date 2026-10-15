"""Issue Q: one "database is locked" from the receipt write must never kill the control pane."""
import sqlite3

import pytest

from steward import control_pane
from steward.control_pane import ControlPane, get_receipt
from steward.fabric import Frame
from steward.store import Store


class FakeHerdrApi:
    def call(self, method, params):
        return {}

    def send(self, pane_id, frame):
        return {"type": "ok"}


class FakeMailbox:
    def __init__(self, frames=None):
        self._queue = list(frames or [])

    def read(self, timeout):
        drained, self._queue = self._queue, []
        return drained


def _cmd(op, args, *, request_id="req-1", origin="cli"):
    return {"request_id": request_id, "op": op, "args": args, "origin": origin}


# ------------------------------------------------- database is locked (issue Q)
# 2026-10-06 ~23:40Z: one `sqlite3.OperationalError: database is locked` from the receipt write in `execute()` escaped
# `run_forever` and killed the control pane (it fell to a bare shell).


class LockingStore:
    """Wraps a real Store; `kv_set` raises 'database is locked' for the first `fails` calls."""

    def __init__(self, store, fails):
        self._store, self.fails, self.attempts = store, fails, 0

    def __getattr__(self, name):
        return getattr(self._store, name)

    def kv_set(self, job_id, key, value):
        self.attempts += 1
        if self.attempts <= self.fails:
            raise sqlite3.OperationalError("database is locked")
        return self._store.kv_set(job_id, key, value)


def test_receipt_write_retries_a_locked_database(steward_home, monkeypatch):
    monkeypatch.setattr(control_pane, "_RECEIPT_RETRY_DELAYS", (0.0, 0.0, 0.0))
    store = LockingStore(Store(steward_home), fails=2)
    pane = ControlPane(store, {"pause": lambda cmd: {"accepted": True, "detail": "ok"}}, print_fn=lambda _line: None)

    result = pane.execute(_cmd("pause", {"job_id": "j1"}))

    assert result["accepted"] is True and store.attempts == 3
    assert get_receipt(store, "req-1") == result


def test_receipt_still_locked_is_kept_and_saved_on_the_next_pass(steward_home, monkeypatch):
    monkeypatch.setattr(control_pane, "_RECEIPT_RETRY_DELAYS", (0.0,))
    store = LockingStore(Store(steward_home), fails=2)
    calls = []
    pane = ControlPane(store, {"pause": lambda cmd: calls.append(cmd) or {"accepted": True, "detail": "ok"}},
                       print_fn=lambda _line: None)

    result = pane.execute(_cmd("pause", {"job_id": "j1"}))  # 2 attempts, both locked: no raise
    assert result["accepted"] is True and get_receipt(store, "req-1") is None
    again = pane.execute(_cmd("pause", {"job_id": "j1"}))  # a resend is still a duplicate, never re-executed
    assert again["duplicate"] is True and len(calls) == 1

    pane.step(FakeMailbox(), FakeHerdrApi(), timeout=0.0)  # the next pass saves it
    assert get_receipt(store, "req-1")["accepted"] is True


def test_non_lock_operational_error_is_not_retried(steward_home, monkeypatch):
    slept = []
    monkeypatch.setattr(control_pane, "_RECEIPT_RETRY_DELAYS", (0.0, 0.0))

    class Broken:
        def kv_set(self, *a):
            raise sqlite3.OperationalError("no such table: kv")

    with pytest.raises(sqlite3.OperationalError):
        control_pane._put_receipt(Broken(), "r", {}, sleep=slept.append)
    assert slept == []


def test_run_forever_survives_an_exception_in_one_step(steward_home):
    store = Store(steward_home)
    lines, handled = [], []
    pane = ControlPane(store, {"pause": lambda cmd: handled.append(cmd) or {"accepted": True, "detail": "ok"}},
                       print_fn=lines.append)

    class FlakyMailbox(FakeMailbox):
        reads = 0

        def read(self, timeout):
            self.reads += 1
            if self.reads == 1:
                raise sqlite3.OperationalError("database is locked")
            return super().read(timeout)

    mailbox = FlakyMailbox([Frame(v=1, seq=1, job="req-1", type="cmd", body=_cmd("pause", {"job_id": "j1"}))])
    n = {"i": 0}

    def running():
        n["i"] += 1
        return n["i"] <= 2

    pane.run_forever(mailbox, FakeHerdrApi(), poll_s=0.0, running=running)

    assert len(handled) == 1
    assert any("database is locked" in line for line in lines)
