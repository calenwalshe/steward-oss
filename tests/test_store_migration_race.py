"""Clean-install finding 2026-10-14: `steward init` starts control, advisor and dash at once on a fresh database; each
Store() saw a column missing and ran the same ALTER TABLE, and the loser crashed with `duplicate column name`.
Adding a column must be idempotent even when another process added it after our check."""
from __future__ import annotations

import sqlite3

from steward.store import Store


def test_adding_a_column_another_process_already_added_is_a_no_op(steward_home):
    store = Store(steward_home)
    store._add_column("instructions", "origin", "TEXT")  # already there: must not raise


def test_a_stale_column_check_does_not_crash_a_second_store(steward_home, monkeypatch):
    Store(steward_home)
    real = Store._columns
    monkeypatch.setattr(Store, "_columns", lambda self, table: set())  # every column looks missing (the race)
    Store(steward_home)
    monkeypatch.setattr(Store, "_columns", real)
    names = {r[1] for r in sqlite3.connect(steward_home / "steward.db").execute("PRAGMA table_info(instructions)")}
    assert "origin" in names
