"""Clean-install finding 2026-10-14: a repo whose pytest files sit at the root (test_calc.py, no tests/, no
pyproject.toml or pytest.ini) discovered no checks, so a passing job was held as "no checks ran"."""
from __future__ import annotations

from steward.discovery import discover


def _lines(repo):
    return [c.line for c in discover(repo).commands]


def test_root_level_test_files_discover_pytest(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_calc.py").write_text("def test_add():\n    assert True\n")
    assert any(line.endswith("pytest") for line in _lines(tmp_path))


def test_suffix_style_test_files_discover_pytest(tmp_path):
    (tmp_path / "calc_test.py").write_text("def test_add():\n    assert True\n")
    assert any(line.endswith("pytest") for line in _lines(tmp_path))


def test_a_repo_without_python_tests_discovers_no_pytest(tmp_path):
    (tmp_path / "README.md").write_text("hi\n")
    (tmp_path / "calc.py").write_text("x = 1\n")
    assert not any("pytest" in line for line in _lines(tmp_path))
