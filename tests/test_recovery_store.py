"""Plan 3 Task 1: additive recovery/spend/measurement tables; insert-only history; nothing existing altered."""
from __future__ import annotations

import sqlite3

import pytest

from steward import store as store_mod
from steward.store import Store

NEW_TABLES = ("spend_limits", "spend_limit_history", "spend_ledger", "spend_commitments", "spend_notices",
              "recovery_attempts", "wait_conditions", "call_checkpoints", "judgment_packs", "judgment_receipts",
              "eval_labels", "run_investigations", "playbook_lessons", "playbook_lesson_history")


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_pre_plan3_database_gains_tables_without_touching_rows(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    conn = sqlite3.connect(home / "steward.db")
    conn.executescript(store_mod._SCHEMA)
    conn.execute("INSERT INTO kv (job_id, key, value_json) VALUES ('j1', 'capabilities', '{\"research\": true}')")
    conn.commit()
    conn.close()
    store = Store(home)
    assert set(NEW_TABLES) <= _tables(store._conn)
    assert store.kv_get("j1", "capabilities") == {"research": True}
    Store(home)  # idempotent second open


def test_eval_label_question_migrates_additively(tmp_path):
    home = tmp_path / "old-recovery"
    home.mkdir()
    conn = sqlite3.connect(home / "steward.db")
    conn.execute("CREATE TABLE eval_labels(label_id TEXT PRIMARY KEY,receipt_id TEXT,lane TEXT,label TEXT,source TEXT,"
                 "actor_kind TEXT,actor_id TEXT,uncertainty TEXT,evidence_json TEXT,created_epoch REAL)")
    conn.execute("INSERT INTO eval_labels VALUES('old','receipt','triage','ok','code_outcome','controller','test','','[]',1)")
    conn.commit()
    conn.close()

    store = Store(home)

    assert "question" in {r["name"] for r in store._conn.execute("PRAGMA table_info(eval_labels)")}
    assert store._conn.execute("SELECT question FROM eval_labels WHERE label_id='old'").fetchone()[0] == ""


@pytest.mark.parametrize("table,cols,vals", [
    ("spend_limit_history", "limit_id, job_id, scope, usd, binding, provenance, source_json, revision, at_epoch",
     "'l', 'j', 'research', 5, 'advisory', 'system_default', '{}', 1, 1"),
    ("playbook_lesson_history", "lesson_id, to_status, actor, reason, at_epoch", "'l', 'active', 'a', 'r', 1"),
])
def test_history_tables_are_insert_only(steward_home, table, cols, vals):
    store = Store(steward_home)
    store._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({vals})")
    store._conn.commit()
    with pytest.raises(sqlite3.DatabaseError, match="insert-only"):
        store._conn.execute(f"UPDATE {table} SET at_epoch = 2")
    with pytest.raises(sqlite3.DatabaseError, match="insert-only"):
        store._conn.execute(f"DELETE FROM {table}")


@pytest.mark.parametrize("table,cols,vals", [
    ("spend_notices", "job_id, scope, update_key, text, delivered, at_epoch", "'j', 'research', 'k', 't', 1, 1"),
    ("call_checkpoints", "checkpoint_id, job_id, call_site, subject_key, attempt, outcome, created_epoch",
     "'c', 'j', 'flow', 's', 1, 'completed', 1"),
    ("judgment_packs", "pack_id, lane, questions_json, created_epoch", "'p', 'triage', '{}', 1"),
    ("eval_labels", "label_id, receipt_id, lane, label, source, actor_kind, actor_id, created_epoch",
     "'l', 'r', 'triage', 'ok', 'code_outcome', 'controller', 'x', 1"),
])
def test_record_tables_cannot_be_rewritten_but_can_be_purged(steward_home, table, cols, vals):
    store = Store(steward_home)
    store._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({vals})")
    store._conn.commit()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute(f"UPDATE {table} SET created_epoch = 2" if table != "spend_notices"
                            else "UPDATE spend_notices SET at_epoch = 2")
    store._conn.execute(f"DELETE FROM {table}")  # retention may delete


def test_limit_checks_reject_bad_rows(steward_home):
    store = Store(steward_home)
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("INSERT INTO spend_limits (limit_id, job_id, scope, usd, binding, provenance, source_json,"
                            " created_epoch, updated_epoch) VALUES ('l', 'j', 'research', 0, 'hard', 'owner_explicit',"
                            " '{}', 1, 1)")
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("INSERT INTO spend_ledger (entry_id, job_id, source, usd, price_basis, created_epoch)"
                            " VALUES ('e', 'j', 'jev', NULL, 'guess', 1)")


def test_one_investigation_per_flag_and_evidence_digest(steward_home):
    store = Store(steward_home)
    sql = ("INSERT INTO run_investigations (investigation_id, flag_id, job_id, evidence_digest, state, created_epoch)"
           " VALUES (?, 'f', 'j', 'd1', 'running', 1)")
    store._conn.execute(sql, ("i1",))
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(sql, ("i2",))
