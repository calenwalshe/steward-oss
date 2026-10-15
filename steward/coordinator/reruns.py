"""False-red reruns (spec §5.12, R4/R5).

A red verification names failing test ids (JUnit `classname::name`, baseline.py `_parse_junit`). This module re-runs
exactly those ids on a detached checkout of the job's head commit — twice under a hermetic overlay env, once under
the daemon's own env (the "live" run, which predicts Steward's re-verification) — and, only when a hermetic head run
failed and the base commit is known, twice on a detached checkout of the base. `classify` turns the runs into one
`Outcome`. Every stop condition (budget, too many ids, unmappable id, worktree failure, broken run) yields
`inconclusive` with a `note`, never an exception.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from steward import baseline
from steward.runner_exec import CommandResult, SubprocessRunner

Outcome = Literal["env_dependent", "flaky", "env_live_red", "preexisting", "real", "inconclusive"]

PASS = "passed"
FAIL = ("failed", "error")
HERMETIC_UNSET = ("HERDR_PANE_ID", "HERDR_WORKSPACE_ID")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass
class RerunResult:
    outcome: Outcome
    head_sha: str
    base_sha: str | None
    ids: list[str]
    node_ids: list[str] = field(default_factory=list)
    head_runs: list[dict] = field(default_factory=list)
    live_run: dict | None = None
    base_runs: list[dict] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    durations_s: list[float] = field(default_factory=list)
    note: str = ""


# ---------- ids ----------

def failing_ids(ev_evidence: dict) -> list[str]:
    """`evidence["new_failures"]` of a red `verification recorded` event, deduped in order."""
    raw = (ev_evidence or {}).get("new_failures") if isinstance(ev_evidence, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item and item not in out:
            out.append(item)
    return out


failing_ids_from_verification = failing_ids


def _inside(tree: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(tree.resolve())
        return True
    except (ValueError, OSError):
        return False


def _is_node_form(test_id: str) -> bool:
    return ".py::" in test_id


def node_id(test_id: str, tree: Path) -> str | None:
    """JUnit id -> pytest node id inside `tree`: "tests.test_x.TestC::t[1]" -> "tests/test_x.py::TestC::t[1]".

    The shortest dotted prefix of the classname that is a `.py` file under `tree` is the module; the remaining
    dotted parts are classes. An id already in node form passes through when its file exists. Else None."""
    tree = Path(tree)
    if not isinstance(test_id, str) or "::" not in test_id or test_id.startswith("-"):
        return None
    if _is_node_form(test_id):
        file_part = test_id.split("::", 1)[0]
        path = tree / file_part
        if Path(file_part).is_absolute() or ".." in Path(file_part).parts or not path.is_file() \
                or not _inside(tree, path):
            return None
        return test_id
    classname, name = test_id.split("::", 1)
    parts = classname.split(".")
    if not name or not classname or any(not p for p in parts) or any(p in (".", "..") or "/" in p for p in parts):
        return None
    for k in range(1, len(parts) + 1):
        rel = "/".join(parts[:k]) + ".py"
        path = tree / rel
        if path.is_file() and _inside(tree, path):
            return "::".join([rel, *parts[k:], name])
    return None


def _junit_key(test_id: str) -> str:
    """The key `_parse_junit` reports for `test_id` (node-form ids are converted back to classname::name)."""
    if not _is_node_form(test_id):
        return test_id
    segs = test_id.split("::")
    module = segs[0][:-3].replace("/", ".")
    return "::".join([".".join([module, *segs[1:-1]]), segs[-1]])


# ---------- classification ----------

def _all(run: dict | None, ids: list[str], want) -> bool:
    return run is not None and all(run.get(i, "error") in want for i in ids)


def classify(head_runs: list[dict], live_run: dict | None, base_runs: list[dict], ids: list[str]) -> Outcome:
    """Spec §5.12. A missing id counts as `error`. `live_run is None` with a green head is `inconclusive` (the
    live prediction is missing); fewer than two head (or, when needed, base) runs is `inconclusive`."""
    if not ids or len(head_runs) < 2:
        return "inconclusive"
    h1, h2 = head_runs[0], head_runs[1]
    if _all(h1, ids, (PASS,)) and _all(h2, ids, (PASS,)):
        if live_run is None:
            return "inconclusive"
        if _all(live_run, ids, (PASS,)):
            return "env_dependent"
        if any(live_run.get(i, "error") in FAIL for i in ids):
            return "env_live_red"
        return "inconclusive"
    for i in ids:
        a, b = h1.get(i, "error"), h2.get(i, "error")
        if (a == PASS and b in FAIL) or (b == PASS and a in FAIL):
            return "flaky"
    if _all(h1, ids, FAIL) and _all(h2, ids, FAIL) and len(base_runs) >= 2:
        b1, b2 = base_runs[0], base_runs[1]
        if _all(b1, ids, FAIL) and _all(b2, ids, FAIL):
            return "preexisting"
        if _all(b1, ids, (PASS,)) and _all(b2, ids, (PASS,)):
            return "real"
    return "inconclusive"


# ---------- execution ----------

class OverlayRunner:
    """Wraps a Runner: every call's env becomes (given env or os.environ) + `overlay`, minus `unset`."""

    def __init__(self, inner, overlay: dict[str, str], unset: tuple[str, ...] = HERMETIC_UNSET):
        self.inner, self.overlay, self.unset = inner, dict(overlay), tuple(unset)

    def _env(self, env: dict | None) -> dict:
        merged = {**(os.environ if env is None else env), **self.overlay}
        for key in self.unset:
            merged.pop(key, None)
        return merged

    def run(self, argv, cwd, *, timeout=None, env=None) -> CommandResult:
        return self.inner.run(argv, cwd, timeout=timeout, env=self._env(env))

    def shell_line(self, line, cwd, *, timeout=None, env=None) -> CommandResult:
        return self.inner.shell_line(line, cwd, timeout=timeout, env=self._env(env))


class _Stop(Exception):
    pass


def _default_worktree_fn(runner):
    def add(repo: Path, sha: str, dest: Path) -> Path:
        res = runner.run(["git", "worktree", "add", "--detach", str(dest), sha], cwd=repo)
        if res.exit_code != 0:
            raise RuntimeError((res.stderr or res.stdout or f"exit {res.exit_code}").strip()[:300])
        return dest
    return add


def _remove_worktree(runner, repo: Path, dest: Path) -> None:
    try:
        runner.run(["git", "worktree", "remove", "--force", str(dest)], cwd=repo)
    except Exception:  # noqa: BLE001 - cleanup is best effort; rmtree below still runs
        pass
    shutil.rmtree(dest, ignore_errors=True)


def _fresh(path: Path) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _line(node_ids: list[str]) -> str:
    return (f"{shlex.quote(sys.executable)} -m pytest -q -p no:randomly -p no:cacheprovider "
            + " ".join(shlex.quote(n) for n in node_ids))


def rerun(*, repo: Path, head_sha: str, base_sha: str | None, ids: list[str], scratch: Path, settings,
          run_check=baseline.run_check, runner=None, worktree_fn=None, clock=time.monotonic) -> RerunResult:
    """Head hermetic x2 -> head live x1 -> base hermetic x2 (only if a hermetic head run failed and `base_sha` is
    known), each in a detached worktree under `scratch`, removed in `finally`. Never raises for run problems."""
    repo, scratch = Path(repo), Path(scratch)
    runner = runner if runner is not None else SubprocessRunner()
    worktree_fn = worktree_fn or _default_worktree_fn(runner)
    ids = list(dict.fromkeys(ids or []))
    res = RerunResult(outcome="inconclusive", head_sha=head_sha, base_sha=base_sha, ids=ids)
    t0 = clock()
    run_no = [0]
    made: list[Path] = []

    def stop(note: str):
        res.note = note
        raise _Stop(note)

    def tree_for(kind: str, sha: str) -> Path:
        dest = scratch / f"{kind}-{sha[:12]}"
        if dest.exists():  # a leftover from a crashed earlier set: never run on a stale tree
            _remove_worktree(runner, repo, dest)
        made.append(dest)
        try:
            return Path(worktree_fn(repo, sha, dest))
        except Exception as exc:  # noqa: BLE001
            stop(f"{kind} worktree could not be created at {sha[:12]}: {exc}")

    def map_ids(tree: Path, kind: str) -> list[str]:
        nodes = []
        for i in ids:
            n = node_id(i, tree)
            if n is None:
                stop(f"id not mappable to a test file in the {kind} tree: {i}")
            nodes.append(n)
        return nodes

    def one(label: str, tree: Path, nodes: list[str], hermetic: bool) -> dict:
        elapsed = clock() - t0
        if elapsed >= settings.rerun_total_s:
            stop(f"rerun budget used ({elapsed:.0f}s of {settings.rerun_total_s:.0f}s) before {label}")
        run_no[0] += 1
        n = run_no[0]
        line = _line(nodes)
        if hermetic:
            overlay = {
                "STEWARD_HOME": str(_fresh(scratch / f"home-{n}")),
                "STEWARD_CONFIG_DIR": str(_fresh(scratch / f"cfg-{n}")),
                "GIT_OPTIONAL_LOCKS": "0",
                "STEWARD_LIVE_ROOT": str(tree),
                "HERDR_SOCKET_PATH": str(scratch / "no.sock"),
            }
            use = OverlayRunner(runner, overlay)
        else:
            use = runner
        timeout = min(float(settings.rerun_run_s), float(settings.rerun_total_s) - elapsed)
        started = clock()
        try:
            cr = run_check(use, line, tree, scratch / f"run-{n}.xml", timeout_s=timeout, fallback=False)
        except Exception as exc:  # noqa: BLE001 - a crashing run is a broken run
            cr = baseline.CheckRun(exit_code=-1, tests={}, broken=f"{type(exc).__name__}: {exc}")
        finally:
            if hermetic:
                shutil.rmtree(scratch / f"home-{n}", ignore_errors=True)
                shutil.rmtree(scratch / f"cfg-{n}", ignore_errors=True)
        res.commands.append(f"{label}: {line}")
        res.durations_s.append(round(clock() - started, 3))
        tests = cr.tests or {}
        outcome = {i: ("error" if cr.broken else tests.get(_junit_key(i), "error")) for i in ids}
        if label.startswith("head hermetic"):
            res.head_runs.append(outcome)
        elif label.startswith("base"):
            res.base_runs.append(outcome)
        else:
            res.live_run = outcome
        if cr.broken:
            stop(f"{label} run broken: {cr.broken}")
        return outcome

    try:
        if not ids:
            stop("no failing ids to rerun")
        if len(ids) > settings.rerun_max_ids:
            stop(f"{len(ids)} failing ids is more than rerun_max_ids={settings.rerun_max_ids}")
        if not isinstance(head_sha, str) or not _SHA_RE.match(head_sha):
            stop(f"head sha is not a commit id: {head_sha!r}")
        if base_sha is not None and not _SHA_RE.match(str(base_sha)):
            stop(f"base sha is not a commit id: {base_sha!r}")
        scratch.mkdir(parents=True, exist_ok=True)
        head_tree = tree_for("head", head_sha)
        res.node_ids = map_ids(head_tree, "head")
        one("head hermetic 1", head_tree, res.node_ids, True)
        one("head hermetic 2", head_tree, res.node_ids, True)
        one("head live", head_tree, res.node_ids, False)
        head_failed = any(r.get(i) != PASS for r in res.head_runs for i in ids)
        if head_failed:
            if not base_sha:
                stop("head failed but the base commit is unknown; no base runs")
            base_tree = tree_for("base", base_sha)
            base_nodes = map_ids(base_tree, "base")
            one("base hermetic 1", base_tree, base_nodes, True)
            one("base hermetic 2", base_tree, base_nodes, True)
        res.outcome = classify(res.head_runs, res.live_run, res.base_runs, ids)
        res.note = res.note or _summary(res)
    except _Stop:
        res.outcome = "inconclusive"
    finally:
        for dest in made:
            _remove_worktree(runner, repo, dest)
    return res


def _summary(res: RerunResult) -> str:
    def green(r: dict | None) -> str:
        return "missing" if r is None else f"{sum(1 for v in r.values() if v == PASS)}/{len(r)} passed"
    parts = [f"head hermetic {', '.join(green(r) for r in res.head_runs)}", f"head live {green(res.live_run)}"]
    if res.base_runs:
        parts.append(f"base hermetic {', '.join(green(r) for r in res.base_runs)}")
    return f"{res.outcome}: " + "; ".join(parts)
