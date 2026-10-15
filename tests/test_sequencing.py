"""Job sequencing: queues (`after`/`queue`), resource locks, auto-launch sweep, the grouped status pane. Fakes only."""

from __future__ import annotations

import re

import pytest

from steward import cli, dashact, dashrender, sequencing, tabnames
from steward.control_pane import ControlPane
from steward.models import JobState
from steward.store import Store


def _state(store, job, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))


def _job(store, name, state=JobState.CONTRACTED):
    job = store.create_job(f"{name} goal", "/tmp/repo")
    store.kv_set(job.job_id, tabnames.NAME_KEY, name)
    store.kv_set(job.job_id, "herdr_workspace", "w1")
    store.kv_set(job.job_id, "herdr_tab", f"w1:{name.replace(' ', '')}")
    if state != JobState.CREATED:
        _state(store, job, state)
    return job


class FakeLauncher:
    def __init__(self, store, fail=False):
        self.store, self.calls, self.fail = store, [], fail

    def __call__(self, job_id, args):
        self.calls.append((job_id, args))
        if self.fail:
            return {"accepted": False, "detail": "no herdr"}
        _state(self.store, self.store.get_job(job_id), JobState.RUNNING)  # a launched job's runner starts it
        return {"accepted": True, "tab_id": "t1"}


@pytest.fixture
def store(steward_home):
    return Store(steward_home)


def _sweep(store, launcher):
    return sequencing.sweep(store, launcher, workspace="w1")


def test_chain_auto_launch_in_order(store):
    a, b, c = (_job(store, n) for n in ("Alpha", "Beta", "Gamma"))
    assert sequencing.queue(store, [a.job_id, b.job_id, c.job_id])[0]
    _state(store, a, JobState.RUNNING)
    fl = FakeLauncher(store)
    assert _sweep(store, fl) == []  # a still running
    assert sequencing.status(store, b.job_id)["text"] == "queued · after Alpha (running)"
    assert sequencing.status(store, c.job_id)["text"] == "queued · after Beta (waiting)"
    _state(store, a, JobState.ACCEPTED)
    assert _sweep(store, fl) == [b.job_id]
    assert _sweep(store, fl) == []  # c waits for b
    _state(store, b, JobState.ACCEPTED)
    assert _sweep(store, fl) == [c.job_id]
    assert [i for i, _ in fl.calls] == [b.job_id, c.job_id]
    ev = [e for e in store.events(b.job_id) if e.what == "queue: auto-launched"]
    assert len(ev) == 1 and ev[0].evidence["after"] == a.job_id


def test_queue_successors_inherit_first_jobs_recorded_branch(store):
    from steward.chain_branch import KEY, target

    a, b, c = (_job(store, n) for n in ("A", "B", "C"))
    store.kv_set(a.job_id, KEY, "release")
    store.kv_set(b.job_id, KEY, "drifted-when-created")
    assert sequencing.queue(store, [a.job_id, b.job_id, c.job_id])[0]
    assert target(store, b.job_id) == "release"
    assert target(store, c.job_id) == "release"


def test_launch_uses_recorded_launch_args(store):
    a, b = _job(store, "Alpha"), _job(store, "Beta")
    store.kv_set(a.job_id, "launch_args", {"workspace": "wX", "worker": "fabric", "agent_args": ["--model", "opus"]})
    sequencing.set_after(store, b.job_id, a.job_id)
    store.kv_set(b.job_id, "launch_args", {"workspace": "wB", "worker": "fabric", "agent_args": ["--model", "sonnet"]})
    _state(store, a, JobState.ACCEPTED)
    fl = FakeLauncher(store)
    _sweep(store, fl)
    assert fl.calls[0][1]["agent_args"] == ["--model", "sonnet"]  # its own recorded args win


@pytest.mark.parametrize("pred_state", [JobState.STOPPED, JobState.NEEDS_HUMAN, JobState.FAILED])
def test_stop_or_escalation_pauses_queue_and_says_why(store, pred_state):
    a, b = _job(store, "Alpha"), _job(store, "Beta")
    sequencing.set_after(store, b.job_id, a.job_id)
    _state(store, a, pred_state)
    fl = FakeLauncher(store)
    assert _sweep(store, fl) == [] and _sweep(store, fl) == [] and not fl.calls
    st = sequencing.status(store, b.job_id)
    assert st["state"] == "paused" and "Alpha" in st["text"]
    paused = [e for e in store.events(b.job_id) if e.what.startswith("queue paused")]
    assert len(paused) == 1  # one event, not one per sweep
    _state(store, a, JobState.ACCEPTED)  # a NEEDS_HUMAN predecessor later accepted resumes the queue
    if pred_state == JobState.NEEDS_HUMAN:
        assert _sweep(store, fl) == [b.job_id]


def test_after_merge_waits_for_incorporation(store):
    a, b = _job(store, "Alpha"), _job(store, "Beta")
    sequencing.set_after(store, b.job_id, a.job_id, after_merge=True)
    _state(store, a, JobState.ACCEPTED)
    fl = FakeLauncher(store)
    assert _sweep(store, fl) == []
    assert "waiting for merge" in sequencing.status(store, b.job_id)["text"]
    store.kv_set(a.job_id, "incorporated_commit", "abc1234")
    assert _sweep(store, fl) == [b.job_id]


def test_exactly_once_even_if_swept_twice_or_state_lags(store):
    a, b = _job(store, "Alpha"), _job(store, "Beta")
    sequencing.set_after(store, b.job_id, a.job_id)
    _state(store, a, JobState.ACCEPTED)

    class Lagging(FakeLauncher):  # the runner has not moved the job out of CONTRACTED yet
        def __call__(self, job_id, args):
            self.calls.append((job_id, args))
            return {"accepted": True}

    fl = Lagging(store)
    for _ in range(3):
        _sweep(store, fl)
    assert len(fl.calls) == 1
    assert len([e for e in store.events(b.job_id) if e.what == "queue: auto-launched"]) == 1


def test_failed_launch_is_retried_and_recorded_once(store):
    a, b = _job(store, "Alpha"), _job(store, "Beta")
    sequencing.set_after(store, b.job_id, a.job_id)
    _state(store, a, JobState.ACCEPTED)
    bad = FakeLauncher(store, fail=True)
    _sweep(store, bad)
    _sweep(store, bad)
    assert len([e for e in store.events(b.job_id) if e.what.startswith("queue launch failed")]) == 1
    assert _sweep(store, FakeLauncher(store)) == [b.job_id]  # claim was released


def test_lock_exclusivity(store):
    a, b, c = (_job(store, n) for n in ("Alpha", "Beta", "Gamma"))
    for j in (a, b, c):
        sequencing.set_lock(store, j.job_id, "hardware")
    sequencing.set_after(store, b.job_id, a.job_id)  # b after a AND lock
    other = _job(store, "Delta")
    sequencing.set_lock(store, other.job_id, "hardware")
    _state(store, other, JobState.RUNNING)  # someone holds the lock
    _state(store, a, JobState.ACCEPTED)
    store.kv_set(c.job_id, sequencing.K_WAIT, True)
    fl = FakeLauncher(store)
    assert _sweep(store, fl) == []
    assert sequencing.status(store, b.job_id)["text"] == "queued · waiting for lock hardware"
    _state(store, other, JobState.ACCEPTED)  # lock frees: exactly one of b/c launches, the other keeps waiting
    first = _sweep(store, fl)
    assert len(first) == 1
    assert _sweep(store, fl) == []
    running = [j for j in (b, c) if store.get_job(j.job_id).state == JobState.RUNNING]
    assert len(running) == 1
    _state(store, running[0], JobState.ACCEPTED)
    assert len(_sweep(store, fl)) == 1


def test_play_on_held_lock_parks_instead_of_launching(store):
    holder, j = _job(store, "Holder"), _job(store, "Beta")
    for x in (holder, j):
        sequencing.set_lock(store, x.job_id, "hardware")
    _state(store, holder, JobState.RUNNING)
    assert sequencing.lock_gate(store, j.job_id) == holder.job_id
    assert "waiting for lock" in sequencing.status(store, j.job_id)["text"]
    _state(store, holder, JobState.ACCEPTED)
    assert _sweep(store, FakeLauncher(store)) == [j.job_id]


def test_reorder_up_down(store):
    a, b, c = (_job(store, n) for n in ("Alpha", "Beta", "Gamma"))
    sequencing.queue(store, [a.job_id, b.job_id, c.job_id])
    assert sequencing.chain_of(store, a.job_id) == [a.job_id, b.job_id, c.job_id]
    assert sequencing.move(store, c.job_id, "up")[0]
    assert sequencing.chain_of(store, a.job_id) == [a.job_id, c.job_id, b.job_id]
    assert sequencing.move(store, a.job_id, "down")[0]
    assert sequencing.chain_of(store, a.job_id)[0] == c.job_id
    assert not sequencing.move(store, c.job_id, "up")[0]  # already first
    # the new head launches first
    _state(store, c, JobState.ACCEPTED)
    fl = FakeLauncher(store)
    assert _sweep(store, fl) == [a.job_id]


def test_play_jumps_queue_and_is_not_launched_twice(store):
    a, b, c = (_job(store, n) for n in ("Alpha", "Beta", "Gamma"))
    sequencing.queue(store, [a.job_id, b.job_id, c.job_id])
    sequencing.on_manual_launch(store, b.job_id)  # what _h_launch does after a successful play
    _state(store, b, JobState.RUNNING)
    fl = FakeLauncher(store)
    assert _sweep(store, fl) == []
    assert sequencing.status(store, b.job_id) is None
    assert sequencing.status(store, c.job_id)["text"] == "queued · after Beta (running)"
    _state(store, a, JobState.ACCEPTED)
    assert _sweep(store, fl) == []  # a finishing does not launch b a second time
    assert not fl.calls


def test_cycle_and_unstaged_rejected(store):
    a, b = _job(store, "Alpha"), _job(store, "Beta")
    assert sequencing.set_after(store, b.job_id, a.job_id)[0]
    assert not sequencing.set_after(store, a.job_id, b.job_id)[0]
    assert not sequencing.set_after(store, a.job_id, a.job_id)[0]
    r = _job(store, "Run", JobState.RUNNING)
    assert not sequencing.set_after(store, r.job_id, a.job_id)[0]


def test_control_pane_event_driven_sweep_and_tick(store):
    a, b = _job(store, "Alpha"), _job(store, "Beta")
    sequencing.set_after(store, b.job_id, a.job_id)
    fl = FakeLauncher(store)
    sweeps = []

    def on_change():
        sweeps.append(sequencing.sweep(store, fl, workspace="w1"))

    pane = ControlPane(store, {"accept": lambda cmd: (_state(store, a, JobState.ACCEPTED), {"accepted": True})[1]},
                       print_fn=lambda _l: None, on_change=on_change)
    pane.execute({"request_id": "r1", "op": "accept", "args": {}, "origin": "cli"})
    pane.execute({"request_id": "r1", "op": "accept", "args": {}, "origin": "cli"})  # duplicate: no second launch
    assert [i for i, _ in fl.calls] == [b.job_id]


def test_cli_verbs_through_control_handlers(store, capsys):
    a, b, c = (_job(store, n) for n in ("Alpha", "Beta", "Gamma"))
    assert cli.main(["queue", a.job_id[:8], b.job_id[:8], c.job_id[:8], "--name", "Trio"]) in (0, 2)
    assert cli.main(["queue", a.job_id, b.job_id, c.job_id]) == 0
    assert cli.main(["queue", "--name", "Trio", a.job_id]) == 0
    capsys.readouterr()
    assert cli.main(["queue", "--show"]) == 0
    out = capsys.readouterr().out
    assert "Trio · 3 jobs · 0/3 done" in out and "queued · after Alpha" in out
    assert cli.main(["queue", "--up", c.job_id]) == 0
    assert sequencing.chain_of(store, a.job_id) == [a.job_id, c.job_id, b.job_id]
    assert cli.main(["after", c.job_id, "--clear"]) == 0
    assert cli.main(["after", b.job_id, c.job_id, "--lock", "hardware"]) == 0
    assert store.kv_get(b.job_id, sequencing.K_LOCK) == "hardware"
    assert cli.main(["queue", a.job_id]) == 2  # needs two jobs


def test_group_by_prefix_orders_naturally(store, capsys):
    jobs = {n: _job(store, f"Recipe book Job {n}") for n in (10, 2, 1)}
    _job(store, "Unrelated thing")
    assert cli.main(["queue", "--group-by-prefix", "Recipe book"]) == 0
    chain = sequencing.chain_of(store, jobs[1].job_id)
    assert chain == [jobs[1].job_id, jobs[2].job_id, jobs[10].job_id]
    assert sequencing.group_name(store, chain) == "Recipe book"


def test_default_group_name_common_prefix(store):
    a, b = _job(store, "Recipe book Job 1"), _job(store, "Recipe book Job 2")
    sequencing.queue(store, [a.job_id, b.job_id])
    assert sequencing.group_name(store, [a.job_id, b.job_id]) == "Recipe book"
    x, y = _job(store, "Alpha"), _job(store, "Zulu")
    sequencing.queue(store, [x.job_id, y.job_id])
    assert sequencing.group_name(store, [x.job_id, y.job_id]) == "Alpha"


def test_new_with_after_and_lock(store, steward_home):
    a = _job(store, "Alpha")
    receipt = cli._build_control_handlers(store)["new"](
        {"args": {"goal": "Beta goal", "repo": "/tmp", "no_baseline": True, "after": a.job_id, "lock": "hardware"}})
    assert receipt["accepted"] and receipt["queued"]
    jid = receipt["job_id"]
    assert store.kv_get(jid, sequencing.K_AFTER) == a.job_id and store.kv_get(jid, sequencing.K_LOCK) == "hardware"


def test_new_after_persists_args_for_sweep(store, steward_home):
    a = _job(store, "Alpha")
    ns = cli._parser().parse_args(["new", "Beta goal", "--repo", "/tmp", "--no-baseline", "--after", a.job_id,
                                   "--agent-arg=--model", "--agent-arg=M", "--worker", "fabric", "--kind", "claude",
                                   "--workspace", "w2"])
    op, args = cli._op_and_args_for(ns)
    receipt = cli._build_control_handlers(store)[op]({"args": args})
    jid = receipt["job_id"]
    _state(store, a, JobState.ACCEPTED)
    fl = FakeLauncher(store)
    assert _sweep(store, fl) == [jid]
    assert fl.calls[0][1]["agent_args"] == ["--model", "M"]
    assert fl.calls[0][1]["workspace"] == "w2"


def test_after_persists_args_for_sweep(store):
    a, b = _job(store, "Alpha"), _job(store, "Beta")
    ns = cli._parser().parse_args(["after", b.job_id, a.job_id, "--agent-arg=--model", "--agent-arg=M",
                                   "--worker", "fabric", "--kind", "claude", "--workspace", "w2"])
    op, args = cli._op_and_args_for(ns)
    receipt = cli._build_control_handlers(store)[op]({"args": args})
    assert receipt["accepted"]
    _state(store, a, JobState.ACCEPTED)
    fl = FakeLauncher(store)
    assert _sweep(store, fl) == [b.job_id]
    assert fl.calls[0][1]["agent_args"] == ["--model", "M"]
    assert fl.calls[0][1]["workspace"] == "w2"


def test_queue_persists_args_for_sweep_and_shows_model(store):
    a, b = _job(store, "Alpha"), _job(store, "Beta")
    ns = cli._parser().parse_args(["queue", a.job_id, b.job_id, "--agent-arg=--model", "--agent-arg=M",
                                   "--worker", "fabric", "--kind", "claude", "--workspace", "w2"])
    op, args = cli._op_and_args_for(ns)
    receipt = cli._build_control_handlers(store)[op]({"args": args})
    assert receipt["accepted"]
    assert "[model M]" in sequencing.render_show(store)
    _state(store, a, JobState.ACCEPTED)
    fl = FakeLauncher(store)
    assert _sweep(store, fl) == [b.job_id]
    assert fl.calls[0][1]["agent_args"] == ["--model", "M"]
    assert fl.calls[0][1]["workspace"] == "w2"


def test_status_shows_queued_model(store, capsys):
    a, b = _job(store, "Alpha"), _job(store, "Beta")
    sequencing.set_after(store, b.job_id, a.job_id, launch_args={"agent_args": ["--model", "M"]})
    cli._print_status(b, None, False, store)
    assert "[model M]" in capsys.readouterr().out


# -- the pane ---------------------------------------------------------------------------------------------

def _pane(steward_home, tmp_path, cols=100, expanded=()):
    path = tmp_path / f"state{cols}"
    dashact.save(dashact.state_path(str(path)), {"expanded": {k: True for k in expanded}, "armed": {}})
    return dashrender.Pane(cols, 0, dashact.state_path(str(path)), store=Store(steward_home)).render(refresh=True)


def _plain(lines):
    return [re.sub(r"\033\[[0-9;]*m", "", ln) for ln in lines]


@pytest.fixture
def recipe(store):
    jobs = [_job(store, f"Recipe book Job {i}") for i in range(1, 7)]
    sequencing.queue(store, [j.job_id for j in jobs])
    _state(store, jobs[0], JobState.ACCEPTED)
    _state(store, jobs[1], JobState.RUNNING)
    return jobs


def test_group_is_one_row_folded_by_default_over_two_members(recipe, steward_home, tmp_path):
    lines, cmap = _pane(steward_home, tmp_path, cols=140)
    text = _plain(lines)
    group = [ln for ln in text if "Recipe book · 6 jobs" in ln]
    assert len(group) == 1 and "1/6 done" in group[0] and "now: Job 2 (running)" in group[0] and "(running)" in group[0]
    assert "▸" in group[0]
    assert not any("Job 3" in ln for ln in text)  # folded: members hidden
    assert any(c.startswith("expand") and "grp:" in c for c in cmap)


def test_group_unfolds_in_queue_order_with_buttons(recipe, steward_home, tmp_path):
    lines, cmap = _pane(steward_home, tmp_path, expanded=(dashrender.GROUP_PREFIX + recipe[0].job_id,))
    text = _plain(lines)
    assert any("▾" in ln and "Recipe book · 6 jobs" in ln for ln in text)
    order = [i for i, ln in enumerate(text) for n in range(2, 7) if f"Recipe book Job {n} " in ln and "staged" in ln or
             (f"Recipe book Job {n} " in ln and "RUNNING" in ln)]
    names = [re.search(r"Job (\d)", text[i]).group(1) for i in order]
    assert names == sorted(names)
    assert any("queued · after Recipe book Job 2 (running)" in ln for ln in text)
    assert any("queued · after Recipe book Job 3 (waiting)" in ln for ln in text)
    assert any("[ ▶ play ]" in ln and "[ ↑ ]" in ln and "[ ↓ ]" in ln for ln in text)
    acts = {c.split()[4] for c in cmap if c.startswith("btn")}
    assert {"play", "qup", "qdown", "fold"} <= acts


def test_two_member_group_is_open_by_default_and_toggle_flips(store, steward_home, tmp_path):
    a, b = _job(store, "Alpha"), _job(store, "Beta")
    sequencing.queue(store, [a.job_id, b.job_id])
    text = _plain(_pane(steward_home, tmp_path)[0])
    assert any("▾" in ln and "2 jobs" in ln for ln in text) and any("queued · " in ln for ln in text)
    key = dashrender.GROUP_PREFIX + a.job_id
    text = _plain(_pane(steward_home, tmp_path, expanded=(key,))[0])
    assert any("▸" in ln and "2 jobs" in ln for ln in text) and not any("queued · " in ln for ln in text)


def test_blockers_roll_up_to_group_row_when_folded(recipe, store, steward_home, tmp_path):
    _state(store, recipe[1], JobState.NEEDS_HUMAN)
    text = _plain(_pane(steward_home, tmp_path)[0])
    group = next(ln for ln in text if "Recipe book · 6 jobs" in ln)
    assert "needs you" in group and "⚠" in group
    assert any("→" in ln and "Job 2:" in ln for ln in text)
    # a stopped predecessor pauses the queue; the group says so
    _state(store, recipe[1], JobState.STOPPED)
    text = _plain(_pane(steward_home, tmp_path)[0])
    assert any("queue paused" in ln for ln in text)
    assert "⚠" in next(ln for ln in text if "Recipe book · 6 jobs" in ln)


def test_no_line_exceeds_cols_with_groups(recipe, steward_home, tmp_path):
    for cols in (60, 80, 100):
        for expanded in ((), (dashrender.GROUP_PREFIX + recipe[0].job_id,)):
            lines, _ = _pane(steward_home, tmp_path, cols, expanded)
            assert all(len(ln) <= cols for ln in _plain(lines)), [ln for ln in _plain(lines) if len(ln) > cols]


def test_dash_actions_map_to_cli(store):
    from steward import pkgenv

    assert dashact.argv_for("play", "J")[-4:-2] == ["play", "J"]  # `steward play` relays the launch op (sequencing hooks run there)
    assert dashact.argv_for("qup", "J")[-5:-2] == ["queue", "--up", "J"]
    assert "--down" in dashact.argv_for("qdown", "J")
