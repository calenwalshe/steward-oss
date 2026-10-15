"""Final whole-branch review fix wave (Phase B): the findings' regression tests.

1 ask-only hold + one line (and the hold retry); 2 cooldown park -> ask-only; 3 nvm PATH in the wrapper and an
uncharged exit 127; 4 the model rung waits for an in-flight TL review; 5 no acting after the ask deadline or after the
Steward-side hold was delivered, and the rerun budget capped to the time left; 7 R11 for a model `runner-restart`;
8 the hold's delivery copied into the episode evidence (digest column).
Hermetic: the daemon fakes come from tests/test_coordinator_daemon.py; nothing real is started.
"""
import json
import re
import subprocess
import time
from pathlib import Path

import pytest

import test_coordinator_daemon as td
from steward import coordinator_link
from steward.coordinator import db, digest, model, triggers
from steward.coordinator.config import Settings
from steward.models import JobState, Judgment
from steward.store import Store

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path / "coord"))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("HERDR_WORKSPACE_ID", raising=False)


class FlakyHold(td.Fake):
    """`coordinator hold` fails the first `fail` times (rc 1, nothing recorded)."""

    def __init__(self, store, *, fail=2, **kw):
        super().__init__(store, **kw)
        self.fail = fail

    def _steward(self, args):
        if args[:2] == ["coordinator", "hold"] and self.fail > 0:
            self.fail -= 1
            return td.R(1, "", "control pane unreachable")
        return super()._steward(args)


def _three_full_episodes(cdb, job_id, now):
    for k in range(3):
        e = cdb.open_episode(job_id, "needs_human:recovery_budget_exhausted", 1, now=now - 20000 + k)
        cdb.close(e, "asked", "d", [], now=now - 19000 + k)


def _diag_park(store):
    j = td._running(store)
    td._to(store, j.job_id, JobState.NEEDS_HUMAN, "diagnosis says a human", "diagnosis_verdict_needs_human")
    return j


# ---------- 1: ask-only relays the hold, retries it, then exactly one line ----------

def test_ask_only_hold_failure_is_retried_then_one_line(steward_home, tmp_path, monkeypatch):
    sent = td._count_asks(monkeypatch)
    store = Store()
    j, _ = td._five_b(store)
    t = [time.time()]
    cdb = db.CoordDB(tmp_path / "c.db")
    _three_full_episodes(cdb, j.job_id, t[0])
    fake = FlakyHold(store, fail=2)                       # the opening relay and the same-tick retry both fail
    d, _ = td._daemon(steward_home, tmp_path, fake, lambda: t[0], cdb=cdb)
    td._cursor_before_park(cdb, store, j.job_id)
    out = d.tick()
    ep = td._ep(cdb)
    assert ep["closed_at"] is None and out["closed"] == [] and sent == []
    assert json.loads(ep["evidence_json"])["held"] is False
    t[0] += 15
    out = d.tick()
    ep = td._ep(cdb)
    assert out["closed"] == [{"eid": ep["episode_id"], "outcome": "asked"}]
    assert len(sent) == 1 and fake.verbs().count("coordinator episode") == 1
    assert "approve" not in fake.verbs() and "steer" not in fake.verbs()
    ev = json.loads(ep["evidence_json"])
    assert ev["held"] is True and ev["delivery"] == "delivered" and ev["how"] == "asked"


def test_ask_only_hold_never_recorded_closes_at_the_deadline(steward_home, tmp_path, monkeypatch):
    sent = td._count_asks(monkeypatch)
    store = Store()
    j, _ = td._five_b(store)
    t = [time.time()]
    cdb = db.CoordDB(tmp_path / "c.db")
    _three_full_episodes(cdb, j.job_id, t[0])
    fake = FlakyHold(store, fail=10 ** 6)
    d, _ = td._daemon(steward_home, tmp_path, fake, lambda: t[0], cdb=cdb)
    td._cursor_before_park(cdb, store, j.job_id)
    d.tick()
    t[0] += 500
    assert d.tick()["closed"] == []
    t[0] += 600                                           # past ask_by_s (1080)
    out = d.tick()
    ep = td._ep(cdb)
    assert out["closed"] == [{"eid": ep["episode_id"], "outcome": "asked"}]
    assert ep["outcome"] == "asked" and sent == []        # no hold -> the handler cannot send; logged, closed anyway


# ---------- 2: a park during the cooldown opens ask-only and sends one line ----------

def test_cooldown_park_opens_ask_only_and_sends_one_line(steward_home, tmp_path, monkeypatch):
    sent = td._count_asks(monkeypatch)
    store = Store()
    j, _ = td._five_b(store)
    t = [time.time()]
    cdb = db.CoordDB(tmp_path / "c.db")
    e0 = cdb.open_episode(j.job_id, "needs_human:recovery_budget_exhausted", 1, now=t[0] - 700)
    cdb.close(e0, "resolved", "d", ["nudge"], now=t[0] - 100)
    fake = td.Fake(store)
    d, _ = td._daemon(steward_home, tmp_path, fake, lambda: t[0], cdb=cdb)
    td._cursor_before_park(cdb, store, j.job_id)
    out = d.tick()
    ep = td._ep(cdb)
    assert ep["episode_id"] != e0 and out["closed"] == [{"eid": ep["episode_id"], "outcome": "asked"}]
    assert "cooldown" in ep["diagnosis"]
    assert fake.verbs() == ["coordinator hold", "coordinator episode"]
    assert len(sent) == 1
    assert store.kv_get(j.job_id, "coordinator_hold")["state"] == "delivered"


# ---------- 3: an uncharged exit 127 ----------

@pytest.mark.parametrize("lane", ["claude:sonnet", "codex:gpt-5.5"])
def test_exit_127_is_not_charged_or_counted(steward_home, tmp_path, lane):
    store = Store()
    j = _diag_park(store)
    cdb = db.CoordDB(tmp_path / "c.db")
    now = time.time()
    s = Settings()
    res = model.call_detail("p", lane=lane, cwd=tmp_path, settings=s, cdb=cdb, now=now,
                            run=lambda a, **k: td.R(127, "", "sh: 1: claude: not found"))
    assert res.plan is None and res.called is False and "127" in res.why
    b = cdb.budget_today(now)
    assert b["usd"] == 0 and b["codex_calls"] == 0
    # plan_episode: neither lane counts toward the episode's model calls
    eid = cdb.open_episode(j.job_id, "needs_human:x", 1, now=now)
    out = model.plan_episode(triggers.RoStore(steward_home / "steward.db", steward_home), cdb, cdb.open_for(j.job_id),
                             triggers.Trigger("needs_human:x", j.job_id, 1, {}), settings=s, workspace="w1",
                             repo=tmp_path, now=now, run=lambda a, **k: td.R(127, "", "not found"),
                             gather=lambda *a, **k: {})
    assert out == ([], None, [])
    ep = cdb.open_for(j.job_id)
    assert ep["episode_id"] == eid and int(ep["model_calls"] or 0) == 0 and float(ep["cost_usd"] or 0) == 0
    assert cdb.budget_today(now)["usd"] == 0 and cdb.budget_today(now)["codex_calls"] == 0


def test_a_failed_started_call_is_still_charged(tmp_path):
    cdb = db.CoordDB(tmp_path / "c.db")
    now = time.time()
    s = Settings()
    res = model.call_detail("p", lane="claude:sonnet", cwd=tmp_path, settings=s, cdb=cdb, now=now,
                            run=lambda a, **k: td.R(1, "", "boom"))
    assert res.called is True and cdb.budget_today(now)["usd"] == s.budget_call_usd


# ---------- 3: the wrapper finds the nvm node bin ----------

def _wrapper_path(tmp_path, env_extra, nvm_versions):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    for v in nvm_versions:
        (home / ".nvm" / "versions" / "node" / v / "bin").mkdir(parents=True)
    fake_py = tmp_path / "fakepy"
    fake_py.write_text('#!/bin/sh\nprintf "%s" "$PATH"\n')
    fake_py.chmod(0o755)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "STEWARD_PYTHON": str(fake_py), **env_extra}
    res = subprocess.run(["sh", str(REPO / "deploy" / "run-steward-coordinator.sh"), "run"], env=env,
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return home, res.stdout.split(":")


def test_wrapper_text_names_the_override_and_the_nvm_discovery():
    text = (REPO / "deploy" / "run-steward-coordinator.sh").read_text()
    assert "STEWARD_NODE_BIN" in text and ".nvm/versions/node/" in text
    assert not re.search(r"node/v\d", text)                 # no hard-coded node version
    assert subprocess.run(["sh", "-n", str(REPO / "deploy" / "run-steward-coordinator.sh")]).returncode == 0


def test_wrapper_prepends_the_newest_nvm_bin(tmp_path):
    home, path = _wrapper_path(tmp_path, {}, ["v9.11.2", "v24.16.0", "v22.3.1"])
    assert path[0] == str(home / ".nvm/versions/node/v24.16.0/bin")


def test_wrapper_override_and_no_nvm(tmp_path):
    other = tmp_path / "nodebin"
    other.mkdir()
    _, path = _wrapper_path(tmp_path, {"STEWARD_NODE_BIN": str(other)}, ["v24.16.0"])
    assert path[0] == str(other)
    _, path = _wrapper_path(tmp_path / "bare", {}, [])        # set -eu safe when the glob matches nothing
    assert not any(".nvm" in p for p in path)


# ---------- 4: the model rung waits while a TL review is in flight ----------

def test_model_rung_waits_for_an_in_flight_tl_review(steward_home, tmp_path, monkeypatch):
    sent = td._count_asks(monkeypatch)
    store = Store()
    j = _diag_park(store)
    store.append_event(j.job_id, what="tech_lead_review_started", evidence={}, rule="tech_lead_review",
                       judgment=Judgment.code)
    t = [time.time()]
    plan = {"diagnosis": "needs owner", "actions": [], "ask": "owner call", "rule": None}
    fake = td.Fake(store, claude_plan=plan)
    d, cdb = td._daemon(steward_home, tmp_path, fake, lambda: t[0])
    cdb.cursor_set("events", store.events_of(j.job_id, ("diagnosis says a human",))[-1].seq - 1)
    out = d.tick()
    ep = td._ep(cdb)
    assert out["opened"] and out["closed"] == [] and ep["closed_at"] is None
    assert not [c for c in fake.calls if "codex" in c or "claude" in c] and sent == []
    t[0] += 15
    assert d.tick()["closed"] == []                        # still waiting
    store.append_event(j.job_id, what="tech_lead_answered", evidence={}, rule="tech_lead_ordered_fallthrough",
                       judgment=Judgment.code)
    t[0] += 15
    out = d.tick()
    assert out["closed"] == [{"eid": ep["episode_id"], "outcome": "asked"}]
    assert [c for c in fake.calls if "codex" in c or "claude" in c] and len(sent) == 1


def test_tl_wait_still_closes_at_the_ask_deadline(steward_home, tmp_path, monkeypatch):
    sent = td._count_asks(monkeypatch)
    store = Store()
    j = _diag_park(store)
    store.append_event(j.job_id, what="tech_lead_review_started", evidence={}, rule="tech_lead_review",
                       judgment=Judgment.code)
    t = [time.time()]
    fake = td.Fake(store)
    d, cdb = td._daemon(steward_home, tmp_path, fake, lambda: t[0])
    cdb.cursor_set("events", store.events_of(j.job_id, ("diagnosis says a human",))[-1].seq - 1)
    d.tick()
    t[0] += 1100
    out = d.tick()
    ep = td._ep(cdb)
    assert out["closed"] == [{"eid": ep["episode_id"], "outcome": "asked"}] and len(sent) == 1
    assert not [c for c in fake.calls if "codex" in c or "claude" in c]


# ---------- 5: never act after the deadline or after the hold was delivered; capped rerun ----------

def test_deadline_passed_during_the_rerun_refuses_to_act(steward_home, tmp_path, monkeypatch):
    sent = td._count_asks(monkeypatch)
    store = Store()
    j, _ = td._five_b(store)
    t = [time.time()]
    seen = []
    green = td._rerun()

    def slow(**kw):
        seen.append(kw["settings"].rerun_total_s)
        t[0] += 1100                                     # the rerun outlived the ask deadline
        return green(**kw)
    fake = td.Fake(store)
    d, cdb = td._daemon(steward_home, tmp_path, fake, lambda: t[0], rerun_fn=slow)
    td._cursor_before_park(cdb, store, j.job_id)
    out = d.tick()
    ep = td._ep(cdb)
    assert "approve" not in fake.verbs() and "steer" not in fake.verbs()
    assert ep["outcome"] == "asked" and "deadline" in ep["diagnosis"] and ep["acted_at"] is None
    assert {"eid": ep["episode_id"], "outcome": "asked"} in out["closed"]
    assert len(sent) == 1 and store.get_job(j.job_id).state is JobState.NEEDS_HUMAN
    assert seen == [900.0]                               # min(rerun_total_s 900, 1080 s left)


def test_rerun_budget_is_capped_to_the_time_left(steward_home, tmp_path, monkeypatch):
    td._count_asks(monkeypatch)
    store = Store()
    j, _ = td._five_b(store)
    t = [time.time()]
    seen = []
    green = td._rerun()

    def rec(**kw):
        seen.append(kw["settings"].rerun_total_s)
        return green(**kw)
    s = Settings(steward_home=steward_home, steward_repo=tmp_path, ask_by_s=600)
    fake = td.Fake(store)
    d, cdb = td._daemon(steward_home, tmp_path, fake, lambda: t[0], rerun_fn=rec, settings=s)
    td._cursor_before_park(cdb, store, j.job_id)
    d.tick()
    assert seen == [600.0] and s.rerun_total_s == 900.0   # the daemon's own settings are untouched
    assert "approve" in fake.verbs()


def test_hold_delivered_by_the_sweep_refuses_to_act(steward_home, tmp_path, monkeypatch):
    sent = td._count_asks(monkeypatch)
    store = Store()
    j, _ = td._five_b(store)
    t = [time.time()]
    green = td._rerun()

    def sweep_meanwhile(**kw):
        coordinator_link.close_hold(store, j.job_id, "coordinator_down", now=t[0], delivery="delivered")
        return green(**kw)
    fake = td.Fake(store)
    d, cdb = td._daemon(steward_home, tmp_path, fake, lambda: t[0], rerun_fn=sweep_meanwhile)
    td._cursor_before_park(cdb, store, j.job_id)
    d.tick()
    ep = td._ep(cdb)
    assert "approve" not in fake.verbs() and "steer" not in fake.verbs()
    assert ep["outcome"] == "asked" and "hold is delivered" in ep["diagnosis"]
    assert sent == []                                    # the sweep already told the owner; no second line
    assert store.get_job(j.job_id).state is JobState.NEEDS_HUMAN


# ---------- 7: R11 for a model-proposed runner-restart ----------

def test_validate_runner_restart_needs_the_watchdog_known_inactive(steward_home):
    store = Store()
    j = _diag_park(store)
    ro = triggers.RoStore(steward_home / "steward.db", steward_home)
    plan = {"diagnosis": "d", "actions": [["runner-restart", j.job_id]], "ask": None, "rule": None}
    for val in (None, True, lambda: True, lambda: None):
        kept, refused = model.validate(plan, j.job_id, ro, workspace="w1", repo=None, watchdog_active=val)
        assert kept == [] and "R11" in refused[0]["why"]
    kept, _ = model.validate(plan, j.job_id, ro, workspace="w1", repo=None)          # default: unknown -> refuse
    assert kept == []
    for val in (False, lambda: False):
        kept, refused = model.validate(plan, j.job_id, ro, workspace="w1", repo=None, watchdog_active=val)
        assert [v.argv for v in kept] == [["runner-restart", j.job_id]] and refused == []


@pytest.mark.parametrize("active,ran", [(True, False), (False, True)])
def test_daemon_threads_watchdog_state_into_the_model_rung(steward_home, tmp_path, monkeypatch, active, ran):
    td._count_asks(monkeypatch)
    store = Store()
    j = _diag_park(store)
    t = [time.time()]
    plan = {"diagnosis": "the runner died", "actions": [["runner-restart", j.job_id]], "ask": "restart?",
            "rule": None}
    fake = td.Fake(store, claude_plan=plan)
    d, cdb = td._daemon(steward_home, tmp_path, fake, lambda: t[0], watchdog_active=lambda: active)
    cdb.cursor_set("events", store.events_of(j.job_id, ("diagnosis says a human",))[-1].seq - 1)
    d.tick()
    assert ("runner-restart" in fake.verbs()) is ran


# ---------- 8: the hold's delivery reaches the episode evidence and the digest ----------

def test_digest_ask_delivery_column_reads_the_merged_delivery(steward_home, tmp_path, monkeypatch):
    td._count_asks(monkeypatch)
    store = Store()
    j = _diag_park(store)
    t = [time.time()]
    fake = td.Fake(store, claude_plan={"diagnosis": "d", "actions": [], "ask": "pick one", "rule": None})
    d, cdb = td._daemon(steward_home, tmp_path, fake, lambda: t[0])
    cdb.cursor_set("events", store.events_of(j.job_id, ("diagnosis says a human",))[-1].seq - 1)
    d.tick()
    ep = td._ep(cdb)
    assert json.loads(ep["evidence_json"])["delivery"] == "delivered"
    html = digest.render(cdb, since=t[0] - 60, until=t[0] + 60, metrics=None)
    assert "<td>delivered</td>" in html
