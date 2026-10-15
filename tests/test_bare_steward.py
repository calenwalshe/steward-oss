"""Phase 19-07 (G3, G8, G17): what bare `steward` does, package mode versus repo mode, on a fake Herdr."""

from __future__ import annotations

import json

import pytest

from fake_herdr import FakeHerdr
from steward import cli, installstate, intro, prereq, scaffold, version as version_module
from test_intro import FakeTerminal, with_optional


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    """Package mode (STEWARD_INSTALL_ROOT set), inside a fake Herdr pane, on a terminal, with a throwaway HOME."""
    work = tmp_path / "proj"
    work.mkdir()
    root = tmp_path / "pkg"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(tmp_path / "config" / "exceptions.toml"))
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    monkeypatch.setenv("STEWARD_INSTALL_ROOT", str(root))
    monkeypatch.delenv("STEWARD_TRAJECTORY", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/fake-herdr.sock")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w1")
    monkeypatch.chdir(work)
    fake = FakeHerdr("w1")
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: fake.api)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: fake.runner)
    state = {"checks": with_optional()}
    monkeypatch.setattr(prereq, "detect", lambda *a, **k: list(state["checks"]))
    monkeypatch.setattr(scaffold, "control_idle", lambda ctx, pane: True)
    monkeypatch.setattr(scaffold, "advisor_idle", lambda ctx, pane: True)
    monkeypatch.setattr(scaffold, "_sleep", lambda s: None)
    monkeypatch.setattr(cli, "_init_isatty", lambda: True)
    monkeypatch.setattr(cli, "_init_osatty", lambda: True)
    terms = []

    def terminal():
        term = FakeTerminal(terms.pop(0) if terms else [])
        pkg_.last = term
        return term

    class P:
        pass

    pkg_ = P()
    pkg_.tmp, pkg_.work, pkg_.fake, pkg_.state, pkg_.terms = tmp_path, work, fake, state, terms
    pkg_.home, pkg_.config = tmp_path / "state", tmp_path / "config"
    pkg_.manifest = lambda: json.loads((pkg_.home / "install.json").read_text())
    monkeypatch.setattr(cli, "_terminal", terminal)
    return pkg_


def seen_manifest(pkg, intro_seen, version="0.1.0"):
    state = installstate.new_state(version, str(pkg.tmp / "pkg"))
    state["intro_seen"] = intro_seen
    installstate.save(state, pkg.home)


def test_first_run_in_herdr_shows_the_intro(pkg, monkeypatch):
    calls = []
    monkeypatch.setattr(intro, "run", lambda term, ctx: calls.append((term, ctx)) or 5)
    assert cli.main([]) == 5  # the intro's exit code is bare steward's
    (term, ctx), = calls
    assert isinstance(term, FakeTerminal) and ctx.sock == "/tmp/fake-herdr.sock" and ctx.workspace == "w1"
    assert ctx.cwd == str(pkg.work)
    assert not pkg.home.exists() and pkg.fake.mutating_calls() == []


def test_seen_install_prints_help(pkg, monkeypatch, capsys):
    seen_manifest(pkg, version_module.version())
    monkeypatch.setattr(intro, "run", lambda *a: pytest.fail("the intro must not run again"))
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: steward") and "doctor" in out and "intro" in out
    assert pkg.fake.calls == []  # no Herdr call at all

    # the same at an older MINOR version: still no intro
    major = version_module.version().split(".")[0]
    seen_manifest(pkg, f"{major}.0.0")
    assert cli.main([]) == 0


def test_outside_herdr_prints_help_and_hint(pkg, monkeypatch, capsys):
    monkeypatch.delenv("HERDR_ENV")
    monkeypatch.setattr(intro, "run", lambda *a: pytest.fail("no intro outside Herdr"))
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: steward")
    assert out.rstrip().splitlines()[-1] == "run steward inside a Herdr pane to set up"
    assert out.count("run steward inside a Herdr pane to set up") == 1
    assert not pkg.home.exists() and not pkg.config.exists() and pkg.fake.calls == []


@pytest.mark.parametrize("tty", [(False, True), (True, False), (False, False)])
def test_non_tty_prints_the_text_checklist(pkg, monkeypatch, capsys, tty):
    monkeypatch.setattr(cli, "_init_isatty", lambda: tty[0])
    monkeypatch.setattr(cli, "_init_osatty", lambda: tty[1])
    monkeypatch.setattr(intro, "run", lambda *a: pytest.fail("no intro without a terminal"))
    assert cli.main([]) == 0
    assert capsys.readouterr().out == prereq.render_text(pkg.state["checks"]) + "\n"

    pkg.state["checks"] = with_optional(missing=("git",))
    assert cli.main([]) == 1
    out = capsys.readouterr().out
    assert out == prereq.render_text(pkg.state["checks"]) + "\n" and "✗ git" in out
    assert not pkg.home.exists() and not pkg.config.exists() and pkg.fake.calls == []


def test_the_text_path_is_what_init_yes_prints(pkg, capsys):
    text = prereq.render_text(pkg.state["checks"])
    assert cli.main(["init", "--yes"]) == 0
    assert capsys.readouterr().out.startswith(text + "\n")

    pkg.state["checks"] = with_optional(missing=("claude",))
    assert cli.main(["init", "--yes"]) == 1
    assert prereq.render_text(pkg.state["checks"]) in capsys.readouterr().out


def test_repo_mode_is_unchanged(pkg, monkeypatch, capsys):
    monkeypatch.delenv("STEWARD_INSTALL_ROOT")
    monkeypatch.setattr(intro, "run", lambda *a: pytest.fail("repo mode has no intro"))
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    assert "the following arguments are required: command" in capsys.readouterr().err
    assert not pkg.home.exists()


def test_corrupt_install_json_is_an_error(pkg, capsys):
    pkg.home.mkdir(mode=0o700)
    (pkg.home / "install.json").write_text("{not json")
    assert cli.main([]) == 2
    assert "install.json" in capsys.readouterr().err


def test_first_run_then_help(pkg, capsys):
    pkg.terms.append(["enter", "enter"])
    assert cli.main([]) == 0
    assert pkg.fake.tabs_labelled("steward") and pkg.fake.tabs_labelled("cockpit")
    assert pkg.manifest()["intro_seen"] == version_module.version()
    capsys.readouterr()
    assert cli.main([]) == 0  # the second bare `steward` is help
    assert capsys.readouterr().out.startswith("usage: steward")


def test_q_leaves_no_trace_and_the_intro_shows_again(pkg, capsys):
    pkg.terms.append(["q"])
    assert cli.main([]) == 0
    assert not pkg.home.exists() and pkg.fake.mutating_calls() == []
    pkg.terms.append(["q"])
    assert cli.main([]) == 0
    assert "will create:" in pkg.last.text(0)  # still the first run


def test_major_change_shows_whatsnew_then_upgrades(pkg, monkeypatch):
    pkg.terms.append(["enter", "enter"])
    assert cli.main([]) == 0
    state = pkg.manifest()
    state["intro_seen"], state["version"] = "0.9.0", "0.9.0"
    installstate.save(state, pkg.home)
    monkeypatch.setattr(version_module, "version", lambda *a, **k: "1.0.0")
    restarts = len(pkg.fake.of("pane", "send-keys"))

    pkg.terms.append(["enter", "enter", "enter"])
    assert cli.main([]) == 0
    page, welcome, done = pkg.last.frames
    assert "What's new in Steward 1.0.0" in pkg.last.text(0) and "replaces 0.9.0" in pkg.last.text(0)
    assert "Steward 1.0.0" in pkg.last.text(1) and "after the upgrade from 0.9.0 to 1.0.0" in pkg.last.text(1)
    assert len(pkg.fake.of("pane", "send-keys")) > restarts  # control, advisor and dash restarted in place
    assert pkg.manifest()["version"] == "1.0.0" and pkg.manifest()["intro_seen"] == "1.0.0"
    assert len(pkg.fake.tabs_labelled("steward")) == 1 and len(pkg.fake.tabs_labelled("cockpit")) == 1

    # the next bare `steward` is help again
    assert cli.main([]) == 0
