# tests/test_train_build.py
from steward import train_build
from steward.store import Store
from test_incorporate import git, mkjob, scratch  # noqa: F401


def _prep(scratch, tmp_path):
    return train_build.prepare_worktree(scratch, tmp_path / "merge-root", "main")


def test_clean_jobs_merge(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    b = mkjob(store, scratch, tmp_path, {"y.txt": "y"})
    wt = _prep(scratch, tmp_path)
    res = train_build.build_train(scratch, wt, "main", [a.job_id, b.job_id], 1, resolve_fn=lambda *_: False)
    assert res.merged == [a.job_id, b.job_id] and res.dropped == {}
    assert (wt / "x.txt").read_text() == "x" and (wt / "y.txt").read_text() == "y"
    assert res.base_sha == git(scratch, "rev-parse", "main").strip()
    assert git(scratch, "rev-parse", "main").strip() == res.base_sha      # deploy checkout untouched


def test_unresolved_conflict_is_dropped_and_worktree_clean(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"a.txt": "from A"})
    b = mkjob(store, scratch, tmp_path, {"a.txt": "from B"})
    c = mkjob(store, scratch, tmp_path, {"z.txt": "z"})
    wt = _prep(scratch, tmp_path)
    res = train_build.build_train(scratch, wt, "main", [a.job_id, b.job_id, c.job_id], 1, resolve_fn=lambda *_: False)
    assert res.merged == [a.job_id, c.job_id]
    assert res.dropped[b.job_id].startswith("conflict: a.txt")
    assert train_build.conflicted_files(wt) == []


def test_resolver_that_fixes_conflict_keeps_job(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"a.txt": "from A"})
    b = mkjob(store, scratch, tmp_path, {"a.txt": "from B"})
    wt = _prep(scratch, tmp_path)

    def fix(worktree, job_id, files):
        (worktree / "a.txt").write_text("from A\nfrom B\n")
        git(worktree, "add", "a.txt")
        return True

    res = train_build.build_train(scratch, wt, "main", [a.job_id, b.job_id], 1, resolve_fn=fix)
    assert res.merged == [a.job_id, b.job_id]
    assert (wt / "a.txt").read_text() == "from A\nfrom B\n"


def test_resolver_claiming_success_with_markers_is_dropped(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"a.txt": "from A"})
    b = mkjob(store, scratch, tmp_path, {"a.txt": "from B"})
    wt = _prep(scratch, tmp_path)
    res = train_build.build_train(scratch, wt, "main", [a.job_id, b.job_id], 1, resolve_fn=lambda *_: True)
    assert b.job_id in res.dropped
    assert train_build.conflicted_files(wt) == []


def test_missing_branch_is_dropped_not_fatal(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    wt = _prep(scratch, tmp_path)
    res = train_build.build_train(scratch, wt, "main", ["no-such-job", a.job_id], 1, resolve_fn=lambda *_: False)
    assert res.dropped == {"no-such-job": "missing branch"}
    assert res.merged == [a.job_id]


def test_rebuild_reuses_worktree(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    wt = _prep(scratch, tmp_path)
    train_build.build_train(scratch, wt, "main", [a.job_id], 1, resolve_fn=lambda *_: False)
    res = train_build.build_train(scratch, wt, "main", [], 2, resolve_fn=lambda *_: False)
    assert res.head == res.base_sha and not (wt / "x.txt").exists()


def test_select_landable_isolates_single_culprit():
    calls = []

    def green(ids):
        calls.append(list(ids))
        return "bad" not in ids

    results = []

    def rec(ids):
        r = green(ids)
        results.append((list(ids), r))
        return r

    good, bad = train_build.select_landable(["a", "b", "bad", "c"], rec)
    assert good == ["a", "b", "c"] and bad == ["bad"]
    assert [ids for ids, r in results if r][-1] == good


def test_select_landable_all_green_single_call():
    calls = []
    good, bad = train_build.select_landable(["a", "b"], lambda ids: calls.append(ids) or True)
    assert good == ["a", "b"] and bad == [] and len(calls) == 1


def test_dirty_leftovers_do_not_poison_next_job(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"a.txt": "from A"})
    b = mkjob(store, scratch, tmp_path, {"a.txt": "from B"})
    c = mkjob(store, scratch, tmp_path, {"z.txt": "z"})
    wt = _prep(scratch, tmp_path)

    def mess(worktree, job_id, files):
        (worktree / "junk.txt").write_text("junk")
        (worktree / "z.txt").write_text("stale")
        return False

    res = train_build.build_train(scratch, wt, "main", [a.job_id, b.job_id, c.job_id], 1, resolve_fn=mess)
    assert res.merged == [a.job_id, c.job_id] and b.job_id in res.dropped
    assert git(wt, "status", "--porcelain").strip() == ""


def test_dirty_tracked_edit_is_rolled_back(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"a.txt": "from A"})
    b = mkjob(store, scratch, tmp_path, {"a.txt": "from B"})
    c = mkjob(store, scratch, tmp_path, {"z.txt": "z"})
    wt = _prep(scratch, tmp_path)

    def mess(worktree, job_id, files):
        (worktree / "a.txt").write_text("edited")
        (worktree / "junk.txt").write_text("junk")
        return False

    res = train_build.build_train(scratch, wt, "main", [a.job_id, b.job_id, c.job_id], 1, resolve_fn=mess)
    assert res.merged == [a.job_id, c.job_id]
    assert (wt / "a.txt").read_text() == "from A"
    assert git(wt, "status", "--porcelain").strip() == ""


def test_resolver_that_commits_itself_is_dropped(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"a.txt": "from A"})
    b = mkjob(store, scratch, tmp_path, {"a.txt": "from B", "b_only.txt": "b"})
    wt = _prep(scratch, tmp_path)

    def sneaky(worktree, job_id, files):
        (worktree / "a.txt").write_text("merged\n")
        git(worktree, "add", "-A")
        git(worktree, "commit", "-q", "-m", "resolver commit")
        return True

    res = train_build.build_train(scratch, wt, "main", [a.job_id, b.job_id], 1, resolve_fn=sneaky)
    assert b.job_id in res.dropped and res.merged == [a.job_id]
    assert not (wt / "b_only.txt").exists()
    assert (wt / "a.txt").read_text() == "from A"


def test_raising_resolver_is_dropped_and_run_continues(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"a.txt": "from A"})
    b = mkjob(store, scratch, tmp_path, {"a.txt": "from B"})
    c = mkjob(store, scratch, tmp_path, {"z.txt": "z"})
    wt = _prep(scratch, tmp_path)

    def boom(*_):
        raise RuntimeError("boom")

    res = train_build.build_train(scratch, wt, "main", [a.job_id, b.job_id, c.job_id], 1, resolve_fn=boom)
    assert res.merged == [a.job_id, c.job_id] and b.job_id in res.dropped


def test_resolve_timeout_returns_false(tmp_path):
    import subprocess

    from steward import train_resolver

    class R:
        def run(self, argv, cwd, timeout=None):
            raise subprocess.TimeoutExpired(argv, timeout)

    assert train_resolver.resolve(tmp_path, "j", ["f"], goal="g", target="main", runner=R(), timeout_s=1) is False


def test_already_contained_branch_is_dropped_not_merged(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    b = mkjob(store, scratch, tmp_path, {"y.txt": "y"})
    git(scratch, "merge", "--no-ff", "-q", "-m", "already in", f"steward/{a.job_id}")
    wt = _prep(scratch, tmp_path)
    res = train_build.build_train(scratch, wt, "main", [a.job_id, b.job_id], 1, resolve_fn=lambda *_: False)
    assert res.merged == [b.job_id]
    assert res.dropped == {a.job_id: "already contained in target"}


def _rerere_env(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "rerere.enabled")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "rerere.autoupdate")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "true")


def test_rerere_replayed_resolution_lands_on_rebuild(scratch, tmp_path, monkeypatch):
    """C1: with rerere.autoupdate a re-merge exits 1 with MERGE_HEAD set and zero U files; it must land."""
    _rerere_env(monkeypatch)
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"a.txt": "from A"})
    b = mkjob(store, scratch, tmp_path, {"a.txt": "from B"})
    wt = _prep(scratch, tmp_path)
    calls = []

    def fix_once(worktree, job_id, files):
        calls.append(job_id)
        if len(calls) > 1:
            return False
        (worktree / "a.txt").write_text("from A\nfrom B\n")
        git(worktree, "add", "a.txt")
        return True

    first = train_build.build_train(scratch, wt, "main", [a.job_id, b.job_id], 1, resolve_fn=fix_once)
    assert first.merged == [a.job_id, b.job_id]
    again = train_build.build_train(scratch, wt, "main", ["no-such-job", a.job_id, b.job_id], 2, resolve_fn=fix_once)
    assert again.merged == [a.job_id, b.job_id], again.dropped
    assert calls == [b.job_id]                                 # rerere did the second resolution, not the resolver
    assert (wt / "a.txt").read_text() == "from A\nfrom B\n"
    assert git(wt, "status", "--porcelain").strip() == ""


def test_rerere_leftover_markers_are_dropped(scratch, tmp_path, monkeypatch):
    """A staged-but-marker-bearing autoupdate result is never committed."""
    _rerere_env(monkeypatch)
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"a.txt": "from A"})
    b = mkjob(store, scratch, tmp_path, {"a.txt": "from B"})
    wt = _prep(scratch, tmp_path)

    def fix(worktree, job_id, files):
        (worktree / "a.txt").write_text("<<<<<<< ours\nx\n>>>>>>> theirs\n")
        git(worktree, "add", "a.txt")
        return True

    # a resolution recorded with markers in it: the train must refuse it on the replay path too
    res = train_build.build_train(scratch, wt, "main", [a.job_id, b.job_id], 1, resolve_fn=fix)
    assert b.job_id in res.dropped
    res = train_build.build_train(scratch, wt, "main", [a.job_id, b.job_id], 2, resolve_fn=lambda *_: False)
    assert b.job_id in res.dropped and git(wt, "status", "--porcelain").strip() == ""


def test_build_never_creates_train_branches(scratch, tmp_path):
    """m4: the train is built on a detached HEAD; `branch` is only a label."""
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    wt = _prep(scratch, tmp_path)
    res = train_build.build_train(scratch, wt, "main", [a.job_id], 3, resolve_fn=lambda *_: False)
    assert res.branch == "train/3"
    assert git(scratch, "for-each-ref", "refs/heads/train/") == ""
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
