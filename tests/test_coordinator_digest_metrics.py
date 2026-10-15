"""Digest zero-touch metrics (spec 5.17): read from `steward metrics zero-touch --since ISO --json`, never raising."""
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from steward.coordinator import db, digest
from steward.coordinator import __main__ as cmain


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))


def _settings(tmp_path):
    return SimpleNamespace(steward_repo=tmp_path / "repo", steward_home=tmp_path / "home", digest_slug="sl")


def _run(rc=0, out="", calls=None):
    def run(argv, **kw):
        if calls is not None:
            calls.append((argv, kw))
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")
    return run


def test_argv_cwd_env_and_dict_on_success(tmp_path):
    calls = []
    payload = {"touches_per_job": 0.2, "coordinator_actions": 3, "asks": 1}
    got = digest.zero_touch_metrics(_settings(tmp_path), since_iso="2026-10-07T00:00:00Z",
                                    run=_run(0, json.dumps(payload), calls))
    assert got == payload
    argv, kw = calls[0]
    assert argv == [sys.executable, "-m", "steward.cli", "metrics", "zero-touch", "--since",
                    "2026-10-07T00:00:00Z", "--json"]
    assert kw["cwd"] == tmp_path / "repo"
    assert kw["env"]["STEWARD_HOME"] == str(tmp_path / "home")
    assert kw["capture_output"] is True and kw["text"] is True and kw["timeout"] == 60


@pytest.mark.parametrize("rc,out", [(1, '{"a": 1}'), (0, "not json"), (0, "[1, 2]"), (0, "")])
def test_none_on_failure_bad_json_or_non_dict(tmp_path, rc, out):
    assert digest.zero_touch_metrics(_settings(tmp_path), since_iso="x", run=_run(rc, out)) is None


def test_none_on_exception(tmp_path):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired("steward", 60)
    assert digest.zero_touch_metrics(_settings(tmp_path), since_iso="x", run=boom) is None
    assert digest.zero_touch_metrics(SimpleNamespace(), since_iso="x", run=_run(0, "{}")) is None


def test_main_digest_renders_parsed_metrics(tmp_path, capsys):
    cdb = db.CoordDB(tmp_path / "c.db")
    now = time.time()
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        if "zero-touch" in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"touches_per_job": 0.27, "coordinator_actions": 41, "asks": 7}), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")
    assert cmain.main(["digest"], run=run, cdb=cdb, clock=lambda: now, settings=_settings(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "touches_per_job: 0.27" in out and "coordinator_actions: 41" in out and "asks: 7" in out
    argv = next(a for a in seen if "zero-touch" in a)
    since_iso = argv[argv.index("--since") + 1]
    assert since_iso.endswith("Z") or since_iso.endswith("+00:00")


def test_since_iso_is_24h_before_now():
    from datetime import datetime
    now = time.time()
    got = datetime.fromisoformat(digest.since_iso(now).replace("Z", "+00:00")).timestamp()
    assert abs((now - 86400) - got) < 2
