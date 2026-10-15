from steward import herdr_tabs
from steward.models import Judgment
from steward.runner_exec import FakeRunner
from steward.store import Store


def test_mark_accepted_is_noop_without_a_recorded_tab(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    runner = FakeRunner()

    herdr_tabs.mark_accepted(store, job.job_id, job.goal, runner)

    assert runner.calls == []


def test_mark_accepted_renames_the_recorded_tab_with_checkmark_and_short_name(steward_home):
    store = Store(steward_home)
    job = store.create_job('Build Steward Phase 18 "ETA" with a long tail of words', "/repo")
    store.kv_set(job.job_id, "herdr_tab", "w3T:t2")
    store._conn.execute("UPDATE jobs SET state='ACCEPTED' WHERE job_id=?", (job.job_id,))
    store._conn.commit()
    runner = FakeRunner()

    herdr_tabs.mark_accepted(store, job.job_id, job.goal, runner)

    assert [c["argv"] for c in runner.calls] == [["herdr", "tab", "rename", "w3T:t2", "✓ P18 ETA"]]
    assert store.kv_get(job.job_id, "herdr_tab_label") == "✓ P18 ETA"


def test_close_is_noop_without_a_recorded_tab(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    runner = FakeRunner()

    herdr_tabs.close(store, job.job_id, runner)

    assert runner.calls == []


def test_close_closes_the_recorded_tab(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    store.kv_set(job.job_id, "herdr_tab", "w3T:t2")
    runner = FakeRunner()

    herdr_tabs.close(store, job.job_id, runner)

    assert runner.calls == [
        {"argv": ["herdr", "tab", "close", "w3T:t2"], "cwd": str(store.home), "timeout": None, "env": None}
    ]


# ---- auto-close sweep (finished job -> tab closed after grace) ----
from datetime import datetime, timedelta, timezone

import pytest

from steward.models import JobState


def _job(store, state, tab="w3T:t2", incorporated=False):
    job = store.create_job("goal", "/repo")
    if tab:
        store.kv_set(job.job_id, "herdr_tab", tab)
    if incorporated:
        store.kv_set(job.job_id, "incorporated_commit", "abc123")
    store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, job.job_id))
    store._conn.commit()
    return job


def _later(s=61):
    return datetime.now(timezone.utc) + timedelta(seconds=s)


def _closes(runner):
    return [c["argv"] for c in runner.calls if c["argv"][:3] == ["herdr", "tab", "close"]]


@pytest.mark.parametrize("state,inc", [
    (JobState.RELEASED, False), (JobState.STOPPED, False), (JobState.ACCEPTED, True)])
def test_sweep_closes_finished_job_tab_after_grace_with_event(steward_home, state, inc):
    store = Store(steward_home)
    job = _job(store, state, incorporated=inc)
    runner = FakeRunner()

    assert herdr_tabs.sweep(store, runner, now=_later()) == [job.job_id]

    assert _closes(runner) == [["herdr", "tab", "close", "w3T:t2"]]
    ev = [e for e in store.events(job.job_id) if e.what == "job_tab_closed"]
    assert len(ev) == 1 and ev[0].evidence["tab_id"] == "w3T:t2"
    # once only
    assert herdr_tabs.sweep(store, runner, now=_later(120)) == []
    assert len(_closes(runner)) == 1


def test_sweep_waits_for_grace(steward_home):
    store = Store(steward_home)
    _job(store, JobState.RELEASED)
    runner = FakeRunner()
    assert herdr_tabs.sweep(store, runner, now=datetime.now(timezone.utc) + timedelta(seconds=30)) == []
    assert runner.calls == []
    assert herdr_tabs.sweep(store, runner, now=_later(), grace_s=10_000) == []


def test_sweep_grace_counts_from_incorporation(steward_home):
    store = Store(steward_home)
    job = _job(store, JobState.ACCEPTED, incorporated=True)
    store.append_event(job.job_id, what="incorporated", evidence={}, rule="r",
                       judgment=__import__("steward.models", fromlist=["Judgment"]).Judgment.human)
    runner = FakeRunner()
    assert herdr_tabs.sweep(store, runner, now=datetime.now(timezone.utc) + timedelta(seconds=30)) == []
    assert herdr_tabs.sweep(store, runner, now=_later()) == [job.job_id]


@pytest.mark.parametrize("state", [
    JobState.RUNNING, JobState.PAUSED, JobState.NEEDS_HUMAN, JobState.CREATED,
    JobState.STOPPING, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED])
def test_sweep_never_closes_live_job_tabs(steward_home, state):
    store = Store(steward_home)
    _job(store, state, incorporated=True)
    runner = FakeRunner()
    assert herdr_tabs.sweep(store, runner, now=_later(10_000)) == []
    assert runner.calls == []


def test_sweep_keeps_tab_of_accepted_but_not_incorporated_job(steward_home):
    store = Store(steward_home)
    _job(store, JobState.ACCEPTED)
    runner = FakeRunner()
    assert herdr_tabs.sweep(store, runner, now=_later(10_000)) == []
    assert runner.calls == []


def test_sweep_ignores_jobs_without_a_recorded_tab(steward_home):
    store = Store(steward_home)
    _job(store, JobState.RELEASED, tab=None)
    runner = FakeRunner()
    assert herdr_tabs.sweep(store, runner, now=_later()) == []
    assert runner.calls == []


def test_sweep_only_closes_the_recorded_tab_not_a_shared_one(steward_home):
    store = Store(steward_home)
    _job(store, JobState.RELEASED, tab="w1:shared")
    _job(store, JobState.RUNNING, tab="w1:shared")  # another live job records the same tab
    done = _job(store, JobState.STOPPED, tab="w1:own")
    runner = FakeRunner()
    assert herdr_tabs.sweep(store, runner, now=_later()) == [done.job_id]
    assert _closes(runner) == [["herdr", "tab", "close", "w1:own"]]


def test_keep_job_tabs_env_disables_sweep(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_KEEP_JOB_TABS", "1")
    store = Store(steward_home)
    _job(store, JobState.RELEASED)
    runner = FakeRunner()
    assert herdr_tabs.sweep(store, runner, now=_later(10_000)) == []
    assert runner.calls == []


def test_control_pane_tick_runs_the_sweep():
    import inspect
    from steward import cli
    assert "herdr_tabs.sweep" in inspect.getsource(cli._cmd_control_locked)


# ---- layout / refresh / tidy (design 2026-09-30) -------------------------------------------------------------------
import json

from herdr_fake import FakeHerdr, live_layout
from steward import tabnames


def _set(store, job, state=None, created=None):
    if state:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (state.value, job.job_id))
    if created:
        store._conn.execute("UPDATE jobs SET created_at=? WHERE job_id=?", (created, job.job_id))
    store._conn.commit()


def _live_jobs(store, herdr=None):
    """The two live jobs of design §0, recorded against the live-layout tabs."""
    a = store.create_job('Build Steward Phase 18 "ETA"', "/repo", job_id="e98c4fd8-0000-0000-0000-000000000000")
    b = store.create_job('Build Steward Phase 19 "Installable package"', "/repo", job_id="d3477a2a-0000-0000-0000-000000000000")
    store.kv_set(a.job_id, "herdr_tab", "w4Q:t1A")
    store.kv_set(b.job_id, "herdr_tab", "w4Q:t1H")
    _set(store, a, JobState.RUNNING, "2026-09-30T10:00:00+00:00")
    _set(store, b, JobState.RUNNING, "2026-09-30T11:00:00+00:00")
    for j in (a, b):  # a live job has recent activity; without any event liveness reads as quiet
        store.append_event(j.job_id, what="heartbeat", evidence={}, rule="r", judgment=Judgment.code)
    return a, b


def _bar_with_jobs(store, *jobs):
    """ops, steward, then one two-pane tab per job, then phone."""
    tabs = [{"tab_id": "t1", "label": "operator-setup"}, {"tab_id": "t2", "label": "health-advisor"}]
    panes = [{"pane_id": "p1", "tab_id": "t1", "label": "steward status"},
             {"pane_id": "p2", "tab_id": "t2", "label": "steward control", "tokens": {"steward_role": "control"}},
             {"pane_id": "p9", "tab_id": "t9", "label": "📱 phone"}]
    for i, job in enumerate(jobs):
        tab = f"tj{i}"
        tabs.append({"tab_id": tab, "label": "x"})
        panes.append({"pane_id": f"pj{i}", "tab_id": tab, "label": f"runner {job.job_id[:8]}"})
        store.kv_set(job.job_id, "herdr_tab", tab)
    tabs.append({"tab_id": "t9", "label": "phone-jobs-list"})
    return FakeHerdr(tabs, panes)


def test_pinned_tabs_are_found_by_their_panes_not_tab_labels():
    h = live_layout()
    assert herdr_tabs.pinned_tabs(h.tabs, h.panes) == {"ops": "w4Q:t1", "steward": "w4Q:t2", "phone": "w4Q:tD"}
    h.tabs[2]["label"] = "anything at all"
    assert herdr_tabs.pinned_tabs(h.tabs, h.panes)["phone"] == "w4Q:tD"


def test_layout_orders_pinned_jobs_then_phone_with_sendback_reslotted(steward_home):
    store = Store(steward_home)
    j1 = store.create_job("one", "/r")
    j2 = store.create_job("two", "/r")
    j3 = store.create_job("three", "/r")
    for j, created in ((j1, "2026-01-01T00:00:00+00:00"), (j2, "2026-01-02T00:00:00+00:00"), (j3, "2026-01-03T00:00:00+00:00")):
        _set(store, j, JobState.RUNNING, created)
    h = _bar_with_jobs(store, j3, j1, j2)  # bar order is scrambled: a send-back tab opened at the end is j2
    kinds = [(s.position, s.kind, s.job_id) for s in herdr_tabs.layout(store, h.tabs, h.panes)]
    assert kinds == [(1, "ops", None), (2, "steward", None), (3, "job", j1.job_id), (4, "job", j2.job_id),
                     (5, "job", j3.job_id), (6, "phone", None)]


def test_order_is_active_then_accepted_unmerged_then_done():
    rows = [("r", "RELEASED", "1", False), ("a2", "RUNNING", "3", False), ("acc", "ACCEPTED", "0", False),
            ("a1", "NEEDS_HUMAN", "2", False), ("merged", "ACCEPTED", "0", True)]
    got = herdr_tabs.order_jobs(rows, lambda r: r[1:])
    assert [r[0] for r in got][:3] == ["a1", "a2", "acc"] and {r[0] for r in got[3:]} == {"r", "merged"}
    assert herdr_tabs.job_position(0) == 3


def test_refresh_moves_phone_last_renames_jobs_into_grammar(steward_home):
    store = Store(steward_home)
    h = live_layout()
    a, b = _live_jobs(store)

    res = herdr_tabs.refresh(store, h, h)

    assert h.labels()[2:4] == ["● P18 ETA", "● P19 Installable"]
    assert h.order()[:2] == ["w4Q:t1", "w4Q:t2"] and h.order()[2:4] == ["w4Q:t1A", "w4Q:t1H"]
    assert h.order()[-1] == "w4Q:tD"
    assert store.kv_get(a.job_id, "herdr_tab_label") == "● P18 ETA"
    assert len(res["renamed"]) == 2


def test_refresh_makes_the_fewest_moves(steward_home):
    store = Store(steward_home)
    h = live_layout()
    h.tabs = [t for t in h.tabs if t["tab_id"] != "w4Q:t1D"]
    _live_jobs(store)
    herdr_tabs.refresh(store, h, h)
    assert [m for m in h.api_calls if m[0] == "tab.move"] == [("tab.move", {"tab_id": "w4Q:tD", "insert_index": 4})]


def test_a_quiet_tick_makes_no_rename_or_move_calls(steward_home):
    store = Store(steward_home)
    h = live_layout()
    _live_jobs(store)
    herdr_tabs.refresh(store, h, h)
    calls_before, api_before = len(h.calls), len([c for c in h.api_calls if c[0] == "tab.move"])

    res = herdr_tabs.refresh(store, h, h)

    assert res["renamed"] == [] and res["moved"] == []
    assert len(h.calls) == calls_before
    assert len([c for c in h.api_calls if c[0] == "tab.move"]) == api_before


def test_refresh_renames_when_the_job_state_changes(steward_home):
    store = Store(steward_home)
    h = live_layout()
    a, _ = _live_jobs(store)
    herdr_tabs.refresh(store, h, h)
    _set(store, a, JobState.PAUSED)
    herdr_tabs.refresh(store, h, h)
    assert next(t for t in h.tabs if t["tab_id"] == "w4Q:t1A")["label"] == "⏸ P18 ETA"


def test_refresh_shows_waiting_questions_and_needs_you(steward_home):
    from questions_helpers import make_record, seed

    store = Store(steward_home)
    h = live_layout()
    a, _ = _live_jobs(store)
    seed(store, a.job_id, [make_record(f"gsd_context:1-CONTEXT.md:G{i}", line=i) for i in range(1, 4)])
    herdr_tabs.refresh(store, h, h)
    assert next(t for t in h.tabs if t["tab_id"] == "w4Q:t1A")["label"] == "◆ P18 ETA ?3"


def test_refresh_shows_a_phase18_eta_when_running(steward_home):
    store = Store(steward_home)
    h = live_layout()
    a, _ = _live_jobs(store)
    store.kv_set(a.job_id, "efficiency_state", {"eta": {"p50_min": 100}})
    herdr_tabs.refresh(store, h, h)
    assert next(t for t in h.tabs if t["tab_id"] == "w4Q:t1A")["label"] == "● P18 ETA ~1h40"


def test_refresh_adopts_a_manual_herdr_rename(steward_home):
    store = Store(steward_home)
    h = live_layout()
    a, _ = _live_jobs(store)
    herdr_tabs.refresh(store, h, h)
    next(t for t in h.tabs if t["tab_id"] == "w4Q:t1A")["label"] = "Android fix"   # Sam renames the tab

    herdr_tabs.refresh(store, h, h)

    assert store.kv_get(a.job_id, "short_name") == "Android fix"
    assert next(t for t in h.tabs if t["tab_id"] == "w4Q:t1A")["label"] == "● Android fix"
    assert [e for e in store.events(a.job_id) if e.what == "job_renamed"]


def test_refresh_survives_a_dead_herdr(steward_home):
    from steward.herdr_transport import HerdrUnavailable

    class Dead:
        def call(self, *a):
            raise HerdrUnavailable("no socket")

    store = Store(steward_home)
    _live_jobs(store)
    assert "error" in herdr_tabs.refresh(store, FakeRunner(), Dead())


def test_refresh_never_renames_the_pinned_tabs(steward_home):
    store = Store(steward_home)
    h = live_layout()
    _live_jobs(store)
    herdr_tabs.refresh(store, h, h)
    assert [c for c in h.cli("herdr", "tab", "rename") if c[3] in ("w4Q:t1", "w4Q:t2", "w4Q:tD")] == []


def test_more_than_seven_jobs_only_the_first_seven_get_a_key(steward_home):
    store = Store(steward_home)
    jobs = []
    for i in range(9):
        j = store.create_job(f"job number {i}", "/r")
        _set(store, j, JobState.RUNNING, f"2026-01-0{i + 1}T00:00:00+00:00")
        jobs.append(j)
    h = _bar_with_jobs(store, *jobs)
    slots = [s for s in herdr_tabs.layout(store, h.tabs, h.panes) if s.kind == "job"]
    keyed = [s for s in slots if s.position <= herdr_tabs.MAX_KEYED_POSITION]
    assert [s.position for s in slots] == list(range(3, 12)) and len(keyed) == 7


def test_sweep_drains_a_legacy_questions_tab_once(steward_home):
    store = Store(steward_home)
    h = live_layout()
    job = store.create_job("goal", "/repo", job_id="e98c4fd8-0000-0000-0000-000000000000")
    store.kv_set(job.job_id, "herdr_tab", "w4Q:t1A")
    _set(store, job, JobState.RELEASED)

    assert herdr_tabs.sweep(store, h, now=_later()) == [job.job_id]

    assert sorted(c[3] for c in h.cli("herdr", "tab", "close")) == ["w4Q:t1A", "w4Q:t1D"]
    assert "w4Q:t1D" not in h.order() and "w4Q:t1A" not in h.order()
    n = len(h.calls)
    herdr_tabs.sweep(store, h, now=_later(300))
    assert len(h.calls) == n  # drained once, closed once


def test_keep_env_still_disables_closing_including_legacy_questions(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_KEEP_JOB_TABS", "1")
    store = Store(steward_home)
    h = live_layout()
    job = store.create_job("goal", "/repo", job_id="e98c4fd8-0000-0000-0000-000000000000")
    store.kv_set(job.job_id, "herdr_tab", "w4Q:t1A")
    _set(store, job, JobState.RELEASED)
    assert herdr_tabs.sweep(store, h, now=_later()) == []
    assert h.calls == []


def test_a_questions_pane_inside_the_job_tab_closes_with_the_job(steward_home):
    store = Store(steward_home)
    h = live_layout()
    h.panes.append({"pane_id": "w4Q:p70", "tab_id": "w4Q:t1A", "label": "questions e98c4fd8"})
    job = store.create_job("goal", "/repo", job_id="e98c4fd8-0000-0000-0000-000000000000")
    store.kv_set(job.job_id, "herdr_tab", "w4Q:t1A")
    _set(store, job, JobState.RELEASED)
    herdr_tabs.sweep(store, h, now=_later())
    assert not [p for p in h.panes if p["tab_id"] == "w4Q:t1A"]
    assert [c[3] for c in h.cli("herdr", "tab", "close") if c[3] == "w4Q:t1A"] == ["w4Q:t1A"]


# ---- steward tabs --tidy ---------------------------------------------------------------------------------------------


def test_tidy_dry_run_prints_every_step_and_changes_nothing(steward_home):
    store = Store(steward_home)
    h = live_layout()
    a, b = _live_jobs(store)
    before = (json.dumps(h.tabs), json.dumps(h.panes))
    lines = []

    steps = herdr_tabs.tidy(store, h, h, dry_run=True, say=lines.append)

    assert (json.dumps(h.tabs), json.dumps(h.panes)) == before
    assert h.calls == [] and not [c for c in h.api_calls if c[0] == "tab.move"]
    assert store.kv_get(a.job_id, "short_name") is None        # dry-run writes nothing either
    assert lines and all(l.startswith(("would:", "skip:")) for l in lines)
    text = "\n".join(lines)
    for needle in ("'⌂ ops'", "'⚕ steward'", "'📱 phone'", "move questions pane w4Q:p4K into job tab w4Q:t1A",
                   "split down off w4Q:p51", "close tab w4Q:t1D", "● P18 ETA", "● P19 Installable", "tab.move"):
        assert needle in text, needle
    assert steps


def test_tidy_migrates_the_live_layout(steward_home):
    store = Store(steward_home)
    h = live_layout()
    a, b = _live_jobs(store)
    lines = []

    herdr_tabs.tidy(store, h, h, say=lines.append)

    assert h.labels() == ["⌂ ops", "⚕ steward", "● P18 ETA", "● P19 Installable", "✓ done", "📱 phone"]
    q = next(p for p in h.panes if p["pane_id"] == "w4Q:p4K")
    assert q["tab_id"] == "w4Q:t1A" and q["label"] == "questions e98c4fd8"
    assert "w4Q:t1D" not in h.order()
    assert all(l.startswith("do:") or l.startswith("skip:") for l in lines)
    assert store.kv_get(a.job_id, "short_name") == "P18 ETA"


def test_tidy_never_moves_a_running_jobs_runner_or_worker_panes(steward_home):
    store = Store(steward_home)
    h = live_layout()
    _live_jobs(store)
    before = {p["pane_id"]: p["tab_id"] for p in h.panes if p["label"].split()[0] in ("runner", "worker", "watchers")}
    herdr_tabs.tidy(store, h, h)
    after = {p["pane_id"]: p["tab_id"] for p in h.panes if p["pane_id"] in before}
    assert after == before
    assert [c[3] for c in h.cli("herdr", "pane", "move")] == ["w4Q:p4K"]


def test_tidy_twice_is_a_noop(steward_home):
    store = Store(steward_home)
    h = live_layout()
    _live_jobs(store)
    herdr_tabs.tidy(store, h, h)
    n = len(h.calls)
    lines = []
    assert herdr_tabs.tidy(store, h, h, say=lines.append) == []
    assert len(h.calls) == n
