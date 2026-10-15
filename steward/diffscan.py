"""Structural diff scanning for the trajectory ledger (TRJ-04).

`scan` is deterministic and model-free: every command goes through the
injected `Runner` (git only), and the only local read is the worktree file on
disk for the "new" side of a public-API comparison — the same shape as
`steward.heartbeat.collect`'s own git reads. Public-API comparisons use
`ast`, never a model: a removed top-level name or a changed argument list is
a fact, not a judgment.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from steward import weakening
from steward.runner_exec import Runner

_INSERTIONS_RE = re.compile(r"(\d+) insertions?\(\+\)")
_DELETIONS_RE = re.compile(r"(\d+) deletions?\(-\)")

_TEST_FILE_RE = re.compile(r"(^|/)(tests?/.*|test_[^/]+\.py|[^/]+_test\.py)$")

_MANIFEST_RE = re.compile(
    r"(^|/)("
    r"pyproject\.toml|requirements[^/]*\.txt|package\.json|package-lock\.json|"
    r"yarn\.lock|pnpm-lock\.yaml|Pipfile|Pipfile\.lock|poetry\.lock|"
    r"go\.mod|go\.sum|Cargo\.toml|Cargo\.lock|Gemfile|Gemfile\.lock"
    r")$"
)
_SUMMARY_RE = re.compile(r"-SUMMARY\.md$")
_INTERFACE_RE = re.compile(r"\.proto$|(^|/)openapi[^/]*$|\.d\.ts$")


@dataclass(frozen=True)
class DiffSample:
    changed_lines: int
    files: list[str] = field(default_factory=list)
    deleted_or_renamed: int = 0
    new_top_level_dirs: list[str] = field(default_factory=list)
    manifests_changed: list[str] = field(default_factory=list)
    summary_commits: list[str] = field(default_factory=list)
    public_api_changes: list[str] = field(default_factory=list)
    interface_files: list[str] = field(default_factory=list)
    weakening: list[str] = field(default_factory=list)
    parse_errors: int = 0


def scan(worktree: Path | str, base_sha: str, runner: Runner) -> DiffSample:
    worktree = Path(worktree)

    shortstat = runner.run(["git", "diff", "--shortstat", base_sha], cwd=worktree)
    changed_lines = _parse_shortstat(shortstat.stdout)

    name_status = runner.run(["git", "diff", "--name-status", base_sha], cwd=worktree)
    changes = _parse_name_status(name_status.stdout)

    files = sorted({new_path or path for _, path, new_path in changes})
    deleted_or_renamed = sum(1 for status, _, _ in changes if status == "D" or status.startswith("R"))

    base_top_level = _top_level_entries(base_sha, runner, worktree)
    new_top_level_dirs = sorted(_new_top_level_dirs(changes, base_top_level))

    manifests_changed = sorted(
        {new_path or path for _, path, new_path in changes if _MANIFEST_RE.search(new_path or path)}
    )
    summary_commits = sorted(
        {new_path or path for status, path, new_path in changes if status == "A" and _SUMMARY_RE.search(new_path or path)}
    )
    interface_files = sorted(
        {new_path or path for _, path, new_path in changes if _INTERFACE_RE.search(new_path or path)}
    )

    public_api_changes, parse_errors = _scan_public_api(changes, base_sha, runner, worktree)

    findings = weakening.detect(worktree, base_sha, runner)
    weakening_list = [f"{finding.kind}:{finding.path}" for finding in findings]

    return DiffSample(
        changed_lines=changed_lines,
        files=files,
        deleted_or_renamed=deleted_or_renamed,
        new_top_level_dirs=new_top_level_dirs,
        manifests_changed=manifests_changed,
        summary_commits=summary_commits,
        public_api_changes=public_api_changes,
        interface_files=interface_files,
        weakening=weakening_list,
        parse_errors=parse_errors,
    )


def _parse_shortstat(text: str) -> int:
    insertions = _INSERTIONS_RE.search(text)
    deletions = _DELETIONS_RE.search(text)
    return (int(insertions.group(1)) if insertions else 0) + (int(deletions.group(1)) if deletions else 0)


def _parse_name_status(text: str) -> list[tuple[str, str, str | None]]:
    changes: list[tuple[str, str, str | None]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R"):
            changes.append(("R", parts[1], parts[2] if len(parts) > 2 else None))
        else:
            changes.append((status, parts[1], None))
    return changes


def _top_level_entries(base_sha: str, runner: Runner, worktree: Path) -> set[str]:
    result = runner.run(["git", "ls-tree", "--name-only", base_sha], cwd=worktree)
    if result.exit_code != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _new_top_level_dirs(changes: list[tuple[str, str, str | None]], base_top_level: set[str]) -> set[str]:
    new_dirs: set[str] = set()
    for _, path, new_path in changes:
        effective = new_path or path
        if "/" not in effective:
            continue
        top = effective.split("/", 1)[0]
        if top not in base_top_level:
            new_dirs.add(top)
    return new_dirs


def _read_base_file(base_sha: str, path: str, runner: Runner, worktree: Path) -> str | None:
    result = runner.run(["git", "show", f"{base_sha}:{path}"], cwd=worktree)
    if result.exit_code != 0:
        return None
    return result.stdout


def _read_worktree_file(path: str, worktree: Path) -> str | None:
    full = worktree / path
    if not full.exists():
        return None
    try:
        return full.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _public_api(tree: ast.Module) -> dict[str, tuple[str, ...] | None]:
    """Top-level `def`/`class` names without a leading underscore, plus
    anything listed in `__all__`. Functions map to their positional argument
    names (a signature fingerprint); classes map to `None` (presence only)."""
    all_names: set[str] | None = None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            all_names = {
                elt.value for elt in node.value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }

    names: dict[str, tuple[str, ...] | None] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_") or (all_names and node.name in all_names):
                names[node.name] = tuple(arg.arg for arg in node.args.args)
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_") or (all_names and node.name in all_names):
                names[node.name] = None
    return names


def _safe_parse(src: str | None) -> tuple[dict[str, tuple[str, ...] | None], bool]:
    """Returns (public_api, had_parse_error). A missing file (None) is not a
    parse error — it means "added" or "deleted", handled by the caller."""
    if src is None:
        return {}, False
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}, True
    return _public_api(tree), False


def _scan_public_api(
    changes: list[tuple[str, str, str | None]], base_sha: str, runner: Runner, worktree: Path
) -> tuple[list[str], int]:
    public_api_changes: list[str] = []
    parse_errors = 0
    for _, path, new_path in changes:
        effective = new_path or path
        if not effective.endswith(".py") or _TEST_FILE_RE.search(effective):
            continue
        base_src = _read_base_file(base_sha, path, runner, worktree)
        new_src = _read_worktree_file(effective, worktree)
        base_names, base_err = _safe_parse(base_src)
        new_names, new_err = _safe_parse(new_src)
        if base_err or new_err:
            parse_errors += 1
            continue
        for name, sig in base_names.items():
            if name not in new_names or (sig is not None and new_names[name] != sig):
                public_api_changes.append(f"{effective}:{name}")
    return public_api_changes, parse_errors
