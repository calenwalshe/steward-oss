"""Phase 26 (FLEET-05): additive normalized records, append-only history, truthful legacy projection."""

from __future__ import annotations

import sqlite3

import pytest

from steward import fleet_graph as fg
from steward import fleet_registry as fr
from steward.fleet_store import FleetStore
from steward.store import Store


def test_fleet_tables_created_idempotently(steward_home):
    Store(steward_home).close()
    s = Store(steward_home)
    names = {r["name"] for r in s._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("fleet_roster_revisions", "fleet_qualifications", "fleet_jobs", "fleet_graph_revisions",
              "fleet_tasks", "fleet_task_deps", "fleet_task_claims"):
        assert t in names


def _seed_cli(home):
    from test_fleet_graph import BLOB, manifest, task
    fs = FleetStore(Store(home))
    fs.record_roster(fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [{
        "id": "claude-sonnet", "provider": "anthropic", "runtime": "claude-code", "model": "m",
        "roles": ["worker"], "capabilities": ["edit"], "strengths": [], "enabled": True, "label": "L"}]}))
    fs.record_qualification("claude-code", "1.0", ["edit"], qualified=True, available=True)
    fs.store.create_job("g", "/repo", job_id="JF")
    fs.record_graph("JF", 1, fg.compile_graph(manifest(task("t1")), committed_plan_blob_id=BLOB, goal="g"))
    fs.store.create_job("old goal", "/repo", job_id="JL")
    return fs


def _rows(home):
    s = Store(home)
    return {t: s._conn.execute(f"SELECT * FROM {t}").fetchall() and
            [tuple(r) for r in s._conn.execute(f"SELECT * FROM {t}")]
            for t in ("jobs", "events", "fleet_roster_revisions", "fleet_qualifications", "fleet_tasks")}


def test_cli_roster_and_graph_are_read_only(steward_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "cfg"))
    import json
    from steward import cli
    _seed_cli(steward_home)
    before = _rows(steward_home)
    assert cli.main(["fleet", "roster", "--json"]) == 0
    roster = json.loads(capsys.readouterr().out)
    assert roster[0]["id"] == "claude-sonnet" and roster[0]["dispatchable"] is True
    assert cli.main(["fleet", "roster"]) == 0
    assert "claude-sonnet" in capsys.readouterr().out
    assert cli.main(["fleet", "graph", "JF"]) == 0
    assert "t1" in capsys.readouterr().out
    assert cli.main(["fleet", "graph", "JL", "--json"]) == 0
    legacy = json.loads(capsys.readouterr().out)
    assert legacy["mode"] == "legacy" and legacy["tasks"][0]["model"] == "unknown"
    assert cli.main(["fleet", "graph", "NOPE"]) == 1
    assert _rows(steward_home) == before


# -- task 3: migration, append-only history, truthful legacy projection --------------

def _table_dump(conn, tables):
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}


def test_pre_fleet_database_is_unchanged_by_initialization(tmp_path):
    from steward import store as store_mod
    home = tmp_path / "h"
    home.mkdir()
    conn = sqlite3.connect(home / "steward.db")
    conn.executescript(store_mod._SCHEMA)  # a database exactly as it was before fleet tables existed
    conn.execute("INSERT INTO jobs VALUES ('J1','goal','/r','CREATED','t0','t0')")
    conn.execute("INSERT INTO events (job_id, ts, what, evidence, rule, judgment) VALUES ('J1','t0','w','{}','r','code')")
    conn.execute("INSERT INTO kv VALUES ('J1','k','1')")
    conn.commit()
    legacy = ["jobs", "events", "kv", "attempts", "checks", "instructions", "control_requests"]
    before = _table_dump(conn, legacy)
    names0 = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any(n.startswith("fleet_") for n in names0)
    conn.close()

    s = Store(home)
    Store(home).close()  # idempotent
    assert _table_dump(s._conn, legacy) == before
    assert s.get_job("J1").state.value == "CREATED"
    assert FleetStore(s).graph_view("J1")["mode"] == "legacy"
    assert _table_dump(s._conn, ["fleet_jobs", "fleet_tasks", "fleet_assignments"]) == \
        {"fleet_jobs": [], "fleet_tasks": [], "fleet_assignments": []}


def test_fleet_event_history_is_append_only_and_masked(steward_home):
    from steward import masking
    from steward.models import Judgment
    fs = FleetStore(Store(steward_home))
    fs.store.create_job("g", "/r", job_id="J1")
    masking.register(["hunter2-secret-value"])
    try:
        seq = fs.append_event("J1", what="lane note hunter2-secret-value", task_id="t1", lane_id="l1",
                              graph_rev=1, registry_hash="h", actor="steward", rule="r",
                              judgment=Judgment.code, evidence={"k": "hunter2-secret-value"})
    finally:
        masking.clear()
    ev = fs.fleet_events("J1")
    assert [e["seq"] for e in ev] == [seq] and "hunter2" not in str(ev)
    assert ev[0]["task_id"] == "t1" and ev[0]["graph_rev"] == 1
    for sql in ("UPDATE fleet_events SET what='x'", "DELETE FROM fleet_events"):
        with pytest.raises(sqlite3.DatabaseError):
            fs.store._conn.execute(sql)


def test_history_tables_reject_update_and_delete(steward_home):
    from test_fleet_graph import BLOB, manifest, task
    fs = FleetStore(Store(steward_home))
    fs.store.create_job("g", "/r", job_id="J1")
    fs.record_graph("J1", 1, fg.compile_graph(manifest(task("t1")), committed_plan_blob_id=BLOB, goal="g"))
    fs.record_roster(fr.validate_roster({"schema_version": 1, "revision": 1, "entries": []}))
    fs.record_qualification("r", "1", [], qualified=True, available=True)
    for t in ("fleet_graph_revisions", "fleet_tasks", "fleet_task_claims", "fleet_roster_revisions",
              "fleet_qualifications"):
        for sql in (f"UPDATE {t} SET rowid=rowid", f"DELETE FROM {t}"):
            with pytest.raises(sqlite3.DatabaseError):
                fs.store._conn.execute(sql)


def test_record_graph_refuses_dropped_required_work(steward_home):
    from test_fleet_graph import BLOB, manifest, task
    fs = FleetStore(Store(steward_home))
    fs.store.create_job("g", "/r", job_id="J1")
    fs.record_graph("J1", 1, fg.compile_graph(manifest(task("a"), task("b")), committed_plan_blob_id=BLOB, goal="g"))
    with pytest.raises(ValueError, match="required"):
        fs.record_graph("J1", 2, fg.compile_graph(manifest(task("a")), committed_plan_blob_id=BLOB, goal="g"))
    fs.record_graph("J1", 2, fg.compile_graph(manifest(task("a"), task("b"), task("c")), committed_plan_blob_id=BLOB, goal="g"))
    assert fs.graph_view("J1")["graph_rev"] == 2


def test_current_graph_projection_uses_compare_and_swap(steward_home):
    fs = FleetStore(Store(steward_home))
    fs.store.create_job("g", "/r", job_id="J1")
    assert fs.set_current_graph("J1", 1, expected_version=0) is True
    assert fs.set_current_graph("J1", 2, expected_version=0) is False  # stale writer loses
    assert fs.set_current_graph("J1", 2, expected_version=1) is True
    assert fs.current_graph("J1") == (2, 2)


def test_lane_attempt_and_integration_shapes_exist(steward_home):
    fs = FleetStore(Store(steward_home))
    fs.store.create_job("g", "/r", job_id="J1")
    fs.record_lane("J1", "l1", task_id="t1", worktree_path="/w", branch="b")
    fs.record_attempt("J1", "l1", 1, provider="p", runtime="r", model="m", adapter_version="1")
    fs.record_integration_batch("J1", "b1", graph_rev=1, inputs=[{"task": "t1", "sha": "abc"}])
    s = fs.store._conn
    assert s.execute("SELECT usage_source FROM fleet_attempts").fetchone()[0] == "unknown"
    for sql in ("UPDATE fleet_attempts SET model='x'", "DELETE FROM fleet_integration_batches"):
        with pytest.raises(sqlite3.DatabaseError):
            s.execute(sql)


def test_cli_graph_shows_not_ready_errors(steward_home, tmp_path, monkeypatch, capsys):
    from test_fleet_graph import BLOB, manifest, task
    from steward import cli
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "cfg"))
    fs = FleetStore(Store(steward_home))
    fs.store.create_job("g", "/r", job_id="JX")
    fs.record_graph("JX", 1, fg.compile_graph(manifest(task("a", depends_on=["ghost"])), committed_plan_blob_id=BLOB, goal="g"))
    assert cli.main(["fleet", "graph", "JX"]) == 0
    assert "NOT READY" in capsys.readouterr().out
