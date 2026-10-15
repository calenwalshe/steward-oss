#!/usr/bin/env python3
"""Hook-gate experiments against agy 1.2.16 in an isolated HOME (never ~/.gemini).

usage: exp.py NAME --prompt TEXT [--allow RULE ...] [--hook MODE] [--hook-timeout N] [--ws-hook MODE]
              [--no-settings] [--wait S]
MODE: deny | allow | crash | sleep:N | malformed | empty | none | killself
Each run gets a fresh agy home under ~/.cache/steward-proof/agyhomes/NAME and a fresh git workspace
under .scratch/hookgate/ws-NAME. Prints the stream tail, stderr, the hook calls and the workspace listing.
"""
import argparse, json, os, shutil, subprocess, sys, time
from pathlib import Path

PROOF = Path.home() / ".cache/steward-proof"
REAL_GEMINI = Path.home() / ".gemini"
HERE = Path(__file__).resolve().parents[3] / ".scratch" / "hookgate"  # work dirs stay out of git
HERE.mkdir(parents=True, exist_ok=True)

HOOKS = {
    "deny": 'echo \'{"decision":"deny","reason":"steward gate: denied"}\'',
    "allow": 'echo \'{"decision":"allow","reason":"steward gate: approved"}\'',
    "crash": 'echo boom >&2; exit 3',
    "malformed": 'echo \'{"decision": allow-ish\'',
    "empty": 'true',
    "killself": 'kill -9 $$',
    "allowbut": 'if grep -q steward-marker {log}/hook-in-$$.json; then echo \'{{"decision":"deny","reason":"steward gate: marker denied"}}\'; else echo \'{{"decision":"allow"}}\'; fi',
}


def hook_script(mode: str, log: Path) -> str:
    if mode.startswith("out:"):
        import shlex
        body = "printf '%s\\n' " + shlex.quote(mode[4:])
    else:
        body = f"sleep {mode.split(':', 1)[1]}; " + HOOKS["allow"] if mode.startswith("sleep:") else HOOKS[mode].replace("{log}", str(log)).replace("{{", "{").replace("}}", "}")
    return f"#!/bin/sh\ncat > {log}/hook-in-$$.json\necho {mode.split(':')[0]} >> {log}/hook-calls.txt\n{body}\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--allow", action="append", default=[])
    ap.add_argument("--hook", default="none")
    ap.add_argument("--hook-timeout", type=int, default=30)
    ap.add_argument("--ws-hook", default="none")
    ap.add_argument("--wait", type=float, default=150)
    ap.add_argument("--ws-hook-name", default="ws-hook")
    ap.add_argument("--ws-root", default=".agents")
    ap.add_argument("--home-hook", default="", help="relative path under the agy HOME for a deny hooks.json")
    ap.add_argument("--mcp", action="store_true", help="configure the marker MCP server in the isolated home")
    ap.add_argument("--ws-disable", action="store_true", help="workspace hooks.json sets the ws hook name enabled:false")
    a = ap.parse_args()

    home = PROOF / "agyhomes" / a.name
    shutil.rmtree(home, ignore_errors=True)
    cli = home / ".gemini/antigravity-cli"
    (cli / "cache").mkdir(parents=True)
    shutil.copy2(REAL_GEMINI / "antigravity-cli/antigravity-oauth-token", cli / "antigravity-oauth-token")
    os.chmod(cli / "antigravity-oauth-token", 0o600)
    shutil.copy2(REAL_GEMINI / "antigravity-cli/cache/onboarding.json", cli / "cache/onboarding.json")
    if a.allow:
        (cli / "settings.json").write_text(json.dumps({"permissions": {"allow": a.allow}}, indent=1))
    gate = PROOF / "gates" / a.name
    shutil.rmtree(gate, ignore_errors=True)
    gate.mkdir(parents=True)
    if a.hook != "none":
        (gate / "hook.sh").write_text(hook_script(a.hook, gate))
        os.chmod(gate / "hook.sh", 0o755)
        cfg = home / ".gemini/config"
        cfg.mkdir(parents=True)
        (cfg / "hooks.json").write_text(json.dumps({"steward-gate": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": str(gate / "hook.sh"), "timeout": a.hook_timeout}]}]}}, indent=1))

    if a.mcp:
        cfg = home / ".gemini/config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "mcp_config.json").write_text(json.dumps({"mcpServers": {"marker": {
            "command": sys.executable, "args": [str(HERE / "mcp_marker.py")],
            "env": {"MARKER_LOG": str(gate / "mcp-calls.txt")}}}}))
    if a.home_hook:
        hp = home / a.home_hook
        hp.parent.mkdir(parents=True, exist_ok=True)
        (gate / "hh.sh").write_text(hook_script("deny", gate).replace("hook-calls.txt", "homehook-calls.txt"))
        os.chmod(gate / "hh.sh", 0o755)
        hp.write_text(json.dumps({"home-hook": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": str(gate / "hh.sh")}]}]}}))
    ws = HERE / f"ws-{a.name}"
    shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    (ws / "seed.txt").write_text("seed\n")
    if a.ws_hook != "none" or a.ws_disable:
        wlog = ws / a.ws_root
        wlog.mkdir(parents=True)
        (wlog / "hook.sh").write_text(hook_script(a.ws_hook if a.ws_hook != "none" else "allow", wlog))
        os.chmod(wlog / "hook.sh", 0o755)
        spec = {"enabled": False} if a.ws_disable else {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "./hook.sh", "timeout": 30}]}]}
        (wlog / "hooks.json").write_text(json.dumps({a.ws_hook_name: spec}))

    env = {k: v for k, v in os.environ.items() if k not in ("META_API_KEY",) and not k.startswith("HERDR_")}
    env.update(HOME=str(home), STEWARD_HOME=str(PROOF / "home"), STEWARD_CONFIG_DIR=str(PROOF / "cfg"),
               TMPDIR=str(PROOF / "tmp"))
    out, err, log = (HERE / f"{a.name}.{x}" for x in ("out", "err", "log"))
    msg = json.dumps({"event": "user", "message": {"role": "user", "content": a.prompt}})
    argv = ["agy", "--input-format", "stream-json", "--output-format", "stream-json", "-p=", "--log-file", str(log)]
    assert "--dangerously-skip-permissions" not in argv
    print("$ cd", ws, "&& echo", repr(msg), "| HOME=" + str(home), " ".join(argv))
    t0 = time.monotonic()
    with open(out, "w") as fo, open(err, "w") as fe:
        p = subprocess.Popen(argv, cwd=ws, env=env, stdin=subprocess.PIPE, stdout=fo, stderr=fe, text=True,
                             start_new_session=True)
        p.stdin.write(msg + "\n")
        p.stdin.close()
        try:
            rc = p.wait(timeout=a.wait)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, 9)
            rc = "TIMEOUT(killed)"
    print(f"rc={rc} elapsed={time.monotonic() - t0:.1f}s")
    for line in out.read_text().splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            print("RAW", line[:300])
            continue
        if m.get("event") == "init":
            continue
        su = m.get("step_update") or {}
        if su.get("step_type") == "tool" or m.get("event") == "result":
            s = json.dumps(m)
            print(s if len(s) < 900 else s[:900] + "…")
    print("--- stderr:\n" + err.read_text()[-1500:])
    calls = gate / "hook-calls.txt"
    print("--- steward hook calls:", calls.read_text().split() if calls.exists() else [])
    for f in sorted(gate.glob("hook-in-*.json")):
        try:
            tc = json.loads(f.read_text()).get("toolCall")
        except ValueError:
            tc = f.read_text()[:200]
        print("   ", json.dumps(tc)[:300])
    if (gate / "homehook-calls.txt").exists():
        print("--- HOME hook calls:", (gate / "homehook-calls.txt").read_text().split())
    if (gate / "mcp-calls.txt").exists():
        print("--- MCP server calls:", (gate / "mcp-calls.txt").read_text().splitlines())
    wcalls = ws / a.ws_root / "hook-calls.txt"
    if wcalls.exists():
        print("--- workspace hook calls:", wcalls.read_text().split())
    print("--- workspace files:", sorted(str(p.relative_to(ws)) for p in ws.rglob("*")
                                       if p.is_file() and ".git" not in p.relative_to(ws).parts[:1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
