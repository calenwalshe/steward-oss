"""Task 15 fix round 1 (spec §5.5 "a red base is a report line and one notice per base head ... and nothing more"):
an unusable base (no per-test results) never freezes the train. The gate judges the candidate absolutely (no
pre-existing credit, rerun and tests_removed still apply), a green candidate lands, a red one is dropped/parked through
the normal path, and the base gets one notice per head. A gate that itself raises skips only that run."""

import subprocess
from pathlib import Path

import pytest

from steward import landing_queue as q
from steward import landing_train as lt
from steward import train_gates as tg
from steward.models import JobState
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from test_incorporate import mkjob, scratch  # noqa: F401 - scratch is a fixture

SUITE = "python3 -m pytest -q -p no:cacheprovider"


@pytest.fixture(autouse=True)
def _hermetic_inner_pytest(monkeypatch):
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT",
                 "PYTEST_XDIST_TESTRUNUID", "PYTEST_CURRENT_TEST", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        monkeypatch.delenv(name, raising=False)


def _settings(scratch, tmp_path):
    return {"enabled": True, "repo": str(scratch), "target": "main", "every_s": 180.0, "max_batch": 8,
            "resolver_timeout_s": 5.0, "suite_cmd": "true", "worktree_root": str(tmp_path / "mroot"), "report_hour": 7}


def _report(store):
    return "".join(p.read_text() for p in sorted((Path(store.home) / "train-report").glob("*.md")))


def _run(store, scratch, tmp_path, now, sent):
    return lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False, deploy_fn=None,
                       stop_fn=lambda _id: None, now=now, notify=lambda t: sent.append(t) or True)


def _unusable_base(monkeypatch, cand: set[str]):
    """Default gate path, with an unusable base suite and a fixed candidate result (rerun included)."""
    base_runs, cand_runs = [], []
    monkeypatch.setattr(tg, "base_tree_suite",
                        lambda repo, sha, tmp, run: (lambda _wt: base_runs.append(sha) or {tg.SUITE_ID}))
    monkeypatch.setattr(tg, "_suite_failures",
                        lambda wt, cmd, tmp_root, runner, ids=None, **k: cand_runs.append(ids) or set(cand))
    monkeypatch.setattr(tg, "tests_removed", lambda *a: [])
    return base_runs, cand_runs


# -- (a) unusable base + red candidate: judged, dropped then parked; one notice per head -------------------------


def test_unusable_base_red_candidate_is_judged_and_parked_never_skipped(scratch, tmp_path, monkeypatch):
    store, sent = Store(), []
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    base_runs, cand_runs = _unusable_base(monkeypatch, {"t.py::x"})
    for i in range(q.MAX_DROPS):
        out = _run(store, scratch, tmp_path, 1000.0 + i, sent)
        assert "skipped" not in out and out["landed"] == []
        assert out["dropped"] == {a.job_id: "new failures: t.py::x"}
    e = q.get(store, a.job_id)
    assert e.status == "parked" and e.attempts == q.MAX_DROPS and e.last_target
    assert len(base_runs) == 1                                   # the unusable base is cached (30 min), not re-run
    assert [ids for ids in cand_runs if ids] == [["t.py::x"]] * q.MAX_DROPS   # the rerun-once still applies
    assert len([t for t in sent if "base suite unusable" in t]) == 1           # one notice per base head
    rep = _report(store)
    assert "unusable (no per-test results): judged absolutely" in rep and "new_failures=['t.py::x']" in rep
    assert store.kv_get(lt.PSEUDO, "base_red_head") is None


def test_unusable_base_red_candidate_suite_error_is_new(scratch, tmp_path, monkeypatch):
    store, sent = Store(), []
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    _unusable_base(monkeypatch, {tg.SUITE_ID})
    out = _run(store, scratch, tmp_path, 1000.0, sent)
    assert "skipped" not in out and out["dropped"][a.job_id].startswith("red suite")


# -- (b) unusable base + green candidate: lands (that is how main gets fixed) --------------------------------------


def test_unusable_base_green_candidate_lands(scratch, tmp_path, monkeypatch):
    store, sent = Store(), []
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    _unusable_base(monkeypatch, set())
    out = _run(store, scratch, tmp_path, 1000.0, sent)
    assert out["landed"] == [a.job_id] and store.get_job(a.job_id).state == JobState.RELEASED
    assert len([t for t in sent if "base suite unusable" in t]) == 1
    assert "judged absolutely" in _report(store)


# -- (c) a raising gate skips that run only; the next run at the same head gates again -----------------------------


def test_raising_gate_skips_one_run_and_persists_no_freeze(scratch, tmp_path, monkeypatch):
    store, sent = Store(), []
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    base_calls = []

    def base_tree_suite(repo, sha, tmp, run):
        def suite(_wt):
            base_calls.append(sha)
            if len(base_calls) == 1:
                raise RuntimeError("worktree add failed")
            return set()
        return suite
    monkeypatch.setattr(tg, "base_tree_suite", base_tree_suite)
    monkeypatch.setattr(tg, "_suite_failures", lambda *a, **k: set())
    monkeypatch.setattr(tg, "tests_removed", lambda *a: [])
    out = _run(store, scratch, tmp_path, 1000.0, sent)
    assert out["skipped"].startswith("gate error") and "worktree add failed" in out["skipped"]
    assert q.get(store, a.job_id).status == "queued" and q.get(store, a.job_id).attempts == 0
    assert store.kv_get(lt.PSEUDO, "base_red_head") is None
    assert not [k for k in _kv_keys(store) if k.startswith("base_suite:")]   # a raise caches no base verdict
    out = _run(store, scratch, tmp_path, 1001.0, sent)                         # same head, seconds later
    assert len(base_calls) == 2 and base_calls[0] == base_calls[1]           # the gate ran again
    assert out["landed"] == [a.job_id]


def _kv_keys(store) -> list[str]:
    return [str(r[0]) for r in store._conn.execute("SELECT key FROM kv").fetchall()]


# -- the gate itself, on a real pytest suite -----------------------------------------------------------------------


def test_gate_on_a_collection_broken_base_is_absolute(tmp_path, steward_home):
    r = tmp_path / "r"; r.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=r, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / "pytest.ini").write_text("[pytest]\n")
    (r / "test_s.py").write_text("import missing_mod_xyz\ndef test_ok(): assert True\n")
    g("add", "-A"); g("commit", "-qm", "base")
    base, store = g("rev-parse", "HEAD"), Store()
    kw = dict(store=store, repo=r, suite_cmd=SUITE, tmp_root=tmp_path / "t", env_digest="e")
    (r / "missing_mod_xyz.py").write_text("")                   # the candidate fixes main
    out = tg.gate(r, base, SubprocessRunner(), now=1.0, **kw)
    assert out["ok"] and out["base_unusable"] and out["new_failures"] == [] and out["preexisting"] == []
    (r / "test_s.py").write_text("import missing_mod_xyz\ndef test_ok(): assert True\ndef test_bad(): assert False\n")
    out = tg.gate(r, base, SubprocessRunner(), now=2.0, **kw)
    assert not out["ok"] and out["base_unusable"] and out["new_failures"] == ["test_s.py::test_bad"]
