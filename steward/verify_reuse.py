"""Verification reuse on merge (latency #4, docs/diagnostics/2026-10-02-latency).

Autopilot runs the checks at the job's exact commit (closeout), then ran the suite again on the merged tree under
the repo lock. In 27 of 35 auto-merges the merged tree was byte-identical to the tested one (1.0 h of redundant
re-test in 3.9 days). The re-test is skipped only when ALL hold:

- the job reached CHECKS_PASSED with every check green (exit 0) at commit C (`record` keeps that receipt);
- the candidate merged tree hash equals C's tree hash (same content, checked under the repo lock);
- the merge would run exactly the same check commands;
- the toolchain fingerprint (interpreter + each command's resolved executable) is unchanged.

Anything else tests as before. `STEWARD_VERIFY_REUSE=0` turns reuse off.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import sys
from typing import Callable

from steward.models import Judgment
from steward.store import Store

RECEIPT_KEY = "verified_receipt"
ENV = "STEWARD_VERIFY_REUSE"
RULE = "verify_reuse_exact_tree"


def enabled(env=None) -> bool:
    return (os.environ if env is None else env).get(ENV, "1").strip().lower() not in ("0", "off", "false", "no")


def toolchain_fingerprint(commands: list[str]) -> str:
    """Interpreter + each command's resolved executable (path, size, mtime): an upgrade invalidates reuse."""
    from steward.pytest_parallel import execution_label

    parts = [sys.executable, platform.python_version(), f"exec={execution_label(commands)}"]
    for line in sorted(set(commands)):
        exe = (line.split() or [""])[0]
        path = shutil.which(exe) or exe
        try:
            st = os.stat(path)
            parts.append(f"{exe}={path}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{exe}={path}:missing")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def record(store: Store, job_id: str, verification, *, fingerprint_fn: Callable[[list[str]], str] | None = None) -> dict:
    """At CHECKS_PASSED: what was verified, so a merge of identical content can reuse it."""
    results = list(getattr(verification, "results", []) or [])
    commands = [r.command for r in results]
    receipt = {
        "commit_sha": verification.commit_sha,
        "commands": commands,
        "all_green": bool(results) and all(r.exit_code == 0 for r in results)
        and not getattr(verification, "docs_only", False),
        "toolchain": (fingerprint_fn or toolchain_fingerprint)(commands),
    }
    store.kv_set(job_id, RECEIPT_KEY, receipt)
    return receipt


def reusable(store: Store, job_id: str, *, merged_tree: str, job_tree: str | None, commands: list[str],
             fingerprint_fn: Callable[[list[str]], str] | None = None) -> tuple[bool, str]:
    """(reuse?, why). `job_tree` is the tree of the receipt's commit, resolved by the caller under the lock."""
    if not enabled():
        return False, "reuse off"
    receipt = store.kv_get(job_id, RECEIPT_KEY)
    if not isinstance(receipt, dict):
        return False, "no verification receipt"
    if not receipt.get("all_green"):
        return False, "verification was not all green"
    if not merged_tree or merged_tree != job_tree:
        return False, "merged tree differs from the tested tree"
    if list(receipt.get("commands") or []) != list(commands):
        return False, "check commands differ"
    if (fingerprint_fn or toolchain_fingerprint)(list(commands)) != receipt.get("toolchain"):
        return False, "toolchain changed"
    return True, "merged tree equals the tested tree; same checks and toolchain"


def note_reused(store: Store, job_id: str, *, merged_tree: str, commands: list[str]) -> None:
    receipt = store.kv_get(job_id, RECEIPT_KEY) or {}
    store.append_event(job_id, what="merged_tree_verification_reused",
                       evidence={"merged_tree": merged_tree, "tested_commit": receipt.get("commit_sha"),
                                 "commands": commands, "toolchain": receipt.get("toolchain")},
                       rule=RULE, judgment=Judgment.code)
