"""Zero-touch Task 15 (spec §5.5): the train gates on NEW failures vs the base (cached per sha+env+command), reruns the
failing ids once, and never freezes on a red base; autopilot's merged-tree check uses the same comparison."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from steward import autopilot, train_gates as tg
from steward import landing_queue as q
from steward import landing_train as lt
from steward.baseline import CheckRun
from steward.models import JobState
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from test_incorporate import mkjob, scratch  # noqa: F401

SUITE = "python3 -m pytest -q -p no:cacheprovider"


@pytest.fixture(autouse=True)
def _hermetic_inner_pytest(monkeypatch):
    """The inner (subprocess) pytest must not inherit this run's plugins/options/xdist worker identity."""
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT",
                 "PYTEST_XDIST_TESTRUNUID", "PYTEST_CURRENT_TEST", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        monkeypatch.delenv(name, raising=False)


def _repo(tmp_path):
    r = tmp_path / "r"; r.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)  # noqa: E731
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / "pytest.ini").write_text("[pytest]\n")   # rootdir + confcutdir = r: no outer conftest/ini is picked up
    (r / "test_s.py").write_text(
        "import os, pathlib\n"
        "def test_base_red(): assert False\n"
        "def test_ok(): assert True\n"
        "def test_flaky():\n"
        "    f = pathlib.Path(os.environ['FLAKY_FILE']); n = int(f.read_text() or 0); f.write_text(str(n+1)); assert n % 2 == 1\n")
    g("add", "-A"); g("commit", "-qm", "base")
    return r, g


def _head(r):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, capture_output=True, text=True).stdout.strip()


# -- the brief's three tests (verbatim behaviour) -----------------------------------------------------------------


def test_new_failure_vs_base(tmp_path, steward_home, monkeypatch):
    r, g = _repo(tmp_path); store = Store()
    monkeypatch.setenv("FLAKY_FILE", str(tmp_path / "flaky")); (tmp_path / "flaky").write_text("1")
    base = _head(r)
    out = tg.gate(r, base, SubprocessRunner(), store=store, repo=r, suite_cmd=SUITE, tmp_root=tmp_path / "t",
                  env_digest="e", now=1.0)
    assert out["ok"] and out["preexisting"] == ["test_s.py::test_base_red"] and out["new_failures"] == []
    (r / "test_s.py").write_text((r / "test_s.py").read_text() + "def test_new(): assert False\n")
    out = tg.gate(r, base, SubprocessRunner(), store=store, repo=r, suite_cmd=SUITE, tmp_root=tmp_path / "t",
                  env_digest="e", now=2.0)
    assert not out["ok"] and out["new_failures"] == ["test_s.py::test_new"]
    assert "test_s.py::test_new" in out["why"]


def test_flaky_ids_pass_on_rerun_are_green(tmp_path, steward_home, monkeypatch):
    r, g = _repo(tmp_path); store = Store()
    monkeypatch.setenv("FLAKY_FILE", str(tmp_path / "flaky")); (tmp_path / "flaky").write_text("1")
    base = _head(r)
    tg.gate(r, base, SubprocessRunner(), store=store, repo=r, suite_cmd=SUITE, tmp_root=tmp_path / "t", env_digest="e", now=1.0)
    (tmp_path / "flaky").write_text("0")       # the candidate run will fail test_flaky once, pass on rerun
    out = tg.gate(r, base, SubprocessRunner(), store=store, repo=r, suite_cmd=SUITE, tmp_root=tmp_path / "t", env_digest="e", now=2.0)
    assert out["ok"] and out["reran_green"] == ["test_s.py::test_flaky"]


def test_base_cache_refreshes_after_30_min(tmp_path, steward_home, monkeypatch):
    r, g = _repo(tmp_path); store = Store(); calls = []
    base = "abc"
    suite = lambda wt: calls.append(wt) or set()  # noqa: E731
    tg.base_failures(store, r, base, env_digest="e", suite_fn=suite, worktree=r, now=0.0)
    tg.base_failures(store, r, base, env_digest="e", suite_fn=suite, worktree=r, now=100.0)
    assert len(calls) == 1
    tg.base_failures(store, r, base, env_digest="e", suite_fn=suite, worktree=r, now=1900.0)
    assert len(calls) == 2


# -- edges ---------------------------------------------------------------------------------------------------------


def test_base_cache_is_keyed_by_env_digest(tmp_path, steward_home):
    store, calls = Store(), []
    suite = lambda wt: calls.append(wt) or {"t.py::a"}  # noqa: E731
    assert tg.base_failures(store, tmp_path, "abc", env_digest="e1", suite_fn=suite, worktree=tmp_path, now=0.0) == {"t.py::a"}
    tg.base_failures(store, tmp_path, "abc", env_digest="e2", suite_fn=suite, worktree=tmp_path, now=1.0)
    tg.base_failures(store, tmp_path, "abd", env_digest="e1", suite_fn=suite, worktree=tmp_path, now=2.0)
    assert len(calls) == 3
    assert store.kv_get("_train", "base_suite:abc:e1") == {"failing": ["t.py::a"], "at": 0.0}
    assert tg.suite_digest("e", "pytest -q") != tg.suite_digest("e", "pytest -q -n 8")


def test_gate_reports_fixed_and_leaves_the_worktree_where_it_was(tmp_path, steward_home, monkeypatch):
    r, g = _repo(tmp_path); store = Store()
    monkeypatch.setenv("FLAKY_FILE", str(tmp_path / "flaky")); (tmp_path / "flaky").write_text("1")
    (r / "test_needs.py").write_text("import pathlib\ndef test_needs_file(): assert pathlib.Path('fixed.txt').exists()\n")
    g("add", "-A"); g("commit", "-qm", "base with a red test")
    base = _head(r)
    (r / "fixed.txt").write_text("x"); g("add", "-A"); g("commit", "-qm", "fix the red without touching a test")
    (r / "scratch.txt").write_text("untracked candidate state\n")
    cand = _head(r)
    out = tg.gate(r, base, SubprocessRunner(), store=store, repo=r, suite_cmd=SUITE, tmp_root=tmp_path / "t",
                  env_digest="e", now=1.0)
    assert out["ok"], out
    assert out["fixed"] == ["test_needs.py::test_needs_file"] and out["preexisting"] == ["test_s.py::test_base_red"]
    assert _head(r) == cand and (r / "scratch.txt").exists()
    wts = subprocess.run(["git", "worktree", "list"], cwd=r, capture_output=True, text=True).stdout.splitlines()
    assert len(wts) == 1                               # the temp base tree is gone


def test_base_suite_raising_never_moves_the_candidate(tmp_path, steward_home, monkeypatch):
    r, g = _repo(tmp_path); store = Store()
    (r / "c.txt").write_text("c"); g("add", "c.txt"); g("commit", "-qm", "cand")
    cand = _head(r)
    base = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=r, capture_output=True, text=True).stdout.strip()
    real = tg._suite_failures

    def boom(wt, *a, **k):
        if Path(wt) != r:
            raise RuntimeError("base suite exploded")
        return real(wt, *a, **k)
    monkeypatch.setattr(tg, "_suite_failures", boom)
    with pytest.raises(RuntimeError):
        tg.gate(r, base, SubprocessRunner(), store=store, repo=r, suite_cmd=SUITE, tmp_root=tmp_path / "t",
                env_digest="e", now=1.0)
    assert _head(r) == cand and not subprocess.run(["git", "status", "--porcelain"], cwd=r, capture_output=True,
                                                  text=True).stdout.strip()
    assert store.kv_get("_train", f"base_suite:{base}:{tg.suite_digest('e', SUITE)}") is None   # nothing cached


def test_collection_error_on_candidate_is_red_and_never_rerun_green(tmp_path, steward_home, monkeypatch):
    r, g = _repo(tmp_path); store = Store()
    monkeypatch.setenv("FLAKY_FILE", str(tmp_path / "flaky")); (tmp_path / "flaky").write_text("1")
    base = _head(r)
    (r / "test_broken.py").write_text("import no_such_module_xyz\n")
    out = tg.gate(r, base, SubprocessRunner(), store=store, repo=r, suite_cmd=SUITE, tmp_root=tmp_path / "t",
                  env_digest="e", now=1.0)
    assert not out["ok"] and out["new_failures"] == [tg.SUITE_ID] and out["reran_green"] == []


def test_unusable_base_judges_the_candidate_absolutely(tmp_path, steward_home, monkeypatch):
    store, ran = Store(), []
    store.kv_set("_train", f"base_suite:abc:{tg.suite_digest('e', SUITE)}", {"failing": [tg.SUITE_ID], "at": 0.0})
    monkeypatch.setattr(tg, "_suite_failures", lambda *a, **k: ran.append(a) or set())
    monkeypatch.setattr(tg, "tests_removed", lambda *a: [])
    out = tg.gate(tmp_path, "abc", None, store=store, repo=tmp_path, suite_cmd=SUITE, tmp_root=tmp_path / "t",
                  env_digest="e", now=10.0)
    assert out["ok"] and out["base_unusable"] and "unjudged" not in out and len(ran) == 1


def test_a_broken_rerun_keeps_every_new_failure(tmp_path, steward_home, monkeypatch):
    store, runs = Store(), []
    store.kv_set("_train", f"base_suite:abc:{tg.suite_digest('e', SUITE)}", {"failing": [], "at": 0.0})

    def fake(wt, cmd, tmp_root, runner, ids=None, **k):
        runs.append((cmd, ids))
        return {"t.py::a", "t.py::b"} if ids is None else {tg.SUITE_ID}   # the rerun itself blew up
    monkeypatch.setattr(tg, "_suite_failures", fake)
    monkeypatch.setattr(tg, "tests_removed", lambda *a: [])
    out = tg.gate(tmp_path, "abc", None, store=store, repo=tmp_path, suite_cmd=SUITE, tmp_root=tmp_path / "t",
                  env_digest="e", now=10.0)
    assert not out["ok"] and out["new_failures"] == ["t.py::a", "t.py::b"] and out["reran_green"] == []
    assert runs[1] == (SUITE + " -p no:randomly", ["t.py::a", "t.py::b"])


def test_failing_ids_are_node_ids_and_compare_across_junit_families(tmp_path):
    xml = tmp_path / "j.xml"
    xml.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_x.TestC" name="test_m[a-1]" file="tests/test_x.py"><failure/></testcase>'
        '<testcase classname="tests.test_x" name="test_f" file="tests/test_x.py"><error/></testcase>'
        '<testcase classname="" name="tests.test_bad" file="tests/test_bad.py"><error/></testcase>'
        '<testcase classname="tests.test_x" name="test_ok" file="tests/test_x.py"/>'
        '<testcase classname="tests.test_y" name="test_old"><failure/></testcase>'
        '</testsuite></testsuites>')
    assert tg.failing_ids(xml) == {"tests/test_x.py::TestC::test_m[a-1]", "tests/test_x.py::test_f",
                                   "tests/test_bad.py", "tests.test_y::test_old"}
    assert tg.junit_key("tests/test_x.py::TestC::test_m[a-1]") == "tests.test_x.TestC::test_m[a-1]"
    assert tg.junit_key("tests.test_x.TestC::test_m[a-1]") == "tests.test_x.TestC::test_m[a-1]"
    new, pre, fixed = tg.compare({"tests/test_y.py::test_old", "tests/test_y.py::test_new"},
                                 {"tests.test_y::test_old", "tests.test_y::test_gone"})
    assert (new, pre, fixed) == (["tests/test_y.py::test_new"], ["tests/test_y.py::test_old"], ["tests.test_y::test_gone"])


# -- the landing train ---------------------------------------------------------------------------------------------


def _settings(scratch, tmp_path, **kw):
    s = {"enabled": True, "repo": str(scratch), "target": "main", "every_s": 180.0, "max_batch": 8,
         "resolver_timeout_s": 5.0, "suite_cmd": "true", "worktree_root": str(tmp_path / "mroot"), "report_hour": 7}
    s.update(kw)
    return s


def _g(ok=True, **kw):
    out = {"ok": ok, "new_failures": [], "preexisting": [], "reran_green": [], "fixed": [], "removed": [],
           "why": "" if ok else "new failures: t.py::x"}
    out.update(kw)
    return out


def _report(store):
    return "".join(p.read_text() for p in sorted((Path(store.home) / "train-report").glob("*.md")))


def test_train_lands_green_candidate_on_a_red_base(scratch, tmp_path):
    store, sent = Store(), []
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    gate = lambda wt, base: _g(preexisting=["t.py::old"])  # noqa: E731
    out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False, gate_fn=gate,
                      deploy_fn=None, stop_fn=lambda _id: None, now=1000.0, notify=lambda t: sent.append(t) or True)
    assert out["landed"] == [a.job_id] and store.get_job(a.job_id).state == JobState.RELEASED
    assert store.kv_get(lt.PSEUDO, "base_red_head") is None
    assert "preexisting=['t.py::old'] new_failures=[]" in _report(store)
    assert len([t for t in sent if "pre-existing" in t]) == 1


def test_absolute_red_suite_seam_no_longer_freezes(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False,
                      suite_fn=lambda wt: False, deploy_fn=None, stop_fn=lambda _id: None, now=1000.0,
                      notify=lambda t: True)
    assert "aborted" not in out and out["dropped"] == {a.job_id: "red suite"}
    assert store.kv_get(lt.PSEUDO, "base_red_head") is None
    out2 = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False,
                       suite_fn=lambda wt: True, deploy_fn=None, stop_fn=lambda _id: None, now=2000.0,
                       notify=lambda t: True)
    assert "skipped" not in out2 or "base red" not in out2["skipped"]


def test_gate_raising_skips_that_run_not_a_crash(scratch, tmp_path, monkeypatch):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    monkeypatch.setattr(tg, "gate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("worktree add failed")))
    out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False,
                      deploy_fn=None, stop_fn=lambda _id: None, now=1000.0, notify=lambda t: True)
    assert out["skipped"].startswith("gate error") and "worktree add failed" in out["skipped"]
    assert q.get(store, a.job_id).attempts == 0


def test_new_failure_drops_only_the_culprit(scratch, tmp_path):
    store = Store()
    good = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    bad = mkjob(store, scratch, tmp_path, {"BREAKS": "1"})
    q.sync_accepted(store, scratch)

    def gate(wt, base):
        if (wt / "BREAKS").exists():
            return _g(False, new_failures=["t.py::x"], preexisting=["t.py::old"])
        return _g(preexisting=["t.py::old"])
    out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False, gate_fn=gate,
                      deploy_fn=None, stop_fn=lambda _id: None, now=1000.0, notify=lambda t: True)
    assert out["landed"] == [good.job_id] and out["dropped"] == {bad.job_id: "new failures: t.py::x"}


# -- autopilot's merged-tree check ---------------------------------------------------------------------------------


def _ap_repo(tmp_path):
    r = tmp_path / "ap"; r.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=r, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / "a.txt").write_text("a\n"); g("add", "-A"); g("commit", "-qm", "base")
    base = g("rev-parse", "HEAD")
    g("checkout", "-qb", "feat"); (r / "b.txt").write_text("b\n"); g("add", "-A"); g("commit", "-qm", "feat")
    g("checkout", "-q", "main")
    return r, base, g


def _fake_check(log):
    def check(runner, line, cwd, xml, env="", **kw):
        log.append(Path(cwd))
        tests = {"tests.test_s::test_old": "failed", "tests.test_s::test_ok": "passed"}
        if (Path(cwd) / "NEWFAIL").exists():
            tests["tests.test_s::test_new"] = "failed"
        return CheckRun(exit_code=1, tests=tests, used=line)
    return check


def test_autopilot_merged_tree_red_only_on_new_failures(tmp_path, steward_home):
    r, base, g = _ap_repo(tmp_path)
    store, log = Store(), []
    job = SimpleNamespace(repo=str(r), job_id="j1")
    ok, detail, _ = autopilot._test_merged_tree(job, "feat", None, ["pytest -q"], check_fn=_fake_check(log),
                                                base=base, store=store)
    assert ok, detail
    assert len(log) == 2 and log[0] != log[1]          # merged tree, then the base tree (cached afterwards)
    g("checkout", "-q", "feat"); (r / "NEWFAIL").write_text("x"); g("add", "-A"); g("commit", "-qm", "break")
    g("checkout", "-q", "main")
    ok, detail, _ = autopilot._test_merged_tree(job, "feat", None, ["pytest -q"], check_fn=_fake_check(log),
                                                base=base, store=store)
    assert not ok and "red" in detail and "test_new" in detail
    assert len(log) == 3                                # the base came from the cache


def test_autopilot_without_store_keeps_absolute_red(tmp_path, steward_home):
    r, base, g = _ap_repo(tmp_path)
    ok, detail, _ = autopilot._test_merged_tree(SimpleNamespace(repo=str(r), job_id="j1"), "feat", None,
                                                ["pytest -q"], check_fn=_fake_check([]), base=base)
    assert not ok and "red" in detail
