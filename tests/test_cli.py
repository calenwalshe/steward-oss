import json
import subprocess
import sys

from fixtures.repos import python_repo

from steward import cli


def test_new_creates_job_and_prints_id_and_state(steward_home, tmp_path, capsys):
    repo = python_repo(tmp_path)
    rc = cli.main(["new", "demo goal", "--repo", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CREATED" in out


def test_new_with_nonexistent_repo_records_discovery_failed_and_proceeds(steward_home, capsys):
    # SP-36 (14-01/NB-03): a bogus --repo is a recoverable discovery failure,
    # not a blocker — "new" retries once, records what happened, and still
    # moves the job to CONTRACTED instead of leaving it stuck.
    rc = cli.main(["new", "demo goal", "--repo", "/repo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo goal" in out
    job_id = out.split()[0]

    rc = cli.main(["status", job_id, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "CONTRACTED"

    rc = cli.main(["events", job_id, "--json"])
    assert rc == 0
    events = json.loads(capsys.readouterr().out)
    assert any(e["what"] == "discovery_failed" and e["judgment"] == "code" for e in events)


def test_status_with_explicit_job_json(steward_home, capsys):
    cli.main(["new", "demo goal", "--repo", "/repo"])
    out = capsys.readouterr().out
    job_id = out.split()[0]

    rc = cli.main(["status", job_id, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == job_id
    assert payload["goal"] == "demo goal"
    # SP-36 (14-01/NB-03): a bogus --repo still proceeds to CONTRACTED (empty
    # discovery, no baseline) instead of getting stuck at CREATED; the last
    # event is the boundary-seeding that follows the fallback contract.
    assert payload["state"] == "CONTRACTED"
    assert payload["last_event"]["judgment"] == "code"


def test_status_without_job_lists_every_active_job(steward_home, capsys):
    # (06-01, MOSHI-05) `steward status` with no job argument is now the
    # phone-friendly dashboard over every still-active job, not a "latest
    # job" lookup — a bare `status` must stay meaningful (and exit 0) when
    # there isn't a single obvious job to fall back to.
    cli.main(["new", "first", "--repo", "/repo"])
    capsys.readouterr()
    cli.main(["new", "second", "--repo", "/repo"])
    capsys.readouterr()

    rc = cli.main(["status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert {p["goal"] for p in payload} == {"first", "second"}


def test_status_without_job_and_no_jobs_at_all_exits_0(steward_home, capsys):
    rc = cli.main(["status", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []


def test_status_human_output_is_one_screen(steward_home, capsys):
    cli.main(["new", "demo goal", "--repo", "/repo"])
    capsys.readouterr()
    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) <= 10


def test_why_prints_last_event(steward_home, capsys):
    cli.main(["new", "demo goal", "--repo", "/repo"])
    out = capsys.readouterr().out
    job_id = out.split()[0]

    rc = cli.main(["why", job_id])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # SP-36 (14-01/NB-03): a bogus --repo still produces (and seeds) a
    # fallback contract, so the last event is the boundary seed that
    # follows it, not the discovery failure itself.
    assert payload["what"] == "authority instruction recorded"
    assert payload["judgment"] == "code"


def test_events_lists_events_json(steward_home, capsys):
    cli.main(["new", "demo goal", "--repo", "/repo"])
    out = capsys.readouterr().out
    job_id = out.split()[0]

    rc = cli.main(["events", job_id, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # "job created", then two (NB-03: one automatic retry) rounds of
    # create_worktree's own exceptions.use("git", ...) event (OPX-04 —
    # 11-01) followed by "discovery_failed" once the nonexistent /repo
    # fails the git subprocess call, then the fallback contract is stored
    # and seeded anyway (SP-36: never stuck).
    assert [e["what"] for e in payload] == [
        "job created",
        "exception_used",
        "discovery_failed",
        "exception_used",
        "discovery_failed",
        "working contract proposed",
        "authority instruction recorded",
    ]


def test_stop_blocks_dispatch_and_duplicate_is_marked(steward_home, capsys):
    cli.main(["new", "demo goal", "--repo", "/repo"])
    out = capsys.readouterr().out
    job_id = out.split()[0]

    rc = cli.main(["stop", job_id, "--request-id", "req-1"])
    assert rc == 0
    first_out = capsys.readouterr().out
    assert "accepted=True" in first_out

    rc = cli.main(["stop", job_id, "--request-id", "req-1"])
    assert rc == 0
    second_out = capsys.readouterr().out
    assert "duplicate=True" in second_out


def test_pause_from_created_is_rejected(steward_home, capsys):
    cli.main(["new", "demo goal", "--repo", "/repo"])
    out = capsys.readouterr().out
    job_id = out.split()[0]

    rc = cli.main(["pause", job_id])
    assert rc == 1
    assert "accepted=False" in capsys.readouterr().out


def test_help_exits_zero_via_module_entrypoint():
    result = subprocess.run(
        [sys.executable, "-m", "steward.cli", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
