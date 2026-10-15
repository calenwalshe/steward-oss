from fixtures.repos import node_repo, python_repo

from steward.baseline import run_baseline
from steward.contract import Contract, load_contract, propose_contract, render, store_contract
from steward.discovery import discover
from steward.models import JobState, Judgment
from steward.runner_exec import SubprocessRunner
from steward.store import Store


def test_boundary_sentence_with_dont_is_captured():
    from steward.discovery import Discovery

    discovery = Discovery(commands=[], instruction_files=[], languages=[])
    contract = propose_contract(
        "Make reconnect reliable. Don't touch the db.", discovery, None
    )
    assert any("db" in b for b in contract.boundaries)
    assert any("touch the db" in b.lower() for b in contract.boundaries)


def test_authority_and_done_when_from_checks(tmp_path):
    repo = python_repo(tmp_path)
    discovery = discover(repo)

    contract = propose_contract("fix the bug", discovery, None)

    assert "edit files in job worktree" in contract.authority
    assert "run: pytest" in contract.authority
    # SP-36: read-only network and running scripts are authority, not a
    # blanket "no network" boundary.
    assert "read-only network (fetch pages/APIs/packages)" in contract.authority
    assert "run scripts and long-running commands in the worktree" in contract.authority
    assert contract.done_when == ["authorized checks pass with no new failures vs baseline"]


def test_ignored_unclassified_commands_are_assumptions(tmp_path):
    repo = node_repo(tmp_path)
    discovery = discover(repo)

    contract = propose_contract("ship the feature", discovery, None)

    deploy_assumptions = [a for a in contract.assumptions if "npm run deploy" in a]
    assert len(deploy_assumptions) == 1
    assert "deploy" in deploy_assumptions[0]


def test_baseline_pre_existing_failures_recorded_as_assumption(tmp_path, steward_home):
    repo = python_repo(tmp_path)
    discovery = discover(repo)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    baseline = run_baseline(store, job.job_id, repo, discovery, SubprocessRunner())

    contract = propose_contract("goal", discovery, baseline)

    assert any("pre-existing failure" in a for a in contract.assumptions)
    assert any("1 pre-existing failure" in a for a in contract.assumptions)


def test_uncommitted_count_becomes_assumption_line(tmp_path):
    repo = python_repo(tmp_path)
    discovery = discover(repo)

    contract = propose_contract("fix the bug", discovery, None, uncommitted_count=3)

    assert any(
        "3 uncommitted file(s) in the source repo are not included" in a
        for a in contract.assumptions
    )


def test_zero_uncommitted_count_adds_no_assumption(tmp_path):
    repo = python_repo(tmp_path)
    discovery = discover(repo)

    contract = propose_contract("fix the bug", discovery, None)

    assert not any("uncommitted file" in a for a in contract.assumptions)


def test_render_is_within_20_lines_and_has_all_headers(tmp_path):
    repo = node_repo(tmp_path)
    discovery = discover(repo)
    contract = propose_contract(
        "Make reconnect reliable. Preserve the public API. Don't touch the db.",
        discovery,
        None,
    )
    rendered = render(contract)
    lines = rendered.splitlines()
    assert len(lines) <= 20
    for header in ("GOAL", "BOUNDARIES", "DONE WHEN", "NEXT", "AUTHORITY"):
        assert any(line.startswith(header) for line in lines)
    assert any("db" in line for line in lines if line.startswith("BOUNDARIES"))


def test_store_contract_transitions_created_to_contracted_with_code_judgment(
    steward_home,
):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    from steward.discovery import Discovery

    discovery = Discovery(commands=[], instruction_files=[], languages=[])
    contract = propose_contract("goal", discovery, None)

    store_contract(store, job.job_id, contract)

    updated = store.get_job(job.job_id)
    assert updated.state == JobState.CONTRACTED

    events = store.events(job.job_id)
    contract_events = [e for e in events if e.what == "working contract proposed"]
    assert len(contract_events) == 1
    assert contract_events[0].judgment == Judgment.code
    assert contract_events[0].rule == "discovery_complete"


def test_load_contract_roundtrips(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    from steward.discovery import Discovery

    discovery = Discovery(commands=[], instruction_files=[], languages=[])
    contract = propose_contract("goal", discovery, None)
    store_contract(store, job.job_id, contract)

    loaded = load_contract(store, job.job_id)
    assert loaded == contract


def test_load_contract_missing_returns_none(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    assert load_contract(store, job.job_id) is None


# -- boundary seeding: goal-derived constraints become active (chair_note) --


def test_store_contract_seeds_boundaries_as_active_constraints(steward_home):
    from steward.discovery import Discovery
    from steward.steering import Steering

    store = Store(steward_home)
    goal = "Make reconnect reliable. Don't touch the db."
    job = store.create_job(goal, "/repo")
    discovery = Discovery(commands=[], instruction_files=[], languages=[])
    contract = propose_contract(goal, discovery, None)

    store_contract(store, job.job_id, contract)

    active = Steering(store).active_constraints(job.job_id)
    texts = [c["text"] for c in active]
    assert any("touch the db" in t.lower() for t in texts)
    # every contract boundary was seeded, not just the goal-derived one
    assert len(active) == len(contract.boundaries)


def test_seeded_boundary_verb_is_avoid_hard_for_path_like_text(steward_home):
    from steward.discovery import Discovery
    from steward.steering import Steering

    store = Store(steward_home)
    goal = "Fix the bug. Don't touch migrations/**."
    job = store.create_job(goal, "/repo")
    discovery = Discovery(commands=[], instruction_files=[], languages=[])
    contract = propose_contract(goal, discovery, None)

    store_contract(store, job.job_id, contract)

    active = Steering(store).active_constraints(job.job_id)
    migration_constraint = next(c for c in active if "migrations" in c["text"])
    assert migration_constraint["verb"] == "avoid"
    assert migration_constraint["enforcement"] == "hard"


def test_seeded_boundary_verb_is_preference_for_plain_text(steward_home):
    from steward.discovery import Discovery
    from steward.steering import Steering

    store = Store(steward_home)
    goal = "Fix the bug. Preserve behavior."
    job = store.create_job(goal, "/repo")
    discovery = Discovery(commands=[], instruction_files=[], languages=[])
    contract = propose_contract(goal, discovery, None)

    store_contract(store, job.job_id, contract)

    active = Steering(store).active_constraints(job.job_id)
    behavior_constraint = next(c for c in active if "preserve behavior" in c["text"].lower())
    assert behavior_constraint["enforcement"] == "preference"


def test_boundary_seeding_is_idempotent_per_contract_revision(steward_home):
    from steward.contract import seed_boundary_instructions
    from steward.discovery import Discovery
    from steward.steering import Steering

    store = Store(steward_home)
    goal = "Fix the bug. Don't touch the db."
    job = store.create_job(goal, "/repo")
    discovery = Discovery(commands=[], instruction_files=[], languages=[])
    contract = propose_contract(goal, discovery, None)

    seed_boundary_instructions(store, job.job_id, contract)
    seed_boundary_instructions(store, job.job_id, contract)  # same revision again

    assert len(Steering(store).all_instructions(job.job_id)) == len(contract.boundaries)


def test_seeded_boundary_verb_is_authority_for_outbound_boundary(steward_home):
    from steward.discovery import Discovery
    from steward.steering import Steering

    store = Store(steward_home)
    goal = "fix the bug"
    job = store.create_job(goal, "/repo")
    discovery = Discovery(commands=[], instruction_files=[], languages=[])
    contract = propose_contract(goal, discovery, None)

    store_contract(store, job.job_id, contract)

    active = Steering(store).active_constraints(job.job_id)
    outbound_constraint = next(c for c in active if "deploy/publish/push" in c["text"])
    assert outbound_constraint["verb"] == "authority"
    assert outbound_constraint["enforcement"] == "authority"


def test_seeded_prose_boundary_with_embedded_glob_is_hard_end_to_end_through_runner(
    steward_home, tmp_path
):
    """Regression (chair_note, phase 06-01).

    `parse_avoid_globs("Don't touch migrations/**.")` used to return every
    whitespace-split word, so `violations()` against a real diff path was
    always empty and a HARD-labelled constraint never actually blocked
    anything. Seed the constraint the way a goal actually produces it (goal
    prose -> contract boundary -> seeded instruction), have a worker touch
    the forbidden path, and drive it through the real `Runner` to confirm
    the violation is detected and closeout is blocked.
    """
    import subprocess

    from steward.baseline import run_baseline
    from steward.diagnosis import Diagnosis, FakeDiagnoser
    from steward.discovery import discover
    from steward.runner import Runner
    from steward.runner_exec import SubprocessRunner
    from steward.steering import Steering
    from steward.worker import FakeWorker, WorkerObservation
    from steward.worktree import create_worktree

    repo = python_repo(tmp_path)
    (repo / "migrations").mkdir()
    (repo / "migrations" / ".gitkeep").write_text("")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add migrations dir"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    store = Store(steward_home)
    goal = "Fix the bug. Don't touch migrations/**."
    job = store.create_job(goal, str(repo))
    real_runner = SubprocessRunner()
    discovery = discover(repo)
    create_worktree(repo, job.job_id, real_runner, base_dir=store.home / "worktrees")
    baseline = run_baseline(store, job.job_id, repo, discovery, real_runner)
    contract = propose_contract(goal, discovery, baseline)
    store_contract(store, job.job_id, contract)

    seeded = next(
        c
        for c in Steering(store).active_constraints(job.job_id)
        if "migrations" in c["text"]
    )
    assert seeded["verb"] == "avoid"
    assert seeded["enforcement"] == "hard"

    def write_migration_and_finish(worker):
        (worker.worktree / "migrations" / "x.py").write_text("bad migration\n")
        subprocess.run(["git", "add", "-A"], cwd=worker.worktree, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add migration"],
            cwd=worker.worktree,
            check=True,
            capture_output=True,
        )
        return WorkerObservation(
            lifecycle="idle", output_tail="STEWARD-DONE", pid_alive=True, ts="t"
        )

    class _Factory:
        def __init__(self, scripts):
            self._scripts = list(scripts)

        def __call__(self, existing_ref):
            script = self._scripts.pop(0) if self._scripts else []
            return FakeWorker(script=script)

    factory = _Factory(scripts=[[write_migration_and_finish]])
    diagnoser = FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="", hypothesis=""))
    runner = Runner(store, factory, real_runner, diagnoser)
    runner.start(job.job_id)

    runner.tick(job.job_id)

    violation_events = [
        e for e in store.events(job.job_id) if e.what == "hard constraint violation detected"
    ]
    assert len(violation_events) == 1
    assert store.kv_get(job.job_id, "closeout_blocked") is True
    assert store.get_job(job.job_id).state == JobState.NEEDS_HUMAN


def test_cli_explain_state_shows_goal_derived_boundary(steward_home, tmp_path, capsys):
    from fixtures.repos import python_repo

    from steward import cli

    repo = python_repo(tmp_path)
    cli.main(["new", "Fix reconnect. Don't touch the db.", "--repo", str(repo), "--no-baseline"])
    job_id = capsys.readouterr().out.split()[0]

    rc = cli.main(["explain-state", job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "touch the db" in out.lower()
    assert "Active constraints: none" not in out


def test_boundaries_seed_independently_for_each_job(steward_home):
    # Regression (live run 2026-09-26): request ids were not job-scoped, so the
    # second job's boundaries deduped against the first job and were never seeded.
    from steward.control import Controller
    from steward.steering import Steering
    from steward.store import Store

    store = Store(steward_home)
    ctl = Controller(store)
    c = Contract(goal="g", boundaries=["Don't touch tests/**."], done_when=["x"], next_step="n",
                 authority=[], assumptions=[], revision=1)
    jobs = [ctl.new_job("g", "/tmp"), ctl.new_job("g", "/tmp")]
    for j in jobs:
        store_contract(store, j.job_id, c)
    for j in jobs:
        assert [i["enforcement"] for i in Steering(store).active_constraints(j.job_id)] == ["hard"]
