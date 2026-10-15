"""Durable request/effect reconciliation for the control pane (issue W, spec §8)."""
import sqlite3

from steward import control_journal, control_pane
from steward.control_pane import ControlPane
from steward.store import Store


class LockedKv:
    """A store whose kv writes fail with `database is locked` while `locked` is set."""

    def __init__(self, store):
        self.s = store
        self.locked = True

    def __getattr__(self, name):
        return getattr(self.s, name)

    def kv_set(self, *a, **k):
        if self.locked:
            raise sqlite3.OperationalError("database is locked")
        return self.s.kv_set(*a, **k)


def _pane(store, calls, journal):
    def handler(cmd):
        calls.append(cmd["request_id"])
        return {"accepted": True, "detail": "did it"}
    pane = ControlPane(store, {"do": handler}, print_fn=lambda _l: None, journal=journal)
    return pane


def test_unsaved_receipt_survives_a_new_process(tmp_path, monkeypatch):
    monkeypatch.setattr(control_pane, "_RECEIPT_RETRY_DELAYS", ())
    real = Store(tmp_path)
    locked = LockedKv(real)
    calls = []
    j = control_journal.Journal(tmp_path)
    r = _pane(locked, calls, j).execute({"request_id": "r1", "op": "do", "args": {}})
    assert r["accepted"] and control_pane.get_receipt(real, "r1") is None

    # control re-execs (memory gone); the DB is free again
    locked.locked = False
    pane2 = _pane(locked, calls, control_journal.Journal(tmp_path))
    pane2._save_unsaved()
    assert control_pane.get_receipt(real, "r1")["detail"] == "did it"
    again = pane2.execute({"request_id": "r1", "op": "do", "args": {}})
    assert again["duplicate"] is True
    assert calls == ["r1"]  # never executed twice


def test_duplicate_is_recognised_from_the_journal_while_the_db_is_still_locked(tmp_path, monkeypatch):
    monkeypatch.setattr(control_pane, "_RECEIPT_RETRY_DELAYS", ())
    locked = LockedKv(Store(tmp_path))
    calls = []
    _pane(locked, calls, control_journal.Journal(tmp_path)).execute({"request_id": "r1", "op": "do", "args": {}})
    again = _pane(locked, calls, control_journal.Journal(tmp_path)).execute({"request_id": "r1", "op": "do", "args": {}})
    assert again["duplicate"] is True and calls == ["r1"]


def test_interrupted_command_gets_an_interrupted_receipt_and_is_not_rerun(tmp_path):
    store = Store(tmp_path)
    j = control_journal.Journal(tmp_path)
    j.received({"request_id": "r2", "op": "do", "args": {}})  # the process died mid-execution
    calls = []
    pane = _pane(store, calls, control_journal.Journal(tmp_path))
    pane._save_unsaved()
    rec = control_pane.get_receipt(store, "r2")
    assert rec["accepted"] is False and rec["interrupted"] is True and "do" in rec["detail"]
    assert pane.execute({"request_id": "r2", "op": "do", "args": {}})["duplicate"] is True
    assert calls == []


def test_journal_compacts_once_receipts_are_saved(tmp_path):
    store = Store(tmp_path)
    calls = []
    j = control_journal.Journal(tmp_path)
    pane = _pane(store, calls, j)
    pane.execute({"request_id": "r3", "op": "do", "args": {}})
    pane._save_unsaved()
    assert j.path.read_text() == ""


def test_torn_last_line_is_ignored(tmp_path):
    j = control_journal.Journal(tmp_path)
    j.done("r4", {"accepted": True})
    with j.path.open("a") as fh:
        fh.write('{"t": "done", "rid"')
    assert j.result("r4") == {"accepted": True}
