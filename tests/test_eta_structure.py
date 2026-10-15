"""The unit structure an ETA reads (phase 18-02): the .planning adapter on real tmp git repos built from the
committed content-free timelines, the units_total cascade, the job facts and the stated durations."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from eta_fixtures import advance, load_timeline, replay_events, setup_job  # noqa: E402
from steward import chat, efficiency, eta_structure  # noqa: E402
from steward.runner_exec import SubprocessRunner  # noqa: E402
from steward.store import Store  # noqa: E402


class Recording:
    """A real runner that remembers every argv."""

    def __init__(self):
        self.real = SubprocessRunner()
        self.calls: list[list[str]] = []

    def run(self, argv, cwd, **kw):
        self.calls.append(list(argv))
        return self.real.run(argv, cwd, **kw)


def world(job: str, minutes: float, home: Path):
    home.mkdir(parents=True, exist_ok=True)
    store = Store(home)
    tl = load_timeline(job)
    until = tl["t0"] + minutes * 60
    setup_job(store, tl, until=until)
    replay_events(store, job, tl, until)
    return store, tl, until


def structure(store, tl, until, runner=None, cache=None, **kw):
    return eta_structure.read(store, tl["job"], runner or SubprocessRunner(), now=until, cache=cache, **kw)


def labels(n, phase=16):
    return [efficiency.unit_label((phase, i)) for i in range(1, n + 1)]


def test_a7d292d8_oracle(tmp_path):
    tl = load_timeline("a7d292d8")
    t0 = tl["t0"]
    summary_at = {c["add"][0].rsplit("/", 1)[-1][:5]: c["at"] for c in tl["commits"] if c["add"] and "SUMMARY" in c["add"][0]}

    store, tl, until = world("a7d292d8", 33.5, tmp_path / "h33.5")
    st = structure(store, tl, until)
    assert st.units_source == "none" and st.planned == {} and st.units_total is None  # the adapter is absent

    store, tl, until = world("a7d292d8", 35.0, tmp_path / "h35.0")
    st = structure(store, tl, until)
    assert st.units_source == "planning"
    assert list(st.planned) == labels(7)
    assert st.units_total == 7 and st.units_exact is True
    assert st.phases == {"16": 7} and st.done == {}
    assert abs((st.known_at - t0) / 60 - 34.2) <= 0.2

    store, tl, until = world("a7d292d8", 64.5, tmp_path / "h64.5")
    st = structure(store, tl, until)
    assert list(st.done) == labels(3)
    assert st.done == {k: float(summary_at[k]) for k in labels(3)}  # the SUMMARY commit times are the boundaries

    store, tl, until = world("a7d292d8", 126.0, tmp_path / "h126.0")
    st = structure(store, tl, until)
    assert list(st.done) == labels(7) and st.units_total == 7
    assert not any(u.startswith(("01-", "15-")) for u in st.planned)  # the 51 base files never count


def test_adapter_is_cached_by_head(tmp_path):
    store, tl, until = world("a7d292d8", 35.0, tmp_path / "h35.0")
    rec, cache = Recording(), {}
    first = structure(store, tl, until, rec, cache)
    assert first.units_source == "planning"
    rec.calls.clear()
    structure(store, tl, until, rec, cache)
    assert [c[1] for c in rec.calls] == ["rev-parse"]
    # a new commit: one rev-parse plus one log
    advance(chat.worktree_path(store, tl["job"]), tl, tl["t0"] + 45 * 60)
    rec.calls.clear()
    st = structure(store, tl, until, rec, cache)
    assert [c[1] for c in rec.calls] == ["rev-parse", "log"]
    assert list(st.done) == labels(1)


def test_no_worktree_no_subprocess(tmp_path):
    store = Store(tmp_path / "home")
    tl = load_timeline("a7d292d8")
    store.create_job(tl["goal"], "/nonexistent", job_id=tl["job"])
    rec = Recording()
    st = structure(store, tl, tl["t0"], rec)
    assert st.units_source == "none" and rec.calls == []


def test_parsers():
    text = "# R\n\n- [ ] **Phase 1: A** - x\n- [ ] **Phase 2: B** - y\n\n### Phase 1: A\n\n**Plans**: 3 plans\n\n" \
           "### Phase 2: B\n\n**Plans**: TBD\n\n### Phase 2.1: C\n"
    assert eta_structure.parse_roadmap(text) == {"1": 3, "2": None, "2.1": None}
    assert eta_structure.parse_state_progress("---\nstatus: x\nprogress:\n  total_phases: 8\n  percent: 100\n---\ntotal_phases: 99\n") \
        == {"total_phases": 8, "percent": 100}
    assert eta_structure.parse_state_progress("# no frontmatter\nprogress:\n  total_phases: 3\n") == {}


# -- the units_total cascade (G2) --------------------------------------------------------------------------


def set_hint(store, job_id, n):
    from steward.contract import Contract

    store.kv_set(job_id, "contract", {"goal": "g", "units_total": n})
    assert Contract(**store.kv_get(job_id, "contract")).units_total == n


def test_hint_always_wins(tmp_path):
    store, tl, until = world("a7d292d8", 64.0, tmp_path / "h")
    set_hint(store, tl["job"], 9)
    st = structure(store, tl, until)
    assert (st.units_source, st.units_total, st.units_exact) == ("hint", 9, True)
    assert list(st.done) == labels(3) and list(st.planned) == labels(7)  # the files still give done / planned


def commit_world(tmp_path, subjects, goal="Ship the thing"):
    """A job whose worktree has only `subjects` committed after the base (no .planning files)."""
    from eta_fixtures import setup_job as _setup

    tl = {"job": "c0ffee01", "goal": goal, "t0": 1_000_000.0, "truth_min": 0, "attempts": [], "done": [], "sent_back": [],
          "base": {"files": [], "roadmap": None, "state": None},
          "commits": [{"at": 1_000_100 + 100 * i, "subject": s, "add": [], "modify": [], "delete": [], "roadmap": None, "state": None}
                      for i, s in enumerate(subjects)]}
    store = Store(tmp_path / "home")
    _setup(store, tl)
    return store, tl


def test_commit_units_path(tmp_path):
    store, tl = commit_world(tmp_path, ["docs(16-01): phase summary", "docs(01-02): complete contract layer plan",
                                        "docs(03-08): auto-generated contexts (autonomous discuss)", "feat(16-03): x"])
    st = structure(store, tl, 1_001_000.0)
    assert st.units_source == "commits"
    assert st.done == {"16-01": 1_000_100.0, "01-02": 1_000_200.0}
    assert st.units_total is None and st.units_exact is False and st.units_lower_bound == 2
    assert eta_structure.commit_units([(1.0, "docs(16-01): phase summary"), (2.0, "docs(16-01): summary again")]) \
        == {"done": {"16-01": 1.0}, "lower_bound": 1}


def test_commit_units_are_read_once_per_head(tmp_path):
    store, tl = commit_world(tmp_path, ["docs(16-01): phase summary"])
    rec, cache = Recording(), {}
    structure(store, tl, 1.0, rec, cache)
    assert [c[1] for c in rec.calls] == ["rev-parse", "log", "log"]  # head, planning log, the bounded subject log
    rec.calls.clear()
    assert structure(store, tl, 1.0, rec, cache).done
    assert [c[1] for c in rec.calls] == ["rev-parse"]


def agent(desc, finished, end=0.0, kind="build"):
    return {"kind": kind, "description": desc, "finished": finished, "end": end, "agent_type": "general-purpose"}


def test_descriptions_path(tmp_path):
    store, tl = commit_world(tmp_path, ["feat(16-01): x"])
    metrics = {"agents": [agent("Execute plan 16-01", True, 500.0), agent("Execute plan 16-02", True, 900.0),
                          agent("Execute plan 16-03", False, 0.0), agent("Plan phase 16", True, 100.0, kind="plan"),
                          agent("Verify phase 16", True, 950.0, kind="verify")]}
    st = structure(store, tl, 1_001_000.0, metrics=metrics)
    assert st.units_source == "descriptions"
    assert st.done == {"16-01": 500.0, "16-02": 900.0}
    assert st.units_lower_bound == 3 and st.units_total is None


def test_stated_duration():
    f = eta_structure.stated_duration_min
    assert f("for 6 hours") == 360 and f("for 1 hour") == 60
    assert f("a 30-minute ticker") == 30 and f("a 5-minute ticker") == 5
    assert f("run for 45 min") == 45 and f("for 2.5h") == 150
    assert f("build phase 15") is None and f("Execute plan 16-01") is None and f("") is None and f(None) is None


def test_goal_source(tmp_path):
    store, tl = commit_world(tmp_path, ["feat(x): y"], goal="Watch the queue for 6 hours and report")
    st = structure(store, tl, 1_001_000.0)
    assert (st.units_source, st.clock_min, st.units_total) == ("goal", 360, None)


def test_job_facts_from_events(tmp_path):
    from eta_fixtures import _clock

    store = Store(tmp_path / "home")
    with _clock(1000.0):
        job = store.create_job("g", "/nonexistent")

    def ev(at, what, **evidence):
        with _clock(at):
            store.append_event(job.job_id, what=what, evidence=evidence, rule="t", judgment=efficiency_judgment())

    def read_():
        return eta_structure.read(store, job.job_id, SubprocessRunner(), now=9999.0)

    ev(1001.0, "job created")
    ev(1002.0, "worker attempt started", attempt_no=1)
    st = read_()
    assert (st.start, st.attempt_no, st.attempt_start, st.rework, st.done_claimed_at) == (1000.0, 1, 1002.0, False, None)
    ev(1100.0, "worker reported done (typed frame)")
    assert read_().done_claimed_at == 1100.0
    ev(1200.0, "sent back for rework; new attempt pending")
    ev(1201.0, "worker attempt started", attempt_no=2, rework=True)
    st = read_()
    assert (st.attempt_no, st.rework, st.done_claimed_at, st.attempt_start) == (2, True, None, 1201.0)
    ev(1300.0, "worker reported done")  # the legacy event name counts too
    assert read_().done_claimed_at == 1300.0


def efficiency_judgment():
    from steward.models import Judgment

    return Judgment.code


class Broken:
    def __init__(self, mode):
        self.mode = mode
        self.real = SubprocessRunner()

    def run(self, argv, cwd, **kw):
        if self.mode == "raise":
            raise RuntimeError("boom")
        if argv[1] == "log":
            from steward.runner_exec import CommandResult

            return CommandResult(argv=argv, cwd=str(cwd), exit_code=128, stdout="", stderr="fatal: bad", duration_s=0.0)
        return self.real.run(argv, cwd, **kw)


@pytest.mark.parametrize("mode", ["raise", "exit128"])
def test_read_never_raises(tmp_path, mode):
    store, tl, until = world("a7d292d8", 64.0, tmp_path / "h")
    st = structure(store, tl, until, Broken(mode))
    assert st.units_source == "none" and st.notes
    set_hint(store, tl["job"], 5)
    assert structure(store, tl, until, Broken(mode)).units_source == "hint"


# -- multi-phase and second-attempt timelines (06ea6a04, ed11ba3c) -----------------------------------------


def replayer(job, tmp_path):
    """One job replayed forward in time: step(minutes) advances the repo and the events, then reads the structure."""
    store = Store(tmp_path / "home")
    tl = load_timeline(job)
    setup_job(store, tl, until=tl["t0"])
    wt = chat.worktree_path(store, job)

    def step(minutes, runner=None, **kw):
        until = tl["t0"] + minutes * 60
        advance(wt, tl, until)
        replay_events(store, job, tl, until)
        return structure(store, tl, until, runner, cache={}, **kw)

    return step, tl


def test_06ea6a04_oracle(tmp_path):
    step, tl = replayer("06ea6a04", tmp_path)
    t0 = tl["t0"]
    st = step(13.0)
    assert st.units_source == "none" and st.phases == {}

    st = step(14.0)
    assert st.units_source == "planning" and st.planned == {} and st.units_total is None
    assert list(st.phases) == [str(i) for i in range(1, 9)] and set(st.phases.values()) == {None}
    assert abs((st.known_at - t0) / 60 - 13.7) <= 0.2

    st = step(34.0)
    assert st.phases["1"] == 3 and list(st.planned) == labels(3, 1) and st.done == {} and st.units_total is None
    assert list(step(40.0).done) == ["01-01"]
    assert list(step(47.0).done) == labels(3, 1)
    st = step(75.0)
    assert st.phases["1"] == 3 and st.phases["2"] == 3 and st.phases["3"] is None
    assert len(step(92.0).done) == 6
    st = step(150.0)
    assert [p for p, n in st.phases.items() if n] == ["1", "2", "3", "4"] and len(st.done) == 8
    st = step(245.0)
    assert [p for p, n in st.phases.items() if n] == ["1", "2", "3", "4", "5", "6", "7"] and st.phases["7"] == 2
    assert len(st.done) == 13
    st = step(423.0)
    assert st.phases == {"1": 3, "2": 3, "3": 3, "4": 3, "5": 3, "6": 3, "7": 2, "8": 3}
    assert (st.units_total, st.units_exact, len(st.done), len(st.planned)) == (23, True, 23, 23)


def test_ed11ba3c_oracle_and_job_facts(tmp_path):
    step, tl = replayer("ed11ba3c", tmp_path)
    t0 = tl["t0"]
    st = step(4.0)  # the false done, 0 units
    assert (st.attempt_no, st.rework, st.units_source) == (1, False, "none")
    assert st.done_claimed_at == pytest.approx(tl["done"][0], abs=0.01) and st.attempt_start == pytest.approx(t0 + 1.13, abs=0.01)
    st = step(6.0)
    assert (st.attempt_no, st.rework, st.done_claimed_at) == (2, True, None)
    assert step(36.5).units_source == "none"
    st = step(37.6)
    assert st.units_source == "planning" and list(st.planned) == labels(7, 17) and st.units_total == 7 and st.phases == {"17": 7}
    assert list(step(57.6).done) == ["17-01"]
    assert len(step(119.8).done) == 4  # 17-04 has two SUMMARY commits: the first add counts
    assert step(119.8).done["17-04"] == pytest.approx(t0 + (119.39) * 60, abs=30)
    st = step(176.6)
    assert len(st.done) == 7 and st.attempt_no == 2
    assert st.done_claimed_at is None  # the real done comes later, after 181 min


def test_state_progress_fallback(tmp_path):
    tl = {"job": "5ca1ab1e", "goal": "g", "t0": 1_000_000.0, "truth_min": 0, "attempts": [], "done": [], "sent_back": [],
          "base": {"files": [], "roadmap": None, "state": None},
          "commits": [{"at": 1_000_100, "subject": "docs: work", "add": [".planning/ROADMAP.md", ".planning/STATE.md"],
                       "modify": [], "delete": [], "roadmap": {}, "state": {"total_phases": 3, "completed_phases": 0}}]}
    store = Store(tmp_path / "home")
    setup_job(store, tl)
    st = structure(store, tl, 1_001_000.0)
    assert st.units_source == "planning" and st.phases == {"1": None, "2": None, "3": None} and st.units_total is None


def test_bounded_reads(tmp_path):
    import subprocess

    step, tl = replayer("06ea6a04", tmp_path)
    wt = chat.worktree_path(Store(tmp_path / "home"), tl["job"])
    shows = 0
    for minutes in [5 + 22 * i for i in range(20)]:
        rec = Recording()
        st = step(minutes, runner=rec)
        head = st.head
        for argv in rec.calls:
            assert argv[0] == "git" and argv[1] in ("rev-parse", "log", "merge-base", "show")
            if argv[1] == "show":
                shows += 1
                assert argv[2] in (f"{head}:.planning/ROADMAP.md", f"{head}:.planning/STATE.md")
                path = argv[2].split(":", 1)[1]
                changed = subprocess.run(["git", "diff", "--name-only", st.base, head], cwd=wt, capture_output=True, text=True).stdout.split()
                assert path in changed  # only a file the job's diff changed is ever shown
        assert not any(a for a in rec.calls if a[1] == "show" and ("PLAN" in a[2] or "SUMMARY" in a[2]))
    assert shows >= 10
