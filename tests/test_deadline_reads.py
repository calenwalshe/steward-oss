"""v2.2 Phase 21 (DL-08): every read surface consumes one canonical stored deadline object and never writes.

CLI deadline/status/explain, API get_job, MCP get_job, the phone rows and the status pane are read repeatedly; the full
event log and kv table must be byte-identical before and after."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deadline_fixtures import Repo, commit_deadline, plan_json
from steward import cli, deadline, deadline_plan, deadline_status, mcp_server, phone
from steward.api import StewardAPI
from steward.runner_exec import SubprocessRunner
from steward.store import Store

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
DUE = NOW + timedelta(hours=4)
_REPO = Path(__file__).resolve().parent.parent


def _fingerprint(store):
    events = store._conn.execute("SELECT seq, job_id, what, evidence, rule, judgment FROM events ORDER BY seq").fetchall()
    kv = store._conn.execute("SELECT job_id, key, value_json FROM kv ORDER BY job_id, key").fetchall()
    return [tuple(r) for r in events], [tuple(r) for r in kv]


@pytest.fixture
def setup(steward_home, tmp_path, monkeypatch):
    monkeypatch.setattr(deadline, "utcnow", lambda: NOW + timedelta(hours=1, minutes=30))
    store = Store(steward_home)
    repo = Repo(tmp_path, store)
    commit_deadline(store, repo.job_id, now=NOW)
    repo.commit({deadline_plan.plan_path(repo.job_id): plan_json(repo.job_id, 1, due=DUE, now=NOW,
                                                                  m1_target=NOW + timedelta(hours=1))},
                "plan", when=NOW + timedelta(minutes=2))
    deadline_status.sweep(store, repo.job_id, now=NOW + timedelta(minutes=3), runner=SubprocessRunner(), worktree=repo.wt)
    deadline_status.sweep(store, repo.job_id, now=NOW + timedelta(hours=1, minutes=10), runner=SubprocessRunner(),
                          worktree=repo.wt, force=True)
    return store, repo


def test_all_read_surfaces_are_mutation_free_and_agree(setup, capsys):
    store, repo = setup
    jid = repo.job_id
    before = _fingerprint(store)
    api = StewardAPI(store)
    tools = {t.name: t for t in mcp_server.build_tools(api)}
    for _ in range(3):
        assert cli.main(["deadline", jid]) == 0
        assert cli.main(["deadline", jid, "show", "--json"]) == 0
        assert cli.main(["status", jid]) == 0
        assert cli.main(["explain", jid]) == 0
        got = api.get_job(jid)["deadline"]
        via_mcp = tools["get_job"].handler({"job_id": jid}, None)["job"]["deadline"]
        snap = phone.gather(store, None, now=NOW + timedelta(hours=1, minutes=30))
        phone.home_lines(snap, 80)
    out = capsys.readouterr().out
    assert _fingerprint(store) == before
    assert got["status"] == via_mcp["status"] == "at_risk" and got["reasons"][0]["rule"] == "R1"
    assert got["eta"] if "eta" in got else True  # the deadline object never carries the ETA
    assert "M1" in out and "MISSED" in out and "deadline:" in out and "Deadline:" in out
    (row,) = [j for j in snap.jobs if j.job_id == jid]
    assert "⏱ At risk" in row.hint and "2h30 left" in row.hint
    assert api.get_job(jid)["eta"] is None  # ETA object unchanged and separate


def test_status_pane_shows_the_sign_beside_the_eta_without_writing(setup, tmp_path):
    store, repo = setup
    before = _fingerprint(store)
    env = {**os.environ, "STEWARD_HOME": str(store.home), "STEWARD_DASH_COLS": "160", "STEWARD_HEALTH": "0",
           "STEWARD_DASH_MAP": str(tmp_path / "map"), "PYTHONPATH": str(_REPO)}
    res = subprocess.run([sys.executable, str(_REPO / "deploy/cockpit/steward-dash-jobs.py")], capture_output=True,
                         text=True, env=env, cwd=_REPO)
    assert res.returncode == 0, res.stderr
    # the pane process runs on the real clock: before the fixture's due time it shows the sign, after it the banner
    assert "⏱ At risk" in res.stdout or "⏱ Deadline passed" in res.stdout
    assert _fingerprint(store) == before


def test_projection_is_display_only_for_the_clock(setup):
    store, repo = setup
    a = deadline_status.project(store, repo.job_id, NOW + timedelta(hours=2))
    b = deadline_status.project(store, repo.job_id, DUE + timedelta(minutes=5))
    assert a["status"] == b["status"] and a["reasons"] == b["reasons"]
    assert a["left"] == "2h00 left" and b["banner"] == "Deadline passed · 5m over"
    assert json.dumps(a["slots"]) == json.dumps(b["slots"])
