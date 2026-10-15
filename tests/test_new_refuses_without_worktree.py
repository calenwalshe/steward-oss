"""u1 (stall fixes 2026-10-14): `steward new` on a repo whose `git worktree add` fails (e.g. a repo with no
commits: "fatal: invalid reference: HEAD") must not produce a contracted job that later dispatches with no
worktree. The job is marked FAILED before any contract/dispatch, with a reason that says what to do."""
import json
import subprocess

from steward import cli


def _empty_repo(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def test_new_on_repo_without_commits_refuses_with_clear_reason(steward_home, tmp_path, capsys):
    repo = _empty_repo(tmp_path)
    rc = cli.main(["new", "demo goal", "--repo", str(repo)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "no commits" in captured.err
    assert "initial commit" in captured.err
    job_id = captured.out.split()[0]

    cli.main(["status", job_id, "--json"])
    assert json.loads(capsys.readouterr().out)["state"] == "FAILED"

    cli.main(["events", job_id, "--json"])
    whats = [e["what"] for e in json.loads(capsys.readouterr().out)]
    assert "worktree_unavailable" in whats
    assert "working contract proposed" not in whats
    assert "worker dispatch started" not in whats


def test_worktree_add_failure_is_not_retried(steward_home, tmp_path, capsys):
    repo = _empty_repo(tmp_path)
    cli.main(["new", "demo goal", "--repo", str(repo)])
    job_id = capsys.readouterr().out.split()[0]
    cli.main(["events", job_id, "--json"])
    events = json.loads(capsys.readouterr().out)
    assert sum(1 for e in events if e["what"] == "discovery_failed") == 1
