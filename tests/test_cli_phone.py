"""Phone-friendly CLI surface (MOSHI-05): `status` (all active jobs), `steer`,
`approve`/`deny`, `approvals` — every one going through `steward.api`, and
every one dash-safe (`sh -lc`) since that's how Moshi's app actually invokes
commands on this box.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from steward import approvals as approvals_module
from steward import cli
from steward.models import JobState, Judgment
from steward.store import Store

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_status_exits_0_under_dash_with_a_fresh_empty_home(tmp_path):
    home = tmp_path / "steward_home"
    home.mkdir()
    env = {**os.environ, "STEWARD_HOME": str(home)}

    result = subprocess.run(
        ["sh", "-lc", f"cd {_REPO_ROOT} && {sys.executable} -m steward.cli status"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr


def test_status_all_is_phone_friendly_json_shape(steward_home, capsys):
    cli.main(["new", "demo goal", "--repo", "/repo"])
    capsys.readouterr()

    rc = cli.main(["status", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    entry = payload[0]
    for key in ("job_id", "state", "goal", "step", "checks", "recoveries", "revision", "deep_link"):
        assert key in entry
    # No herdr workspace recorded yet for a freshly created job: the deep
    # link is omitted (None), not a bare `moshi://herdr` with no params.
    assert entry["deep_link"] is None


def test_status_all_includes_deep_link_once_herdr_workspace_is_known(steward_home, capsys):
    job_id = _new_job_id(capsys)
    store = Store(steward_home)
    store.kv_set(job_id, "herdr_workspace", "ws-1")
    store.kv_set(job_id, "herdr_pane", "pane-1")
    capsys.readouterr()

    rc = cli.main(["status", "--json"])
    assert rc == 0
    entry = json.loads(capsys.readouterr().out)[0]
    assert entry["deep_link"] == "moshi://herdr?workspace=ws-1&pane=pane-1"

    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "link: moshi://herdr?workspace=ws-1&pane=pane-1" in out


def test_status_all_human_output_is_at_most_8_lines_per_job(steward_home, capsys):
    cli.main(["new", "demo goal", "--repo", "/repo"])
    capsys.readouterr()

    rc = cli.main(["status"])

    assert rc == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) <= 8


def _new_job_id(capsys) -> str:
    cli.main(["new", "demo goal", "--repo", "/repo"])
    out = capsys.readouterr().out
    return out.split()[0]


def test_steer_records_instruction_via_api(steward_home, capsys):
    job_id = _new_job_id(capsys)

    rc = cli.main(["steer", job_id, "look at retries first", "--verb", "focus"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "accepted=True" in out
    assert "revision=" in out


def test_steer_stale_expect_rev_exits_3_and_prints_current_revision(steward_home, capsys):
    job_id = _new_job_id(capsys)
    capsys.readouterr()

    rc = cli.main(["steer", job_id, "second try", "--expect-rev", "999"])

    assert rc == 3
    out = capsys.readouterr().out
    assert "CONFLICT" in out
    assert "current_revision=" in out


def test_steer_matching_expect_rev_succeeds(steward_home, capsys):
    job_id = _new_job_id(capsys)
    store = Store(steward_home)
    current_rev = store.get_revision(job_id)
    capsys.readouterr()

    rc = cli.main(["steer", job_id, "second try", "--expect-rev", str(current_rev)])

    assert rc == 0
    assert "accepted=True" in capsys.readouterr().out


def test_approve_and_deny_via_cli(steward_home, capsys):
    job_id = _new_job_id(capsys)
    store = Store(steward_home)
    approvals_module.open_approval(store, job_id, "may I proceed?", approval_id="ap-1")
    capsys.readouterr()

    rc = cli.main(["approve", job_id, "ap-1"])
    assert rc == 0
    assert "accepted=True" in capsys.readouterr().out
    assert approvals_module.get_approval(store, job_id, "ap-1")["status"] == "approved"

    approvals_module.open_approval(store, job_id, "may I proceed too?", approval_id="ap-2")
    rc = cli.main(["deny", job_id, "ap-2"])
    assert rc == 0
    assert approvals_module.get_approval(store, job_id, "ap-2")["status"] == "denied"


def test_approvals_lists_pending_and_decided(steward_home, capsys):
    job_id = _new_job_id(capsys)
    store = Store(steward_home)
    approvals_module.open_approval(store, job_id, "pending one", approval_id="ap-1")
    approvals_module.open_approval(store, job_id, "decided one", approval_id="ap-2")
    approvals_module.decide(store, job_id, "ap-2", "approve", request_id="req-1")
    capsys.readouterr()

    rc = cli.main(["approvals", job_id, "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    statuses = {r["id"]: r["status"] for r in payload}
    assert statuses == {"ap-1": "pending", "ap-2": "approved"}


def test_pause_resume_stop_go_through_api_and_keep_old_output_shape(steward_home, capsys):
    # SP-36 (14-01/NB-03): `new` already leaves the job CONTRACTED (even for
    # a bogus --repo), so only the CONTRACTED -> RUNNING transition is left
    # to do here.
    job_id = _new_job_id(capsys)
    store = Store(steward_home)
    store.set_state(
        job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code
    )
    capsys.readouterr()

    rc = cli.main(["pause", job_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "accepted=True" in out
    assert "revision=" in out  # api.py's receipt dict always carries revision

    rc = cli.main(["resume", job_id])
    assert rc == 0
    capsys.readouterr()

    rc = cli.main(["stop", job_id])
    assert rc == 0


def test_mcp_token_create_prints_a_token_once(steward_home, capsys):
    rc = cli.main(["mcp", "token", "create"])

    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("token: ")
    plaintext = out.splitlines()[0].removeprefix("token: ")
    assert len(plaintext) > 20
    on_disk = (steward_home / "mcp_tokens.json").read_text()
    assert plaintext not in on_disk  # only the hash is ever persisted


def test_mcp_token_create_with_scopes_and_list_and_revoke(steward_home, capsys):
    rc = cli.main(["mcp", "token", "create", "--scopes", "runner:read,runner:steer"])
    assert rc == 0
    out = capsys.readouterr().out
    token_id = out.splitlines()[1].split()[1]

    rc = cli.main(["mcp", "token", "list", "--json"])
    assert rc == 0
    entries = json.loads(capsys.readouterr().out)
    assert entries[0]["scopes"] == ["runner:read", "runner:steer"]

    rc = cli.main(["mcp", "token", "revoke", token_id])
    assert rc == 0
    assert "revoked" in capsys.readouterr().out


def test_cli_phone_questions_flags(steward_home, capsys, monkeypatch):
    from steward import phone

    store = Store()
    job = store.create_job("ask things", "/tmp/somerepo")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w4Q")
    assert cli.main(["phone", "--questions", job.job_id, "--open", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"? questions {job.job_id[:8]}" in out and "herdr tab create" in out

    assert cli.main(["phone", "--questions", "nosuchjob", "--open", "--dry-run"]) == 1
    assert "unknown job" in capsys.readouterr().err

    seen = []
    monkeypatch.setattr(phone, "run_tui", lambda questions_job=None: seen.append(questions_job) or 7)
    assert cli.main(["phone", "--questions", job.job_id[:6]]) == 7  # a prefix resolves
    assert seen == [job.job_id]
    assert cli.main(["phone"]) == 7 and seen[-1] is None  # plain `steward phone` is unchanged
