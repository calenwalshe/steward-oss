"""v2.2 Phase 22 Task 1 (DL-09, DL-10): durable local check-ins — backfilled slots, one masked event + one local draft
per slot across racing connections, separate misses, catch-up after downtime. No notifier is ever invoked here."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from deadline_fixtures import Repo, commit_deadline, plan_json, set_state
from steward import deadline, deadline_outbox, deadline_plan, deadline_status, masking, notify, tabnames
from steward.models import JobState, Judgment
from steward.runner_exec import SubprocessRunner
from steward.store import Store

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
DUE = NOW + timedelta(hours=4)
RUN = SubprocessRunner()


class Boom:
    """A notifier that fails the test if anything calls it."""

    def send(self, event):
        raise AssertionError("a notifier was invoked while recording local check-ins")


@pytest.fixture(autouse=True)
def _no_send(monkeypatch):
    monkeypatch.setattr(notify, "notifier_from_env", lambda runner: Boom())
    monkeypatch.setattr(deadline_outbox, "notifier_for_channel", lambda channel, runner: Boom())
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")


@pytest.fixture
def store(steward_home):
    return Store(steward_home)


@pytest.fixture
def repo(tmp_path, store):
    r = Repo(tmp_path, store)
    commit_deadline(store, r.job_id, now=NOW)
    deadline.set_mode(store, r.job_id, "active", judgment=Judgment.human, origin="cli", request_id="mode")
    return r


def _plan(store, repo, m1=60, cand=200, when=2):
    repo.commit({deadline_plan.plan_path(repo.job_id): plan_json(
        repo.job_id, deadline.current(store, repo.job_id)["rev"], due=DUE, now=NOW,
        m1_target=NOW + timedelta(minutes=m1), cand_target=NOW + timedelta(minutes=cand))},
        f"plan m1={m1}", when=NOW + timedelta(minutes=when))


def _sweep(store, repo, minutes, **kw):
    return deadline_status.sweep(store, repo.job_id, now=NOW + timedelta(minutes=minutes), runner=RUN,
                                 worktree=repo.wt, force=True, **kw)


def _whats(store, jid, what):
    return [e for e in store.events(jid) if e.what == what]


# -- schema migration + backfill ------------------------------------------------------------------------------

def test_existing_database_gains_the_tables_without_losing_rows(steward_home):
    db = steward_home / "steward.db"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, goal TEXT NOT NULL, repo TEXT NOT NULL, state TEXT "
                       "NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
                       "INSERT INTO jobs VALUES ('old', 'g', '/r', 'RUNNING', 't', 't');")
    conn.commit()
    conn.close()
    store = Store(steward_home)
    assert store.get_job("old").state == JobState.RUNNING
    names = {r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"deadline_slots", "deadline_outbox", "deadline_attempts"} <= names


def test_phase21_overdue_and_superseded_snapshots_backfill_once(store, repo):
    _plan(store, repo, m1=60)
    deadline_plan.refresh(store, repo.job_id, worktree=repo.wt, runner=RUN, now=NOW + timedelta(minutes=3))
    _plan(store, repo, m1=90, when=10)
    deadline_plan.refresh(store, repo.job_id, worktree=repo.wt, runner=RUN, now=NOW + timedelta(minutes=11))
    before = len(store.events(repo.job_id))
    assert deadline_outbox.backfill(store, repo.job_id) == 4  # M1@60, M1@90, candidate, due
    assert deadline_outbox.backfill(store, repo.job_id) == 0
    assert len(store.events(repo.job_id)) == before
    keys = [deadline_outbox.slot_key(s) for s in deadline_outbox.slots(store, repo.job_id)]
    assert "1:M1:2026-10-02T21:00:00Z" in keys and "1:M1:2026-10-02T21:30:00Z" in keys
    assert "1:due:2026-10-03T00:00:00Z" in keys


# -- target-time records ----------------------------------------------------------------------------------------

def test_every_target_including_superseded_and_candidate_records_one_local_checkin(store, repo):
    _plan(store, repo, m1=60, cand=200)
    _sweep(store, repo, 3)
    _plan(store, repo, m1=90, cand=200, when=20)
    _sweep(store, repo, 21)
    repo.commit({"importer.py": "x\n"}, "importer", when=NOW + timedelta(minutes=50))
    for m in (60, 61, 90, 91, 200, 201, 240, 241):
        _sweep(store, repo, m)
    checkins = _whats(store, repo.job_id, "deadline_checkin")
    slots = [e.evidence["slot"] for e in checkins]
    assert slots == ["1:M1:2026-10-02T21:00:00Z", "1:M1:2026-10-02T21:30:00Z", "1:candidate:2026-10-02T23:20:00Z",
                     "1:due:2026-10-03T00:00:00Z"]
    old = checkins[0].evidence
    assert old["superseded"] and old["replacement_target"] == "2026-10-02T21:30:00Z" and old["met"]  # on track: still
    assert all(e.judgment == Judgment.code for e in checkins)
    drafts = deadline_outbox.drafts(store, repo.job_id)
    assert {d["draft_key"] for d in drafts} >= {f"checkin:{s}" for s in slots}
    assert all(d["state"] == "local" and d["version"] == 1 for d in drafts)
    assert all(len(d["title"]) <= 40 and len(d["body"]) <= 140 for d in drafts)


def test_grace_miss_is_separate_and_a_late_completion_never_erases_it(store, repo):
    _plan(store, repo, m1=60)
    _sweep(store, repo, 3)
    _sweep(store, repo, 61)  # target: check-in (unmet), not yet a miss
    assert len(_whats(store, repo.job_id, "deadline_checkin")) == 1
    assert _whats(store, repo.job_id, "deadline_milestone_missed") == []
    _sweep(store, repo, 67)  # past target + grace
    (miss,) = _whats(store, repo.job_id, "deadline_milestone_missed")
    assert miss.evidence["draft"] == "miss:1:M1:2026-10-02T21:00:00Z"
    repo.commit({"importer.py": "x\n"}, "late", when=NOW + timedelta(minutes=80))
    _sweep(store, repo, 81)
    assert len(_whats(store, repo.job_id, "deadline_milestone_missed")) == 1
    assert len(_whats(store, repo.job_id, "deadline_milestone_met")) == 1


def test_candidate_ready_early_keeps_its_target_slot_but_retires_the_due_slot(store, repo):
    _plan(store, repo, cand=200)
    _sweep(store, repo, 3)
    store.kv_set(repo.job_id, "deadline_candidate", {"sha": "abc", "ready": True, "items": [], "reasons": []})
    set_state(store, repo.job_id, JobState.NEEDS_HUMAN)
    _sweep(store, repo, 201)
    _sweep(store, repo, 241)
    slots = {e.evidence["slot"] for e in _whats(store, repo.job_id, "deadline_checkin")}
    assert "1:candidate:2026-10-02T23:20:00Z" in slots and not any(s.startswith("1:due") for s in slots)
    states = {deadline_outbox.slot_key(s): s["state"] for s in deadline_outbox.slots(store, repo.job_id)}
    assert states["1:due:2026-10-03T00:00:00Z"] == "retired"


def test_two_connections_racing_record_each_slot_once(store, repo, steward_home):
    _plan(store, repo, m1=60)
    _sweep(store, repo, 3)
    barrier = threading.Barrier(4)
    results = []

    def go():
        own = Store(steward_home)
        barrier.wait()
        results.append(deadline_outbox.record_due(own, repo.job_id, NOW + timedelta(minutes=61)))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(len(r) for r in results) == [0, 0, 0, 1]
    assert len(_whats(store, repo.job_id, "deadline_checkin")) == 1
    assert len(deadline_outbox.drafts(store, repo.job_id)) == 1


def test_downtime_records_each_overdue_slot_and_one_stable_catchup(store, repo):
    _plan(store, repo, m1=60, cand=200)
    _sweep(store, repo, 3)
    _sweep(store, repo, 250)  # Steward was down across M1, candidate and due
    keys = [e.evidence["slot"] for e in _whats(store, repo.job_id, "deadline_checkin")]
    assert len(keys) == 3
    (catch,) = [d for d in deadline_outbox.drafts(store, repo.job_id) if d["kind"] == "catchup"]
    assert sorted(catch["detail"]["slots"]) == sorted(keys) and catch["state"] == "local"
    restarted = Store(store.home)
    deadline_status.sweep(restarted, repo.job_id, now=NOW + timedelta(minutes=251), runner=RUN, worktree=repo.wt,
                          force=True)
    deadline_outbox.backfill(restarted, repo.job_id)
    assert len(_whats(store, repo.job_id, "deadline_checkin")) == 3
    assert len([d for d in deadline_outbox.drafts(store, repo.job_id) if d["kind"] == "catchup"]) == 1


def test_secret_is_masked_in_event_evidence_and_draft_payload(store, repo):
    sentinel = "sk-SENTINEL-778899"
    store.kv_set(repo.job_id, tabnames.NAME_KEY, f"job {sentinel}")
    masking.register([sentinel])
    _plan(store, repo, m1=60)
    _sweep(store, repo, 3)
    _sweep(store, repo, 61)
    raw_events = store._conn.execute("SELECT what || evidence FROM events WHERE job_id = ?", (repo.job_id,)).fetchall()
    raw_drafts = store._conn.execute("SELECT title || body || detail_json || recipient FROM deadline_outbox").fetchall()
    assert raw_drafts and all(sentinel not in r[0] for r in raw_events + raw_drafts)


def test_shadow_mode_records_no_checkins_and_does_not_burst_when_activated(store, repo):
    deadline.set_mode(store, repo.job_id, "shadow", judgment=Judgment.human, origin="cli", request_id="m2")
    _plan(store, repo, m1=60)
    _sweep(store, repo, 3)
    _sweep(store, repo, 61)
    assert _whats(store, repo.job_id, "deadline_checkin") == [] and deadline_outbox.drafts(store, repo.job_id) == []
    deadline.set_mode(store, repo.job_id, "active", judgment=Judgment.human, origin="cli", request_id="m3")
    _sweep(store, repo, 62)
    assert _whats(store, repo.job_id, "deadline_checkin") == []


def test_paused_job_slots_fire_from_the_control_pane_sweep(store, repo):
    _plan(store, repo, m1=60)
    _sweep(store, repo, 3)
    set_state(store, repo.job_id, JobState.PAUSED)
    assert deadline_status.sweep_all(Store(store.home), RUN, now=NOW + timedelta(minutes=61)) == 1
    assert len(_whats(store, repo.job_id, "deadline_checkin")) == 1


def test_store_append_event_with_outbox_is_one_unique_masked_transaction(store, repo):
    masking.register(["sk-OTHER-112233"])
    draft = {"key": "k1", "kind": "checkin", "created_at": "t", "recipient": "r", "channel": "telegram",
             "title": "x sk-OTHER-112233", "body": "b", "link": None, "detail": {"s": "sk-OTHER-112233"}}
    ev = store.append_event_with_outbox(repo.job_id, what="deadline_checkin", evidence={"v": "sk-OTHER-112233"},
                                        rule="r", judgment=Judgment.code, draft=draft)
    assert ev is not None and "sk-OTHER" not in str(ev.evidence)
    assert store.append_event_with_outbox(repo.job_id, what="deadline_checkin", evidence={}, rule="r",
                                          judgment=Judgment.code, draft=draft) is None
    (d,) = deadline_outbox.drafts(store, repo.job_id)
    assert d["event_seq"] == ev.seq and "sk-OTHER" not in d["title"] + str(d["detail"])


def test_terminal_job_only_catches_up_slots_that_came_due_while_it_was_live(store, repo):
    _plan(store, repo, m1=60, cand=200)
    _sweep(store, repo, 3)
    with store._conn:  # stopped at 20:30Z (before every target)
        store._conn.execute("UPDATE jobs SET state = 'STOPPED', updated_at = ? WHERE job_id = ?",
                            ("2026-10-02T20:30:00+00:00", repo.job_id))
    deadline_status.sweep_all(store, RUN, now=NOW + timedelta(minutes=300))
    assert _whats(store, repo.job_id, "deadline_checkin") == []
    assert _whats(store, repo.job_id, "deadline_passed") == []
