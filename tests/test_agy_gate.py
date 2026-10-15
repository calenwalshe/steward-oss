"""The agy hook gate (steward/agy_gate.py): the hook command agy runs per tool call, executed for real with `sh -c`
against a real gate socket. The gate must be fail-closed: only an explicit Steward approval may print allow."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from steward.agy_gate import ALLOW_LINE, ALLOW_RULES, AgyGate, gate_root, hook_command, permission_view

PAYLOAD = {"conversationId": "c1", "stepIdx": 2, "workspacePaths": ["/w"],
           "artifactDirectoryPath": "", "toolCall": {"name": "run_command",
                                                     "args": {"CommandLine": "touch marker", "Cwd": "/w"}}}


def token_home(tmp_path) -> Path:
    th = tmp_path / "userhome"
    (th / ".gemini/antigravity-cli/cache").mkdir(parents=True)
    (th / ".gemini/antigravity-cli/antigravity-oauth-token").write_text('{"token": "t"}')
    (th / ".gemini/antigravity-cli/cache/onboarding.json").write_text('{"onboardingComplete": true}')
    return th


@pytest.fixture
def gate(tmp_path):
    g = AgyGate(tmp_path / "gate", token_home=token_home(tmp_path), answer_timeout_s=5)
    g.materialize()
    g.start()
    yield g
    g.close()


def run_hook(cmd: str, payload=PAYLOAD, timeout=30) -> dict:
    out = subprocess.run(["sh", "-c", cmd], input=json.dumps(payload), capture_output=True, text=True,
                         timeout=timeout)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def answer_when_asked(g: AgyGate, approve: bool, seen: list):
    def go():
        for _ in range(200):
            reqs = g.pending()
            if reqs:
                seen.extend(reqs)
                g.answer(reqs[0].request_id, approve)
                return
            time.sleep(0.02)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def test_the_hook_blocks_until_steward_approves_and_then_prints_allow(gate):
    seen = []
    t = answer_when_asked(gate, True, seen)
    assert run_hook(hook_command(gate.sock_path, 10)) == {"decision": "allow"}
    t.join(2)
    assert seen[0].tool == "Bash" and seen[0].input["command"] == "touch marker"


def test_a_steward_denial_is_printed_as_deny(gate):
    seen = []
    answer_when_asked(gate, False, seen)
    assert run_hook(hook_command(gate.sock_path, 10))["decision"] == "deny"


def test_no_answer_in_time_is_a_deny(tmp_path):
    g = AgyGate(tmp_path / "gate", token_home=token_home(tmp_path), answer_timeout_s=0.3)
    g.materialize()
    g.start()
    try:
        ans = run_hook(hook_command(g.sock_path, 10))
        assert ans["decision"] == "deny" and "no answer" in ans["reason"]
        assert not g.answer(g.pending()[0].request_id, True)  # too late: the hook already denied
    finally:
        g.close()


def test_an_unreachable_steward_is_a_deny(tmp_path):
    ans = run_hook(hook_command(str(tmp_path / "nothing-listens"), 10))
    assert ans["decision"] == "deny" and "unreachable" in ans["reason"]


def test_closing_the_gate_denies_every_open_request(gate):
    res = {}
    t = threading.Thread(target=lambda: res.update(run_hook(hook_command(gate.sock_path, 10))), daemon=True)
    t.start()
    for _ in range(100):
        if gate._open:
            break
        time.sleep(0.02)
    gate.close()
    t.join(5)
    assert res["decision"] == "deny"


@pytest.mark.parametrize("inner", ["exit 3", "sh -c 'kill -9 $$'", "true", "echo", "echo ' '", "echo '{}'",
                                   "echo '{\"decision\":\"ask\"}'", "echo '{\"decision\":\"force_ask\"}'",
                                   "echo '{\"decision\": allow-ish'", "echo '{\"decision\":\"allow\"} junk'",
                                   "echo '{\"decision\":\"ALLOW\"}'", "sleep 0.2; echo"])
def test_whatever_the_relay_does_wrong_the_hook_prints_deny_never_blank_or_ask(inner):
    """agy 1.2.16 runs the tool on blank hook output or `ask` (the broad allow rule answers), so the outer case
    must turn every non-approval into an explicit deny."""
    assert run_hook(hook_command("/unused", 10, inner=inner)) == json.loads(
        '{"decision":"deny","reason":"steward gate: no valid answer from Steward"}')


def test_a_killed_hook_exits_nonzero_which_agy_treats_as_a_block():
    """agy 1.2.16 blocked the tool when the hook process died (exit 137 / "signal: killed"): never a silent allow."""
    out = subprocess.run(["sh", "-c", hook_command("/unused", 10, inner="kill -9 $$")], input="{}",
                         capture_output=True, text=True, timeout=30)
    assert out.returncode != 0 and out.stdout == ""


def test_only_the_exact_allow_line_passes(tmp_path):
    assert run_hook(hook_command("/unused", 10, inner=f"echo '{ALLOW_LINE}'")) == {"decision": "allow"}


def test_a_tool_call_naming_the_gate_is_denied_without_asking(gate):
    for args in ({"CommandLine": f"echo {{}} > {gate.home}/.gemini/config/hooks.json"},
                 {"CommandLine": "rm ~/.gemini/config/hooks.json"},
                 {"TargetFile": str(gate.root / "home/.gemini/antigravity-cli/settings.json")},
                 {"CommandLine": f"ls {gate.sock_dir}"}):
        ans = run_hook(hook_command(gate.sock_path, 10), {**PAYLOAD, "toolCall": {"name": "x", "args": args}})
        assert ans["decision"] == "deny" and "gate" in ans["reason"], args
    assert gate.pending() == [] and len(gate.hard_denied) == 4


def test_agy_artifact_dir_stays_usable_but_not_as_a_way_out(gate):
    art = str(gate.home / ".gemini/antigravity-cli/brain/c1")
    seen = []
    answer_when_asked(gate, True, seen)
    ok = run_hook(hook_command(gate.sock_path, 10), {**PAYLOAD, "artifactDirectoryPath": art, "toolCall": {
        "name": "write_to_file", "args": {"TargetFile": art + "/task.md", "CodeContent": "plan"}}})
    assert ok == {"decision": "allow"} and seen[0].tool == "Write"
    out = run_hook(hook_command(gate.sock_path, 10), {**PAYLOAD, "artifactDirectoryPath": art, "toolCall": {
        "name": "write_to_file", "args": {"TargetFile": art + "/../../../config/hooks.json"}}})
    assert out["decision"] == "deny"


def test_materialize_scopes_the_allow_rule_and_the_hook_to_a_per_spawn_snapshot(tmp_path):
    th = token_home(tmp_path)
    before = {p: p.read_bytes() for p in th.rglob("*") if p.is_file()}
    g = AgyGate(tmp_path / "gate", token_home=th, answer_timeout_s=60)
    env = g.materialize()
    assert env["HOME"] == str(g.home)
    snap = g._snap
    settings = json.loads((snap / "cli/settings.json").read_text())
    assert settings == {"permissions": {"allow": list(ALLOW_RULES)}}
    assert json.loads((snap / "cli/hooks.json").read_text()) == {}
    hooks = json.loads((snap / "config/hooks.json").read_text())
    (name, spec), = hooks.items()
    assert name == g.hook_name and len(name) > 30
    (group,), = [spec["PreToolUse"]]
    assert group["matcher"] == "*" and group["hooks"][0]["timeout"] > 60
    assert g.sock_path in group["hooks"][0]["command"] and " -I -S -c " in group["hooks"][0]["command"]
    tok = g.home / ".gemini/antigravity-cli/antigravity-oauth-token"
    assert tok.read_text() == '{"token": "t"}' and oct(tok.stat().st_mode & 0o777) == "0o600"
    # outside the sandbox the home allows nothing: the mount points are empty
    assert (g.home / ".gemini/antigravity-cli/settings.json").read_text() == ""
    assert not (g.home / ".gemini/config/hooks.json").exists()
    assert {p: p.read_bytes() for p in th.rglob("*") if p.is_file()} == before  # the user's agy home is only read
    g.close()
    assert not snap.exists()


def test_every_spawn_gets_a_new_hook_name_in_a_fresh_snapshot(tmp_path):
    g = AgyGate(tmp_path / "gate", token_home=token_home(tmp_path))
    g.materialize()
    first, snap1 = g.hook_name, g._snap
    g.materialize()
    assert g.hook_name != first and g._snap != snap1
    assert first not in (g._snap / "config/hooks.json").read_text()
    assert snap1.exists()  # an earlier process may still have it mounted; close() removes both
    g.close()
    assert not snap1.exists() and not g._snap.exists()


def mounts(argv):
    """bwrap argv -> [(kind, src, dst)] in order."""
    out, i = [], 1
    while argv[i] != "--":
        a = argv[i]
        if a in ("--bind", "--ro-bind", "--dev-bind"):
            out.append((a, argv[i + 1], argv[i + 2]))
            i += 3
        elif a in ("--tmpfs", "--remount-ro", "--chdir", "--dev"):
            out.append((a, None, argv[i + 1]))
            i += 2
        else:
            out.append((a, None, None))
            i += 1
    return out


def test_the_sandbox_makes_every_gate_config_path_read_only(tmp_path):
    th = token_home(tmp_path)
    g = AgyGate(tmp_path / "gate", token_home=th)
    g.materialize()
    wt = tmp_path / "wt"
    wt.mkdir()
    argv = g.sandbox_argv(wt)
    m = mounts(argv)
    assert m[0] == ("--bind", "/", "/") and argv[0].endswith("bwrap")
    ro = {dst: src for kind, src, dst in m if kind == "--ro-bind"}
    gem = g.home / ".gemini"
    assert ro[str(g.root)] == str(g.root) and ro[str(gem)] == str(gem)
    assert ro[str(gem / "config")] == str(g._snap / "config")
    for name in ("settings.json", "hooks.json", "keybindings.json", "secplugins", "plugins", "plugin_data", "skills"):
        assert ro[str(gem / "antigravity-cli" / name)] == str(g._snap / "cli" / name)
    assert ro[str(g.sock_dir)] == str(g.sock_dir)
    assert ro[str((th / ".gemini").resolve())] == str((th / ".gemini").resolve())  # the user's agy config
    # the only writable gate path is agy's own state dir, mounted BEFORE the read-only config over it
    rw = [dst for kind, _, dst in m if kind == "--bind"]
    assert rw == ["/", str(g.home), str(gem / "antigravity-cli")]
    order = [dst for _, _, dst in m]
    assert order.index(str(gem / "antigravity-cli")) < order.index(str(gem / "antigravity-cli/hooks.json"))
    for name in (".agents", ".agent", "_agents", "_agent", ".gemini"):
        i = order.index(str(wt / name))
        assert m[i][0] == "--tmpfs" and m[i + 1] == ("--remount-ro", None, str(wt / name))
    assert {"--unshare-user", "--disable-userns", "--die-with-parent", "--dev"} <= {k for k, _, _ in m}
    g.close()
    assert not any((wt / n).exists() for n in (".agents", ".agent", "_agents", "_agent", ".gemini"))


def test_without_bwrap_the_gate_refuses_to_run(tmp_path, monkeypatch):
    g = AgyGate(tmp_path / "gate", token_home=token_home(tmp_path))
    g.materialize()
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        g.sandbox_argv(tmp_path)
    g.close()


def test_a_file_where_a_workspace_root_belongs_refuses_to_run(tmp_path):
    g = AgyGate(tmp_path / "gate", token_home=token_home(tmp_path))
    g.materialize()
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".agent").write_text("x")
    with pytest.raises(RuntimeError):
        g.sandbox_argv(wt)
    g.close()


def test_the_relay_runs_on_the_root_owned_system_python(tmp_path):
    g = AgyGate(tmp_path / "gate", token_home=token_home(tmp_path))
    if Path("/usr/bin/python3").is_file():
        assert g.python == "/usr/bin/python3"
    g.close()


def test_no_sign_in_token_refuses_to_build_a_gate(tmp_path):
    g = AgyGate(tmp_path / "gate", token_home=tmp_path / "empty")
    with pytest.raises(FileNotFoundError):
        g.materialize()
    g.close()


def test_the_socket_is_private(gate):
    assert oct(os.stat(gate.sock_dir).st_mode & 0o777) == "0o700"


def test_permission_view_speaks_the_shared_policy_vocabulary():
    assert permission_view("run_command", {"CommandLine": "ls", "Cwd": "/w"}) == ("Bash", {"command": "ls", "cwd": "/w"})
    assert permission_view("write_to_file", {"TargetFile": "/w/a", "CodeContent": "x"})[0] == "Write"
    assert permission_view("replace_file_content", {"TargetFile": "/w/a"}) == ("Edit", {"file_path": "/w/a"})
    assert permission_view("view_file", {"AbsolutePath": "/w/a"}) == ("Read", {"file_path": "/w/a"})
    assert permission_view("read_url_content", {"Url": "https://x"}) == ("WebFetch", {"url": "https://x"})
    assert permission_view("search_web", {"query": "q"}) == ("WebSearch", {"query": "q"})
    assert permission_view("call_mcp_tool", {"ServerName": "s", "ToolName": "t", "Arguments": {"a": 1}}) == (
        "mcp__s__t", {"a": 1})
    assert permission_view("invoke_subagent", {"Subagents": [], "toolAction": "x"}) == (
        "agy:invoke_subagent", {"Subagents": []})


def test_gate_root_is_under_steward_home_per_lane(tmp_path):
    assert gate_root(tmp_path, "job/1", "lane a") == tmp_path / "agy-gate" / "job_1" / "lane_a"


@pytest.mark.parametrize("tool", ["invoke_subagent", "define_subagent", "browser_subagent", "schedule", "run_workflow"])
def test_tools_that_start_another_agent_loop_are_denied_and_never_relayed(gate, tool):
    """agy 1.2.16 runs a subagent's tool calls WITHOUT the PreToolUse hook (audit: Hook-gate route, test 4), so the
    gate closes the only way in: these are denied outright, and no Steward answer can approve them."""
    ans = run_hook(hook_command(gate.sock_path, 10), {**PAYLOAD, "toolCall": {"name": tool, "args": {"Prompt": "x"}}})
    assert ans["decision"] == "deny" and "another agent" in ans["reason"]
    assert gate.pending() == [] and gate.hard_denied == [tool]
