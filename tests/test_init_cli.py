"""Phase 19-05 (G3, G4, G6, G10): `steward init` on a fake Herdr, in a throwaway HOME."""

from __future__ import annotations

import json
import stat

import pytest

from fake_herdr import FakeHerdr
from steward import cli, prereq
from test_scaffold import checks


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp HOME, STEWARD_HOME and config dir (none of them created yet), a fake Herdr, faked prerequisites."""
    work = tmp_path / "proj"
    work.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(tmp_path / "config" / "exceptions.toml"))
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    monkeypatch.delenv("STEWARD_INSTALL_ROOT", raising=False)
    monkeypatch.delenv("STEWARD_TRAJECTORY", raising=False)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/fake-herdr.sock")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w1")
    monkeypatch.chdir(work)
    fake = FakeHerdr("w1")
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: fake.api)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: fake.runner)
    state = {"checks": checks()}
    monkeypatch.setattr(prereq, "detect", lambda *a, **k: state["checks"])

    class Env:
        pass

    e = Env()
    e.tmp, e.work, e.fake, e.state = tmp_path, work, fake, state
    e.home, e.config = tmp_path / "state", tmp_path / "config"
    return e


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_init_yes_first_run(env, capsys):
    assert cli.main(["init", "--yes"]) == 0
    out = capsys.readouterr().out
    fake = env.fake

    assert _mode(env.home) == 0o700 and _mode(env.config) == 0o700
    assert 'name = "Sam"' in (env.config / "steward.toml").read_text()
    assert (env.config / "trajectory.toml").exists()
    assert (env.config / "exceptions.toml").exists()

    (steward_tab,) = fake.tabs_labelled("steward")
    control, advisor = fake.by_role("control"), fake.by_role("advisor")
    assert control["tab_id"] == steward_tab["tab_id"] and advisor["tab_id"] == steward_tab["tab_id"]
    assert advisor["split_from"] == control["pane_id"]
    assert control["label"] == "steward control" and advisor["label"] == "steward advisor"

    (cockpit_tab,) = fake.tabs_labelled("cockpit")
    operator, dash = fake.pane_by_label("operator"), fake.pane_by_label("steward dash")
    assert operator["tab_id"] == dash["tab_id"] == cockpit_tab["tab_id"]
    assert dash["split_from"] == operator["pane_id"] and dash["direction"] == "right"
    assert operator["foreground_cwd"] == str(env.work)
    runs = fake.runs()
    assert "claude" in runs[operator["pane_id"]][0]
    assert " steward.cli dash" in runs[dash["pane_id"]][0]

    manifest = json.loads((env.home / "install.json").read_text())
    for key in ("version", "install_root", "tabs", "template_hashes", "python", "installed_at", "updated_at", "files"):
        assert key in manifest, key
    assert manifest["tabs"] == ["steward", "cockpit"]
    assert set(manifest["template_hashes"]) == {"steward.toml", "trajectory.toml", "roster.toml"}
    assert _mode(env.home / "install.json") == 0o600

    assert "created" in out and "started" in out
    assert f"control pane started: {control['pane_id']}" in out
    assert f"advisor pane started: {advisor['pane_id']}" in out
    assert f"dash pane started: {dash['pane_id']}" in out
    last = out.strip().splitlines()[-1]
    assert "Run Steward on <repo>: <goal>" in last and "operator pane" in last and "cockpit" in last


def test_init_dry_run_touches_nothing(env, capsys):
    assert cli.main(["init", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Steward will set up" in out and "tab `steward`" in out and "tab `cockpit`" in out
    assert not env.home.exists() and not env.config.exists()
    assert not (env.tmp / "home").exists()
    assert env.fake.mutating_calls() == []

    env.state["checks"] = checks(missing=("git",))
    assert cli.main(["init", "--dry-run"]) == 1
    assert "git" in capsys.readouterr().out
    assert not env.home.exists() and not env.config.exists()
    assert env.fake.mutating_calls() == []


def test_init_refuses_on_missing_required(env, capsys):
    env.state["checks"] = checks(missing=("claude",))
    assert cli.main(["init", "--yes"]) == 1
    out = capsys.readouterr().out
    assert prereq.render_text(env.state["checks"]) in out
    assert not env.home.exists() and not env.config.exists()
    assert env.fake.mutating_calls() == []


def test_init_needs_yes_off_a_tty(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_init_isatty", lambda: False)
    assert cli.main(["init"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert not env.home.exists() and env.fake.mutating_calls() == []

    # a terminal on stdin alone is not enough: the intro needs a screen too
    monkeypatch.setattr(cli, "_init_isatty", lambda: True)
    monkeypatch.setattr(cli, "_init_osatty", lambda: False)
    assert cli.main(["init"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert not env.home.exists() and env.fake.mutating_calls() == []


def test_init_on_a_tty_opens_the_intro(env, monkeypatch, capsys):
    from steward import intro

    monkeypatch.setattr(cli, "_init_isatty", lambda: True)
    monkeypatch.setattr(cli, "_init_osatty", lambda: True)
    opened = []

    class Term:
        def __enter__(self):
            opened.append("enter")
            return self

        def __exit__(self, *exc):
            opened.append("exit")

    monkeypatch.setattr(cli, "_terminal", lambda: Term())
    calls = []
    monkeypatch.setattr(intro, "run", lambda term, ctx: calls.append((term, ctx)) or 0)
    assert cli.main(["init"]) == 0
    (term, ctx), = calls
    assert isinstance(term, Term) and opened == ["enter", "exit"]
    assert ctx.sock == "/tmp/fake-herdr.sock" and ctx.workspace == "w1" and ctx.cwd == str(env.work)
    assert ctx.new_workspace is False and ctx.no_operator is False
    assert capsys.readouterr().out == ""  # the intro draws; init prints no plain prompt of its own
    assert not env.home.exists() and env.fake.mutating_calls() == []

    calls.clear()
    assert cli.main(["init", "--no-operator", "--new-workspace"]) == 0
    assert calls[0][1].no_operator is True and calls[0][1].new_workspace is True

    # --yes and --dry-run never open it, even on a terminal
    calls.clear()
    assert cli.main(["init", "--dry-run"]) == 0
    assert cli.main(["init", "--yes"]) == 0
    assert calls == []


def test_init_yes_prints_the_checklist_before_it_acts(env, capsys):
    assert cli.main(["init", "--yes"]) == 0
    out = capsys.readouterr().out
    text = prereq.render_text(env.state["checks"])
    assert out.startswith(text + "\n")
    assert out.index(text) < out.index("created")


def test_init_no_operator_flag(env, capsys):
    assert cli.main(["init", "--yes", "--no-operator"]) == 0
    out = capsys.readouterr().out
    fake = env.fake
    (cockpit_tab,) = fake.tabs_labelled("cockpit")
    in_tab = fake.panes_in(cockpit_tab["tab_id"])
    assert [p["label"] for p in in_tab] == ["steward dash"]
    assert in_tab[0]["split_from"] is None  # the dash pane is the tab's root pane
    assert fake.pane_by_label("operator") is None
    assert "operator skipped (--no-operator)" in out


@pytest.mark.parametrize("flags", [[], ["--no-operator"]])
def test_init_without_claude_is_refused_either_way(env, capsys, flags):
    env.state["checks"] = checks(missing=("claude",))
    assert cli.main(["init", "--yes", *flags]) == 1
    assert "claude CLI" in capsys.readouterr().out
    assert not env.home.exists() and env.fake.mutating_calls() == []


def test_init_needs_a_herdr_socket(env, monkeypatch, capsys):
    monkeypatch.delenv("HERDR_SOCKET_PATH")
    assert cli.main(["init", "--yes"]) == 2
    assert "HERDR_SOCKET_PATH" in capsys.readouterr().err
    assert not env.home.exists()


def test_init_new_workspace(env, capsys):
    assert cli.main(["init", "--yes", "--new-workspace"]) == 0
    fake = env.fake
    (ws,) = [w for w in fake.workspaces if w["label"] == "steward"]
    (cockpit_tab,) = fake.tabs_labelled("cockpit")
    (steward_tab,) = fake.tabs_labelled("steward")
    assert cockpit_tab["workspace_id"] == ws["workspace_id"] == steward_tab["workspace_id"]
    assert fake.by_role("control")["workspace_id"] == ws["workspace_id"]
    assert not [t for t in fake.tabs if t["workspace_id"] == "w1"]
    operator, dash = fake.pane_by_label("operator"), fake.pane_by_label("steward dash")
    assert operator["split_from"] is None and dash["split_from"] == operator["pane_id"]
    runs = fake.runs()
    assert len(runs[operator["pane_id"]]) == 1 and "claude" in runs[operator["pane_id"]][0]
    assert len(runs[dash["pane_id"]]) == 1
    assert _mode(env.home) == 0o700
    out = capsys.readouterr().out
    assert f"operator pane started: {operator['pane_id']}" in out and "cockpit tab created" in out

    assert cli.main(["init", "--yes", "--new-workspace"]) == 0
    assert len([w for w in fake.workspaces if w["label"] == "steward"]) == 1
    assert len(fake.tabs_labelled("cockpit")) == 1 and len(fake.tabs_labelled("steward")) == 1


def test_init_failing_step_does_not_stamp(env, monkeypatch, capsys):
    real = env.fake._handle

    def broken(argv, cwd):
        if argv[1:3] == ["tab", "create"] and "cockpit" in argv:
            env.fake.calls.append(list(argv))
            from steward.runner_exec import CommandResult
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=1, stdout="", stderr="boom", duration_s=0.0)
        return real(argv, cwd)

    env.fake.runner.responses = broken
    assert cli.main(["init", "--yes"]) == 1
    captured = capsys.readouterr()
    assert "boom" in captured.err
    assert not (env.home / "install.json").exists()
    assert env.fake.by_role("control") is not None  # earlier steps ran; a re-run finishes the job

    env.fake.runner.responses = real
    assert cli.main(["init", "--yes"]) == 0
    assert (env.home / "install.json").exists()
    assert len(env.fake.tabs_labelled("steward")) == 1
