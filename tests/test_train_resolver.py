# tests/test_train_resolver.py
from steward import train_resolver
from steward.runner_exec import CommandResult, FakeRunner
from test_incorporate import git  # noqa: F401


def test_resolve_runs_headless_opus_without_api_key(tmp_path):
    seen = {}

    def respond(argv, cwd):
        seen.update(argv=argv, cwd=cwd)
        return CommandResult(argv, cwd, 0, "", "", 1.0)

    ok = train_resolver.resolve(tmp_path, "job1", ["a.txt"], goal="g", target="fleet-registry",
                                runner=FakeRunner(respond), timeout_s=1200)
    assert ok is True
    argv = seen["argv"]
    assert argv[:3] == ["env", "-u", "ANTHROPIC_API_KEY"]
    assert argv[argv.index("--model") + 1] == "opus"
    assert "fleet-registry" in argv[argv.index("-p") + 1]
    assert seen["cwd"] == str(tmp_path)


def test_has_markers(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "f.txt").write_text("ok\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> b\n")
    git(tmp_path, "add", "f.txt")
    assert train_resolver.has_markers(tmp_path, ["f.txt"]) is True
    (tmp_path / "f.txt").write_text("ok\n")
    git(tmp_path, "add", "f.txt")
    assert train_resolver.has_markers(tmp_path, ["f.txt"]) is False


def test_resolve_runs_claude_with_tmpdir_under_merge_root(tmp_path):
    """m3: the resolver's claude gets TMPDIR under the merge root's tmp, never /tmp."""
    runner = FakeRunner(lambda argv, cwd: CommandResult(argv, cwd, 0, "", "", 1.0))
    tmpd = tmp_path / "mroot" / "tmp" / "resolver"
    train_resolver.resolve(tmp_path, "job1", ["a.txt"], goal="g", target="main", runner=runner, timeout_s=10,
                           tmp_dir=tmpd)
    assert runner.calls[-1]["env"]["TMPDIR"] == str(tmpd) and tmpd.is_dir()
