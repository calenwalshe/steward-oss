import json
import time
from pathlib import Path

from steward import coordinator_link
from steward.coordinator import config, db
from steward.coordinator.config import Settings


def test_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))
    s = config.load()
    assert s.enabled is True
    assert (s.poll_s, s.hold_s, s.ask_by_s) == (15, 1200, 1080)
    assert (s.rerun_run_s, s.rerun_total_s, s.rerun_max_ids) == (300, 900, 50)
    assert (s.episodes_per_job_day, s.cooldown_s, s.tries_per_rung_day) == (3, 1800, 2)
    assert (s.resolved_watch_s, s.orphan_s, s.orphan_scan_s, s.health_red_s) == (600, 3600, 300, 120)
    assert s.model_primary == "codex:gpt-5.5" and s.model_fallback == "claude:sonnet"
    assert s.model_reopen == "claude:opus"
    assert (s.model_timeout_s, s.model_calls_per_episode, s.model_actions_per_call) == (300, 2, 4)
    assert (s.budget_daily_usd, s.budget_daily_codex_calls, s.budget_call_usd) == (10.0, 20, 2.0)
    assert (s.digest_slug, s.digest_at) == ("steward-coordinator", "07:00")
    assert isinstance(s.steward_home, Path) and isinstance(s.steward_repo, Path)
    assert Settings().poll_s == 15
    assert Settings(steward_home=tmp_path, steward_repo=tmp_path).steward_home == tmp_path


def test_config_file_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))
    (tmp_path / "coordinator.toml").write_text(
        'poll_s = 5\ncooldown_s = 60\n[budget]\ndaily_usd = 3\n'
        '[model]\nprimary = "claude:sonnet"\nunknown = 1\n[digest]\nat = "08:30"\n')
    s = config.load()
    assert s.poll_s == 5 and s.cooldown_s == 60
    assert s.budget_daily_usd == 3.0 and isinstance(s.budget_daily_usd, float)
    assert s.model_primary == "claude:sonnet" and s.digest_at == "08:30"
    assert s.model_fallback == "claude:sonnet" and s.hold_s == 1200


def test_config_malformed_and_hold_mismatch(tmp_path, caplog):
    p = tmp_path / "c.toml"
    p.write_text("poll_s = [[[")
    assert config.load(p).poll_s == 15
    p.write_text("hold_s = 99\n")
    with caplog.at_level("WARNING"):
        s = config.load(p)
    assert s.hold_s == coordinator_link.HOLD_S
    assert caplog.records
    assert config.load(tmp_path / "missing.toml").poll_s == 15


def test_episode_lifecycle(tmp_path):
    d = db.CoordDB(tmp_path / "c.db")
    eid = d.open_episode("job1", "needs_human:x", 100, now=1000.0)
    row = d.open_for("job1")
    assert row["episode_id"] == eid and row["trigger_seq"] == 100 and row["last_seq"] == 100
    assert row["opened_at"] == 1000.0 and not row["reopened"]
    d.attach(eid, 101)
    d.attach(eid, 102, "note ignored")
    d.mark(eid, acted_at=1100.0, unstuck_at=1200.0, model_calls=1, cost_usd=0.5,
           evidence_json=json.dumps({"a": 1}))
    row = d.open_for("job1")
    assert row["last_seq"] == 102 and row["acted_at"] == 1100.0 and row["model_calls"] == 1
    d.close(eid, "resolved", "env", ["approve x", "steer nudge"], now=1500.0,
            rungs=[{"rung": "false_red"}], rule_proposal_id="T1", ask="q?")
    assert d.open_for("job1") is None
    r = d.episodes_since(0)[0]
    assert r["outcome"] == "resolved" and r["closed_at"] == 1500.0 and r["last_seq"] == 102
    assert r["rule_proposal_id"] == "T1" and r["ask"] == "q?" and r["unstuck_at"] == 1200.0
    assert json.loads(r["rungs_json"]) == [{"rung": "false_red"}]
    assert json.loads(r["actions_json"]) == ["approve x", "steer nudge"]
    assert d.episodes_since(2000.0) == []
    assert d.episodes_for_job("job1", 0) == 1 and d.episodes_for_job("job1", 1001.0) == 0
    assert d.episodes_for_job_today("job1", now=2000.0) == 1
    assert d.last_closed("job1")["episode_id"] == eid and d.last_closed("nope") is None
    assert d.last_closed_at("job1") == 1500.0
    e2 = d.open_episode("job1", "t", 5, now=2000.0, reopened=True)
    assert d.open_for("job1")["reopened"] == 1 and e2 != eid


def test_tries_window(tmp_path):
    d = db.CoordDB(tmp_path / "c.db")
    assert d.try_count("j", "false_red", now=10.0) == 0
    d.record_try("j", "false_red", now=10.0)
    d.record_try("j", "false_red", now=20.0)
    assert d.try_count("j", "false_red", now=30.0) == 2
    assert d.try_count("j", "false_red", now=30.0 + 86400) == 0
    assert d.try_count("j", "false_red", now=30.0, window_s=5) == 0
    assert d.try_count("j", "other", now=30.0) == 0


def test_budget_local_day(tmp_path):
    d = db.CoordDB(tmp_path / "c.db")
    now = time.time()
    assert d.budget_today(now) == {"usd": 0, "codex_calls": 0, "tg_sent": 0}
    d.spend(now, usd=1.5, codex_calls=1)
    d.spend(now, usd=0.5)
    d.note_tg(now)
    d.note_tg(now)
    assert d.budget_today(now) == {"usd": 2.0, "codex_calls": 1, "tg_sent": 2}
    assert d.budget_today(now + 3 * 86400)["usd"] == 0


def test_cursor(tmp_path):
    d = db.CoordDB(tmp_path / "c.db")
    assert d.cursor_get("events") == 0 and d.cursor_get("x", 7) == 7
    d.cursor_set("events", 99)
    d.cursor_set("events", 100)
    assert d.cursor_get("events") == 100
    assert db.CoordDB(tmp_path / "c.db").cursor_get("events") == 100


def test_overturn_and_actions(tmp_path):
    d = db.CoordDB(tmp_path / "c.db")
    e1 = d.open_episode("j", "t", 1, now=0.0)
    d.close(e1, "resolved", "d", ["steer"], now=1.0, rungs=[{"rung": "false_red"}])
    e2 = d.open_episode("k", "t", 2, now=0.0)
    d.close(e2, "resolved", "d", [], now=1.0, rungs=[{"rung": "other"}])
    assert d.actions_in(86400, now=3.0) == 1
    assert d.overturn(e1, "wrong", now=2.0) is True
    assert d.overturn(e1, "again", now=2.5) is False
    assert d.overturn("nope", "x", now=2.0) is False
    assert d.overturns_in(86400, now=3.0) == 1
    assert d.overturns_in(86400, now=3.0, rung="false_red") == 1
    assert d.overturns_in(86400, now=3.0, rung="other") == 0
    assert d.overturns_in(1, now=1000.0) == 0
    assert d.overturn(e2, "w", now=2.0)
    assert d.overturns_in(86400, now=3.0, rung="other") == 1


def test_rung_disable_enable(tmp_path):
    d = db.CoordDB(tmp_path / "c.db")
    assert not d.rung_disabled("false_red")
    d.disable_rung("false_red", "overturned twice", now=3.0)
    assert d.rung_disabled("false_red") and not d.rung_disabled("x")
    d.enable_rung("false_red")
    assert not d.rung_disabled("false_red")


def test_config_float_fields_not_truncated(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("poll_s = 0.5\ncooldown_s = 10.5\n[model]\ntimeout_s = 2.5\n")
    s = config.load(p)
    assert s.poll_s == 0.5 and s.cooldown_s == 10.5 and s.model_timeout_s == 2.5
    p.write_text("poll_s = 7\nrerun_max_ids = 9\n")
    s = config.load(p)
    assert s.poll_s == 7.0 and isinstance(s.poll_s, float) and s.rerun_max_ids == 9
