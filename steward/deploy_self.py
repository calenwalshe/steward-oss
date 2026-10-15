"""`steward deploy-self`: Steward updates itself after a merge into its own repo, no operator steps.

Order (the first refusal stops everything, nothing is half-restarted):
  1. import check of the new code in a fresh interpreter (broken import -> refuse, change nothing);
  2. never while a permission answer is in flight (`restart_guard.check_all`, the lost-approval debounce);
  3. copy deploy/cockpit/steward-dash* into ~/bin when they differ;
  4. `systemctl --user restart steward-mcp`;
  5. the control pane re-execs itself in place (relayed `self_exec` op, same pane, like runner self-restart), then
     is VERIFIED: its `ping` must report a new process start and the deployed HEAD as its loaded code, or the deploy
     fails naming both shas (an accepted `self_exec` alone proves nothing; `ps` start time survives an execv);
  6. the resident dashapp re-execs itself in place when it sees the signal file (`signal_path`) touched;
  7. `steward health` is run and its result reported.
One deploy at a time (non-blocking flock). Everything external is injectable; `--dry-run` only prints the plan.

`fresh_deploy` / `fresh_preflight` run the same in a NEW interpreter at `root` (`python -m steward.deploy_self`): a
long-lived caller (the landing train) has the old code cached in sys.modules, so a lazy import of a changed module
in-process would mix new and old code and fail on a correct landing (train 30, 2026-10-13: the new cli imported
`contract.PAUSE_VALUES` from the stale cached contract)."""
from __future__ import annotations

import fcntl
import filecmp
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from steward import restart_guard, selfrestart, stability
from steward.runner_exec import Runner, SubprocessRunner
from steward.store import Store

ENV_SWITCH = "STEWARD_AUTODEPLOY"
LAUNCHERS = "deploy/cockpit"
LAUNCHER_GLOB = "steward-*"
LOCK_NAME = "deploy-self.lock"
HEALTH_TRIES = 10
HEALTH_WAIT_S = 1.0
RELOAD_WAIT_S = 60.0  # how long the re-exec'd control pane has to answer a ping on the new code
RELOAD_POLL_S = 1.0
PING_TIMEOUT_S = 5.0


def autodeploy_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(ENV_SWITCH, "1")).strip().lower() not in {"0", "false", "no", "off"}


def signal_path(env=None) -> str:
    """The file whose mtime tells the resident dashapp to re-exec (it compares with its own start time)."""
    env = os.environ if env is None else env
    return os.path.join(env.get("XDG_RUNTIME_DIR") or "/tmp", "steward-dash.reexec")


def launcher_changes(root: Path, bin_dir: Path) -> list[tuple[Path, Path]]:
    src_dir = Path(root) / LAUNCHERS
    out = []
    for src in sorted(src_dir.glob(LAUNCHER_GLOB)):
        dst = Path(bin_dir) / src.name
        if src.is_file() and not (dst.is_file() and filecmp.cmp(src, dst, shallow=False)):
            out.append((src, dst))
    return out


def plan(root: Path, bin_dir: Path) -> list[str]:
    copies = launcher_changes(root, bin_dir)
    return ["verify the new code imports",
            "check no permission answer is in flight",
            f"copy {len(copies)} launcher file(s) to {bin_dir}" + (": " + ", ".join(d.name for _, d in copies) if copies else ""),
            "systemctl --user restart steward-mcp",
            "re-exec the control pane in place and verify it runs the deployed HEAD",
            "re-exec the resident dashapp in place",
            "run steward health"]


@contextmanager
def deploy_lock(store: Store):
    path = Path(store.home) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def default_relay(store: Store) -> Callable[[str, dict], dict]:
    def relay(op: str, args: dict) -> dict:
        from steward import cli

        try:
            return cli._relay(store, op, args, origin="deploy-self")
        except cli._RelayFailed as exc:
            return {"accepted": False, "detail": str(exc)}
    return relay


def default_ping(store: Store) -> Callable[[], dict | None]:
    """The control pane's `ping` receipt (pid, head, started_at, root), or None when it did not answer in time."""
    def ping() -> dict | None:
        import uuid

        from steward import cli, control_pane

        rid = f"deploy-ping-{uuid.uuid4().hex[:12]}"
        try:
            rec = cli._relay(store, "ping", {}, request_id=rid, origin="deploy-self", timeout_s=PING_TIMEOUT_S)
        except cli._RelayFailed:
            return None
        finally:
            store.kv_delete(control_pane._CONTROL_KV_JOB, control_pane._receipt_key(rid))
        return rec if rec.get("accepted") else None
    return ping


def verify_reload(before: dict | None, target: str, ping_fn: Callable[[], dict | None], *,
                  clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                  wait_s: float = RELOAD_WAIT_S, poll_s: float = RELOAD_POLL_S) -> tuple[bool, str]:
    """Did the control pane really re-exec onto `target`? Yes only when a ping answers from a process that started
    after `before` (a different `started_at`; same pid is expected: execv) AND reports `target` as its loaded head
    (when `target` is known). Returns (ok, what was seen)."""
    deadline = clock() + wait_s
    last: dict | None = None
    while True:
        pong = ping_fn()
        if pong is not None:
            last = pong
            fresh = before is None or pong.get("started_at") != before.get("started_at")
            if fresh and (not target or pong.get("head") == target):
                return True, f"control runs {str(pong.get('head') or '?')[:12]} (pid {pong.get('pid')})"
        if clock() + poll_s > deadline:
            break
        sleep(poll_s)
    if last is None:
        return False, f"control did not reload: no ping answer within {wait_s:.0f}s of the re-exec"
    loaded = str(last.get("head") or "?")[:12]
    if before is not None and last.get("started_at") == before.get("started_at"):
        return False, (f"control did not reload: pid {last.get('pid')} still runs {loaded} (same process start), "
                       f"deployed HEAD {target[:12] or '?'}")
    return False, f"control did not reload onto the deployed HEAD: runs {loaded}, deployed HEAD {target[:12]}"


def default_health(store: Store) -> Callable[[], tuple[bool, str]]:
    def health() -> tuple[bool, str]:
        from steward import health as health_module

        comps = health_module.check_all(store, api=None, ping=True)
        red = [c.name for c in comps if c.ok is False]
        return (not red, "ok" if not red else "RED: " + ", ".join(red))
    return health


def _fail(store: Store, steps: list[str], why: str, *, dry_run=False, transient: bool = True) -> dict:
    """A refusal: nothing was restarted. `transient` = it may go through later unchanged (another deploy running,
    an answer in flight); False = the code itself is the problem (it does not import)."""
    return {"ok": False, "refused": True, "transient": transient, "dry_run": dry_run, "steps": steps, "detail": why,
            "summary": f"deploy-self refused: {why}"}


def preflight(store: Store, *, health_fn: Callable[[], tuple[bool, str]] | None = None,
              clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> str | None:
    """Could a deploy-self run right now and be judged fairly? None = yes; else why not. Changes nothing and never
    waits: no other deploy running, no permission answer in flight, health already green (a red health before the
    deploy would be blamed on the landed code)."""
    with deploy_lock(store) as got:
        if not got:
            return "another deploy-self is already running"
    why = restart_guard.check_all(store, clock=clock, sleep=sleep, wait_s=0)
    if why:
        return why.removeprefix("deploy refused: ")
    ok, health = (health_fn or default_health(store))()
    return None if ok else f"health not green before the deploy ({health})"


def deploy(store: Store, *, root: Path | str = stability.REPO_ROOT, bin_dir: Path | str | None = None,
           dry_run: bool = False, runner: Runner | None = None, relay_fn: Callable[[str, dict], dict] | None = None,
           import_check_fn: Callable[[], str | None] | None = None,
           health_fn: Callable[[], tuple[bool, str]] | None = None, signal_file: str | None = None,
           clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
           health_tries: int = HEALTH_TRIES, ping_fn: Callable[[], dict | None] | None = None,
           head_fn: Callable[[], str] | None = None, reload_wait_s: float = RELOAD_WAIT_S) -> dict:
    root = Path(root)
    bin_dir = Path(bin_dir) if bin_dir else Path.home() / "bin"
    steps = plan(root, bin_dir)
    if dry_run:
        return {"ok": True, "dry_run": True, "steps": steps, "summary": "deploy-self dry-run: " + "; ".join(steps)}
    with deploy_lock(store) as got:
        if not got:
            return _fail(store, steps, "another deploy-self is already running")
        err = (import_check_fn or (lambda: selfrestart.import_check(root)))()
        if err:
            return _fail(store, steps, f"the new code does not import ({err})", transient=False)
        why = restart_guard.check_all(store, clock=clock, sleep=sleep)
        if why:
            return _fail(store, steps, why.removeprefix("deploy refused: "))
        runner = runner or SubprocessRunner()
        relay_fn = relay_fn or default_relay(store)
        done: list[str] = []
        problems: list[str] = []
        copies = launcher_changes(root, bin_dir)
        if copies:
            bin_dir.mkdir(parents=True, exist_ok=True)
            for src, dst in copies:
                shutil.copy2(src, dst)
            done.append(f"copied {', '.join(d.name for _, d in copies)}")
        r = runner.run(["systemctl", "--user", "restart", "steward-mcp"], root, timeout=60)
        if r.exit_code == 0:
            done.append("steward-mcp")
        else:
            problems.append(f"steward-mcp restart failed ({(r.stderr or r.stdout).strip()[:100]})")
        ping_fn = ping_fn or default_ping(store)
        target = (head_fn or (lambda: stability.repo_head(root)))()
        before = ping_fn()
        rec = relay_fn("self_exec", {})
        if rec.get("accepted"):
            reloaded, seen = verify_reload(before, target, ping_fn, clock=clock, sleep=sleep, wait_s=reload_wait_s)
            if reloaded:
                done.append("control")
            else:
                problems.append(seen)
        else:
            problems.append(f"control re-exec failed ({str(rec.get('detail'))[:100]})")
        path = signal_file or signal_path()
        try:
            Path(path).touch()
            os.utime(path, (clock(), clock()))
            done.append("dash")
        except OSError as exc:
            problems.append(f"dash signal failed ({exc})")
        health_fn = health_fn or default_health(store)
        ok, health = False, "not run"
        for i in range(max(1, health_tries)):
            ok, health = health_fn()
            if ok:
                break
            if i + 1 < health_tries:
                sleep(HEALTH_WAIT_S)
        summary = "restarted " + (", ".join(done) or "nothing") + f"; health {health}"
        if problems:
            summary += "; " + "; ".join(problems)
        good = ok and not problems
        if good:  # the control runs the deployed HEAD: runners waiting on a self-merge may follow now
            from steward import deploy_pending, deployed

            deploy_pending.clear(store)
            try:  # what the watchdog restores dead processes from (issue W)
                deployed.record(store.home, target, str(root))
            except OSError:
                pass
        return {"ok": good, "dry_run": False, "steps": steps, "done": done, "problems": problems,
                "health": health, "deployed_head": target, "summary": "deploy-self: " + summary}


FRESH_TIMEOUT_S = 900.0   # import check + mcp restart + control reload wait + health retries, with slack
RESULT_PREFIX = "deploy-self-result: "


def _fresh(store: Store, root: Path | str, op: str, timeout: float) -> tuple[dict | None, str]:
    """Run `python -m steward.deploy_self <op>` in a new interpreter at `root`. (parsed result, why there is none)."""
    env = {**os.environ, "STEWARD_HOME": str(store.home)}
    try:
        p = subprocess.run([sys.executable, "-m", "steward.deploy_self", op, "--root", str(root),
                            "--home", str(store.home)], cwd=str(root), env=env, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout:.0f}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    for line in reversed(p.stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                res = json.loads(line[len(RESULT_PREFIX):])
            except ValueError:
                break
            if isinstance(res, dict):
                return res, ""
            break
    return None, (p.stderr.strip().splitlines() or [f"exit {p.returncode}, no result"])[-1][:300]


def fresh_deploy(store: Store, *, root: Path | str = stability.REPO_ROOT, timeout: float = FRESH_TIMEOUT_S) -> dict:
    """`deploy` in a fresh interpreter at `root` (the new HEAD). No result = the new code could not even run its own
    deploy (it does not import): a refusal about the code itself, i.e. red."""
    res, why = _fresh(store, root, "deploy", timeout)
    if res is not None:
        return res
    if why.startswith("timed out"):
        return {"ok": False, "dry_run": False, "steps": [], "detail": why, "summary": f"deploy-self: {why}"}
    return _fail(store, [], f"the new code does not import ({why})", transient=False)


def fresh_preflight(store: Store, *, root: Path | str = stability.REPO_ROOT, timeout: float = 120.0) -> str | None:
    """`preflight` in a fresh interpreter at `root`: None = a deploy could run now, else why not."""
    res, why = _fresh(store, root, "preflight", timeout)
    if res is None:
        return f"pre-flight could not run ({why})"
    return res.get("why") or None


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m steward.deploy_self")
    ap.add_argument("op", choices=["deploy", "preflight"])
    ap.add_argument("--root", default=str(stability.REPO_ROOT))
    ap.add_argument("--home", default=None)
    args = ap.parse_args(argv)
    store = Store(args.home)
    if args.op == "deploy":
        res = deploy(store, root=Path(args.root))
    else:
        res = {"why": preflight(store)}
    print(RESULT_PREFIX + json.dumps(res, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
