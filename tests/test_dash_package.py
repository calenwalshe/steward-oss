"""Phase 19-04 (G4): the status pane lives in the package and renders from package files only."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from steward import cli, version
from steward.models import JobState, Judgment
from steward.store import Store

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "steward" / "templates" / "cockpit" / "steward-dash.sh"
LEGACY_OWNER = "Cal" + "en"  # spelled in two parts so this file is not itself a marker


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HERDR_", "STEWARD_", "CLAUDE_")) and k != "TYPESAFE_API_KEY"}
    env.update({
        "STEWARD_HOME": str(tmp_path / "home"),
        "STEWARD_CONFIG_DIR": str(tmp_path / "config"),
        "STEWARD_OWNER_NAME": LEGACY_OWNER,
        "STEWARD_HEALTH": "0",
        "STEWARD_RESOLVER": "0",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(REPO),
    })
    env.update(extra)
    return env


def _run(argv, env, cwd=REPO):
    return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=120)


def _seed_running_job(home: Path) -> None:
    store = Store(home)
    job = store.create_job("ship the thing", str(home))
    store.set_state(job.job_id, JobState.CONTRACTED, what="contracted", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.RUNNING, what="running", evidence={}, rule="t", judgment=Judgment.code)
    with store._conn:
        store._conn.execute("UPDATE jobs SET created_at='2020-01-01T00:00:00+00:00', updated_at='2020-01-01T00:00:00+00:00'")


@pytest.mark.parametrize("seeded", [False, True])
def test_dash_jobs_module_matches_the_repo_renderer(tmp_path, seeded):
    env = _env(tmp_path)
    if seeded:
        _seed_running_job(tmp_path / "home")
    package = _run([sys.executable, "-m", "steward.dash_jobs"], env)
    legacy = _run([sys.executable, str(REPO / "deploy" / "cockpit" / "steward-dash-jobs.py")], env)
    assert package.returncode == 0, package.stderr
    assert legacy.returncode == 0, legacy.stderr
    # Relative ages ("3s ago") can tick over between the two runs; everything else must match byte for byte.
    ages = re.compile(r"\d+[smhd] ago")
    assert ages.sub("N ago", package.stdout) == ages.sub("N ago", legacy.stdout)
    if seeded:
        assert "ship the thing" in package.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash missing")
def test_steward_dash_once_renders_a_frame(tmp_path):
    env = _env(tmp_path)
    res = _run([sys.executable, "-m", "steward.cli", "dash", "--once"], env)
    assert res.returncode == 0, res.stderr
    assert "STEWARD" in res.stdout.splitlines()[0]
    assert "public connector" in res.stdout and "off (no public connector)" in res.stdout
    for seq in ("\x1b[?1049", "\x1b[?1000", "\x1b[?1006", "\x1b[?25"):
        assert seq not in res.stdout


def test_template_script_has_no_personal_or_repo_paths():
    text = TEMPLATE.read_text()
    for marker in (LEGACY_OWNER, LEGACY_OWNER.lower(), "cairn" "labs", "/home/", "work-2026", "$HOME/bin", "steward-mcp."):
        assert marker not in text, marker
    if shutil.which("bash"):
        assert subprocess.run(["bash", "-n", str(TEMPLATE)], capture_output=True).returncode == 0


def test_dash_module_has_no_personal_markers():
    text = (REPO / "steward" / "dash_jobs.py").read_text()
    for marker in (LEGACY_OWNER, "cairn" "labs", "/home/", "work-2026", "$HOME/bin"):
        assert marker not in text, marker


def test_dash_command_execs_bash_with_the_right_env(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(os, "execvp", lambda f, argv: calls.append((f, list(argv), dict(os.environ))))
    for name in ("STEWARD_PYTHON", "STEWARD_INSTALL_ROOT", "STEWARD_DASH_ONCE", "STEWARD_PUBLIC_URL"):
        monkeypatch.setenv(name, "x")  # recorded, so the test restores them
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(cli, "Store", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Store for dash")))
    # plan item 9 (2026-10-05): `steward dash` execs the resident pane; the bash loop is the STEWARD_DASH_LEGACY=1
    # fallback (tests/test_perf_followup.py covers the resident path)
    monkeypatch.setenv("STEWARD_DASH_LEGACY", "1")
    monkeypatch.setattr(os, "execv", lambda f, argv: calls.append(("execv", list(argv), dict(os.environ))))

    cli.main(["dash"])
    f, argv, env = calls[-1]
    assert f == "bash" and argv == ["bash", str(TEMPLATE)]
    assert env["STEWARD_PYTHON"] == sys.executable
    assert env["STEWARD_INSTALL_ROOT"] == str(version.install_root())
    assert "STEWARD_DASH_ONCE" not in env
    assert env["STEWARD_PUBLIC_URL"] == ""

    cli.main(["dash", "--once"])
    assert calls[-1][2]["STEWARD_DASH_ONCE"] == "1"


def test_public_url_comes_only_from_mcp_json(monkeypatch, tmp_path):
    from steward import dash_services

    cfg = tmp_path / "config"
    cfg.mkdir()
    assert dash_services.public_probe_url(cfg) == ""
    (cfg / "mcp.json").write_text(json.dumps({"public_base_url": "https://connector.example.org", "port": 9520}))
    assert dash_services.public_probe_url(cfg) == "https://connector.example.org/.well-known/oauth-protected-resource/mcp"
    (cfg / "mcp.json").write_text("{not json")
    assert dash_services.public_probe_url(cfg) == ""


def test_deploy_files_are_untouched():
    if shutil.which("git") is None:
        pytest.skip("git missing")
    # re-pinned 2026-10-06 from d837857 to 92ada94, the last job f257aa7b commit that amended OPERATOR.md (control code-sha
    # check, verified deploy-self reload, runners wait for a self-merge's deploy), then to c9359b5 (phase 41 merge), to
    # the merge of fleet-registry into the issue M replace-link branch (f96217d) and to the merge of fleet-registry into
    # the Phase 42 branch (96aee55, was 7329210); the pin moves with each deliberate edit
    # coordinator Task 14: OPERATOR.md re-pinned (was 73bef77) to the commit that added the Coordinator section
    pin = "292fd76"  # OPERATOR.md Coordinator section; earlier zero-touch Task 18, fleet-registry, issue M and Phase 42
    # re-pinned 2026-10-07 from 92ada94 to 3b57f58: issue W adds deploy/steward-watchdog.service and its wrapper and
    # dry-run installer (new files; no existing deploy file or OPERATOR.md line changed); then to be81c05, the merge of
    # issue W with fleet-registry. Manual merge 2026-10-08 (job 15c5c684): OPERATOR.md is held at fleet-registry's pin
    # and deploy/ at issue W's, since no single earlier commit holds both
    # coordinator Task 13: deploy/ re-pinned to the commit that added the coordinator units (new files only)
    # coordinator final-review fix wave: deploy/ re-pinned to the commit that added the nvm PATH to the wrapper
    # OSS scrub 2026-10-14: both re-pinned to 292fd76, which neutralised hostnames, paths and the owner name only
    deploy_pin = "292fd76"
    known = subprocess.run(["git", "cat-file", "-e", f"{pin}^{{commit}}"], cwd=REPO, capture_output=True)
    if known.returncode != 0:
        pytest.skip(f"commit {pin} not available")
    res = subprocess.run(["git", "diff", "--quiet", pin, "--", "docs/OPERATOR.md"], cwd=REPO)
    assert res.returncode == 0
    known = subprocess.run(["git", "cat-file", "-e", f"{deploy_pin}^{{commit}}"], cwd=REPO, capture_output=True)
    if known.returncode != 0:
        pytest.skip(f"commit {deploy_pin} not available")
    res = subprocess.run(["git", "diff", "--quiet", deploy_pin, "--", "deploy"], cwd=REPO)
    assert res.returncode == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash missing")
def test_dash_once_shows_the_services_block_without_install_json(tmp_path):
    env = _env(tmp_path, STEWARD_DASH_UNITS="")  # no unit probing of the real machine
    res = _run([sys.executable, "-m", "steward.cli", "dash", "--once"], env)
    assert res.returncode == 0, res.stderr
    assert "off (no public connector)" in res.stdout
    assert "off (workers run without them)" in res.stdout or "GSD / superpowers" in res.stdout
    assert "off (no cloudflared)" in res.stdout or "cloudflared" in res.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash missing")
def test_dash_once_shows_the_services_block_with_a_minimal_install_json(tmp_path):
    from steward import installstate

    state = installstate.new_state("0.1.0", str(REPO), python="3.12.0", now="2026-01-01T00:00:00+00:00")
    state["components"] = {"mcp": {"enabled": True, "mode": "herdr", "units": []}}
    installstate.save(state, tmp_path / "home")
    env = _env(tmp_path)
    res = _run([sys.executable, "-m", "steward.cli", "dash", "--once"], env)
    assert res.returncode == 0, res.stderr
    plain = re.sub(r"\x1b\[[0-9;]*m", "", res.stdout)
    assert re.search(r"● mcp\s+\(services tab\)", plain), plain
    assert "steward-mcp-tunnel-local" not in plain  # the recorded install replaces the legacy trio
    assert "public connector" in plain and "off (no public connector)" in plain
