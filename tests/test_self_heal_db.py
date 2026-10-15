"""Issue W piece (1): the shared store connection waits 30 s for a busy DB, and a receipt write never blocks the
control loop for longer than a total deadline."""
import sqlite3

import pytest

from steward import control_pane
from steward.store import Store


def test_conn_has_30s_busy_timeout(tmp_path):
    s = Store(tmp_path)
    assert s._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_put_receipt_stops_at_total_deadline():
    t = [0.0]
    calls = []

    class Locked:
        def kv_set(self, *a):
            calls.append(t[0])
            t[0] += 30.0  # each attempt waits the full busy timeout
            raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        control_pane._put_receipt(Locked(), "r", {}, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert t[0] <= control_pane.RECEIPT_DEADLINE_S + 30.0
    assert len(calls) == 2  # 0 s, then 30.2 s; a third try would start past the 45 s deadline


def test_put_receipt_still_retries_short_locks():
    n = [0]

    class Flaky:
        def kv_set(self, *a):
            n[0] += 1
            if n[0] < 3:
                raise sqlite3.OperationalError("database is locked")

    control_pane._put_receipt(Flaky(), "r", {}, sleep=lambda s: None)
    assert n[0] == 3
