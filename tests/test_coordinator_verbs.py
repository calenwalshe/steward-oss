import json
import subprocess
import sys

import pytest

from steward.coordinator import verbs
from steward.coordinator.config import Settings

J = "11111111-2222-3333-4444-555555555555"


def test_builders_shape():
    assert verbs.nudge(J, "hi").argv == ["steer", J, "hi", "--verb", "nudge"]
    assert verbs.focus(J, "hi").argv == ["steer", J, "hi", "--verb", "focus"]
    assert verbs.avoid(J, "hi").argv == ["steer", J, "hi", "--verb", "avoid"]
    assert verbs.send_back(J, "x", "w1").argv == ["send-back", J, "x", "--workspace", "w1"]
    assert verbs.approve(J, "a1").argv == ["approve", J, "a1"]
    assert verbs.deny(J, "a1").argv == ["deny", J, "a1"]
    assert verbs.stop(J).argv == ["stop", J]
    assert verbs.runner_restart(J).argv == ["runner-restart", J]
    assert verbs.answer_recommended(J, ["Q1", "Q2"]).argv == ["answer", J, "--accept-recommended", "Q1", "Q2"]
    assert verbs.answer_option(J, "Q1", "a").argv == ["answer", J, "Q1", "--option", "a"]
    assert verbs.answer_option(J, "Q1", "a", cite="x.md:1").argv == ["answer", J, "Q1", "--option", "a", "--note", "x.md:1"]
    assert verbs.answer_other(J, "Q1", "ok cite: a.md:3").argv == ["answer", J, "Q1", "--other", "ok cite: a.md:3"]
    u = verbs.up("w9")
    assert u.argv == ["up", "--workspace", "w9"] and u.direct
    assert verbs.hold(J, "e1", "t").argv == ["coordinator", "hold", J, "--episode", "e1", "--trigger", "t"]


def test_episode_argv():
    e = verbs.episode(J, "e1", "t", "resolved", "d", ["approve a1", "stop"]).argv
    assert e == ["coordinator", "episode", J, "--episode", "e1", "--trigger", "t", "--outcome", "resolved",
                 "--diagnosis", "d", "--action", "approve a1", "--action", "stop"]
    e = verbs.episode(J, "e1", "t", "asked", "d", [], rule_task="T1", model_used=True, ask="line").argv
    assert e[-5:] == ["--rule-task", "T1", "--model-used", "--ask", "line"][-5:]
    assert "--rule-task" in e and "--model-used" in e and e[e.index("--ask") + 1] == "line"


def test_check_command_ok():
    for argv in (["steer", J, "t", "--verb", "nudge"], ["send-back", J, "x", "--workspace", "w"],
                 ["answer", J, "--accept-recommended", "Q1"], ["approve", J, "a"], ["deny", J, "a"],
                 ["runner-restart", J], ["stop", J], ["answer", J, "q", "--other", "yes. cite: docs/x.md:12"],
                 ["coordinator", "hold", J, "--episode", "e", "--trigger", "t"],
                 ["coordinator", "episode", J, "--episode", "e"]):
        assert verbs.check_command(J, argv) is None, argv
    assert verbs.check_command(None, ["up", "--workspace", "w1"]) is None
    assert verbs.check_command(None, ["up", "--workspace", "w1"], workspace="w1") is None


@pytest.mark.parametrize("flag", ["--origin", "--urgent", "--then-accept", "--write-source", "--and-continue",
                                  "--done", "--release", "--home", "--defer", "--model", "--goal-append"])
def test_forbidden_flags(flag):
    assert verbs.check_command(J, ["stop", J, flag, "x"])
    assert verbs.check_command(J, ["stop", J, flag + "=x"])


def test_check_command_refusals():
    assert verbs.check_command(J, [])
    assert verbs.check_command(J, ["steer", "other", "t", "--verb", "nudge"])
    assert verbs.check_command(J, ["coordinator", "hold", "other"])
    assert verbs.check_command(J, ["steer", J, "t", "--verb", "goal"])
    assert verbs.check_command(J, ["steer", J, "t"])
    assert verbs.check_command(J, ["answer", J, "q", "--other", "no citation"])
    assert verbs.check_command(None, ["up", "--sock", "s"])
    assert verbs.check_command(None, ["up", "--workspace", "w1", "x"])
    assert verbs.check_command(None, ["up"])
    assert verbs.check_command(None, ["up", "--workspace", "w1"], workspace="w2")
    assert verbs.check_command(J, ["coordinator", "overturn", J])
    assert verbs.check_command(J, ["coordinator"])
    assert verbs.check_command(None, ["stop", J])
    for v in ("accept", "release", "incorporate", "launch", "new", "deploy-self", "fix-merge", "pause", "resume"):
        assert verbs.check_command(J, [v, J]), v


class _R:
    def __init__(self, rc=0, out="ok", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_run_forced_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/s")
    seen = {}

    def fake(argv, **kw):
        seen.update(argv=argv, **kw)
        return _R()

    st = Settings(steward_home=tmp_path / "h", steward_repo=tmp_path / "r")
    out = verbs.run(verbs.nudge(J, "t"), settings=st, workspace="w", run=fake)
    assert out["ok"] and out["rc"] == 0 and out["out"] == "ok"
    assert seen["argv"][:3] == [sys.executable, "-m", "steward.cli"] and out["argv"] == seen["argv"]
    assert seen["cwd"] == st.steward_repo and seen["capture_output"] and seen["text"] and seen["timeout"] == 180
    env = seen["env"]
    assert env["STEWARD_FORCED_ORIGIN"] == "coordinator" and env["STEWARD_HOME"] == str(st.steward_home)
    assert env["PYTHONPATH"] == str(st.steward_repo) and env["HERDR_SOCKET_PATH"] == "/s"
    assert verbs.run(verbs.up("w"), settings=st, workspace="w", run=fake)["ok"]
    assert verbs.run(verbs.hold(J, "e", "t"), settings=st, workspace="w", run=fake)["ok"]


def test_run_refuses_without_running(tmp_path):
    st = Settings(steward_home=tmp_path, steward_repo=tmp_path)
    calls = []
    for v in (verbs.Verb(["accept", J], "accept"), verbs.up("w1")):
        out = verbs.run(v, settings=st, workspace="w2" if v.argv[0] == "up" else "w1", run=lambda *a, **k: calls.append(1))
        assert not out["ok"] and out["rc"] == -1 and out["err"].startswith("refused:")
    assert not calls


def test_run_timeout_and_error(tmp_path):
    st = Settings(steward_home=tmp_path, steward_repo=tmp_path)

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    def boom(argv, **kw):
        raise OSError("nope")

    out = verbs.run(verbs.stop(J), settings=st, workspace="w", run=slow)
    assert not out["ok"] and out["rc"] == 124
    out = verbs.run(verbs.stop(J), settings=st, workspace="w", run=boom)
    assert not out["ok"] and out["rc"] == -1 and "nope" in out["err"]


def test_workspace_from_control_json(tmp_path):
    (tmp_path / "control.json").write_text(json.dumps({"pane": "w7:1-2"}))

    def nope(*a, **k):
        raise AssertionError("herdr called")

    assert verbs.workspace(tmp_path, run=nope) == "w7"


def test_workspace_from_herdr(tmp_path):
    out = json.dumps({"result": {"panes": [{"pane_id": "w3:1", "workspace_id": "w3"}]}})
    assert verbs.workspace(tmp_path, run=lambda *a, **k: _R(0, out)) == "w3"
    out = json.dumps({"result": {"panes": [{"pane_id": "w4:1"}]}})
    assert verbs.workspace(tmp_path, run=lambda *a, **k: _R(0, out)) == "w4"


def test_workspace_none(tmp_path):
    assert verbs.workspace(tmp_path, run=lambda *a, **k: _R(1, "", "x")) is None
    assert verbs.workspace(tmp_path, run=lambda *a, **k: _R(0, "garbage")) is None

    def boom(*a, **k):
        raise FileNotFoundError("herdr")

    assert verbs.workspace(tmp_path, run=boom) is None


@pytest.mark.parametrize("argv", [
    ["answer", J, "q", "--other=no citation"],
    ["answer", J, "q", "--other", "a cite: x:1", "--other", "none"],
    ["answer", J, "q", "--other", "a cite: x:1", "--other=none"],
    ["steer", J, "t", "--verb", "nudge", "--verb=goal"],
    ["steer", J, "t", "--verb=accept"],
    ["steer", J, "t", "--verb=goal"],
    ["steer", J, "t", "--urg", "--verb", "nudge"],
    ["steer", J, "t", "--urgent=1", "--verb", "nudge"],
    ["steer", J, "--verb", "nudge"][:0] + ["steer", J, "--leading", "--verb", "nudge"],
    ["answer", J, "q", "--oth", "x cite: y"],
    ["answer", J, "q", "--option", "a", "--option", "b"],
    ["send-back", J, "x", "--workspace", "w", "--workspace", "v"],
    ["send-back", J, "x", "--work", "w"],
    ["stop", J, "--force"],
    ["coordinator", "hold", J, "--episode", "e", "--trigger", "t", "--outcome", "x"],
    ["coordinator", "episode", J, "--model-used=1"],
    ["coordinator", "episode", J, "--episode", "e", "--episode", "f"],
    ["steer", J, "t", "--verb"],
])
def test_option_bypasses_refused(argv):
    assert verbs.check_command(J, argv), argv


def test_option_positive_forms():
    assert verbs.check_command(J, ["steer", J, "t", "--verb=nudge"]) is None
    assert verbs.check_command(J, ["answer", J, "q", "--option", "k", "--note", "cite: docs/x.md:3"]) is None
    assert verbs.check_command(J, ["answer", J, "q", "--other=ok cite: a:1"]) is None
    assert verbs.check_command(J, ["coordinator", "episode", J, "--episode", "e", "--action", "a", "--action", "b",
                                   "--model-used", "--ask", "x"]) is None
