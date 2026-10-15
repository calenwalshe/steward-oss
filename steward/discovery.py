"""Repo discovery with provenance and classification (SP-7).

Discovering a command never authorizes running it: every command is
classified `check` (test/lint/typecheck-shaped, auto-authorized to run) or
`unclassified` (everything else — deploy/publish/release/network/destructive
commands stay unclassified even if they were the only thing found). Only
`check` commands ever reach `steward.baseline` / a worker's authority list.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")

# Checked first: anything matching one of these is unclassified regardless of
# also looking test/lint-shaped (a command is judged by its most dangerous
# word, not its friendliest one).
_UNSAFE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bdeploy\b"), "deploy"),
    (re.compile(r"\bpublish\b"), "publish"),
    (re.compile(r"\brelease\b"), "release"),
    (re.compile(r"\bpush\b"), "push"),
    (re.compile(r"rm\s+-rf\b"), "rm -rf"),
    (re.compile(r"\bcurl\b"), "curl"),
    (re.compile(r"\bwget\b"), "wget"),
    (re.compile(r"docker\s+push\b"), "docker push"),
    (re.compile(r"\bkubectl\b"), "kubectl"),
    (re.compile(r"terraform\s+apply\b"), "terraform apply"),
]

_CHECK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bpytest\b"), "pytest"),
    (re.compile(r"\bruff\b"), "ruff"),
    (re.compile(r"\bmypy\b"), "mypy"),
    (re.compile(r"\btsc\b"), "tsc"),
    (re.compile(r"\beslint\b"), "eslint"),
    (re.compile(r"\btypecheck\b"), "typecheck"),
    (re.compile(r"npm\s+test\b"), "npm test"),
    (re.compile(r"npm\s+run\s+(test|lint|typecheck)\b"), "npm run test|lint|typecheck"),
    (re.compile(r"make\s+(test|lint|check)\b"), "make test|lint|check"),
    (re.compile(r"\btest\b"), "test"),
    (re.compile(r"\blint\b"), "lint"),
]


def classify(text: str) -> tuple[str, str]:
    """Classify a command line as `check` or `unclassified`, with a reason."""
    lowered = text.lower()
    for pattern, tag in _UNSAFE_PATTERNS:
        if pattern.search(lowered):
            return "unclassified", f"contains '{tag}' — not auto-authorized"
    for pattern, tag in _CHECK_PATTERNS:
        if pattern.search(lowered):
            return "check", f"matches check pattern '{tag}'"
    return "unclassified", "not a recognized check command"


@dataclass(frozen=True)
class DiscoveredCommand:
    line: str
    source: str
    kind: str  # "check" | "unclassified"
    reason: str


@dataclass(frozen=True)
class Discovery:
    commands: list[DiscoveredCommand] = field(default_factory=list)
    instruction_files: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    # The project's own Python environment found in the repo (e.g. ".venv", "poetry", "uv"); the pytest check
    # runs under it unless an explicit steward.toml command wins. "" when none: bare/`python3 -m pytest` apply.
    python_env: str = ""


def _scan_makefile_targets(text: str) -> list[tuple[int, str]]:
    """Very small Makefile scanner: unindented `name:` lines are targets."""
    targets = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.startswith(("\t", " ", "#")) or not raw.strip():
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*:(?!=)", raw)
        if m and not m.group(1).startswith("."):
            targets.append((lineno, m.group(1)))
    return targets


def _scan_ci_run_lines(text: str) -> list[tuple[int, str]]:
    """Small line scanner for GitHub Actions `run:` steps (no PyYAML)."""
    results: list[tuple[int, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        m = re.match(r"^-?\s*run:\s*(.*)$", stripped)
        if m:
            cmd = m.group(1).strip()
            lineno = i + 1
            if cmd in ("", "|", ">", "|-", ">-"):
                # Block scalar: take the first non-blank following line.
                j = i + 1
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                if j < len(lines):
                    cmd = lines[j].strip()
                    lineno = j + 1
                else:
                    cmd = ""
            else:
                cmd = cmd.strip('"').strip("'")
            if cmd:
                results.append((lineno, cmd))
        i += 1
    return results


_VENV_PYTHONS = (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe", "venv/Scripts/python.exe")


def project_python(repo: Path | str) -> tuple[str, str] | None:
    """The repo's own interpreter prefix for `-m pytest`: `(command_prefix, label)` or None.

    Order: a project venv (`.venv`/`venv`, which is also where `uv` puts its env), then `poetry run`
    when it is a poetry project, then `uv run` when `uv.lock` exists. Venv interpreters are absolute so
    the same line runs from the job worktree and a merge-base checkout (neither carries the untracked venv).
    """
    repo = Path(repo).resolve()
    for rel in _VENV_PYTHONS:
        exe = repo / rel
        if exe.is_file():
            return shlex.quote(str(exe)), rel.split("/")[0]
    pyproject = repo / "pyproject.toml"
    text = pyproject.read_text() if pyproject.exists() else ""
    if ((repo / "poetry.lock").exists() or "[tool.poetry]" in text) and shutil.which("poetry"):
        return "poetry run python", "poetry"
    if (repo / "uv.lock").exists() and shutil.which("uv"):
        return "uv run python", "uv"
    return None


def explicit_checks(repo: Path) -> list[str]:
    """Check commands the repo declares itself in `steward.toml` (`[checks] commands = [...]` or
    `command = "..."`); they win over everything discovered."""
    path = repo / "steward.toml"
    if not path.exists():
        return []
    try:
        table = tomllib.loads(path.read_text()).get("checks") or {}
    except (tomllib.TOMLDecodeError, OSError):
        return []
    found = list(table.get("commands") or [])
    if table.get("command"):
        found.insert(0, table["command"])
    return [c.strip() for c in found if isinstance(c, str) and c.strip()]


_MISSING_MODULE_RE = re.compile(r"No module named ['\"]?([\w.\-]+)")


def explain_broken(base: str, output: str, python_env: str = "") -> str:
    """Name the likely cause of a broken pytest run: the missing dependency, and the project env the
    command did not use (`python_env`), instead of only the bare exit code."""
    m = _MISSING_MODULE_RE.search(output or "")
    parts = []
    if m:
        parts.append(f"missing dependency {m.group(1).split('.')[0]}")
    if python_env:
        parts.append(f"repo has {python_env}")
    return f"{base} - likely cause: {'; '.join(parts)}" if parts else base


def discover(repo: Path | str) -> Discovery:
    repo = Path(repo)
    commands: list[DiscoveredCommand] = []
    seen: set[str] = set()

    def _add(line: str, source: str, kind: str, reason: str) -> None:
        if line in seen:
            return
        seen.add(line)
        commands.append(DiscoveredCommand(line=line, source=source, kind=kind, reason=reason))

    pyproject = repo / "pyproject.toml"
    pytest_ini = repo / "pytest.ini"
    tests_dir = repo / "tests"

    explicit = explicit_checks(repo)
    for line in explicit:
        kind, reason = classify(line)
        if kind == "unclassified" and reason == "not a recognized check command":
            kind, reason = "check", "explicit check command declared in steward.toml"
        _add(line, "steward.toml [checks]", kind, reason)

    py_env = project_python(repo)
    pyproject_text = pyproject.read_text() if pyproject.exists() else ""
    pytest_source = None
    if pyproject.exists() and "pytest" in pyproject_text:
        pytest_source = "pyproject.toml"
    elif pytest_ini.exists():
        pytest_source = "pytest.ini"
    elif tests_dir.is_dir():
        pytest_source = "tests/"
    elif any(repo.glob("test_*.py")) or any(repo.glob("*_test.py")):
        pytest_source = "test files at the repo root"
    if pytest_source and not explicit:
        kind, reason = classify("pytest")
        line = "pytest"
        if py_env:
            line = f"{py_env[0]} -m pytest"
            pytest_source += f" (project env: {py_env[1]})"
        _add(line, pytest_source, kind, reason)

    package_json = repo / "package.json"
    if package_json.exists():
        data = json.loads(package_json.read_text())
        scripts = data.get("scripts", {})
        for key, value in scripts.items():
            line = "npm test" if key == "test" else f"npm run {key}"
            kind, reason = classify(f"{line} {value}")
            _add(line, "package.json", kind, reason)

    makefile = repo / "Makefile"
    if makefile.exists():
        for lineno, target in _scan_makefile_targets(makefile.read_text()):
            line = f"make {target}"
            kind, reason = classify(line)
            _add(line, f"Makefile:{lineno}", kind, reason)

    workflows_dir = repo / ".github" / "workflows"
    if workflows_dir.is_dir():
        workflow_files = sorted(workflows_dir.glob("*.yml")) + sorted(
            workflows_dir.glob("*.yaml")
        )
        for wf in workflow_files:
            text = wf.read_text()
            for lineno, cmd in _scan_ci_run_lines(text):
                kind, reason = classify(cmd)
                _add(cmd, f"{wf.relative_to(repo)}:{lineno}", kind, reason)

    instruction_files = [name for name in _INSTRUCTION_FILES if (repo / name).exists()]

    languages: list[str] = []
    if pyproject.exists() or pytest_ini.exists() or (repo / "setup.py").exists():
        languages.append("python")
    if package_json.exists():
        languages.append("node")

    return Discovery(
        commands=commands,
        instruction_files=instruction_files,
        languages=sorted(languages),
        python_env=py_env[1] if py_env else "",
    )
