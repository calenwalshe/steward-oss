"""G20 tier 3: the INSTALLED package driven inside a throwaway named Herdr session (`steward-test-pk<pid>-*`).

Marked `integration`; skipped by default and without herdr, node, npm or Python 3.12. Each test builds an empty
machine (tests/pack_env.py), installs the audited tarball into its tmp prefix offline, starts its own named session
and types `steward ...` into a pane. The session's server starts from a small environment with `/bin/sh` as the shell,
so no secret or setting of the host reaches a pane; the typed command sources the machine's exports first. The panes
Steward creates get the machine's environment through package mode (plan 19-03), which is part of what is proved.

Reading pane text here is a test of the screen a user sees, not a production message path. Every session is stopped and
deleted in a `finally`; nothing of the live system (`~/.config/steward`, `~/.local/state/steward`, `~/bin`, any
other Herdr session) is read or written. The Herdr CLI used here is `herdr --session <name> {tab list, pane list,
pane process-info --pane, pane read}` plus the socket API's `pane.send_input` for single keys.
"""

from __future__ import annotations

import json
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

import pack_env
from herdr_session import herdr_available

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not herdr_available(), reason="herdr is not installed"),
              pytest.mark.skipif(pack_env.skip_reason() is not None, reason=pack_env.skip_reason() or "")]

VERSION_A = pack_env.pyproject_version()
VERSION_B = pack_env.bump(VERSION_A)
FIRST_USE = "Ready. In the operator pane (tab cockpit), say: Run Steward on <repo>: <goal>"


@pytest.fixture(scope="module")
def tarballs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pack")
    return {"A": pack_env.build_tarball(tmp), "B": pack_env.build_tarball(tmp, version=VERSION_B)}


@dataclass
class World:
    env: dict
    session: object
    pane: str
    repo: Path

    @property
    def home(self) -> Path:
        return Path(self.env["STEWARD_HOME"])

    @property
    def config(self) -> Path:
        return Path(self.env["STEWARD_CONFIG_DIR"])

    @property
    def install_json(self) -> Path:
        return self.home / "install.json"

    def install_state(self) -> dict:
        return json.loads(self.install_json.read_text())

    def steward(self, *args: str, timeout: float = 90.0, pane: str | None = None) -> tuple[int, str]:
        return self.session.run(pane or self.pane, "steward " + " ".join(args), timeout=timeout)


@pytest.fixture
def world(tmp_path, tarballs):
    """`make(tag, **machine_options)` -> a World: an empty machine with tarball A installed and a started session."""
    made: list = []

    def make(tag: str = "a", **options) -> World:
        env = pack_env.machine(tmp_path / f"machine-{tag}", **options)
        res = pack_env.install_tarball(env, tarballs["A"])
        assert res.returncode == 0, f"install failed:\n{res.stdout}\n{res.stderr}"
        repo = tmp_path / f"repo-{tag}"
        repo.mkdir()
        session = pack_env.new_session(env, tag)
        made.append(session)
        return World(env, session, session.create_pane(cwd=str(repo)), repo)

    try:
        yield make
    finally:
        for session in made:
            session.teardown()
    for session in made:  # after teardown: nothing of that session is left running
        pack_env.wait_for(lambda s=session: not pack_env.session_exists(s.name) or None, timeout=10,
                          what=f"session {session.name} to be gone")
        pack_env.wait_for(lambda s=session: not pack_env.leftover_processes(s.name) or None, timeout=10,
                          what=f"the processes of {session.name} to exit")


def _cockpit(world: World) -> dict:
    """Assert the whole cockpit exists (control and advisor in the `steward` tab, operator and dash in `cockpit`)
    and return its tab and pane ids."""
    s = world.session
    steward_tab, cockpit_tab = s.tab_labelled("steward"), s.tab_labelled("cockpit")
    assert steward_tab and cockpit_tab, s.tabs()
    control = s.pane_labelled("steward control", steward_tab["tab_id"])
    advisor = s.pane_labelled("steward advisor", steward_tab["tab_id"])
    operator = s.pane_labelled("operator", cockpit_tab["tab_id"])
    dash = s.pane_labelled("steward dash", cockpit_tab["tab_id"])
    assert control and advisor and operator and dash, s.pane_list()
    return {"steward_tab": steward_tab["tab_id"], "cockpit_tab": cockpit_tab["tab_id"], "control": control["pane_id"],
            "advisor": advisor["pane_id"], "operator": operator["pane_id"], "dash": dash["pane_id"]}


def _wait_foreground(world: World, pane: str, needle: str, what: str, timeout: float = 30.0):
    return pack_env.wait_for(
        lambda: any(needle in " ".join(argv) for argv in world.session.argvs(pane)) or None, timeout=timeout,
        what=what)


def _assert_working_cockpit(world: World) -> dict:
    ids = _cockpit(world)
    fake_claude = str(Path(world.env["PATH"].split(":")[0]) / "claude")
    _wait_foreground(world, ids["operator"], fake_claude, "the operator pane to run the fake claude")
    _wait_foreground(world, ids["control"], "steward.cli control", "the control pane to run steward control")
    _wait_foreground(world, ids["advisor"], "steward.cli advisor", "the advisor pane to run steward advisor")
    # `steward dash` starts as `python -m steward.cli dash` and replaces itself with the packaged script
    _wait_foreground(world, ids["dash"], "steward-dash.sh", "the dash pane to run steward-dash.sh")
    return ids


def test_first_run_leaves_the_cockpit(world):
    w = world("first")
    code, text = w.steward("init", "--yes")
    assert code == 0, text
    assert w.install_json.is_file()
    ids = _assert_working_cockpit(w)
    assert len({ids[k] for k in ("control", "advisor", "operator", "dash")}) == 4
    for name in ("steward.toml", "trajectory.toml", "exceptions.toml"):
        assert (w.config / name).is_file(), name
    assert stat.S_IMODE(w.config.stat().st_mode) == 0o700
    assert stat.S_IMODE(w.home.stat().st_mode) == 0o700
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    marker = max(i for i, ln in enumerate(lines) if re.fullmatch(r"EXIT-\d+=0", ln))
    assert lines[marker - 1] == FIRST_USE
    assert w.install_state()["version"] == VERSION_A


def test_dry_run_inside_a_pane_prints_the_plan_and_writes_nothing(world):
    w = world("dry")
    code, text = w.steward("init", "--dry-run")
    assert code == 0, text
    assert "steward" in text and "cockpit" in text and "steward.toml" in text
    assert pack_env.tree(w.home) == [] and pack_env.tree(w.config) == []
    assert [t["label"] for t in w.session.tabs()] == ["1"]


def test_second_run_creates_nothing(world):
    w = world("second")
    assert w.steward("init", "--yes")[0] == 0
    _assert_working_cockpit(w)
    ids, hashes = w.session.ids(), pack_env.sha256_tree(w.config)
    before = w.install_state()
    code, text = w.steward("init", "--yes")
    assert code == 0, text
    assert w.session.ids() == ids
    assert pack_env.sha256_tree(w.config) == hashes
    after = w.install_state()
    assert {k: v for k, v in after.items() if k != "updated_at"} == {k: v for k, v in before.items() if k != "updated_at"}
    assert [p for p in pack_env.tree(w.config) if p.endswith(".new")] == []


def test_upgrade_restarts_in_place(world, tarballs):
    w = world("upgrade")
    assert w.steward("init", "--yes")[0] == 0
    ids = _assert_working_cockpit(w)
    s = w.session
    pids = {role: s.pids(ids[role]) for role in ("control", "advisor", "dash", "operator")}
    all_ids, hashes = s.ids(), pack_env.sha256_tree(w.config)
    assert w.install_state()["version"] == VERSION_A

    res = pack_env.install_tarball(w.env, tarballs["B"])
    assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
    code, text = w.steward("up", timeout=120)
    assert code == 0, text

    assert s.ids() == all_ids  # nothing new, nothing closed
    assert w.install_state()["version"] == VERSION_B
    assert pack_env.sha256_tree(w.config) == hashes
    assert [p for p in pack_env.tree(w.config) if p.endswith(".new")] == []

    def restarted(role: str):
        return s.pids(ids[role]) != pids[role] and bool(s.pids(ids[role])) or None

    for role in ("control", "advisor", "dash"):
        pack_env.wait_for(lambda r=role: restarted(r), timeout=60, what=f"the {role} pane to be re-run in place")
    assert s.pids(ids["operator"]) == pids["operator"]  # the operator is left alone
    assert _assert_working_cockpit(w) == ids  # control, advisor and dash run again, in the same panes
    settled = {role: s.pids(ids[role]) for role in ("control", "advisor", "dash", "operator")}
    assert w.steward("up")[0] == 0 and {role: s.pids(ids[role]) for role in settled} == settled  # and `up` is idempotent
    out = pack_env.run_installed(w.env, "--version").stdout
    assert out.startswith(f"steward {VERSION_B} ")


def test_first_keypress_path(world):
    w = world("intro")
    s = w.session
    s.run_in_pane(w.pane, f". {s.pane_env_file} && steward")
    pack_env.wait_for(lambda: re.search(r"Enter (scaffold|go)", s.visible(w.pane)) or None, timeout=60,
                      what="the intro page's Enter key line")
    assert not w.install_json.exists()  # nothing is done before the key
    s.send_key(w.pane, "enter")
    pack_env.wait_for(w.install_json.is_file, timeout=90, what="install.json after one Enter")
    _assert_working_cockpit(w)
    state = pack_env.wait_for(lambda: w.install_state().get("intro_seen") and w.install_state(), timeout=30,
                              what="intro_seen to be recorded")
    assert state["intro_seen"] == VERSION_A


def test_uninstall_closes_tabs_and_keeps_config(world):
    w = world("uninstall")
    assert w.steward("init", "--yes")[0] == 0
    _assert_working_cockpit(w)
    code, text = w.steward("uninstall")
    assert code == 0, text
    pack_env.wait_for(lambda: not w.session.tab_labelled("steward") and not w.session.tab_labelled("cockpit"),
                      timeout=30, what="the steward and cockpit tabs to close")
    assert not w.install_json.exists()
    assert w.config.is_dir() and (w.config / "steward.toml").is_file()
    assert w.home.is_dir()
    assert [t["label"] for t in w.session.tabs()] == ["1"]


def test_degraded_run_is_a_working_cockpit(world):
    w = world("degraded", codex="absent")
    out_file = w.env["HOME"] + "/doctor.json"
    code, text = w.steward("init", "--yes")
    assert code == 0, text
    _assert_working_cockpit(w)
    code, text = w.session.run(w.pane, f"steward doctor --json > {out_file}")
    assert code == 0, text
    rows = {r["id"]: r for r in json.loads(Path(out_file).read_text())}
    assert rows["jev"]["detail"] == "off (no key)"
    assert rows["codex"]["detail"] == "off (no codex)"
    assert rows["systemd"]["detail"] == "off (no systemd user manager)"
    assert rows["connector"]["detail"] == "off (no public connector)"
    assert [r["id"] for r in rows.values() if r["level"] == "required" and r["status"] != "ok"] == []


def test_no_leftovers(world):
    w = world("left")
    assert w.steward("init", "--yes")[0] == 0
    _assert_working_cockpit(w)
    name = w.session.name
    assert pack_env.session_exists(name)
    w.session.teardown()
    pack_env.wait_for(lambda: not pack_env.session_exists(name), timeout=15, what="the session to disappear")
    deadline = time.monotonic() + 15
    while pack_env.leftover_processes(name) and time.monotonic() < deadline:
        time.sleep(0.5)
    assert pack_env.leftover_processes(name) == []
    assert pack_env.leftover_processes() == [], "pgrep -af steward-test- is not empty"
