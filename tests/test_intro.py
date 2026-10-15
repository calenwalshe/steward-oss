"""Phase 19-07 (G8, G9): the first-run flow driven by a scripted terminal, a fake Herdr and a throwaway HOME."""

from __future__ import annotations

import io
import json
import re
import stat

import pytest

from fake_herdr import FakeHerdr
from steward import cli, components, installstate, intro, prereq, scaffold, version as version_module
from steward.jobview import fit
from steward.runner_exec import CommandResult, FakeRunner
from test_scaffold import checks as required_checks


class FakeTerminal:
    """Keys come from a script; `read_line` from a second one. Every `write` is recorded; a write that starts with the
    clear-screen sequence is one drawn page (`frames`)."""

    def __init__(self, keys, cols=80, rows=24, lines=(), tty=True):
        self.keys, self.typed = list(keys), list(lines)
        self.cols, self.rows, self.tty = cols, rows, tty
        self.writes: list[str] = []
        self.prompts: list[tuple[str, bool]] = []

    def write(self, text):
        self.writes.append(text)

    def read_key(self):
        return self.keys.pop(0) if self.keys else "eof"

    def read_line(self, prompt, secret=False):
        self.prompts.append((prompt, secret))
        return self.typed.pop(0) if self.typed else ""

    def size(self):
        return self.cols, self.rows

    def is_tty(self):
        return self.tty

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    @property
    def frames(self) -> list[list[str]]:
        return [w[len(intro.CLEAR):].split("\n") for w in self.writes if w.startswith(intro.CLEAR)]

    def text(self, n=-1) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(self.frames[n]))


def with_optional(missing=(), **status):
    """Required checks plus the eight optional ones (off unless named in `status`, e.g. jev=OK)."""
    out = required_checks(missing=missing)
    for id_, label, level, _fn in prereq.CHECKS:
        if level != prereq.OPTIONAL:
            continue
        if status.get(id_) == prereq.OK:
            out.append(prereq.Check(id_, label, level, prereq.OK, "on"))
            continue
        fix = (prereq.Fix("local", "add the key to keys.env", "keys_env") if id_ == "jev"
               else prereq.Fix("command", f"install {id_}"))
        out.append(prereq.Check(id_, label, level, prereq.OFF, f"off (no {id_})", fix))
    return out


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    work = tmp_path / "proj"
    work.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(tmp_path / "config" / "exceptions.toml"))
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    monkeypatch.delenv("STEWARD_INSTALL_ROOT", raising=False)
    monkeypatch.delenv("STEWARD_TRAJECTORY", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/fake-herdr.sock")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p0")
    monkeypatch.chdir(work)
    fake = FakeHerdr("w1")
    state = {"checks": with_optional()}
    monkeypatch.setattr(prereq, "detect", lambda *a, **k: list(state["checks"]))
    monkeypatch.setattr(scaffold, "control_idle", lambda ctx, pane: True)
    monkeypatch.setattr(scaffold, "advisor_idle", lambda ctx, pane: True)
    monkeypatch.setattr(scaffold, "_sleep", lambda s: None)

    class Rig:
        pass

    r = Rig()
    r.tmp, r.work, r.fake, r.state = tmp_path, work, fake, state
    r.home, r.config = tmp_path / "state", tmp_path / "config"
    r.uv = {"code": 0, "after": None}  # `after`: called when `uv` runs (a test's way to change the world)

    def handler(argv, cwd):
        if argv[0] == "herdr":
            return fake._handle(argv, cwd)
        if argv[0] == "uv" and r.uv["after"]:
            r.uv["after"]()
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=r.uv["code"] if argv[0] == "uv" else 0, stdout="",
                             stderr="", duration_s=0.0)

    r.runner = FakeRunner(handler)
    r.argvs = lambda: [c["argv"] for c in r.runner.calls]
    r.ctx = intro.Ctx(runner=r.runner, herdr_api=fake.api, sock="/tmp/fake-herdr.sock", workspace="w1",
                      env={"HERDR_PANE_ID": "w1:p0", "HERDR_WORKSPACE_ID": "w1"}, cwd=str(work),
                      which=lambda name: f"/usr/bin/{name}")
    r.manifest = lambda: json.loads((r.home / "install.json").read_text())
    return r


def test_enter_scaffolds_and_marks_intro_seen(rig):
    term = FakeTerminal(["enter", "enter"])
    assert intro.run(term, rig.ctx) == 0
    first, done = term.frames
    assert re.sub(r"\x1b\[[0-9;]*m", "", first[0]).startswith("Steward ") and first[-1].startswith("Enter scaffold")
    assert any("will create:" == ln for ln in first)
    (steward_tab,) = rig.fake.tabs_labelled("steward")
    (cockpit_tab,) = rig.fake.tabs_labelled("cockpit")
    assert rig.fake.by_role("control")["tab_id"] == steward_tab["tab_id"]
    assert rig.fake.pane_by_label("operator")["tab_id"] == cockpit_tab["tab_id"]
    assert "Run Steward on proj: <goal>" in "\n".join(done) and "operator pane" in "\n".join(done)
    assert rig.manifest()["intro_seen"] == version_module.version()
    assert mode(rig.home) == 0o700 and mode(rig.home / "install.json") == 0o600


def test_the_screen_shows_the_plan_enter_runs(rig):
    term = FakeTerminal(["enter", "enter"])
    d, actions = intro._observe(rig.ctx)
    shown = scaffold.render_plan(actions).splitlines()[1:]
    intro.run(term, rig.ctx)
    first = term.text(0)
    assert len(shown) >= 5
    for step in shown:
        assert fit(step, 80) in first
    # every directory the screen promised exists afterwards
    assert rig.home.is_dir() and rig.config.is_dir() and (rig.config / "steward.toml").exists()


def test_q_quits_without_a_trace(rig):
    term = FakeTerminal(["q"])
    assert intro.run(term, rig.ctx) == 0
    assert not rig.home.exists() and not rig.config.exists()
    assert rig.fake.mutating_calls() == []
    assert len(term.frames) == 1
    assert not (rig.tmp / "home").exists()


def test_eof_is_a_quiet_quit(rig):
    assert intro.run(FakeTerminal([]), rig.ctx) == 0
    assert not rig.home.exists() and rig.fake.mutating_calls() == []


def test_nothing_to_create(rig):
    assert intro.run(FakeTerminal(["enter", "enter"]), rig.ctx) == 0
    state = rig.manifest()
    state["intro_seen"] = None
    installstate.save(state, rig.home)
    calls_before = len(rig.fake.mutating_calls())

    term = FakeTerminal(["enter"])
    assert intro.run(term, rig.ctx) == 0
    assert "everything is in place" in term.text(0)
    assert "will create:" not in term.text(0)
    assert len(term.frames) == 1
    assert rig.manifest()["intro_seen"] == version_module.version()
    assert len(rig.fake.mutating_calls()) == calls_before


def test_required_missing_blocks_enter(rig):
    rig.state["checks"] = with_optional(missing=("git",))
    term = FakeTerminal(["enter", "q"])
    assert intro.run(term, rig.ctx) == 1
    assert intro.REQUIRED_FIRST in term.text(0)
    assert intro.REQUIRED_FIRST in term.text(1)
    assert "✗ git" in term.text(0)
    assert not rig.home.exists() and rig.fake.mutating_calls() == []


def test_scaffold_error_is_shown_and_not_recorded(rig, monkeypatch):
    monkeypatch.setattr(scaffold, "apply_scaffold",
                        lambda actions, ctx: {"lines": [], "created": [], "errors": ["dirs: PermissionError"],
                                              "deferred": []})
    term = FakeTerminal(["enter", "enter"])
    assert intro.run(term, rig.ctx) == 1
    assert "error: dirs: PermissionError" in term.text(1)
    assert not (rig.home / "install.json").exists()


class FakeTermios:
    TCSADRAIN, TCIFLUSH = 1, 0

    def __init__(self):
        self.calls = []

    def tcgetattr(self, fd):
        self.calls.append(("get", fd))
        return ["SAVED"]

    def tcsetattr(self, fd, when, attrs):
        self.calls.append(("set", fd, when, attrs))

    def tcflush(self, fd, which):
        self.calls.append(("flush", fd))


class FakeTty:
    def __init__(self, log):
        self.log = log

    def setcbreak(self, fd):
        self.log.append(("cbreak", fd))


def real_terminal(reads=(), **kw):
    tm = FakeTermios()
    data = list(reads)
    term = intro.RealTerminal(fd=7, out=io.StringIO(), termios_mod=tm, tty_mod=FakeTty(tm.calls),
                              read=lambda n: data.pop(0) if data else b"", size=lambda: (100, 40), **kw)
    return term, tm


def test_terminal_restored():
    term, tm = real_terminal()
    with pytest.raises(RuntimeError):
        with term:
            assert ("cbreak", 7) in tm.calls
            raise RuntimeError("boom")
    assert tm.calls[-1] == ("set", 7, FakeTermios.TCSADRAIN, ["SAVED"])

    term, tm = real_terminal()
    with term:
        pass
    assert tm.calls[-1] == ("set", 7, FakeTermios.TCSADRAIN, ["SAVED"])


def test_real_terminal_keys_and_lines():
    term, tm = real_terminal(reads=[b"\r", b"q\x1b[A", b"", ], input_fn=lambda p: f"typed<{p}>",
                             getpass_fn=lambda p: f"secret<{p}>")
    with term:
        assert term.read_key() == "enter"
        assert term.read_key() == "q"
        assert term.read_key() == "esc"  # an arrow key is one key, never `A`
        assert term.read_key() == "eof"
        n = len(tm.calls)
        assert term.read_line("name: ") == "typed<name: >"
        assert term.read_line("key: ", secret=True) == "secret<key: >"
        # cooked mode for the typed line, cbreak again afterwards
        assert tm.calls[n] == ("set", 7, FakeTermios.TCSADRAIN, ["SAVED"]) and ("cbreak", 7) in tm.calls[n:]
        assert term.size() == (100, 40) and term.is_tty()


def test_intro_command_off_a_tty_exits_2(rig, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_init_isatty", lambda: False)
    assert cli.main(["intro"]) == 2
    err = capsys.readouterr().err
    assert "steward init --yes" in err

    monkeypatch.setattr(cli, "_init_isatty", lambda: True)
    monkeypatch.setattr(cli, "_init_osatty", lambda: False)  # stdout piped: no screen to draw on
    assert cli.main(["intro"]) == 2
    assert "steward init --yes" in capsys.readouterr().err
    assert not rig.home.exists() and rig.fake.mutating_calls() == []


def test_intro_command_opens_the_screen(rig, monkeypatch):
    term = FakeTerminal(["q"])
    monkeypatch.setattr(cli, "_init_isatty", lambda: True)
    monkeypatch.setattr(cli, "_init_osatty", lambda: True)
    monkeypatch.setattr(cli, "_terminal", lambda: term)
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: rig.fake.api)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: rig.fake.runner)
    assert cli.main(["intro"]) == 0
    assert len(term.frames) == 1 and "will create:" in term.text(0)
    assert not rig.home.exists()


# -- task 2: local fixes, the login pane, the optional page, what's new ------------------------------------------------


def python_row(local: bool) -> prereq.Check:
    fix = prereq.Fix("local", "uv python install 3.12", "uv_python") if local else prereq.Fix(
        "command", "install Python 3.12 or newer")
    return prereq.Check("python", "Python", prereq.REQUIRED, prereq.MISSING, "3.11.4 (need 3.12+)", fix)


def with_python(chk, row):
    return [row if c.id == "python" else c for c in chk]


def test_f_applies_only_local_fixes(rig):
    rig.state["checks"] = with_python(with_optional(), python_row(local=True))
    rig.ctx.which = lambda name: f"/usr/bin/{name}"

    def python_installed():
        rig.state["checks"] = with_optional()

    rig.uv["after"] = python_installed
    term = FakeTerminal(["f", "f", "q"])
    assert intro.run(term, rig.ctx) == 0

    assert mode(rig.home) == 0o700 and mode(rig.config) == 0o700
    keys_env = rig.config / "keys.env"
    assert keys_env.exists() and mode(keys_env) == 0o600
    content = keys_env.read_text()
    assert [a for a in rig.argvs() if a[0] == "uv"] == [["uv", "python", "install", "3.12"]]
    assert not any("sudo" in " ".join(a) for a in rig.argvs())
    assert rig.fake.mutating_calls() == []  # no Herdr change from `f`

    first, after_f, after_second = term.frames
    assert "f will: create directories, uv python install 3.12, create keys.env" in "\n".join(first)
    assert "→ [f] uv python install 3.12" in term.text(0)
    assert "✓ Python" in term.text(1) and "created" in term.text(1)  # re-detected and redrawn, result shown
    assert "nothing to fix here" in term.text(2)
    assert keys_env.read_text() == content and len([a for a in rig.argvs() if a[0] == "uv"]) == 1
    assert not (rig.home / "install.json").exists()  # `f` alone scaffolds nothing


def test_f_leaves_an_existing_keys_file_alone(rig):
    rig.config.mkdir(parents=True, mode=0o700)
    rig.home.mkdir(mode=0o700)
    keys_env = rig.config / "keys.env"
    keys_env.write_text("TYPESAFE_API_KEY=not-a-real-key\n")
    keys_env.chmod(0o644)
    term = FakeTerminal(["f", "q"], rows=60)
    assert intro.run(term, rig.ctx) == 0
    assert keys_env.read_text() == "TYPESAFE_API_KEY=not-a-real-key\n" and mode(keys_env) == 0o644
    assert "nothing to fix here" in term.text(1)
    assert "add TYPESAFE_API_KEY to keys.env" in term.text(0)  # no [f]: only the key itself is missing
    assert "f will" not in term.text(0)


def test_without_uv_no_install_is_attempted(rig):
    rig.state["checks"] = with_python(with_optional(), python_row(local=False))
    rig.ctx.which = lambda name: None if name == "uv" else f"/usr/bin/{name}"
    term = FakeTerminal(["f", "q"])
    assert intro.run(term, rig.ctx) == 1
    assert not any(a[0] == "uv" for a in rig.argvs())
    assert "→ install Python 3.12 or newer" in term.text(0)
    assert "uv python install" not in term.text(0)
    assert "uv is not installed" in intro.apply_fix("uv_python", rig.ctx)
    assert not any(a[0] == "uv" for a in rig.argvs())


def test_apply_fix_accepts_only_the_three_local_ids(rig):
    import inspect

    assert intro.LOCAL_FIXES == ("uv_python", "keys_env", "config_dirs")
    assert list(inspect.signature(intro.apply_fix).parameters) == ["fix_id", "ctx"]
    for bad in ("sudo apt install git", "claude_login", "herdr", "", "uv python install 3.12"):
        with pytest.raises(ValueError):
            intro.apply_fix(bad, rig.ctx)
    assert rig.argvs() == []
    assert intro.apply_fix("config_dirs", rig.ctx).startswith("created")
    assert intro.apply_fix("config_dirs", rig.ctx) == "directories already exist"


def test_nothing_else_is_ever_run(rig):
    missing = {
        "herdr_version": prereq.Fix("command", f"install Herdr {prereq.TESTED_HERDR} or newer"),
        "git": prereq.Fix("command", "install git"),
        "claude": prereq.Fix("command", "install the Claude Code CLI"),
    }
    chk = [prereq.Check(c.id, c.label, c.level, prereq.MISSING, "not found", missing[c.id])
           if c.id in missing else c for c in with_optional()]
    chk = [prereq.Check("codex", "codex", prereq.OPTIONAL, prereq.OFF, "off (no codex)",
                        prereq.Fix("command", "install the Codex CLI")) if c.id == "codex" else c for c in chk]
    rig.state["checks"] = chk
    term = FakeTerminal(["f", "q"])
    assert intro.run(term, rig.ctx) == 1
    first = term.text(0)
    assert "→ install Herdr 0.9.1 or newer" in first and "→ install git" in first
    assert "→ install the Claude Code CLI" in first
    assert all(a[0] == "herdr" for a in rig.argvs())  # only read-only Herdr lookups; no install, no login
    assert rig.fake.mutating_calls() == []
    assert not any(w in " ".join(a) for a in rig.argvs() for w in ("sudo", "install", "login", "npm", "apt"))


def logged_out_claude(rig, detail="not logged in"):
    _tab, (pane,) = rig.fake.add_tab("w1", "main", panes=("",))
    rig.ctx.env = {**rig.ctx.env, "HERDR_PANE_ID": pane}
    row = prereq.Check("claude", "claude CLI", prereq.REQUIRED, prereq.MISSING, detail,
                       prereq.Fix("command", "claude auth login"))
    rig.state["checks"] = [row if c.id == "claude" else c for c in with_optional()]
    return pane


def test_l_opens_claude_login_in_a_pane(rig):
    pane = logged_out_claude(rig)

    def logged_in():
        rig.state["checks"] = with_optional()

    term = FakeTerminal(["l", "r", "l", "q"])
    # the user finishes the login in the new pane before pressing `r`
    real_run = rig.runner.run

    def run(argv, cwd, **kw):
        res = real_run(argv, cwd, **kw)
        if argv[:3] == ["herdr", "pane", "run"] and argv[4] == "claude auth login":
            logged_in()
        return res

    rig.runner.run = run
    assert intro.run(term, rig.ctx) == 0
    (split,) = rig.fake.of("pane", "split")
    assert split[:6] == ["herdr", "pane", "split", "--pane", pane, "--direction"] and split[6] == "down"
    new = rig.fake.pane_by_label("claude login")
    assert new is not None and new["split_from"] == pane
    assert rig.fake.runs()[new["pane_id"]] == ["claude auth login"]
    assert "l login" in term.text(0) and "r recheck" in term.text(0)
    assert "claude login is open" in term.text(1)
    assert "✓ claude CLI" in term.text(2) and "l login" not in term.text(2)  # re-checked: logged in, no `l`
    assert len(rig.fake.of("pane", "split")) == 1  # the last `l` was ignored: nothing to log in to


def test_l_is_not_offered_when_claude_is_missing_or_logged_in(rig):
    logged_out_claude(rig, detail="claude not found")
    term = FakeTerminal(["l", "q"])
    intro.run(term, rig.ctx)
    assert "l login" not in term.text(0) and rig.fake.of("pane", "split") == []

    rig.state["checks"] = with_optional()
    term = FakeTerminal(["l", "q"])
    intro.run(term, rig.ctx)
    assert "l login" not in term.text(0) and rig.fake.of("pane", "split") == []

    logged_out_claude(rig)
    rig.ctx.env = {k: v for k, v in rig.ctx.env.items() if k != "HERDR_PANE_ID"}  # nowhere to split
    term = FakeTerminal(["l", "q"])
    intro.run(term, rig.ctx)
    assert "l login" not in term.text(0) and rig.fake.of("pane", "split") == []


def recorded_install(rig, **enabled):
    state = installstate.new_state(version_module.version(), "/pkg")
    for cid in enabled:
        installstate.record_component(state, cid, enabled=True, mode="systemd")
    installstate.save(state, rig.home)
    return state


def test_o_lists_the_eight_components(rig, monkeypatch):
    recorded_install(rig)
    seen = []

    def fake_enable(cid, cctx):
        seen.append((cid, cctx))
        assert cctx.ask("Type the hostname (x.example.com): ") == "x.example.com"
        assert cctx.ask_secret("key: ") == "s3cret"
        cctx.out(f"{cid} enabled.")
        cctx.out("To keep it running after you log out, run: loginctl enable-linger sam")
        return 0

    monkeypatch.setattr(components, "enable", fake_enable)
    term = FakeTerminal(["o", "1", "y", "b", "q"], lines=["x.example.com", "s3cret"])
    assert intro.run(term, rig.ctx) == 0
    welcome, listing, detail, after, back = term.frames
    assert [ln for ln in term.text(1).splitlines() if re.match(r"\d ", ln)] and \
        len([ln for ln in term.text(1).splitlines() if re.match(r"\d ", ln)]) == 8
    assert all("off (not enabled)" in ln for ln in term.text(1).splitlines()[1:9])
    for cid in components.COMPONENT_IDS:
        assert cid in term.text(1)
    assert "Enable mcp" in term.text(2) and "exposes" in term.text(2) and "y enable" in term.text(2)
    (cid, cctx), = seen
    assert cid == "mcp" and cctx.yes is True and cctx.interactive is True
    assert term.prompts == [("Type the hostname (x.example.com): ", False), ("key: ", True)]
    assert "mcp enabled." in term.text(3) and "loginctl enable-linger" in term.text(3)  # back at the list, with the result
    assert term.text(3).splitlines()[0] == "Optional components"
    assert term.text(4).startswith("Steward") or "Steward" in term.text(4).splitlines()[0]  # `b` returns to the first page


def test_o_back_and_quit_from_anywhere(rig):
    recorded_install(rig)
    term = FakeTerminal(["o", "b", "q"])
    assert intro.run(term, rig.ctx) == 0
    assert len(term.frames) == 3 and "Optional components" in term.text(1)

    term = FakeTerminal(["o", "2", "q"])
    assert intro.run(term, rig.ctx) == 0
    assert "Enable tunnel" in term.text(2)

    term = FakeTerminal(["o", "q"])
    assert intro.run(term, rig.ctx) == 0


def test_o_blocked_and_already_on(rig, monkeypatch):
    recorded_install(rig, mcp=True)
    calls = []
    monkeypatch.setattr(components, "enable", lambda cid, cctx: calls.append(cid) or 0)
    term = FakeTerminal(["o", "1", "6", "y", "b", "q"])  # 1: already on; 6: android (needs mcp, which is on)
    intro.run(term, rig.ctx)
    assert "mcp is on; turn it off with `steward disable mcp`" in term.text(2)
    assert "1 ✓ mcp" in term.text(1)
    assert calls == ["android"]  # 6 was not blocked (mcp is on): `y` enabled it, once

    calls.clear()
    recorded_install(rig)  # mcp off: android is blocked on it
    term = FakeTerminal(["o", "6", "y", "q"])
    intro.run(term, rig.ctx)
    assert "BLOCKED" in term.text(2) and "y enable" not in term.text(2)
    assert calls == []  # `y` on a blocked plan never calls enable


def test_o_before_the_first_init_says_init_first(rig):
    term = FakeTerminal(["o", "1", "q"])
    intro.run(term, rig.ctx)
    assert "BLOCKED" in term.text(2) and "steward init" in term.text(2)
    assert not rig.home.exists() or not (rig.home / "install.json").exists()


def test_typed_confirmation_is_asked_on_the_terminal(rig, monkeypatch):
    recorded_install(rig, mcp=True)
    outcome = {}

    def fake_enable(cid, cctx):
        outcome["typed"] = cctx.ask(f"Type the hostname (steward.example.com) to enable {cid}: ")
        return 0 if outcome["typed"] == "steward.example.com" else 1

    monkeypatch.setattr(components, "enable", fake_enable)
    term = FakeTerminal(["o", "2", "y", "q"], lines=["steward.example.com"])
    intro.run(term, rig.ctx)
    assert outcome["typed"] == "steward.example.com"
    assert term.prompts[0][0].startswith("Type the hostname")


def seen_install(rig, seen, version):
    """An install.json written by an earlier version that showed the intro for `seen`."""
    assert intro.run(FakeTerminal(["enter", "enter"]), rig.ctx) == 0
    state = rig.manifest()
    state["intro_seen"], state["version"] = seen, seen
    installstate.save(state, rig.home)


def test_whatsnew_on_a_major_change(rig, monkeypatch):
    seen_install(rig, "0.9.0", "0.9.0")
    monkeypatch.setattr(version_module, "version", lambda *a, **k: "1.0.0")
    before = len(rig.fake.of("pane", "run"))
    term = FakeTerminal(["enter", "enter", "enter"])
    assert intro.run(term, rig.ctx) == 0
    page, welcome, done = term.frames
    assert page[0].endswith("What's new in Steward 1.0.0") or "What's new in Steward 1.0.0" in page[0]
    assert "replaces 0.9.0" in term.text(0) and len(page) <= 14
    assert "Steward 1.0.0" in term.text(1) and "after the upgrade from 0.9.0 to 1.0.0" in term.text(1)
    # the upgrade is applied by the Enter on the normal screen, not by the page
    assert len(rig.fake.of("pane", "run")) > before
    state = rig.manifest()
    assert state["version"] == "1.0.0" and state["intro_seen"] == "1.0.0"
    assert "Run Steward on proj" in term.text(2)


def test_whatsnew_q_leaves_no_trace(rig, monkeypatch):
    seen_install(rig, "0.9.0", "0.9.0")
    monkeypatch.setattr(version_module, "version", lambda *a, **k: "1.0.0")
    calls = len(rig.fake.mutating_calls())
    assert intro.run(FakeTerminal(["q"]), rig.ctx) == 0
    assert rig.manifest()["intro_seen"] == "0.9.0" and rig.manifest()["version"] == "0.9.0"
    assert len(rig.fake.mutating_calls()) == calls


def test_a_minor_change_shows_no_page(rig, monkeypatch):
    seen_install(rig, "1.0.0", "1.0.0")
    monkeypatch.setattr(version_module, "version", lambda *a, **k: "1.1.0")
    term = FakeTerminal(["q"])
    intro.run(term, rig.ctx)
    assert len(term.frames) == 1 and "What's new" not in term.text(0)


def test_whatsnew_uses_the_major_entry_when_there_is_one(rig, monkeypatch):
    monkeypatch.setitem(intro.WHATS_NEW, 1, ("Brand new thing.", "Another thing."))
    seen_install(rig, "0.9.0", "0.9.0")
    monkeypatch.setattr(version_module, "version", lambda *a, **k: "1.0.0")
    term = FakeTerminal(["q"])
    intro.run(term, rig.ctx)
    assert "Brand new thing." in term.text(0) and "replaces" not in term.text(0)
