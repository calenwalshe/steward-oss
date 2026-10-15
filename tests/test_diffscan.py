"""Tests for steward/diffscan.py (TRJ-04)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from steward.diffscan import DiffSample, scan
from steward.runner_exec import CommandResult, FakeRunner, SubprocessRunner

BASE = "base-sha"


def _fake_runner(*, shortstat="", name_status="", ls_tree="", show_map=None, weakening_diffs=None):
    """A FakeRunner dispatching on the literal argv, so `diffscan`'s own
    `git diff --name-status <base>` and `weakening.detect`'s
    `git diff --name-status <base>..HEAD` (a different range) can be given
    independent canned responses."""
    show_map = show_map or {}
    weakening_diffs = weakening_diffs or {}

    def respond(argv, cwd):
        joined = " ".join(argv)
        if argv[:3] == ["git", "diff", "--shortstat"]:
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=shortstat, stderr="", duration_s=0.0)
        if argv[:4] == ["git", "diff", "--name-status", f"{BASE}..HEAD"]:
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=name_status, stderr="", duration_s=0.0)
        if argv[:3] == ["git", "diff", "--name-status"]:
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=name_status, stderr="", duration_s=0.0)
        if argv[:3] == ["git", "ls-tree", "--name-only"]:
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=ls_tree, stderr="", duration_s=0.0)
        if argv[:2] == ["git", "show"]:
            spec = argv[2]
            if spec in show_map:
                return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=show_map[spec], stderr="", duration_s=0.0)
            return CommandResult(argv=argv, cwd=cwd, exit_code=1, stdout="", stderr="not found", duration_s=0.0)
        if joined in weakening_diffs:
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=weakening_diffs[joined], stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout="", stderr="", duration_s=0.0)

    return FakeRunner(respond)


# -- changed_lines / files / deleted_or_renamed -----------------------------


def test_changed_lines_from_shortstat():
    runner = _fake_runner(shortstat=" 3 files changed, 10 insertions(+), 4 deletions(-)\n")
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.changed_lines == 14


def test_changed_lines_zero_with_empty_shortstat():
    runner = _fake_runner(shortstat="")
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.changed_lines == 0


def test_files_lists_paths_only():
    runner = _fake_runner(name_status="M\ta.py\nA\tb.py\n")
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.files == ["a.py", "b.py"]


def test_deleted_or_renamed_counts_d_and_r_statuses():
    runner = _fake_runner(name_status="D\told.py\nR100\told2.py\tnew2.py\nM\tunrelated.py\n")
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.deleted_or_renamed == 2
    assert "new2.py" in sample.files


def test_deleted_or_renamed_negative_all_modifications():
    runner = _fake_runner(name_status="M\ta.py\nA\tb.py\n")
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.deleted_or_renamed == 0


# -- new_top_level_dirs -----------------------------------------------------


def test_new_top_level_dir_detected():
    runner = _fake_runner(name_status="A\tnewdir/file.py\n", ls_tree="steward\ntests\nREADME.md\n")
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.new_top_level_dirs == ["newdir"]


def test_no_new_top_level_dir_for_existing_dir():
    runner = _fake_runner(name_status="M\tsteward/file.py\n", ls_tree="steward\ntests\n")
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.new_top_level_dirs == []


# -- manifests_changed -------------------------------------------------------


def test_manifest_change_detected():
    runner = _fake_runner(name_status="M\tpyproject.toml\nM\tsteward/x.py\n")
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.manifests_changed == ["pyproject.toml"]


def test_no_manifest_change_for_unrelated_file():
    runner = _fake_runner(name_status="M\tsteward/x.py\n")
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.manifests_changed == []


# -- summary_commits ----------------------------------------------------------


def test_summary_commit_detected_on_added_file():
    runner = _fake_runner(name_status="A\t.planning/phases/12-01-SUMMARY.md\n")
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.summary_commits == [".planning/phases/12-01-SUMMARY.md"]


def test_summary_commit_not_detected_when_only_modified():
    """"Added" is the signal (D5: "a commit adds *-SUMMARY.md"); a modified
    summary file is not a new phase boundary."""
    runner = _fake_runner(name_status="M\t.planning/phases/12-01-SUMMARY.md\n")
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.summary_commits == []


# -- interface_files ----------------------------------------------------------


def test_interface_files_detected():
    runner = _fake_runner(name_status="M\tapi.proto\nM\topenapi.yaml\nM\ttypes.d.ts\nM\tsteward/x.py\n")
    sample = scan(Path("/repo"), BASE, runner)
    assert set(sample.interface_files) == {"api.proto", "openapi.yaml", "types.d.ts"}


def test_no_interface_files_for_unrelated_change():
    runner = _fake_runner(name_status="M\tsteward/x.py\n")
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.interface_files == []


# -- public_api_changes -------------------------------------------------------


def test_public_api_removal_detected(tmp_path):
    (tmp_path / "mod.py").write_text("def bar():\n    pass\n")  # `foo` removed
    runner = _fake_runner(
        name_status="M\tmod.py\n",
        show_map={f"{BASE}:mod.py": "def foo():\n    pass\n\ndef bar():\n    pass\n"},
    )
    sample = scan(tmp_path, BASE, runner)
    assert sample.public_api_changes == ["mod.py:foo"]
    assert sample.parse_errors == 0


def test_public_api_signature_change_detected(tmp_path):
    (tmp_path / "mod.py").write_text("def foo(a, b, c):\n    pass\n")
    runner = _fake_runner(
        name_status="M\tmod.py\n",
        show_map={f"{BASE}:mod.py": "def foo(a, b):\n    pass\n"},
    )
    sample = scan(tmp_path, BASE, runner)
    assert sample.public_api_changes == ["mod.py:foo"]


def test_public_api_private_name_ignored(tmp_path):
    (tmp_path / "mod.py").write_text("")  # `_helper` removed entirely
    runner = _fake_runner(
        name_status="M\tmod.py\n",
        show_map={f"{BASE}:mod.py": "def _helper():\n    pass\n"},
    )
    sample = scan(tmp_path, BASE, runner)
    assert sample.public_api_changes == []


def test_public_api_test_file_ignored(tmp_path):
    (tmp_path / "test_mod.py").write_text("")
    runner = _fake_runner(
        name_status="M\ttest_mod.py\n",
        show_map={f"{BASE}:test_mod.py": "def test_foo():\n    pass\n"},
    )
    sample = scan(tmp_path, BASE, runner)
    assert sample.public_api_changes == []


def test_public_api_new_function_is_not_a_change(tmp_path):
    """Adding a public function is not flagged -- only removal/signature
    change is a trigger-worthy risk (D5)."""
    (tmp_path / "mod.py").write_text("def foo():\n    pass\n\ndef bar():\n    pass\n")
    runner = _fake_runner(
        name_status="M\tmod.py\n",
        show_map={f"{BASE}:mod.py": "def foo():\n    pass\n"},
    )
    sample = scan(tmp_path, BASE, runner)
    assert sample.public_api_changes == []


def test_public_api_parse_error_is_skipped_and_counted(tmp_path):
    (tmp_path / "mod.py").write_text("def foo(:\n")  # invalid syntax
    runner = _fake_runner(
        name_status="M\tmod.py\n",
        show_map={f"{BASE}:mod.py": "def foo():\n    pass\n"},
    )
    sample = scan(tmp_path, BASE, runner)
    assert sample.public_api_changes == []
    assert sample.parse_errors == 1


def test_public_api_deleted_file_reports_all_public_names_removed(tmp_path):
    # no worktree file at all -> deleted
    runner = _fake_runner(
        name_status="D\tmod.py\n",
        show_map={f"{BASE}:mod.py": "def foo():\n    pass\n\nclass Bar:\n    pass\n"},
    )
    sample = scan(tmp_path, BASE, runner)
    assert set(sample.public_api_changes) == {"mod.py:foo", "mod.py:Bar"}


def test_public_api_dunder_all_includes_underscore_name(tmp_path):
    (tmp_path / "mod.py").write_text("__all__ = ['_special']\n")  # `_special` removed
    runner = _fake_runner(
        name_status="M\tmod.py\n",
        show_map={f"{BASE}:mod.py": "__all__ = ['_special']\n\ndef _special():\n    pass\n"},
    )
    sample = scan(tmp_path, BASE, runner)
    assert sample.public_api_changes == ["mod.py:_special"]


# -- weakening passthrough -----------------------------------------------------


def test_weakening_findings_are_formatted_as_kind_colon_path():
    runner = _fake_runner(
        name_status="D\ttests/test_x.py\n",
        weakening_diffs={},
    )
    sample = scan(Path("/repo"), BASE, runner)
    assert sample.weakening == ["deleted_test_file:tests/test_x.py"]


# -- real git repo end-to-end ---------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def test_scan_against_a_real_git_repo(tmp_path):
    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=tmp_path)
    _git("config", "user.name", "t", cwd=tmp_path)
    (tmp_path / "mod.py").write_text("def foo():\n    pass\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    _git("add", ".", cwd=tmp_path)
    _git("commit", "-q", "-m", "base", cwd=tmp_path)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()

    # remove foo, add a new dependency, add a new top-level dir
    (tmp_path / "mod.py").write_text("def bar():\n    pass\n")
    (tmp_path / "requirements.txt").write_text("requests==2.0\n")
    (tmp_path / "newdir").mkdir()
    (tmp_path / "newdir" / "thing.py").write_text("x = 1\n")
    _git("add", "-A", cwd=tmp_path)  # stage new/untracked files so `git diff <base>` sees them

    runner = SubprocessRunner()
    sample = scan(tmp_path, base_sha, runner)

    assert sample.changed_lines > 0
    assert "mod.py:foo" in sample.public_api_changes
    assert "requirements.txt" in sample.manifests_changed
    assert sample.new_top_level_dirs == ["newdir"]
    assert isinstance(sample, DiffSample)
