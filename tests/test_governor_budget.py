"""Budget advisory across accounts. Temp sqlite only."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from steward import governor
from steward.store import Store

NOW = datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)


def _db(tmp_path, rows):
    p = tmp_path / "usage.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL,"
              " account_label TEXT NOT NULL, cdp_port INTEGER NOT NULL, org_id TEXT NOT NULL, bucket TEXT NOT NULL,"
              " utilization REAL, resets_at TEXT, extra_usage_enabled INTEGER, raw_json TEXT NOT NULL, user_id TEXT)")
    for age_min, acct, bucket, pct in rows:
        ts = (NOW - timedelta(minutes=age_min)).isoformat()
        c.execute("INSERT INTO snapshots (captured_at, account_label, cdp_port, org_id, bucket, utilization, resets_at,"
                  " raw_json) VALUES (?,?,?,?,?,?,?,?)", (ts, acct, 1, "o", bucket, pct, "2026-10-07T07:00:00+00:00", "{}"))
    c.commit(); c.close()
    return p


def test_one_account_high_other_has_room_says_switch(tmp_path):
    p = _db(tmp_path, [(5, "primary", "seven_day", 9.0), (5, "secondary", "seven_day", 99.0),
                       (5, "secondary", "spend", None), (60, "secondary", "seven_day", 50.0)])
    b = governor.budget(db=p, now=NOW.timestamp())
    assert b.accounts["secondary"][0] == 99.0 and b.accounts["primary"][0] == 9.0
    assert b.state == "warning" and b.best == "primary" and b.advice == "switch to primary"
    assert "secondary 99%" in b.line and "primary 9%" in b.line


@pytest.mark.parametrize("pcts,state,advice", [
    ((10.0, 20.0), "ok", ""),
    ((80.0, 85.0), "warning", ""),
    ((92.0, 97.0), "high", "consider --kind codex"),
])
def test_states(tmp_path, pcts, state, advice):
    p = _db(tmp_path, [(5, "a", "seven_day", pcts[0]), (5, "b", "seven_day", pcts[1])])
    b = governor.budget(db=p, now=NOW.timestamp())
    assert (b.state, b.advice) == (state, advice)


def test_stale_and_missing_are_unknown(tmp_path):
    p = _db(tmp_path, [(7 * 60, "a", "seven_day", 50.0)])
    assert governor.budget(db=p, now=NOW.timestamp()).state == "unknown"
    assert governor.budget(db=tmp_path / "nope.db", now=NOW.timestamp()).state == "unknown"


def test_notice_once_per_week(tmp_path, steward_home):
    store = Store(steward_home)
    p = _db(tmp_path, [(5, "a", "seven_day", 95.0)])
    b = governor.budget(db=p, now=NOW.timestamp())
    sent = []
    assert governor.budget_notice(store, b, send=lambda t: sent.append(t) or True) is True
    assert governor.budget_notice(store, b, send=lambda t: sent.append(t) or True) is False
    assert len(sent) == 1 and "95%" in sent[0]


def test_launch_hint(tmp_path):
    p = _db(tmp_path, [(5, "a", "seven_day", 95.0)])
    b = governor.budget(db=p, now=NOW.timestamp())
    assert "codex" in governor.launch_hint(b, "claude")
    assert governor.launch_hint(b, "codex") is None


def test_health_budget_component_is_never_red(steward_home, tmp_path, monkeypatch):
    from steward import health

    store = Store(steward_home)
    p = _db(tmp_path, [(5, "a", "seven_day", 99.0)])
    monkeypatch.setattr(governor, "USAGE_DB", p)
    monkeypatch.setattr(governor.time, "time", lambda: NOW.timestamp())
    from steward.herdr_transport import HerdrUnavailable

    class DeadHerdr:  # only the budget component matters here
        def call(self, *a, **k): raise HerdrUnavailable("down")

    comps = {c.name: c for c in health.check_all(store, api=DeadHerdr(), ping=False, unit_active=lambda u: True,
                                                 pid_alive=lambda p: True, sleep=lambda s: None)}
    assert comps["budget"].ok is True and comps["budget"].detail.startswith("warning: ")
    state = {"components": {n: {"ok": c.ok, "detail": c.detail} for n, c in comps.items()}}
    assert any("a 99%" in line for line in health.render_header(state, color=False))
