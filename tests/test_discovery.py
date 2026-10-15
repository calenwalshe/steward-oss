from fixtures.repos import (
    ci_repo,
    instructions_repo,
    make_repo,
    node_repo,
    python_repo,
)

from steward.discovery import classify, discover


def test_classify_check_patterns():
    assert classify("pytest")[0] == "check"
    assert classify("ruff check .")[0] == "check"
    assert classify("mypy .")[0] == "check"
    assert classify("npm test")[0] == "check"
    assert classify("npm run lint")[0] == "check"
    assert classify("make test")[0] == "check"


def test_classify_unsafe_patterns_beat_check_patterns():
    kind, reason = classify("npm run deploy")
    assert kind == "unclassified"
    assert "deploy" in reason


def test_classify_default_unclassified_with_reason():
    kind, reason = classify("webpack")
    assert kind == "unclassified"
    assert reason == "not a recognized check command"


def test_python_repo_discovers_pytest_with_provenance(tmp_path):
    repo = python_repo(tmp_path)
    discovery = discover(repo)
    pytest_cmds = [c for c in discovery.commands if c.line == "pytest"]
    assert len(pytest_cmds) == 1
    cmd = pytest_cmds[0]
    assert cmd.kind == "check"
    assert cmd.source == "pyproject.toml"
    assert discovery.languages == ["python"]


def test_node_repo_classifies_each_script(tmp_path):
    repo = node_repo(tmp_path)
    discovery = discover(repo)
    by_line = {c.line: c for c in discovery.commands}

    assert by_line["npm test"].kind == "check"
    assert by_line["npm run lint"].kind == "check"
    assert by_line["npm run build"].kind == "unclassified"
    assert by_line["npm run deploy"].kind == "unclassified"
    assert "deploy" in by_line["npm run deploy"].reason
    for cmd in discovery.commands:
        assert cmd.source == "package.json"
    assert discovery.languages == ["node"]


def test_make_repo_classifies_targets_with_line_provenance(tmp_path):
    repo = make_repo(tmp_path)
    discovery = discover(repo)
    by_line = {c.line: c for c in discovery.commands}

    assert by_line["make test"].kind == "check"
    assert by_line["make test"].source == "Makefile:1"
    assert by_line["make lint"].kind == "check"
    assert by_line["make release"].kind == "unclassified"
    assert "release" in by_line["make release"].reason


def test_ci_repo_parses_run_lines_and_flags_publish(tmp_path):
    repo = ci_repo(tmp_path)
    discovery = discover(repo)
    by_line = {c.line: c for c in discovery.commands}

    assert by_line["npm ci"].kind == "unclassified"
    assert by_line["npm test"].kind == "check"
    assert by_line["npm publish"].kind == "unclassified"
    assert "publish" in by_line["npm publish"].reason
    assert all(
        c.source.startswith(".github/workflows/ci.yml:") for c in discovery.commands
    )


def test_deploy_or_delete_style_commands_never_authorized(tmp_path):
    repo = ci_repo(tmp_path)
    discovery = discover(repo)
    unclassified = {c.line for c in discovery.commands if c.kind == "unclassified"}
    checks = {c.line for c in discovery.commands if c.kind == "check"}
    assert "npm publish" in unclassified
    assert "npm publish" not in checks


def test_instructions_repo_reports_instruction_files_and_no_languages(tmp_path):
    repo = instructions_repo(tmp_path)
    discovery = discover(repo)
    assert discovery.instruction_files == ["AGENTS.md", "CLAUDE.md"]
    assert discovery.commands == []
    assert discovery.languages == []


def test_dedupe_keeps_first_provenance(tmp_path):
    repo = node_repo(tmp_path)
    # ci.yml also invokes "npm test" — package.json was scanned first, so
    # that provenance wins and no second entry is created for the same line.
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  build:\n    steps:\n      - run: npm test\n"
    )
    discovery = discover(repo)
    npm_test_lines = [c for c in discovery.commands if c.line == "npm test"]
    assert len(npm_test_lines) == 1
    assert npm_test_lines[0].source == "package.json"
