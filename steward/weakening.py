"""Test-weakening detection (SP-16).

Deleting the failing test is not a fix. `detect` inspects the diff between a
base commit and HEAD for signs a worker "fixed" verification by weakening it
rather than the code: a deleted test file, a removed test function, a net
removal of assertions, a newly added skip/xfail marker, or a weakening edit to
test/CI configuration. Config is judged by hunk, not by file name (RC-1,
2026-10-11): `conftest.py` flags only weakening tokens or a net removal of
fixtures/asserts; `pyproject.toml`/`setup.cfg` flag only hunks inside a
test-tool section; `pytest.ini`, `tox.ini`, jest config and CI workflows stay
file-level. Every finding is a signal closeout policy treats as
blocking automatic acceptance — never silently ignored (SP-17).
"""

from __future__ import annotations

import io
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

from steward.runner_exec import Runner

_TEST_FILE_RE = re.compile(
    r"(^|/)(tests?/.*|test_[^/]+\.py|[^/]+_test\.py|[^/]+\.spec\.\w+|[^/]+\.test\.\w+)$"
)
_TEST_CONFIG_RE = re.compile(
    r"(^|/)(pytest\.ini|tox\.ini|jest\.config\.\w+)$"
    r"|^\.github/workflows/"
)
_CONFTEST_RE = re.compile(r"(^|/)conftest\.py$")
_SECTIONED_CONFIG_RE = re.compile(r"(^|/)(pyproject\.toml|setup\.cfg)$")
_WEAKENING_TOKEN_RE = re.compile(
    r"collect_ignore|pytest_collection_modifyitems|ignore_collect|deselect|\bskip|xfail"
    r"|addopts|-p\s*no:|(^|[\s\"'\[,])-k\b"
    # timeout only as a real setting: the marker, the CLI option, the plugin, a config key
    r"|pytest\.mark\.timeout|--timeout|pytest_timeout"
    r"|^\s*[\"']?timeout(_method|_func_only)?[\"']?\s*[=:](?!=)|\.timeout\s*=(?!=)"
    r"|[\"']timeout(_method|_func_only)?[\"']\s*:"
)
_FIXTURE_RE = re.compile(r"@pytest\.fixture\b")
_SECTION_RE = re.compile(r"^\s*\[+\s*([^\]]+?)\s*\]+\s*$")
_TEST_SECTION_RE = re.compile(
    r"^(tool\.pytest|tool\.coverage|tool\.tox|tool\.hatch\.envs|tool:pytest|coverage:)"
)
_KEY_RE = re.compile(r"^\s*([A-Za-z_][\w.-]*)\s*=")
_RISKY_KEYS = frozenset({
    "addopts", "testpaths", "python_files", "python_classes", "python_functions",
    "norecursedirs", "filterwarnings", "timeout", "fail_under", "omit", "exclude_lines",
    "exclude_also", "source", "commands", "env_list", "envlist",
})
_DEF_TEST_RE = re.compile(r"^\s*def\s+test_")
_IT_TEST_RE = re.compile(r"^(it|test)\(")
_ASSERT_RE = re.compile(r"\bassert\b|\bexpect\(")
_SKIP_MARK_RE = re.compile(r"@pytest\.mark\.(skip|xfail)\b|\.skip\(|\bxit\(")


@dataclass(frozen=True)
class Finding:
    path: str
    kind: str
    detail: str


def detect(worktree: Path | str, base_sha: str, runner: Runner) -> list[Finding]:
    worktree = Path(worktree)
    findings: list[Finding] = []

    name_status = runner.run(
        ["git", "diff", "--name-status", f"{base_sha}..HEAD"], cwd=worktree
    )
    changes = _parse_name_status(name_status.stdout)

    for status, path, new_path in changes:
        effective_path = new_path or path

        if status == "D" and _TEST_FILE_RE.search(path):
            findings.append(
                Finding(path=path, kind="deleted_test_file", detail=f"{path} was deleted")
            )
            continue

        if _CONFTEST_RE.search(effective_path) or _SECTIONED_CONFIG_RE.search(effective_path):
            diff_result = runner.run(
                ["git", "diff", "--unified=100000", f"{base_sha}..HEAD", "--", effective_path],
                cwd=worktree,
            )
            inspect = _inspect_conftest if _CONFTEST_RE.search(effective_path) else _inspect_sectioned
            reason = inspect(diff_result.stdout)
            if reason:
                findings.append(
                    Finding(
                        path=effective_path,
                        kind="test_config_changed",
                        detail=f"{effective_path} changed ({status}): {reason}",
                    )
                )
            continue

        if _TEST_CONFIG_RE.search(effective_path):
            findings.append(
                Finding(
                    path=effective_path,
                    kind="test_config_changed",
                    detail=f"{effective_path} changed ({status})",
                )
            )
            continue

        if status == "D":
            continue  # non-test file deletion is not a weakening signal here

        if not _TEST_FILE_RE.search(effective_path):
            continue

        diff_result = runner.run(
            ["git", "diff", f"{base_sha}..HEAD", "--", effective_path], cwd=worktree
        )
        findings.extend(_inspect_hunks(effective_path, diff_result.stdout))

    return findings


def _parse_name_status(text: str) -> list[tuple[str, str, str | None]]:
    changes: list[tuple[str, str, str | None]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R"):
            # rename: status, old path, new path
            changes.append(("R", parts[1], parts[2] if len(parts) > 2 else None))
        else:
            changes.append((status, parts[1], None))
    return changes


def _inspect_hunks(path: str, diff_text: str) -> list[Finding]:
    findings: list[Finding] = []
    removed_test_defs = 0
    added_skip_lines: list[str] = []
    removed_asserts = 0
    added_asserts = 0

    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("-"):
            content = line[1:]
            if _DEF_TEST_RE.match(content) or _IT_TEST_RE.match(content.strip()):
                removed_test_defs += 1
            if _ASSERT_RE.search(content):
                removed_asserts += 1
        elif line.startswith("+"):
            content = line[1:]
            if _SKIP_MARK_RE.search(content):
                added_skip_lines.append(content.strip())
            if _ASSERT_RE.search(content):
                added_asserts += 1

    if removed_test_defs:
        findings.append(
            Finding(
                path=path,
                kind="removed_test_function",
                detail=f"{removed_test_defs} test function(s) removed from {path}",
            )
        )
    if added_skip_lines:
        findings.append(
            Finding(
                path=path,
                kind="added_skip_marker",
                detail=f"{added_skip_lines[0]} added in {path}",
            )
        )
    net_removed_asserts = removed_asserts - added_asserts
    if net_removed_asserts > 0:
        findings.append(
            Finding(
                path=path,
                kind="removed_assertions",
                detail=f"net {net_removed_asserts} assertion(s) removed from {path}",
            )
        )

    return findings


def _diff_body(diff_text: str) -> list[str]:
    """Hunk lines (` `, `+`, `-` prefixed), without file headers or `@@` markers."""
    body: list[str] = []
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk and line[:1] in (" ", "+", "-"):
            body.append(line)
    return body


def _code_only(lines: list[str]) -> list[str]:
    """Blank out comments and docstrings so prose never reads as a weakening token.

    A string is a docstring when it is a whole statement. Source that does not
    tokenize is returned unchanged, which keeps the check conservative.
    """
    masked = [list(line) for line in lines]
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO("\n".join(lines) + "\n").readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return lines
    prev = tokenize.NEWLINE
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1].type if i + 1 < len(tokens) else tokenize.ENDMARKER
        docstring = (
            tok.type == tokenize.STRING
            and prev in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT)
            and nxt in (tokenize.NEWLINE, tokenize.COMMENT, tokenize.ENDMARKER)
        )
        if tok.type == tokenize.COMMENT or docstring:
            (srow, scol), (erow, ecol) = tok.start, tok.end
            for row in range(srow - 1, erow):
                start = scol if row == srow - 1 else 0
                end = ecol if row == erow - 1 else len(masked[row])
                masked[row][start:end] = " " * (end - start)
        if tok.type not in (tokenize.COMMENT, tokenize.NL):
            prev = tok.type
    return ["".join(chars) for chars in masked]


def _inspect_conftest(diff_text: str) -> str | None:
    """A conftest edit weakens only via a weakening token or by dropping fixtures/asserts."""
    body = _diff_body(diff_text)
    post = [i for i, line in enumerate(body) if line[0] != "-"]
    code = dict(zip(post, _code_only([body[i][1:] for i in post])))
    removed_fixtures = added_fixtures = removed_asserts = added_asserts = 0
    for i, line in enumerate(body):
        sign, content = line[0], line[1:]
        if sign == " ":
            continue
        if sign == "+" and _WEAKENING_TOKEN_RE.search(code[i]):
            return f"added `{content.strip()}`"
        if _FIXTURE_RE.search(content):
            if sign == "+":
                added_fixtures += 1
            else:
                removed_fixtures += 1
        if _ASSERT_RE.search(content):
            if sign == "+":
                added_asserts += 1
            else:
                removed_asserts += 1
    if removed_fixtures > added_fixtures:
        return f"net {removed_fixtures - added_fixtures} fixture(s) removed"
    if removed_asserts > added_asserts:
        return f"net {removed_asserts - added_asserts} assertion(s) removed"
    return None


def _inspect_sectioned(diff_text: str) -> str | None:
    """pyproject.toml / setup.cfg: only changes inside a test-tool section count.

    Packaging and lint tables never flag. Inside a test section, added `markers`
    entries never flag; removed lines, risky keys and weakening tokens do.
    """
    section = ""
    key = ""
    for line in _diff_body(diff_text):
        sign, content = line[0], line[1:]
        header = _SECTION_RE.match(content)
        if header:
            section, key = header.group(1), ""
            continue
        key_match = _KEY_RE.match(content)
        if key_match:
            key = key_match.group(1).lower()
        if sign == " " or not _TEST_SECTION_RE.match(section):
            continue
        stripped = content.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if sign == "-":
            return f"removed `{stripped}` from [{section}]"
        if key == "markers":
            continue
        if key in _RISKY_KEYS or _WEAKENING_TOKEN_RE.search(content):
            return f"added `{stripped}` in [{section}]"
    return None
