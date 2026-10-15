"""Landing-train gates (2026-10-08 spec): no tests removed vs the train base, and no NEW failing test ids vs the base
at the same env (§5.5: base run cached per sha+env+command, failing ids rerun once, a red base never freezes the train)."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from steward import weakening

BLOCKING_KINDS = frozenset({"removed_test_function", "removed_assertions", "deleted_test_file", "added_skip_marker"})
SUITE_ID = "<suite>"            # the run produced no usable per-test result (collection error, timeout, crash)
BASE_MAX_AGE_S = 1800.0         # §5.5: a cached base older than 30 min is recomputed (a flaky base self-corrects)
RERUN_MAX = 100                 # more new failures than this is a real break, not flakiness: no rerun
BASE_CACHE_KEEP = 20            # base_suite kv rows kept (one per target head x env)
_RAN_EXITS = (0, 1)             # pytest: all tests ran (1 = some failed); 2-5, 124, ... = no trustworthy result


def tests_removed(worktree, base_sha: str, runner) -> list:
    """Findings that block a landing, measured base_sha..HEAD of the merged train tree."""
    return [f for f in weakening.detect(worktree, base_sha, runner) if f.kind in BLOCKING_KINDS]


def run_suite(worktree, cmd: str, tmp_root, runner, *, timeout: float = 3600.0, junit: Path | None = None):
    """One suite run with its own TMPDIR/basetemp, always deleted afterwards. A timeout is a red run. With `junit`, the
    run writes a JUnit report there (xunit1, so each case carries its file and the ids are pytest node ids)."""
    from steward.runner_exec import CommandResult

    tmp_root = Path(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)
    run_tmp = Path(tempfile.mkdtemp(prefix="suite-", dir=tmp_root))
    drop = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_") and k not in drop}
    env["TMPDIR"] = str(run_tmp)
    line = f"{cmd} --basetemp {shlex.quote(str(run_tmp / 'bt'))}"
    if junit is not None:
        line += f" -o junit_family=xunit1 --junitxml {shlex.quote(str(junit))}"
    argv = ["bash", "-lc", line]
    try:
        return runner.run(argv, str(worktree), timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return CommandResult(argv, str(worktree), 124, "", f"suite timed out after {timeout:.0f}s", float(timeout))
    finally:
        shutil.rmtree(run_tmp, ignore_errors=True)


# -- relative gate (§5.5) -----------------------------------------------------------------------------------------


def _node_id(classname: str, name: str, file: str) -> str:
    """The pytest node id of a JUnit case (needs xunit1's `file`); without `file`, the JUnit `classname::name` key."""
    if file:
        if not classname:                                   # a collection error is reported against the module
            return file
        module = re.sub(r"\.py$", "", file).replace("/", ".")
        if classname == module:
            return f"{file}::{name}"
        if classname.startswith(module + "."):
            return "::".join([file, *classname[len(module) + 1:].split("."), name])
    return f"{classname}::{name}"


def failing_ids(junit: Path) -> set[str]:
    """Failed/errored test ids in a JUnit report (same outcome rules as `baseline._parse_junit`), as node ids."""
    from steward.baseline import _OUTCOME_TAGS, _SEVERITY

    junit = Path(junit)
    if not junit.exists():
        return set()
    try:
        root = ET.parse(junit).getroot()
    except (ET.ParseError, OSError):
        return set()
    worst: dict[str, str] = {}
    for case in root.iter("testcase"):
        tid = _node_id(case.get("classname", ""), case.get("name", ""), case.get("file", ""))
        outcome = next((mapped for tag, mapped in _OUTCOME_TAGS.items() if case.find(tag) is not None), "passed")
        if _SEVERITY[outcome] >= _SEVERITY[worst.get(tid, "passed")]:
            worst[tid] = outcome
    return {k for k, v in worst.items() if v in ("failed", "error")}


def junit_key(test_id: str) -> str:
    """A node id (`a/b.py::C::t[x]`) or a JUnit key (`a.b.C::t[x]`) as the JUnit `classname::name` key (pytest's
    `mangle_test_address`), so ids from either junit family compare equal. Idempotent on JUnit keys."""
    path, bracket, params = test_id.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return "::".join([".".join(names[:-1]), names[-1]]) if len(names) > 1 else names[0]


def compare(candidate, base) -> tuple[list[str], list[str], list[str]]:
    """(new, preexisting, fixed) between a candidate's and the base's failing ids (sorted; compared as JUnit keys)."""
    bkeys = {junit_key(b) for b in base}
    ckeys = {junit_key(c) for c in candidate}
    new = sorted(c for c in candidate if junit_key(c) not in bkeys)
    pre = sorted(c for c in candidate if junit_key(c) in bkeys)
    fixed = sorted(b for b in base if junit_key(b) not in ckeys)
    return new, pre, fixed


def suite_digest(env_digest: str, cmd: str) -> str:
    """The cache identity of a suite: the environment AND the command (`-n 8` vs serial, a selection, ...)."""
    return f"{env_digest}-{hashlib.sha256(cmd.encode()).hexdigest()[:8]}"


def _suite_failures(worktree, cmd: str, tmp_root, runner, ids=None, *, timeout: float = 3600.0) -> set[str]:
    """Failing node ids of one run; {SUITE_ID} when the run produced no trustworthy per-test result."""
    tmp_root = Path(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)
    junit = tmp_root / f"junit-{uuid.uuid4().hex[:8]}.xml"
    line = cmd + ("".join(" " + shlex.quote(i) for i in ids) if ids else "")
    try:
        res = run_suite(worktree, line, tmp_root, runner, timeout=timeout, junit=junit)
        failed = failing_ids(junit)
    finally:
        junit.unlink(missing_ok=True)
    if res.exit_code not in _RAN_EXITS or (res.exit_code != 0 and not failed):
        return {SUITE_ID}                     # collection error / interrupted / timeout: everything is red
    return failed


def base_failures(store, repo, base_sha: str, *, env_digest: str, suite_fn, worktree, now: float,
                  max_age_s: float = BASE_MAX_AGE_S) -> set[str]:
    """The base's failing ids, cached in kv `_train/base_suite:<sha>:<env>` = {"failing": [...], "at": now}; an entry
    older than `max_age_s` (or from the future: a clock step) is recomputed. A raising suite_fn caches nothing."""
    key = f"base_suite:{base_sha}:{env_digest}"
    rec = store.kv_get("_train", key) or {}
    if rec and 0.0 <= now - float(rec.get("at", 0.0)) < max_age_s:
        return set(rec.get("failing") or [])
    failing = set(suite_fn(worktree))
    store.kv_set("_train", key, {"failing": sorted(failing), "at": now})
    keys = [k for k in (store.kv_get("_train", "base_suite_keys") or []) if k != key] + [key]
    for old in keys[:-BASE_CACHE_KEEP]:
        store.kv_delete("_train", old)
    store.kv_set("_train", "base_suite_keys", keys[-BASE_CACHE_KEEP:])
    return failing


def base_tree_suite(repo, base_sha: str, tmp_root, run):
    """suite_fn for `base_failures`: `run(tree)` in a throwaway detached worktree of `repo` at `base_sha`. The
    candidate worktree is never checked out elsewhere, so a raising base run cannot leave it on the wrong head."""
    def suite(_worktree) -> set[str]:
        Path(tmp_root).mkdir(parents=True, exist_ok=True)
        holder = Path(tempfile.mkdtemp(prefix="base-", dir=tmp_root))
        tree = holder / "tree"
        try:
            add = subprocess.run(["git", "worktree", "add", "-q", "--detach", str(tree), base_sha], cwd=str(repo),
                                 capture_output=True, text=True)
            if add.returncode != 0:
                raise RuntimeError(f"base worktree at {base_sha[:12]} failed: {(add.stderr or add.stdout).strip()[:200]}")
            return run(tree)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=str(repo), capture_output=True)
            shutil.rmtree(holder, ignore_errors=True)
            subprocess.run(["git", "worktree", "prune"], cwd=str(repo), capture_output=True)
    return suite


def gate(worktree, base_sha: str, runner, *, store, repo, suite_cmd: str, tmp_root, env_digest: str, now: float,
         timeout: float = 3600.0, max_age_s: float = BASE_MAX_AGE_S) -> dict:
    """The train's gate on the built tree in `worktree` vs `base_sha`: ok when no test id fails that does not also fail
    on the base (after one rerun of the new ones) and no test was removed. `base_unusable`: the base produced no usable
    per-test result, so there is no pre-existing credit and the candidate is judged absolutely (every failing id is
    new); a green candidate still lands, which is how a broken main gets fixed. Never a skip: a broken base must not
    freeze the train (spec §5.5); the 30-min cache expiry retries the base."""
    tmp_root = Path(tmp_root)
    digest = suite_digest(env_digest, suite_cmd)
    base = base_failures(store, repo, base_sha, env_digest=digest, now=now, max_age_s=max_age_s, worktree=worktree,
                         suite_fn=base_tree_suite(repo, base_sha, tmp_root,
                                                  lambda t: _suite_failures(t, suite_cmd, tmp_root, runner,
                                                                            timeout=timeout)))
    out = {"ok": False, "new_failures": [], "preexisting": [], "fixed": [], "reran_green": [], "removed": [],
           "why": "", "base_unusable": SUITE_ID in base}
    if out["base_unusable"]:
        base = set()                         # absolute judgment: no pre-existing credit
    cand = _suite_failures(worktree, suite_cmd, tmp_root, runner, timeout=timeout)
    new, pre, fixed = compare(cand, base)
    reran: list[str] = []
    if new and SUITE_ID not in new and len(new) <= RERUN_MAX:
        still = _suite_failures(worktree, f"{suite_cmd} -p no:randomly", tmp_root, runner, ids=new, timeout=timeout)
        if SUITE_ID not in still:            # a rerun that itself broke proves nothing: every new failure stands
            skeys = {junit_key(i) for i in still}
            reran = sorted(i for i in new if junit_key(i) not in skeys)
            new = sorted(i for i in new if junit_key(i) in skeys)
    if SUITE_ID in new:
        new, pre = [SUITE_ID], []
        why = "red suite: no per-test results (collection error, timeout or crash)"
    else:
        why = ("new failures: " + ", ".join(new[:5]) + (f" (+{len(new) - 5})" if len(new) > 5 else "")) if new else ""
    removed = [] if why else tests_removed(worktree, base_sha, runner)
    if removed:
        why = "tests removed: " + ", ".join(sorted({f.kind for f in removed}))
    return {**out, "ok": not why, "new_failures": new, "preexisting": pre, "fixed": fixed, "reran_green": reran,
            "removed": [f.kind for f in removed], "why": why}
