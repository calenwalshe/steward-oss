"""Plan 4 Task 3: durable check-run receipts; equivalent runs are shared, different ones never (spec §8)."""
from __future__ import annotations

import sqlite3
import subprocess

import pytest

from steward import check_runs as cr
from steward import masking
from steward import store as store_mod
from steward.baseline import CheckRun
from steward.runner_exec import SubprocessRunner
from steward.store import Store

TABLES = ("check_runs", "check_run_waiters", "repair_episodes", "delivery_receipts")


def spec(**kw):
    base = dict(tree_sha="t1", command="python3 -m pytest -q", env_digest="e1", toolchain="tc1", heavy=False,
                predicted_s=None)  # scoped by default: the heavy slot and FIFO order are Task 4's tests
    base.update(kw)
    return cr.RunSpec(**base)


def run_to_done(store, row, *, exit_code=0, tests=None, broken=None, now=10.0, output="1 passed"):
    assert cr.try_start(store, row.run_id, owner="me", pid=1, now=now)
    return cr.complete(store, row.run_id, owner="me", run=CheckRun(exit_code=exit_code, tests=tests or {"t::a": "passed"},
                                                                    broken=broken, used=row.command, output=output),
                       junit_src=None, now=now + 1)


def test_pre_plan4_database_gains_tables_without_touching_rows(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    conn = sqlite3.connect(home / "steward.db")
    conn.executescript(store_mod._SCHEMA)
    conn.execute("INSERT INTO jobs VALUES ('J1','goal','/r','ACCEPTED','t0','t0')")
    conn.commit()
    before = [tuple(r) for r in conn.execute("SELECT * FROM jobs")]
    conn.close()
    s = Store(home)
    Store(home).close()
    names = {r[0] for r in s._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES) <= names
    assert [tuple(r) for r in s._conn.execute("SELECT * FROM jobs")] == before


def test_delivery_receipts_are_insert_only(steward_home):
    s = Store(steward_home)
    s._conn.execute("INSERT INTO delivery_receipts (job_id, milestone, actor_kind, actor_id, revision, evidence_json,"
                    " created_epoch) VALUES ('J1','incorporation','controller','delivery','abc','{}',1.0)")
    for sql in ("UPDATE delivery_receipts SET revision='x'", "DELETE FROM delivery_receipts"):
        with pytest.raises(sqlite3.DatabaseError):
            s._conn.execute(sql)


def test_one_open_repair_episode_per_root(steward_home):
    s = Store(steward_home)
    sql = ("INSERT INTO repair_episodes (episode_id, root_job, repo, target_branch, state, created_epoch,"
           " updated_epoch) VALUES (?, 'R1', '/r', 'main', ?, 1.0, 1.0)")
    s._conn.execute(sql, ("rep-1", "open"))
    with pytest.raises(sqlite3.IntegrityError):
        s._conn.execute(sql, ("rep-2", "repairing"))
    s._conn.execute("UPDATE repair_episodes SET state='resolved' WHERE episode_id='rep-1'")
    s._conn.execute(sql, ("rep-3", "open"))


def test_equivalent_requests_share_one_run_and_record_every_waiter(steward_home):
    s = Store(steward_home)
    a, how_a = cr.request(s, spec(), job_id="JA", purpose="verify", now=1.0)
    b, how_b = cr.request(s, spec(), job_id="JB", purpose="verify", now=2.0)
    assert (how_a, how_b) == ("enqueued", "attached") and a.run_id == b.run_id
    run_to_done(s, a)
    c, how_c = cr.request(s, spec(), job_id="JC", purpose="merged_tree", now=20.0)
    assert how_c == "reused" and c.run_id == a.run_id
    assert sorted((w["job_id"], w["how"]) for w in cr.waiters(s, a.run_id)) == [
        ("JA", "enqueued"), ("JB", "attached"), ("JC", "reused")]
    assert s._conn.execute("SELECT COUNT(*) FROM check_runs").fetchone()[0] == 1


@pytest.mark.parametrize("field,value", [("tree_sha", "t2"), ("command", "python3 -m pytest -q tests/x.py"),
                                         ("env_digest", "e2"), ("toolchain", "tc2")])
def test_a_different_revision_selection_environment_or_toolchain_is_never_reused(steward_home, field, value):
    s = Store(steward_home)
    a, _ = cr.request(s, spec(), job_id="JA", purpose="verify", now=1.0)
    run_to_done(s, a)
    b, how = cr.request(s, spec(**{field: value}), job_id="JB", purpose="verify", now=20.0)
    assert how == "enqueued" and b.run_id != a.run_id


def test_broken_or_expired_receipts_are_not_reused(steward_home):
    s = Store(steward_home)
    a, _ = cr.request(s, spec(), job_id="JA", purpose="verify", now=1.0)
    run_to_done(s, a, exit_code=4, tests={}, broken="pytest usage error (exit 4)")
    assert cr.request(s, spec(), job_id="JB", purpose="verify", now=20.0)[1] == "enqueued"
    s2 = spec(tree_sha="t9")
    c, _ = cr.request(s, s2, job_id="JA", purpose="verify", now=1.0)
    run_to_done(s, c)
    assert cr.request(s, s2, job_id="JB", purpose="verify", now=2.0 + cr.REUSE_MAX_AGE_S + 60)[1] == "enqueued"


def test_red_results_are_real_results_and_are_shared(steward_home):
    s = Store(steward_home)
    a, _ = cr.request(s, spec(), job_id="JA", purpose="verify", now=1.0)
    run_to_done(s, a, exit_code=1, tests={"t::a": "failed"})
    b, how = cr.request(s, spec(), job_id="JB", purpose="verify", now=20.0)
    assert how == "reused" and b.exit_code == 1 and b.tests == {"t::a": "failed"}


def test_only_the_running_owner_can_complete(steward_home):
    s = Store(steward_home)
    a, _ = cr.request(s, spec(), job_id="JA", purpose="verify", now=1.0)
    assert cr.try_start(s, a.run_id, owner="me", pid=1, now=2.0)
    assert not cr.try_start(s, a.run_id, owner="other", pid=2, now=2.0)
    with pytest.raises(ValueError):
        cr.complete(s, a.run_id, owner="other", run=CheckRun(exit_code=0, tests={}), junit_src=None, now=3.0)


def test_output_is_masked_bounded_and_junit_is_kept(steward_home, tmp_path):
    s = Store(steward_home)
    masking.register(["sekrit-value-123"])
    try:
        a, _ = cr.request(s, spec(), job_id="JA", purpose="verify", now=1.0)
        cr.try_start(s, a.run_id, owner="me", pid=1, now=2.0)
        xml = tmp_path / "r.xml"
        xml.write_text("<testsuites/>")
        row = cr.complete(s, a.run_id, owner="me", junit_src=xml, now=3.0,
                          run=CheckRun(exit_code=0, tests={}, output="x" * 9000 + " token sekrit-value-123"))
    finally:
        masking.clear()
    assert len(row.output_excerpt) <= cr.OUTPUT_EXCERPT_CHARS and "sekrit-value-123" not in row.output_excerpt
    out = tmp_path / "copy.xml"
    assert cr.as_check_run(row, out).exit_code == 0 and out.read_text() == "<testsuites/>"


def test_spec_for_refuses_dirty_or_unreadable_trees(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    for argv in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "a@b"],
                 ["git", "config", "user.name", "a"]):
        subprocess.run(argv, cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "a"], cwd=repo, check=True)
    runner = SubprocessRunner()
    sp = cr.spec_for(runner, repo, "python3 -m pytest -q", toolchain_fn=lambda c: "tc")
    assert sp is not None and len(sp.tree_sha) == 40 and sp.heavy
    (repo / "b.py").write_text("y = 2\n")
    assert cr.spec_for(runner, repo, "python3 -m pytest -q", toolchain_fn=lambda c: "tc") is None
    assert cr.spec_for(runner, tmp_path, "python3 -m pytest -q", toolchain_fn=lambda c: "tc") is None


@pytest.mark.parametrize("command,predicted,heavy", [
    ("python3 -m pytest -q", None, True), ("pytest", None, True), ("python3 -m pytest -q tests/test_a.py", None, False),
    ("python3 -m pytest -q -k slow", None, False), ("python3 -m pytest -q tests/test_a.py", 900.0, True),
    ("python3 -m pytest -q", 30.0, False), ("make lint", None, False)])
def test_heavy_rules(command, predicted, heavy):
    assert cr.is_heavy(command, predicted) is heavy


def test_purge_keeps_recent_and_unconsumed_receipts(steward_home):
    s = Store(steward_home)
    day = 86400.0
    old, _ = cr.request(s, spec(tree_sha="old"), job_id="JA", purpose="verify", now=1.0)
    run_to_done(s, old, now=2.0)
    cr.consume(s, old.run_id, "JA", "verify", now=3.0)
    held, _ = cr.request(s, spec(tree_sha="held"), job_id="JB", purpose="verify", now=1.0)
    run_to_done(s, held, now=2.0)
    new, _ = cr.request(s, spec(tree_sha="new"), job_id="JC", purpose="verify", now=40 * day)
    run_to_done(s, new, now=40 * day + 1)
    cr.consume(s, new.run_id, "JC", "verify", now=40 * day + 2)
    assert cr.purge(s, now=40 * day + 10) == 1
    assert cr.get(s, old.run_id) is None and cr.get(s, held.run_id) and cr.get(s, new.run_id)


def test_check_runs_script_runs_standalone_without_pythonpath():
    """`python3 steward/check_runs.py` from the repo root must import cleanly with no PYTHONPATH (finish-line command)."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    r = subprocess.run([sys.executable, "steward/check_runs.py"], cwd=root, env=env, capture_output=True, text=True)
    # v2.7 Phase 42 gave the module a `run RUN_ID JOB_ID` entry point: with no arguments it now imports, reaches main()
    # and exits 2 with its usage line (before Phase 42 the script had no main and exited 0 after the import).
    # An import failure would instead exit 1 with a traceback.
    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 2 and r.stderr.startswith("usage: python3 -m steward.check_runs run"), (r.returncode, r.stderr)
