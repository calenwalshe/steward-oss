"""Phase 19-04 (G4, G5): the operator guide ships generic and `steward cockpit` is idempotent by label."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from steward import cli, cockpit, launch, pkgenv, version
from steward.runner_exec import CommandResult, FakeRunner

LEGACY_OWNER = "Cal" + "en"  # two parts so this file is not itself a marker


def _res(payload=None, code=0, stderr=""):
    return CommandResult(argv=[], cwd=".", exit_code=code, stdout=json.dumps(payload) if payload is not None else "",
                         stderr=stderr, duration_s=0.0)


class FakeHerdr:
    """A tiny in-memory Herdr: tabs, panes, labels and foreground processes behind `herdr ...` argv."""

    def __init__(self):
        self.tabs: list[dict] = []
        self.panes: list[dict] = []
        self.workspaces: list[dict] = []
        self.foreground: dict[str, list[list[str]]] = {}  # pane id -> argv lists; missing = unknown (error)
        self.calls: list[list[str]] = []
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    def add_tab(self, workspace, label, panes=()):
        tab = {"tab_id": self._id("t"), "label": label, "workspace_id": workspace, "pane_count": len(panes)}
        self.tabs.append(tab)
        made = []
        for lab in panes:
            pane = {"pane_id": self._id("p"), "tab_id": tab["tab_id"], "label": lab, "cwd": "/w"}
            self.panes.append(pane)
            made.append(pane["pane_id"])
        return tab["tab_id"], made

    def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        assert argv[0] == "herdr", argv
        rest = argv[1:]
        head = " ".join(rest[:2])
        if head == "tab list":
            return _res({"result": {"tabs": self.tabs}})
        if head == "pane list":
            return _res({"result": {"panes": self.panes}})
        if head == "workspace list":
            return _res({"result": {"workspaces": self.workspaces}})
        if head == "tab create":
            ws = rest[rest.index("--workspace") + 1]
            label = rest[rest.index("--label") + 1]
            tab_id, (root,) = self.add_tab(ws, label, panes=("",))
            return _res({"result": {"tab": {"tab_id": tab_id}, "root_pane": {"pane_id": root}}})
        if head == "workspace create":
            ws = self._id("w")
            self.workspaces.append({"workspace_id": ws, "label": rest[rest.index("--label") + 1]})
            tab_id, (root,) = self.add_tab(ws, "1", panes=("",))
            return _res({"result": {"workspace": {"workspace_id": ws}, "tab": {"tab_id": tab_id},
                                    "root_pane": {"pane_id": root}}})
        if head == "tab rename":
            next(t for t in self.tabs if t["tab_id"] == rest[2])["label"] = rest[3]
            return _res({"result": {}})
        if head == "pane rename":
            next(p for p in self.panes if p["pane_id"] == rest[2])["label"] = rest[3]
            return _res({"result": {}})
        if head == "pane split":
            base = next(p for p in self.panes if p["pane_id"] == rest[rest.index("--pane") + 1])
            pane = {"pane_id": self._id("p"), "tab_id": base["tab_id"], "label": "", "cwd": "/w"}
            self.panes.append(pane)
            return _res({"result": {"pane": {"pane_id": pane["pane_id"]}}})
        if head == "pane process-info":
            pane_id = rest[rest.index("--pane") + 1]
            if pane_id not in self.foreground:
                return _res(None, code=1, stderr="unknown")
            procs = [{"argv": a} for a in self.foreground[pane_id]]
            return _res({"result": {"process_info": {"foreground_processes": procs}}})
        if head in ("tab focus", "pane run", "workspace focus"):
            return _res({"result": {}})
        raise AssertionError(f"unexpected herdr call {argv}")

    def of(self, *words):
        return [c for c in self.calls if c[1 : 1 + len(words)] == list(words)]

    def pane_by_label(self, label):
        return next((p for p in self.panes if p["label"] == label), None)

    def runs(self):
        return {c[3]: c[4] for c in self.calls if c[1:3] == ["pane", "run"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    monkeypatch.delenv("STEWARD_INSTALL_ROOT", raising=False)
    return tmp_path


def _ensure(herdr, workspace="w1", **kw):
    kw.setdefault("cwd", "/work/proj")
    kw.setdefault("which", lambda n: f"/usr/bin/{n}")
    return cockpit.ensure_cockpit(FakeRunner(herdr), None, "/tmp/h.sock", workspace, **kw)


# -- the guide ----------------------------------------------------------------------------------------------------


def test_guide_is_generic_and_complete():
    text = cockpit.render_guide("Sam", "steward")
    for title in ("## The control pane", "## Workbench posture", "**Shape**", "**Run**", "**Steer**", "**Finish**",
                  "## Steering verbs", "## Approvals and acceptance", "## Incorporate a finished job", "## Send back",
                  "## Question queue"):
        assert title in text, title
    assert "Sam" in text and "Sam's" in text
    for marker in (LEGACY_OWNER, LEGACY_OWNER.lower(), "cairn" "labs", "/home/", "work-2026", ".planning/",
                   "moshi://herdr?workspace=w", "python3 -m steward.cli", "%%", "~/.config", "~/.api-keys"):
        assert marker not in text, marker
    assert "steward new " in text


def test_template_has_the_three_tokens_and_no_markers():
    raw = (Path(cockpit.__file__).parent / "templates" / "OPERATOR.md").read_text()
    for token in ("%%OWNER%%", "%%OWNER_POSS%%", "%%STEWARD%%"):
        assert token in raw
    for marker in (LEGACY_OWNER, "cairn" "labs", "/home/", "work-2026", ".planning/", "python3 -m steward.cli"):
        assert marker not in raw, marker


def test_guide_fills_the_invocation():
    text = cockpit.render_guide("the operator", "/opt/py -m steward.cli")
    assert "/opt/py -m steward.cli new " in text and "the operator" in text


def test_steward_invocation_prefers_the_steward_on_path():
    assert cockpit.steward_invocation(which=lambda n: "/usr/bin/steward") == "steward"
    off = cockpit.steward_invocation(which=lambda n: None, python="/opt/my python/bin/python3")
    assert off == "'/opt/my python/bin/python3' -m steward.cli"


def test_seed_text():
    seed = cockpit.seed_text("/state/cockpit/OPERATOR.md", "steward", "p9", owner_name="Sam", steward="steward")
    assert "/state/cockpit/OPERATOR.md" in seed and "'steward'" in seed and "pane p9" in seed
    for word in ("SHAPE", "RUN", "STEER", "FINISH", "Sam"):
        assert word in seed
    assert seed.rstrip().endswith("active Steward jobs from `steward status`.")
    assert "ONE line" in seed and "ready" in seed
    assert "\n" not in seed.strip()  # one paragraph: it is a single prompt argument


# -- the pane commands --------------------------------------------------------------------------------------------


def test_operator_command(env):
    seed = env / "home" / "cockpit" / "seed.txt"
    cmd = cockpit.operator_command(seed, cwd="/work/proj", args=[])
    assert cmd.startswith(launch._env_exports() + "export PYTHONPATH=")
    assert f"cd /work/proj && claude \"$(cat {shlex.quote(str(seed))})\"" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    with_args = cockpit.operator_command(seed, cwd="/work/proj", args=shlex.split("--model opus --add-dir '/a b'"))
    assert "claude --model opus --add-dir '/a b' \"$(cat" in with_args


def test_operator_command_in_package_mode_uses_the_package_exports(env, monkeypatch):
    monkeypatch.setenv("STEWARD_INSTALL_ROOT", "/opt/pkg")
    cmd = cockpit.operator_command(env / "seed.txt", cwd="/w", args=[])
    assert cmd.startswith(launch._env_exports()) and "export PYTHONPATH=/opt/pkg &&" in cmd
    assert cmd.count("export PYTHONPATH=") == 1


def test_dash_command(env):
    cmd = cockpit.dash_command()
    root = shlex.quote(str(version.install_root()))
    assert cmd.endswith(f"cd {root} && {shlex.quote(sys.executable)} -m steward.cli dash")
    assert cmd.startswith(launch._env_exports())


def test_operator_args_come_from_steward_toml(env):
    cfg = env / "config"
    cfg.mkdir()
    assert cockpit.operator_args() == []
    (cfg / "steward.toml").write_text('[cockpit]\noperator_args = "--model opus --add-dir \'/a b\'"\n')
    assert cockpit.operator_args() == ["--model", "opus", "--add-dir", "/a b"]
    (cfg / "steward.toml").write_text('[cockpit]\noperator_args = ["--model", "haiku"]\n')
    assert cockpit.operator_args() == ["--model", "haiku"]
    (cfg / "steward.toml").write_text("[cockpit]\noperator_args = 5\n")
    assert cockpit.operator_args() == []
    (cfg / "steward.toml").write_text("not toml [")
    assert cockpit.operator_args() == []


# -- ensure_cockpit -----------------------------------------------------------------------------------------------


def test_ensure_cockpit_creates_tab_operator_and_dash(env):
    herdr = FakeHerdr()
    res = _ensure(herdr)
    assert res["created"] == ["tab", "operator", "dash"]
    assert herdr.of("tab", "create") == [["herdr", "tab", "create", "--workspace", "w1", "--label", "cockpit",
                                          "--cwd", "/work/proj", "--no-focus"]]
    root = res["operator_pane"]
    assert herdr.of("pane", "rename")[0][3:] == [root, "operator"]
    assert herdr.of("pane", "split") == [["herdr", "pane", "split", "--pane", root, "--direction", "right",
                                          "--ratio", "0.5", "--cwd", "/work/proj", "--no-focus"]]
    assert herdr.pane_by_label("steward dash")["pane_id"] == res["dash_pane"]
    runs = herdr.runs()
    assert "claude" in runs[res["operator_pane"]] and "cat" in runs[res["operator_pane"]]
    assert runs[res["dash_pane"]].endswith("-m steward.cli dash")
    assert res["tab_id"] == herdr.tabs[0]["tab_id"]
    seed = (env / "home" / "cockpit" / "seed.txt").read_text()
    assert res["dash_pane"] in seed and res["operator_pane"] in seed
    guide = (env / "home" / "cockpit" / "OPERATOR.md").read_text()
    assert "Sam" in guide and "%%" not in guide
    assert herdr.of("tab", "focus")  # the cockpit is brought to the front


def test_second_call_creates_nothing(env):
    herdr = FakeHerdr()
    first = _ensure(herdr)
    herdr.foreground[first["operator_pane"]] = [["node", "/usr/lib/claude/cli.js"]]
    herdr.foreground[first["dash_pane"]] = [["bash", "/pkg/steward/templates/cockpit/steward-dash.sh"]]
    herdr.calls.clear()
    second = _ensure(herdr)
    assert second["created"] == [] and second["restarted"] == []
    assert second["tab_id"] == first["tab_id"]
    assert second["operator_pane"] == first["operator_pane"] and second["dash_pane"] == first["dash_pane"]
    verbs = {tuple(c[1:3]) for c in herdr.calls}
    assert verbs <= {("tab", "list"), ("pane", "list"), ("tab", "focus"), ("pane", "process-info")}, verbs
    assert not herdr.of("pane", "run") and not herdr.of("pane", "split") and not herdr.of("tab", "create")


def test_missing_dash_is_recreated_by_splitting_the_operator(env):
    herdr = FakeHerdr()
    tab, (op,) = herdr.add_tab("w1", "cockpit", ["operator"])
    herdr.foreground[op] = [["claude"]]
    res = _ensure(herdr)
    assert res["created"] == ["dash"] and res["operator_pane"] == op
    split = herdr.of("pane", "split")
    assert len(split) == 1 and split[0][split[0].index("--pane") + 1] == op and "right" in split[0]
    assert herdr.pane_by_label("steward dash")["pane_id"] == res["dash_pane"]
    assert set(herdr.runs()) == {res["dash_pane"]}  # the live operator is never re-run


def test_missing_operator_is_recreated_by_splitting_the_dash(env):
    herdr = FakeHerdr()
    tab, (dash,) = herdr.add_tab("w1", "cockpit", ["steward dash"])
    herdr.foreground[dash] = [["bash", "steward-dash.sh"]]
    res = _ensure(herdr)
    assert res["created"] == ["operator"] and res["dash_pane"] == dash
    split = herdr.of("pane", "split")
    assert len(split) == 1 and split[0][split[0].index("--pane") + 1] == dash and "down" in split[0]
    assert herdr.pane_by_label("operator")["pane_id"] == res["operator_pane"]
    assert set(herdr.runs()) == {res["operator_pane"]}


def test_dead_panes_are_restarted_in_place(env):
    herdr = FakeHerdr()
    tab, (op, dash) = herdr.add_tab("w1", "cockpit", ["operator", "steward dash"])
    herdr.foreground[op] = [["bash"]]  # only a shell left
    herdr.foreground[dash] = [["-bash"]]
    res = _ensure(herdr)
    assert res["created"] == [] and sorted(res["restarted"]) == ["dash", "operator"]
    runs = herdr.runs()
    assert set(runs) == {op, dash} and "claude" in runs[op] and runs[dash].endswith("-m steward.cli dash")
    assert not herdr.of("pane", "split") and not herdr.of("tab", "create")
    assert (env / "home" / "cockpit" / "seed.txt").exists()  # a fresh seed for the restarted operator
    assert not any(c[1:3] in (["pane", "close"], ["tab", "close"]) for c in herdr.calls)


def test_a_pane_whose_state_is_unknown_is_left_alone(env):
    herdr = FakeHerdr()
    herdr.add_tab("w1", "cockpit", ["operator", "steward dash"])  # process-info fails for both
    res = _ensure(herdr)
    assert res["restarted"] == [] and not herdr.of("pane", "run")


def test_other_workspaces_and_tabs_do_not_count(env):
    herdr = FakeHerdr()
    herdr.add_tab("w2", "cockpit", ["operator", "steward dash"])
    herdr.add_tab("w1", "other", ["operator"])
    res = _ensure(herdr)
    assert res["created"] == ["tab", "operator", "dash"]


def test_no_operator_paths(env):
    herdr = FakeHerdr()
    res = _ensure(herdr, operator=False)
    assert res["created"] == ["tab", "dash"] and res["operator_pane"] is None
    assert res["operator_note"] == "operator skipped (--no-operator)"
    assert [p["label"] for p in herdr.panes] == ["steward dash"] and not herdr.of("pane", "split")
    assert not (env / "home" / "cockpit" / "seed.txt").exists()

    herdr = FakeHerdr()
    res = _ensure(herdr, which=lambda n: None)
    assert res["operator_note"] == "operator skipped (no claude)" and res["operator_pane"] is None
    assert [p["label"] for p in herdr.panes] == ["steward dash"]


def test_new_workspace(env):
    herdr = FakeHerdr()
    res = _ensure(herdr, None, new_workspace=True)
    assert herdr.of("workspace", "create") == [["herdr", "workspace", "create", "--label", "steward", "--cwd",
                                                "/work/proj", "--focus"]]
    assert res["created"] == ["workspace", "operator", "dash"]
    assert herdr.tabs[0]["label"] == "cockpit"
    assert herdr.pane_by_label("operator") and herdr.pane_by_label("steward dash")
    assert not herdr.of("tab", "create")

    herdr.calls.clear()
    again = _ensure(herdr, None, new_workspace=True)
    assert again["created"] == [] and again["workspace"] == res["workspace"]
    assert {tuple(c[1:3]) for c in herdr.calls} == {("workspace", "list"), ("workspace", "focus")}


def test_tab_failures_are_reported_not_raised(env):
    def boom(argv, cwd):
        return _res(None, code=1, stderr="no socket") if argv[1:3] != ["tab", "list"] else _res({"result": {"tabs": []}})

    res = cockpit.ensure_cockpit(FakeRunner(lambda a, c: boom(a, c) if a[1:3] != ["pane", "list"] else
                                            _res({"result": {"panes": []}})),
                                 None, "/tmp/s", "w1", cwd="/w", which=lambda n: "/x")
    assert "error" in res and "tab create" in res["error"]


# -- the CLI ------------------------------------------------------------------------------------------------------


def _fake_result(**over):
    base = {"workspace": "w1", "tab_id": "t1", "operator_pane": "p1", "dash_pane": "p2", "created": ["tab", "operator", "dash"],
            "restarted": [], "operator_note": None}
    base.update(over)
    return base


def test_cli_cockpit_dispatch(monkeypatch, capsys, env):
    seen = {}

    def fake(runner, api, sock, workspace, **kw):
        seen.update(sock=sock, workspace=workspace, **kw)
        return _fake_result()

    monkeypatch.setattr(cockpit, "ensure_cockpit", fake)
    monkeypatch.setattr(cli, "Store", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Store for cockpit")))
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/h.sock")
    assert cli.main(["cockpit", "--workspace", "W"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["cockpit tab started: t1", "operator pane started: p1", "dash pane started: p2"]
    assert seen["workspace"] == "W" and seen["sock"] == "/tmp/h.sock"
    assert seen["operator"] is True and seen["new_workspace"] is False and seen["cwd"] == os.getcwd()

    monkeypatch.setattr(cockpit, "ensure_cockpit", lambda *a, **k: (seen.update(k, workspace=a[3]), _fake_result(
        created=[], restarted=["dash"], operator_pane=None, operator_note="operator skipped (--no-operator)"))[1])
    assert cli.main(["cockpit", "--new-workspace", "--no-operator"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["cockpit tab already running: t1", "operator skipped (--no-operator)", "dash pane restarted: p2"]
    assert seen["new_workspace"] is True and seen["operator"] is False and seen["workspace"] is None


def test_cli_cockpit_needs_a_socket_and_a_workspace(monkeypatch, capsys, env):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    assert cli.main(["cockpit", "--workspace", "W"]) == 2
    assert "HERDR_SOCKET_PATH" in capsys.readouterr().err
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/h.sock")
    assert cli.main(["cockpit"]) == 2
    assert "--workspace" in capsys.readouterr().err


def test_cli_cockpit_reports_an_error(monkeypatch, capsys, env):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/h.sock")
    monkeypatch.setattr(cockpit, "ensure_cockpit", lambda *a, **k: {"error": "herdr tab create failed: x", "created": []})
    assert cli.main(["cockpit", "--workspace", "W"]) == 2
    assert "herdr tab create failed" in capsys.readouterr().err
