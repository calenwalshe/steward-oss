import subprocess

from fixtures.repos import python_repo

from steward import cli
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from steward.worktree import create_worktree, remove_worktree, uncommitted_files


def test_create_worktree_adds_branch_and_path(tmp_path):
    repo = python_repo(tmp_path)
    base_dir = tmp_path / "worktrees"
    runner = SubprocessRunner()

    worktree_path = create_worktree(repo, "job-1", runner, base_dir=base_dir)

    assert worktree_path == base_dir / "job-1"
    assert worktree_path.is_dir()
    assert (worktree_path / "pyproject.toml").exists()

    branches = subprocess.run(
        ["git", "branch", "--list", "steward/job-1"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "steward/job-1" in branches


def test_create_worktree_never_blocks_on_dirty_tree(tmp_path):
    """SP-36 (14-01/NB-03): a dirty source repo never blocks — the worktree
    is created from HEAD anyway."""
    repo = python_repo(tmp_path)
    (repo / "pyproject.toml").write_text("# dirty change\n" + (repo / "pyproject.toml").read_text())
    base_dir = tmp_path / "worktrees"
    runner = SubprocessRunner()

    worktree_path = create_worktree(repo, "job-2", runner, base_dir=base_dir)

    assert worktree_path == base_dir / "job-2"
    assert worktree_path.is_dir()
    # The worktree is from HEAD, not the dirty change.
    assert "dirty change" not in (worktree_path / "pyproject.toml").read_text()


def test_create_worktree_records_uncommitted_files_event(tmp_path, steward_home):
    repo = python_repo(tmp_path)
    (repo / "pyproject.toml").write_text("# dirty change\n" + (repo / "pyproject.toml").read_text())
    base_dir = tmp_path / "worktrees"
    runner = SubprocessRunner()
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))

    create_worktree(repo, job.job_id, runner, base_dir=base_dir, store=store)

    events = [e for e in store.events(job.job_id) if e.what == "source_uncommitted_ignored"]
    assert len(events) == 1
    assert events[0].judgment.value == "code"
    assert events[0].evidence["total"] == 1
    assert "pyproject.toml" in events[0].evidence["files"][0]


def test_uncommitted_files_lists_dirty_names(tmp_path):
    repo = python_repo(tmp_path)
    (repo / "pyproject.toml").write_text("# dirty change\n" + (repo / "pyproject.toml").read_text())
    runner = SubprocessRunner()

    names = uncommitted_files(repo, runner)

    assert any("pyproject.toml" in n for n in names)


def test_uncommitted_files_empty_for_clean_repo(tmp_path):
    repo = python_repo(tmp_path)
    runner = SubprocessRunner()

    assert uncommitted_files(repo, runner) == []


def test_remove_worktree(tmp_path):
    repo = python_repo(tmp_path)
    base_dir = tmp_path / "worktrees"
    runner = SubprocessRunner()
    worktree_path = create_worktree(repo, "job-3", runner, base_dir=base_dir)
    assert worktree_path.is_dir()

    remove_worktree(repo, worktree_path, runner)
    assert not worktree_path.exists()


def test_worktree_base_dir_defaults_outside_repo(tmp_path, steward_home):
    repo = python_repo(tmp_path)
    runner = SubprocessRunner()

    worktree_path = create_worktree(repo, "job-4", runner)

    assert worktree_path == steward_home / "worktrees" / "job-4"
    assert worktree_path.is_dir()


def test_cli_new_runs_full_pipeline_and_prints_contract(
    tmp_path, steward_home, capsys
):
    repo = python_repo(tmp_path)

    rc = cli.main(
        [
            "new",
            "fix X. Don't touch the db.",
            "--repo",
            str(repo),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "GOAL: fix X. Don't touch the db." in out
    boundary_lines = [line for line in out.splitlines() if line.startswith("BOUNDARIES")]
    assert boundary_lines
    assert "db" in boundary_lines[0]
    assert "AUTHORITY" in out
    assert "run: pytest" in out


def test_cli_new_on_dirty_repo_still_accepts_and_notes_assumption(tmp_path, steward_home, capsys):
    """SP-36 (14-01/NB-03) end to end: a dirty source repo never blocks
    `steward new` — it succeeds, and the contract's ASSUMPTIONS say what was
    left out."""
    repo = python_repo(tmp_path)
    (repo / "pyproject.toml").write_text("# dirty change\n" + (repo / "pyproject.toml").read_text())

    rc = cli.main(["new", "fix things", "--repo", str(repo), "--no-baseline"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "uncommitted file(s) in the source repo are not included" in out
    job_id = out.split()[0]
    rc = cli.main(["status", job_id, "--json"])
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "CONTRACTED"


def test_cli_new_no_baseline_flag_skips_check_run(tmp_path, steward_home, capsys):
    repo = python_repo(tmp_path)

    rc = cli.main(
        ["new", "fix things", "--repo", str(repo), "--no-baseline"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "pre-existing failure" not in out


def test_cli_contract_re_renders_stored_contract(tmp_path, steward_home, capsys):
    repo = python_repo(tmp_path)
    cli.main(["new", "fix things", "--repo", str(repo), "--no-baseline"])
    out = capsys.readouterr().out
    job_id = out.splitlines()[0].split("\t")[0]

    rc = cli.main(["contract", job_id])
    assert rc == 0
    out2 = capsys.readouterr().out
    assert "GOAL: fix things" in out2


def test_cli_new_with_nonexistent_repo_still_proceeds_to_contracted(steward_home, capsys):
    # SP-36 (14-01/NB-03): a bogus repo path is a recoverable discovery
    # failure, not a blocker — "new" retries once, then still produces a
    # contract (no baseline) and moves the job to CONTRACTED, recording
    # what happened rather than leaving it stuck in CREATED.
    rc = cli.main(["new", "demo goal", "--repo", "/repo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo goal" in out
    assert "proceeding with no baseline" in out

    job_id = out.split()[0]
    rc = cli.main(["status", job_id, "--json"])
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "CONTRACTED"

    rc = cli.main(["events", job_id, "--json"])
    events = json.loads(capsys.readouterr().out)
    discovery_failed = [e for e in events if e["what"] == "discovery_failed" and e["judgment"] == "code"]
    # one retry: two attempts, both fail for a repo that doesn't exist
    assert len(discovery_failed) == 2


def test_worktree_links_user_skills_and_agents_git_excluded(tmp_path, monkeypatch):
    import subprocess
    from steward import worktree as wt_mod
    claude = tmp_path / "claude"
    (claude / "skills" / "gsd-plan-phase").mkdir(parents=True)
    (claude / "skills" / "gsd-plan-phase" / "SKILL.md").write_text("x")
    (claude / "agents").mkdir()
    (claude / "agents" / "gsd-planner.md").write_text("y")
    monkeypatch.setenv("STEWARD_CLAUDE_DIR", str(claude))
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env)
    from steward.runner_exec import SubprocessRunner
    path = wt_mod.create_worktree(repo, "job1", SubprocessRunner(), base_dir=tmp_path / "wts")
    assert (path / ".claude" / "skills" / "gsd-plan-phase" / "SKILL.md").read_text() == "x"
    assert (path / ".claude" / "agents" / "gsd-planner.md").is_symlink()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True).stdout
    assert status == ""  # links are git-excluded, never committed
    monkeypatch.setenv("STEWARD_USER_LIBRARY", "0")
    assert wt_mod.link_user_library(tmp_path / "other") == {}


def test_user_plugin_dirs_respects_enabled_plugins(tmp_path, monkeypatch):
    import json
    from steward import worktree as wt_mod
    claude = tmp_path / "claude"
    sp, fd = tmp_path / "sp", tmp_path / "fd"
    sp.mkdir(); fd.mkdir()
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {
        "superpowers@x": [{"installPath": str(sp)}], "figma@x": [{"installPath": str(fd)}],
        "gone@x": [{"installPath": str(tmp_path / "missing")}]}}))
    monkeypatch.setenv("STEWARD_CLAUDE_DIR", str(claude))
    assert wt_mod.user_plugin_dirs() == [str(fd), str(sp)]  # all installed when none listed
    (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"superpowers@x": True, "figma@x": False}}))
    assert wt_mod.user_plugin_dirs() == [str(sp)]
    monkeypatch.setenv("STEWARD_USER_LIBRARY", "0")
    assert wt_mod.user_plugin_dirs() == []
