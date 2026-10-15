"""Phase 19-05 (G18): a package install never scaffolds into, adopts or disturbs a workspace that runs another
install (for example the owner's live repo cockpit). Fake Herdr, temp dirs, package mode via STEWARD_INSTALL_ROOT."""

from __future__ import annotations

import json
import os

import pytest

from fake_herdr import FakeHerdr
from steward import cli, installstate, prereq
from test_scaffold import checks

FOREIGN = "/some/repo/checkout"
WAYS_OUT = ("herdr --session <name>", "STEWARD_HOME", "stop the other cockpit")


@pytest.fixture
def env(tmp_path, monkeypatch):
    work = tmp_path / "proj"
    work.mkdir()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (tmp_path / "pkglink").symlink_to(pkg)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(tmp_path / "config" / "exceptions.toml"))
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    monkeypatch.setenv("STEWARD_INSTALL_ROOT", str(tmp_path / "pkglink"))
    monkeypatch.delenv("STEWARD_TRAJECTORY", raising=False)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/fake-herdr.sock")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w1")
    monkeypatch.chdir(work)
    fake = FakeHerdr("w1")
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: fake.api)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: fake.runner)
    monkeypatch.setattr(prereq, "detect", lambda *a, **k: checks())

    class Env:
        pass

    e = Env()
    e.tmp, e.work, e.fake, e.pkg = tmp_path, work, fake, pkg
    e.home, e.config = tmp_path / "state", tmp_path / "config"
    return e


def _live_control(fake, cwd):
    _tab, (pane,) = fake.add_tab("w1", "steward", panes=("steward control",))
    fake.set_live(pane, role="control", argv=["python3", "-m", "steward.cli", "control"], foreground_cwd=cwd)
    return pane


def _untouched(env):
    assert not env.home.exists() and not env.config.exists()
    assert env.fake.mutating_calls() == []


def test_live_repo_cockpit_is_refused(env, capsys):
    pane = _live_control(env.fake, FOREIGN)
    assert cli.main(["init", "--yes"]) == 1
    out = capsys.readouterr().out
    assert pane in out and FOREIGN in out and "will not scaffold" in out
    for way in WAYS_OUT:
        assert way in out, way
    _untouched(env)


def test_same_root_is_fine(env, capsys):
    pane = _live_control(env.fake, str(env.pkg))  # the real path; STEWARD_INSTALL_ROOT is a symlink to it
    assert cli.main(["init", "--yes"]) == 0
    assert f"control pane already running: {pane}" in capsys.readouterr().out
    assert len(env.fake.tabs_labelled("steward")) == 1
    assert pane not in env.fake.runs()
    assert (env.home / "install.json").exists()


def test_npm_replaced_root_is_this_installs_predecessor(env, capsys):
    """`npm install -g` over a running install renames the old directory to `.<name>-<random>` and deletes it, so the
    control pane's cwd reads `<scope>/.<name>-XXXX (deleted)`. That is this install's own predecessor, not another
    install (19-09: the live upgrade test caught the refusal)."""
    old = env.pkg.parent / f".{env.pkg.name}-Ab12Cd34"
    pane = _live_control(env.fake, f"{old} (deleted)")
    assert cli.main(["init", "--yes"]) == 0, capsys.readouterr().out
    assert "will not scaffold" not in capsys.readouterr().out
    assert len(env.fake.tabs_labelled("steward")) == 1
    assert env.fake.by_role("control")["pane_id"] == pane


def test_deleted_root_elsewhere_is_still_foreign(env, capsys):
    # deleted, but not in this install's own directory
    _live_control(env.fake, f"{env.tmp / 'elsewhere' / ('.' + env.pkg.name + '-Ab12Cd34')} (deleted)")
    assert cli.main(["init", "--yes"]) == 1
    assert "will not scaffold" in capsys.readouterr().out
    _untouched(env)


def test_deleted_sibling_without_the_staging_name_is_still_foreign(env, capsys):
    _live_control(env.fake, f"{env.pkg.parent / 'someone-else'} (deleted)")
    assert cli.main(["init", "--yes"]) == 1
    assert "will not scaffold" in capsys.readouterr().out
    _untouched(env)


def test_install_json_of_another_root_is_refused(env, capsys):
    other = env.tmp / "other"
    other.mkdir()
    state = installstate.new_state("0.1.0", other)
    installstate.save(state, env.home)
    before = (env.home / "install.json").read_text()
    assert cli.main(["init", "--yes"]) == 1
    out = capsys.readouterr().out
    assert f"managed by the install at {other}" in out and "will not scaffold" in out
    for way in WAYS_OUT:
        assert way in out, way
    assert (env.home / "install.json").read_text() == before
    assert not env.config.exists() and env.fake.mutating_calls() == []

    other.rmdir()  # the other install is gone: its manifest is stale
    assert cli.main(["init", "--yes"]) == 0
    out = capsys.readouterr().out
    assert f"{other}" in out and "no longer exists" in out
    assert json.loads((env.home / "install.json").read_text())["install_root"] == str(env.tmp / "pkglink")


def test_package_mode_up_and_cockpit_need_init(env, monkeypatch, capsys):
    for verb in (["up"], ["cockpit"]):
        assert cli.main(verb) == 2
        assert "steward init" in capsys.readouterr().err
    _untouched(env)

    # installed here, but a foreign control pane is live: the same message init gives
    assert cli.main(["init", "--yes"]) == 0
    capsys.readouterr()
    for pane in env.fake.panes:  # our own panes stop; a foreign cockpit comes up in the same session
        pane["tokens"].pop("steward_role", None)
    foreign = _live_control(env.fake, FOREIGN)
    n = len(env.fake.calls)
    assert cli.main(["init", "--yes"]) == 1
    init_msg = capsys.readouterr().out
    for verb in (["up"], ["cockpit"]):
        assert cli.main(verb) == 1
        err = capsys.readouterr().err
        assert foreign in err and FOREIGN in err
        body = err[err.index("A Steward control pane"):].strip()
        assert body in init_msg
    assert [c for c in env.fake.mutating_calls() if c in env.fake.calls[n:]] == []

    # repo mode: both behave as before (no install.json needed, no guard)
    monkeypatch.delenv("STEWARD_INSTALL_ROOT")
    fresh = FakeHerdr("w9")
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: fresh.api)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: fresh.runner)
    monkeypatch.setenv("STEWARD_HOME", str(env.tmp / "repo-state"))
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w9")
    assert cli.main(["up"]) == 0
    assert "control pane started" in capsys.readouterr().out
    assert cli.main(["cockpit"]) == 0
    assert fresh.tabs_labelled("cockpit")
    assert not (env.tmp / "repo-state" / "install.json").exists()


def test_dry_run_shows_the_refusal(env, capsys):
    _live_control(env.fake, FOREIGN)
    assert cli.main(["init", "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "Steward will not set up this workspace" in out and FOREIGN in out
    _untouched(env)


def test_no_control_pane_anywhere_is_not_foreign(env, capsys):
    assert cli.main(["init", "--yes"]) == 0
    assert "control pane started" in capsys.readouterr().out
    control = env.fake.by_role("control")
    assert os.path.realpath(control["foreground_cwd"]) in (os.path.realpath(env.pkg),
                                                          os.path.realpath(cli.launch._STEWARD_ROOT))
    assert cli.main(["init", "--yes"]) == 0  # its own cockpit is never foreign
    assert cli.main(["up"]) == 0
    assert cli.main(["cockpit"]) == 0
    assert len(env.fake.tabs_labelled("steward")) == 1 and len(env.fake.tabs_labelled("cockpit")) == 1


def test_a_foreign_store_is_never_opened(env, monkeypatch, capsys):
    """Refusing happens before the store of the other install is opened (a Store() may migrate its schema)."""
    env.home.mkdir()
    (env.home / "steward.db").write_bytes(b"")
    _live_control(env.fake, FOREIGN)
    import steward.store as store_module

    opened = []
    real = store_module.Store
    monkeypatch.setattr(store_module, "Store", lambda *a, **k: opened.append(a) or real(*a, **k))
    assert cli.main(["init", "--yes"]) == 1
    assert "will not scaffold" in capsys.readouterr().out
    assert opened == []

    env.fake.panes.clear()  # the other cockpit is gone: now the (own) store may be read
    (env.home / "steward.db").unlink()
    real(env.home)
    assert cli.main(["init", "--dry-run"]) == 0
    assert opened
