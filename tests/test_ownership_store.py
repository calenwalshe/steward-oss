"""Plan 1 Task 2: additive ownership tables; insert-only history; flag preservation enforced by the database."""
from __future__ import annotations

import sqlite3

import pytest

from steward import store as store_mod
from steward.store import Store

TABLES = ("blockers", "blocker_history", "action_intents", "decision_records", "decision_outcomes", "run_flags",
          "run_flag_notes")


def _names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_pre_ownership_database_gains_tables_without_touching_rows(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    conn = sqlite3.connect(home / "steward.db")
    conn.executescript(store_mod._SCHEMA)
    conn.execute("INSERT INTO jobs VALUES ('J1','goal','/r','NEEDS_HUMAN','t0','t0')")
    conn.execute("INSERT INTO kv VALUES ('J1','approvals','[]')")
    conn.commit()
    before = [tuple(r) for r in conn.execute("SELECT * FROM jobs")] + [tuple(r) for r in conn.execute("SELECT * FROM kv")]
    assert not set(TABLES) & _names(conn)
    conn.close()
    s = Store(home)
    Store(home).close()  # idempotent
    assert set(TABLES) <= _names(s._conn)
    after = [tuple(r) for r in s._conn.execute("SELECT * FROM jobs")] + [tuple(r) for r in s._conn.execute("SELECT * FROM kv")]
    assert after == before


@pytest.mark.parametrize("table,cols,vals", [
    ("blocker_history", "blocker_id, job_id, at_epoch, to_disposition, actor, reason", "'b','J1',1.0,'candidate','controller','r'"),
    ("run_flag_notes", "flag_id, actor_kind, actor_id, note, created_epoch", "'f','owner','cli','n',1.0"),
])
def test_history_tables_are_insert_only(steward_home, table, cols, vals):
    s = Store(steward_home)
    s._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({vals})")
    for sql in (f"UPDATE {table} SET actor_kind = actor_kind" if table == "run_flag_notes" else f"UPDATE {table} SET reason='x'",
                f"DELETE FROM {table}"):
        with pytest.raises(sqlite3.DatabaseError):
            s._conn.execute(sql)


def test_decision_records_cannot_be_updated_or_purged_under_an_open_flag(steward_home):
    s = Store(steward_home)
    c = s._conn
    c.execute("INSERT INTO decision_records (decision_id, job_id, run_id, actor_kind, actor_id, stage, decision,"
              " created_epoch) VALUES ('d1','J1','J1:a1','controller','ownership','effect','x',1.0)")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE decision_records SET decision='y'")
    c.execute("INSERT INTO run_flags (flag_id, job_id, run_id, actor_kind, actor_id, description, state,"
              " created_epoch) VALUES ('f1','J1','J1:a1','owner','cli','wrong escalation','open',2.0)")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM decision_records WHERE decision_id='d1'")
    c.execute("UPDATE run_flags SET state='closed', closed_epoch=3.0 WHERE flag_id='f1'")
    c.execute("DELETE FROM decision_records WHERE decision_id='d1'")  # retention may purge once no flag is open
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM run_flags")


def test_blocker_issue_episode_is_unique(steward_home):
    s = Store(steward_home)
    row = ("blk-1", "J1", "k:1", 1, "finish_line_unmet", "c", "candidate", "tech_lead", "d", "controller", 1.0, 1.0)
    sql = ("INSERT INTO blockers (blocker_id, job_id, issue_key, episode, kind, claim, disposition, responsible,"
           " evidence_digest, origin_actor, created_epoch, updated_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
    s._conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        s._conn.execute(sql, ("blk-2", *row[1:]))
