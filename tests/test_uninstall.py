"""Phase 19-06 (G16): `steward uninstall` undoes only what install.json records, and `--purge` needs the typed name.

FakeHerdr + FakeRunner for every Herdr and systemctl call; a real Store with jobs in a temp STEWARD_HOME; temp HOME,
XDG_CONFIG_HOME and config dir. Nothing real is stopped, closed or deleted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from steward import cli, components, installstate, mcp_auth, uninstall
from steward.models import JobState, Judgment
from steward.store import Store
from test_components import Env
from test_runner import _contracted_job


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def _job(env, state, repo="/srv/repo"):
    store = Store(env.state_dir)
    job = _contracted_job(store, repo=repo)
    paths = {"CONTRACTED": [], "RUNNING": ["RUNNING"], "PAUSED": ["RUNNING", "PAUSED"], "STOPPED": ["STOPPED"],
             "NEEDS_HUMAN": ["NEEDS_HUMAN"], "ACCEPTED": ["NEEDS_HUMAN", "ACCEPTED"]}
    for step in paths[state]:
        store.set_state(job.job_id, JobState[step], what="t", evidence={}, rule="t", judgment=Judgment.human)
    return job


def _setup_full_install(env, monkeypatch):
    """mcp (systemd), moshi and android enabled; the four tabs exist; this pane lives in `cockpit`."""
    assert components.enable("mcp", env.ctx(yes=True)) == 0
    assert components.enable("moshi", env.ctx(yes=True)) == 0
    assert components.enable("android", env.ctx(yes=True)) == 0
    tabs = {}
    for label, panes in (("steward", ("steward control", "steward advisor")), ("cockpit", ("operator", "steward dash")),
                         ("services", ("svc x",))):
        tabs[label] = env.fake.add_tab("w1", label, panes=panes)
    job_tab = env.fake.add_tab("w1", "⚙ abcd1234 goal", panes=("runner abcd1234",))
    state = env.state
    state["tabs"] = ["steward", "cockpit", "services", *[t for t in state["tabs"] if t not in ("steward", "cockpit", "services")]]
    env.save_state(state)
    monkeypatch.setenv("HERDR_PANE_ID", tabs["cockpit"][1][0])
    env.out.clear()
    env.misc.clear()
    return tabs, job_tab


def test_no_install_record_does_nothing(env):
    (env.state_dir / "install.json").unlink()
    assert uninstall.uninstall(env.ctx()) == 1
    assert "nothing to uninstall: no package install is recorded here" in env.text()
    assert env.misc == [] and env.fake.calls == []


def test_refuses_while_jobs_run_or_pause(env):
    running, paused = _job(env, "RUNNING"), _job(env, "PAUSED")
    assert uninstall.uninstall(env.ctx()) == 1
    assert running.job_id[:8] in env.text() and paused.job_id[:8] in env.text()
    assert (env.state_dir / "install.json").exists()
    env.out.clear()
    assert uninstall.uninstall(env.ctx(), force=True) == 0
    assert not (env.state_dir / "install.json").exists()


def test_other_job_states_do_not_block(env):
    for state in ("CONTRACTED", "STOPPED", "NEEDS_HUMAN", "ACCEPTED"):
        _job(env, state)
    assert uninstall.uninstall(env.ctx()) == 0, env.text()


def test_undoes_the_record_and_keeps_the_rest(env, monkeypatch):
    tabs, job_tab = _setup_full_install(env, monkeypatch)
    job = _job(env, "STOPPED")
    worktree = env.state_dir / "worktrees" / job.job_id
    worktree.mkdir(parents=True)
    (worktree / "work.txt").write_text("keep me")
    (env.config / "keys.env").write_text("X=1\n")
    unit = env.unit_dir / "steward-mcp.service"
    token_id = env.state["components"]["android"]["token_id"]
    seen = {}
    recorded = {tabs[label][0] for label in ("steward", "cockpit", "services")}
    real = env.runner.responses

    def spy(argv, cwd):
        if argv[:3] == ["herdr", "tab", "close"] and argv[3] in recorded and "at_first_close" not in seen:
            seen["at_first_close"] = env.text()
        return real(argv, cwd)

    env.runner.responses = spy
    assert uninstall.uninstall(env.ctx()) == 0, env.text()
    assert not unit.exists()
    assert env.systemctl()[-2:] == ["disable --now steward-mcp", "daemon-reload"]
    assert mcp_auth.list_tokens(home=env.state_dir)[0]["revoked"] is True and mcp_auth.list_tokens(home=env.state_dir)[0]["id"] == token_id
    # tabs: the recorded ones closed, the job's and chat's never; the tab holding this pane last
    remaining = {t["label"] for t in env.fake.tabs}
    assert remaining == {"⚙ abcd1234 goal"}
    closes = [c[3] for c in env.fake.of("tab", "close")]
    assert closes[-1] == tabs["cockpit"][0]
    assert job_tab[0] not in closes
    assert "npm rm -g the package you installed" in seen["at_first_close"]
    assert "uninstalled" in seen["at_first_close"]
    # what is kept
    assert not (env.state_dir / "install.json").exists()
    assert env.config.exists() and (env.config / "keys.env").exists() and env.state_dir.exists()
    assert (worktree / "work.txt").read_text() == "keep me"
    assert not any(a[0] == "git" for a in env.misc)
    assert "kept:" in env.text() and str(worktree.parent) not in env.text()


def test_a_unit_the_user_changed_is_left_and_listed(env):
    assert components.enable("mcp", env.ctx(yes=True)) == 0
    unit = env.unit_dir / "steward-mcp.service"
    unit.write_text(unit.read_text() + "# mine\n")
    env.out.clear()
    assert uninstall.uninstall(env.ctx()) == 0
    assert unit.exists() and "changed since install" in env.text() and str(unit) in env.text()


def test_outside_herdr_the_tabs_are_left_with_a_note(env):
    state = env.state
    state["tabs"] = ["steward", "cockpit"]
    env.save_state(state)
    env.fake.add_tab("w1", "steward", panes=("p",))
    assert uninstall.uninstall(env.ctx(workspace=None)) == 0
    assert "tabs not closed" in env.text() and len(env.fake.tabs) == 1


def test_uninstall_is_idempotent(env):
    assert uninstall.uninstall(env.ctx()) == 0
    env.out.clear()
    assert uninstall.uninstall(env.ctx()) == 1
    assert "nothing to uninstall" in env.text()


def _purge_fixture(env):
    job = _job(env, "STOPPED", repo="/srv/repo")
    worktree = env.state_dir / "worktrees" / job.job_id
    worktree.mkdir(parents=True)
    (worktree / "f").write_text("x")
    return job, worktree


def test_purge_needs_the_typed_name(env):
    job, worktree = _purge_fixture(env)
    # not at a terminal and no --confirm: refused, nothing changed
    assert uninstall.uninstall(env.ctx(), purge=True) == 1
    assert "--confirm state" in env.text()
    assert str(worktree) in env.text()
    assert (env.state_dir / "install.json").exists() and worktree.exists()
    # a wrong string aborts with nothing deleted and nothing undone
    env.out.clear()
    assert uninstall.uninstall(env.ctx(), purge=True, confirm="nope") == 1
    assert "nothing was changed or deleted" in env.text()
    assert (env.state_dir / "install.json").exists() and env.config.exists() and worktree.exists()
    # interactively
    env.out.clear()
    assert uninstall.uninstall(env.ctx(interactive=True, ask=lambda p: "config"), purge=True) == 1
    assert env.state_dir.exists()
    # the right name (here: --confirm) removes both directories
    env.out.clear()
    assert uninstall.uninstall(env.ctx(), purge=True, confirm="state") == 0, env.text()
    assert not env.state_dir.exists() and not env.config.exists()
    assert env.home.exists()  # the user's home is untouched
    text = env.text()
    assert str(env.state_dir) in text and str(env.config) in text and str(worktree) in text
    assert "git -C /srv/repo worktree prune" in text
    assert not any(a[0] == "git" for a in env.misc)  # git is never run, no branch is touched


def test_purge_typed_interactively(env):
    _purge_fixture(env)
    assert uninstall.uninstall(env.ctx(interactive=True, ask=lambda p: "state"), purge=True) == 0
    assert not env.state_dir.exists()


def test_purge_refuses_dangerous_targets(env, tmp_path):
    for bad in (Path("/"), env.home, Path(""), env.home.parent, Path("/tmp")):
        assert uninstall._unsafe_purge_target(bad), bad
    assert uninstall._unsafe_purge_target(env.state_dir) is None
    # and through the verb: STEWARD_HOME that is the user's home directory
    installstate.save(installstate.new_state("0.1.0", "/pkg"), env.home)
    assert uninstall.uninstall(env.ctx(home=env.home), purge=True, confirm="home") == 1
    assert "your home directory" in env.text() and env.home.exists()
    assert (env.home / "install.json").exists()


def test_cli_uninstall(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: env.runner)
    assert cli.main(["uninstall"]) == 0
    assert not (env.state_dir / "install.json").exists()
    assert cli.main(["uninstall"]) == 1
    assert "nothing to uninstall" in capsys.readouterr().out


def test_cli_uninstall_purge_with_confirm(env, monkeypatch):
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: env.runner)
    assert cli.main(["uninstall", "--purge", "--confirm", "state"]) == 0
    assert not env.state_dir.exists()


def test_repo_install_without_a_record_is_never_touched(env, capsys):
    (env.state_dir / "install.json").unlink()
    (env.unit_dir).mkdir(parents=True)
    hand_linked = env.unit_dir / "steward-mcp.service"
    hand_linked.write_text("# hand linked\n")
    assert uninstall.uninstall(env.ctx(), purge=True, confirm="state") == 1
    assert hand_linked.read_text() == "# hand linked\n" and env.state_dir.exists() and env.config.exists()
