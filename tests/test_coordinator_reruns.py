"""Coordinator false-red reruns (spec §5.12, R4/R5): node-id mapping, classification, run order, budgets, overlay env."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from steward.baseline import CheckRun
from steward.coordinator import reruns
from steward.coordinator.config import Settings
from steward.runner_exec import FakeRunner

HEAD = "a" * 40
BASE = "b" * 40
IDS = ["tests.test_a::test_one", "tests.test_a::test_two"]
OK = {i: "passed" for i in IDS}
BAD = {i: "failed" for i in IDS}


# ---------- node_id ----------

@pytest.fixture
def tree(tmp_path):
    t = tmp_path / "tree"
    (t / "tests" / "sub").mkdir(parents=True)
    (t / "tests" / "test_x.py").write_text("")
    (t / "tests" / "sub" / "test_y.py").write_text("")
    (t / "test_root.py").write_text("")
    return t


def test_node_id_module(tree):
    assert reruns.node_id("tests.test_x::test_a", tree) == "tests/test_x.py::test_a"
    assert reruns.node_id("tests.sub.test_y::test_b", tree) == "tests/sub/test_y.py::test_b"
    assert reruns.node_id("test_root::t", tree) == "test_root.py::t"


def test_node_id_class_and_params(tree):
    assert reruns.node_id("tests.test_x.TestC::t[1]", tree) == "tests/test_x.py::TestC::t[1]"
    assert reruns.node_id("tests.test_x.TestC.Inner::t[a.b-c]", tree) == "tests/test_x.py::TestC::Inner::t[a.b-c]"


def test_node_id_already_node_form(tree):
    assert reruns.node_id("tests/test_x.py::test_a", tree) == "tests/test_x.py::test_a"
    assert reruns.node_id("tests/test_x.py::TestC::t[1]", tree) == "tests/test_x.py::TestC::t[1]"
    assert reruns.node_id("tests/missing.py::t", tree) is None


def test_node_id_unmappable(tree):
    for bad in ("tests.test_missing::t", "nocolons", "::t", "tests.test_x::", "..tests.test_x::t",
                "../tree/tests/test_x.py::t", "/etc/passwd.py::t", "-x::t"):
        assert reruns.node_id(bad, tree) is None, bad


# ---------- failing_ids ----------

def test_failing_ids():
    assert reruns.failing_ids({"new_failures": ["a::b", "c::d", "a::b"], "fixed": []}) == ["a::b", "c::d"]
    assert reruns.failing_ids({}) == []
    assert reruns.failing_ids({"new_failures": "a::b"}) == []
    assert reruns.failing_ids_from_verification is reruns.failing_ids


# ---------- classify: all six outcomes ----------

def test_classify_env_dependent_and_env_live_red():
    assert reruns.classify([OK, OK], OK, [], IDS) == "env_dependent"
    assert reruns.classify([OK, OK], {IDS[0]: "passed", IDS[1]: "failed"}, [], IDS) == "env_live_red"
    assert reruns.classify([OK, OK], {IDS[0]: "passed", IDS[1]: "error"}, [], IDS) == "env_live_red"


def test_classify_live_missing_is_inconclusive():
    assert reruns.classify([OK, OK], None, [], IDS) == "inconclusive"


def test_classify_flaky():
    assert reruns.classify([OK, BAD], OK, [BAD, BAD], IDS) == "flaky"
    one_flips = {IDS[0]: "failed", IDS[1]: "passed"}
    assert reruns.classify([BAD, one_flips], BAD, [BAD, BAD], IDS) == "flaky"


def test_classify_preexisting_and_real():
    assert reruns.classify([BAD, BAD], BAD, [BAD, BAD], IDS) == "preexisting"
    assert reruns.classify([BAD, BAD], OK, [OK, OK], IDS) == "real"
    errs = {i: "error" for i in IDS}
    assert reruns.classify([errs, BAD], BAD, [errs, BAD], IDS) == "preexisting"


def test_classify_inconclusive_cases():
    assert reruns.classify([BAD, BAD], BAD, [OK, BAD], IDS) == "inconclusive"   # base flaky
    assert reruns.classify([BAD, BAD], BAD, [], IDS) == "inconclusive"          # no base runs
    mixed = {IDS[0]: "failed", IDS[1]: "passed"}
    assert reruns.classify([mixed, mixed], mixed, [BAD, BAD], IDS) == "inconclusive"
    skipped = {i: "skipped" for i in IDS}
    assert reruns.classify([skipped, skipped], OK, [], IDS) == "inconclusive"
    assert reruns.classify([OK], OK, [], IDS) == "inconclusive"                 # only one head run
    assert reruns.classify([OK, OK], OK, [], []) == "inconclusive"


# ---------- rerun harness ----------

class Rec:
    """Fake run_check: records every call; outcome by label (head trees vs base tree, overlay vs live)."""

    def __init__(self, head=None, live=None, base=None, broken_at=None, clock=None, step=0.0):
        self.calls = []
        self.head, self.live, self.base = head or OK, live or OK, base or BAD
        self.broken_at, self.clock, self.step = broken_at, clock, step

    def __call__(self, runner, line, cwd, xml_path, python_env="", *, timeout_s=None, fallback=True):
        hermetic = isinstance(runner, reruns.OverlayRunner)
        self.calls.append(dict(runner=runner, line=line, cwd=Path(cwd), xml=xml_path, timeout_s=timeout_s,
                               fallback=fallback, hermetic=hermetic))
        if self.clock is not None:
            self.clock.t += self.step
        if self.broken_at == len(self.calls):
            return CheckRun(exit_code=124, tests={}, broken=f"timed out after {timeout_s}s")
        if Path(cwd).name.startswith("base-"):
            tests = self.base
        else:
            tests = self.head if hermetic else self.live
        if callable(tests):
            tests = tests(len(self.calls))
        return CheckRun(exit_code=0 if all(v == "passed" for v in tests.values()) else 1, tests=dict(tests))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _wt(made):
    def fn(repo, sha, dest):
        dest = Path(dest)
        (dest / "tests").mkdir(parents=True, exist_ok=True)
        (dest / "tests" / "test_a.py").write_text("")
        made.append((sha, dest))
        return dest
    return fn


def _rerun(tmp_path, rc, *, ids=IDS, base_sha=BASE, settings=None, worktree_fn=None, clock=None, runner=None):
    made = []
    res = reruns.rerun(repo=tmp_path / "repo", head_sha=HEAD, base_sha=base_sha, ids=ids, scratch=tmp_path / "s",
                       settings=settings or Settings(), run_check=rc, runner=runner or FakeRunner(),
                       worktree_fn=worktree_fn or _wt(made), clock=clock or Clock())
    return res, made


def test_head_green_live_green_skips_base(tmp_path):
    rc = Rec()
    res, made = _rerun(tmp_path, rc)
    assert res.outcome == "env_dependent"
    assert [c["hermetic"] for c in rc.calls] == [True, True, False]
    assert all(c["cwd"].name == f"head-{HEAD[:12]}" for c in rc.calls)
    assert [sha for sha, _ in made] == [HEAD]
    assert res.base_runs == [] and len(res.head_runs) == 2 and res.live_run == OK
    assert res.node_ids == ["tests/test_a.py::test_one", "tests/test_a.py::test_two"]
    assert len(res.commands) == 3 and len(res.durations_s) == 3
    line = rc.calls[0]["line"]
    assert line.startswith(f"{sys.executable} -m pytest -q -p no:randomly -p no:cacheprovider ") or \
        "-m pytest -q -p no:randomly -p no:cacheprovider" in line
    assert line.endswith("tests/test_a.py::test_one tests/test_a.py::test_two")
    assert all(c["timeout_s"] == 300.0 and c["fallback"] is False for c in rc.calls)


def test_head_green_live_red_is_env_live_red(tmp_path):
    res, _ = _rerun(tmp_path, Rec(live=BAD))
    assert res.outcome == "env_live_red"
    assert res.base_runs == []


def test_head_red_runs_base(tmp_path):
    rc = Rec(head=BAD, live=BAD, base=BAD)
    res, made = _rerun(tmp_path, rc)
    assert res.outcome == "preexisting"
    assert [sha for sha, _ in made] == [HEAD, BASE]
    assert [c["hermetic"] for c in rc.calls] == [True, True, False, True, True]
    assert all(c["cwd"].name == f"base-{BASE[:12]}" for c in rc.calls[3:])
    assert len(res.base_runs) == 2 and len(res.commands) == 5


def test_head_red_base_green_is_real(tmp_path):
    res, _ = _rerun(tmp_path, Rec(head=BAD, live=BAD, base=OK))
    assert res.outcome == "real"


def test_head_red_without_base_sha_is_inconclusive(tmp_path):
    rc = Rec(head=BAD, live=BAD)
    res, made = _rerun(tmp_path, rc, base_sha=None)
    assert res.outcome == "inconclusive" and "base" in res.note
    assert len(rc.calls) == 3 and [s for s, _ in made] == [HEAD]


def test_flaky(tmp_path):
    res, _ = _rerun(tmp_path, Rec(head=lambda n: OK if n == 1 else BAD, live=OK, base=BAD))
    assert res.outcome == "flaky"


def test_missing_id_counts_as_error(tmp_path):
    rc = Rec(head={IDS[0]: "passed"}, live=OK, base=OK)
    res, _ = _rerun(tmp_path, rc)
    assert res.head_runs[0][IDS[1]] == "error"
    assert len(res.base_runs) == 2  # a missing id is a head failure, so base ran


def test_node_form_ids_are_read_back_from_junit_keys(tmp_path):
    ids = ["tests/test_a.py::test_one"]
    rc = Rec(head={"tests.test_a::test_one": "passed"}, live={"tests.test_a::test_one": "passed"})
    res, _ = _rerun(tmp_path, rc, ids=ids)
    assert res.outcome == "env_dependent" and res.head_runs[0] == {ids[0]: "passed"}


def test_broken_run_stops_inconclusive_with_note(tmp_path):
    rc = Rec(broken_at=2)
    res, _ = _rerun(tmp_path, rc)
    assert res.outcome == "inconclusive"
    assert "timed out" in res.note
    assert len(rc.calls) == 2
    assert res.head_runs[-1] == {i: "error" for i in IDS}


def test_broken_live_run_stops(tmp_path):
    res, _ = _rerun(tmp_path, Rec(broken_at=3))
    assert res.outcome == "inconclusive" and "timed out" in res.note and "live" in res.note


def test_total_budget_stop(tmp_path):
    clock = Clock()
    rc = Rec(clock=clock, step=450.0)  # two runs use the whole 900 s
    res, _ = _rerun(tmp_path, rc, clock=clock)
    assert res.outcome == "inconclusive" and "budget" in res.note
    assert len(rc.calls) == 2


def test_per_run_timeout_capped_by_remaining_budget(tmp_path):
    clock = Clock()
    rc = Rec(clock=clock, step=400.0)
    _rerun(tmp_path, rc, clock=clock)
    assert [c["timeout_s"] for c in rc.calls] == [300.0, 300.0, 100.0]


def test_too_many_ids_stops_before_any_run(tmp_path):
    rc = Rec()
    ids = [f"tests.test_a::t{i}" for i in range(51)]
    res, made = _rerun(tmp_path, rc, ids=ids)
    assert res.outcome == "inconclusive" and "50" in res.note
    assert rc.calls == [] and made == []


def test_no_ids_is_inconclusive(tmp_path):
    rc = Rec()
    res, made = _rerun(tmp_path, rc, ids=[])
    assert res.outcome == "inconclusive" and rc.calls == [] and made == []


def test_unmappable_id_stops(tmp_path):
    rc = Rec()
    res, _ = _rerun(tmp_path, rc, ids=["tests.test_nope::t"])
    assert res.outcome == "inconclusive" and "tests.test_nope::t" in res.note
    assert rc.calls == []


def test_worktree_failure_is_inconclusive(tmp_path):
    def boom(repo, sha, dest):
        raise RuntimeError("fatal: invalid reference")
    rc = Rec()
    res, _ = _rerun(tmp_path, rc, worktree_fn=boom)
    assert res.outcome == "inconclusive" and "worktree" in res.note and "invalid reference" in res.note
    assert rc.calls == []


def test_default_worktree_fn_failure_is_inconclusive(tmp_path):
    from steward.runner_exec import CommandResult

    def resp(argv, cwd):
        code = 128 if argv[:3] == ["git", "worktree", "add"] else 0
        return CommandResult(argv=argv, cwd=cwd, exit_code=code, stdout="", stderr="fatal: bad sha", duration_s=0)
    fr = FakeRunner(resp)
    res = reruns.rerun(repo=tmp_path, head_sha=HEAD, base_sha=BASE, ids=IDS, scratch=tmp_path / "s",
                       settings=Settings(), run_check=Rec(), runner=fr, clock=Clock())
    assert res.outcome == "inconclusive" and "bad sha" in res.note
    adds = [c["argv"] for c in fr.calls if c["argv"][:3] == ["git", "worktree", "add"]]
    assert adds == [["git", "worktree", "add", "--detach", str(tmp_path / "s" / f"head-{HEAD[:12]}"), HEAD]]
    removes = [c["argv"] for c in fr.calls if c["argv"][:3] == ["git", "worktree", "remove"]]
    assert removes == [["git", "worktree", "remove", "--force", str(tmp_path / "s" / f"head-{HEAD[:12]}")]]


def test_bad_sha_is_inconclusive(tmp_path):
    rc = Rec()
    made = []
    res = reruns.rerun(repo=tmp_path, head_sha="--upload-pack=x", base_sha=BASE, ids=IDS, scratch=tmp_path / "s",
                       settings=Settings(), run_check=rc, runner=FakeRunner(), worktree_fn=_wt(made), clock=Clock())
    assert res.outcome == "inconclusive" and made == [] and rc.calls == []


def test_worktrees_removed_in_finally(tmp_path):
    fr = FakeRunner()
    rc = Rec(head=BAD, live=BAD, base=OK)
    res, made = _rerun(tmp_path, rc, runner=fr)
    removes = [c["argv"][-1] for c in fr.calls if c["argv"][:3] == ["git", "worktree", "remove"]]
    assert removes == [str(d) for _, d in made]
    assert not any(d.exists() for _, d in made)


# ---------- overlay env ----------

def test_overlay_env_hermetic_and_live_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_PANE_ID", "p-live")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w-live")
    seen = []

    def rc(runner, line, cwd, xml_path, python_env="", *, timeout_s=None, fallback=True):
        base_env = {**os.environ, "TMPDIR": "/x"}
        runner.shell_line(line, cwd, env=base_env, timeout=timeout_s)
        seen.append((Path(cwd), runner))
        return CheckRun(exit_code=0, tests=dict(OK))

    fr = FakeRunner()
    res, made = _rerun(tmp_path, rc, runner=fr)
    assert res.outcome == "env_dependent"
    shells = [c for c in fr.calls if c["argv"][:2] == ["bash", "-lc"]]
    assert len(shells) == 3
    scratch = tmp_path / "s"
    tree = made[0][1]
    homes = set()
    for call in shells[:2]:
        env = call["env"]
        assert Path(env["STEWARD_HOME"]).parent == scratch
        assert Path(env["STEWARD_CONFIG_DIR"]).parent == scratch
        assert env["STEWARD_LIVE_ROOT"] == str(tree)
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
        assert Path(env["HERDR_SOCKET_PATH"]).parent == scratch
        assert "HERDR_PANE_ID" not in env and "HERDR_WORKSPACE_ID" not in env
        assert env["TMPDIR"] == "/x"  # run_check's own env survives under the overlay
        homes.add(env["STEWARD_HOME"])
    assert len(homes) == 2  # fresh per run
    live = shells[2]["env"]
    assert live["HERDR_PANE_ID"] == "p-live" and live["HERDR_WORKSPACE_ID"] == "w-live"
    assert live["STEWARD_HOME"] == os.environ["STEWARD_HOME"]
    assert "STEWARD_LIVE_ROOT" not in live or live["STEWARD_LIVE_ROOT"] == os.environ.get("STEWARD_LIVE_ROOT")
    assert seen[2][1] is fr  # the live run gets the plain runner


def test_overlay_runner_env_none_starts_from_os_environ(monkeypatch):
    monkeypatch.setenv("HERDR_PANE_ID", "p")
    monkeypatch.setenv("SOME_VAR", "kept")
    fr = FakeRunner()
    reruns.OverlayRunner(fr, {"A": "1"}, unset=("HERDR_PANE_ID",)).run(["true"], "/")
    env = fr.calls[0]["env"]
    assert env["A"] == "1" and env["SOME_VAR"] == "kept" and "HERDR_PANE_ID" not in env


# ---------- real git + real pytest smoke ----------

def _git(repo, *args, **kw):
    return subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                           "-c", "commit.gpgsign=false", *args], cwd=repo, check=True, capture_output=True,
                          text=True, **kw)


def test_rerun_real_git_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("STEWARD_PYTEST_NESTED", raising=False)
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "tests" / "test_a.py").write_text("def test_one():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "tests" / "test_a.py").write_text(
        "import os\n\ndef test_one():\n    assert True\n\ndef test_two():\n    assert os.environ['STEWARD_HOME']\n")
    _git(repo, "commit", "-qam", "head")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    scratch = tmp_path / "s"
    res = reruns.rerun(repo=repo, head_sha=head, base_sha=base, ids=["tests.test_a::test_one"], scratch=scratch,
                       settings=Settings())
    assert res.outcome == "env_dependent", (res.note, res.head_runs, res.live_run)
    assert res.node_ids == ["tests/test_a.py::test_one"]
    assert res.head_runs == [{"tests.test_a::test_one": "passed"}] * 2
    assert res.live_run == {"tests.test_a::test_one": "passed"} and res.base_runs == []
    assert not list(scratch.glob("head-*")) and not list(scratch.glob("base-*"))
    listed = _git(repo, "worktree", "list", "--porcelain").stdout
    assert str(scratch) not in listed
