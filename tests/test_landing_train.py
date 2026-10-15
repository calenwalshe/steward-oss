import os

from steward import landing_queue as q
from steward import landing_train as lt
from steward import train_land
from steward.models import JobState
from steward.store import Store
from test_incorporate import git, mkjob, scratch  # noqa: F401


def _settings(scratch, tmp_path, **kw):
    s = {"enabled": True, "repo": str(scratch), "target": "main", "every_s": 180.0, "max_batch": 8,
         "resolver_timeout_s": 5.0, "suite_cmd": "true", "worktree_root": str(tmp_path / "mroot"), "report_hour": 7}
    s.update(kw)
    return s


def test_tick_off_does_nothing(scratch, tmp_path):
    store = Store()
    assert lt.tick(store, None, now=1000.0, settings=_settings(scratch, tmp_path, enabled=False)) == {"skipped": "off"}


def test_tick_throttles_and_spawns_once(scratch, tmp_path):
    store = Store()
    mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    spawned = []
    s = _settings(scratch, tmp_path)
    first = lt.tick(store, None, now=1000.0, settings=s, spawn=lambda *_: spawned.append(1) or os.getpid())
    assert first["spawned"] == os.getpid()
    assert lt.tick(store, None, now=1010.0, settings=s, spawn=lambda *_: 1)["skipped"] == "throttle"
    assert lt.tick(store, None, now=1300.0, settings=s, spawn=lambda *_: 1)["skipped"] == "running"  # our pid is alive
    assert len(spawned) == 1


def test_dead_pid_does_not_block(scratch, tmp_path):
    store = Store()
    mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    store.kv_set(lt.PSEUDO, "run", {"pid": 2 ** 22 + 12345, "since": 0})
    out = lt.tick(store, None, now=5000.0, settings=_settings(scratch, tmp_path), spawn=lambda *_: 777)
    assert out == {"spawned": 777}


def test_run_once_lands_green_and_drops_culprit(scratch, tmp_path):
    store = Store()
    good = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    bad = mkjob(store, scratch, tmp_path, {"BREAKS": "1"})
    q.sync_accepted(store, scratch)

    def suite(wt):
        return not (wt / "BREAKS").exists()

    out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False, suite_fn=suite,
                      deploy_fn=None, stop_fn=lambda _id: None, now=1000.0, notify=lambda t: True)
    assert out["landed"] == [good.job_id] and out["dropped"] == {bad.job_id: "red suite"}
    assert store.get_job(good.job_id).state == JobState.RELEASED
    assert q.get(store, bad.job_id).attempts == 1 and q.get(store, bad.job_id).status == "queued"


def test_red_base_aborts_without_dropping(scratch, tmp_path):
    # zero-touch spec §5.5: the base-red freeze is removed; a red base is judged per test id by the relative gate
    # (tests/test_train_relative_gate.py). The absolute suite_fn seam is red for the candidate, so it is dropped.
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False,
                      suite_fn=lambda wt: False, deploy_fn=None, stop_fn=lambda _id: None, now=1000.0,
                      notify=lambda t: True)
    assert "aborted" not in out and store.kv_get(lt.PSEUDO, "base_red_head") is None
    assert q.get(store, a.job_id).attempts == 1


def test_run_once_resumes_pending_first(scratch, tmp_path, monkeypatch):
    store = Store()
    mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    order = []
    monkeypatch.setattr(train_land, "resume_pending", lambda *a, **k: order.append("resume") or None)
    real_land = train_land.land
    monkeypatch.setattr(train_land, "land", lambda *a, **k: order.append("land") or real_land(*a, **k))
    lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False, suite_fn=lambda wt: True,
                deploy_fn=None, stop_fn=lambda _id: None, now=1000.0, notify=lambda t: True)
    assert order == ["resume", "land"]


def test_notice_sent_once_per_head(scratch, tmp_path):
    store = Store()
    sent = []
    n = lt._once_per_head(store, lambda x: sent.append(x) or True)
    n("boom", "h1")
    n("boom", "h1")
    n("boom", "h2")
    n("different text", "h1")
    assert sent == ["boom", "boom"]


def test_resolver_budget_skips_runner(scratch, tmp_path):
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"README.md": "conflict A"})
    b = mkjob(store, scratch, tmp_path, {"README.md": "conflict B"})
    q.sync_accepted(store, scratch)

    from steward.runner_exec import SubprocessRunner

    class Boom(SubprocessRunner):
        def run(self, argv, *a, **k):
            assert "claude" not in argv, "resolver must not be invoked"
            return super().run(argv, *a, **k)

    out = lt.run_once(store, Boom(), _settings(scratch, tmp_path, resolver_timeout_s=1.0),
                      suite_fn=lambda wt: True, deploy_fn=None, stop_fn=lambda _id: None, now=1000.0,
                      notify=lambda t: True)
    assert out["landed"]
    assert {a.job_id, b.job_id} - set(out["landed"]) <= set(out["dropped"])


def _intent(store, scratch, **kw):
    head = git(scratch, "rev-parse", "HEAD").strip()
    rec = {"repo": str(scratch), "target": "main", "train_head": head, "base": head, "job_ids": []}
    rec.update(kw)
    store.kv_set(*train_land.KEY, rec)
    return head


def test_unresolved_intent_aborts_early(scratch, tmp_path, monkeypatch):
    store = Store()
    mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    head = _intent(store, scratch)
    monkeypatch.setattr(train_land, "resume_pending", lambda *a, **k: None)   # leaves the intent in place
    built = []
    monkeypatch.setattr(lt.train_build, "build_train", lambda *a, **k: built.append(1))
    out = lt.run_once(store, None, _settings(scratch, tmp_path), suite_fn=lambda wt: True, deploy_fn=None,
                      stop_fn=lambda _id: None, now=1000.0, notify=lambda t: True)
    assert out["aborted"] == "pending landing unresolved" and not built
    rep = list((store.home / "train-report").glob("*.md"))[0].read_text()
    assert f"pending landing unresolved: {head[:7]}" in rep


def test_resume_notice_keyed_by_intent_head(scratch, tmp_path, monkeypatch):
    store = Store()
    head = _intent(store, scratch)
    seen = []

    def fake_resume(store_, *, deploy_fn, notify_fn, **_kw):
        notify_fn("a")
        return None

    monkeypatch.setattr(train_land, "resume_pending", fake_resume)
    lt.run_once(store, None, _settings(scratch, tmp_path), suite_fn=lambda wt: True, deploy_fn=None,
                stop_fn=lambda _id: None, now=1000.0, notify=lambda x: seen.append(x) or True)
    assert [x for x in seen if x == "a"] == ["a"] and store.kv_get(lt.PSEUDO, "notified") == [head]


def test_stuck_unreverted_landing_resumed_once(scratch, tmp_path, monkeypatch):
    store = Store()
    _intent(store, scratch)
    calls = []
    monkeypatch.setattr(train_land, "resume_pending", lambda *a, **k: calls.append(1) or None)
    for _ in range(3):
        out = lt.run_once(store, None, _settings(scratch, tmp_path), suite_fn=lambda wt: True, deploy_fn=None,
                          stop_fn=lambda _id: None, now=1000.0, notify=lambda t: True)
        assert out["aborted"] == "pending landing unresolved"
    assert len(calls) == 1


def test_no_job_both_landed_and_dropped(scratch, tmp_path):
    store = Store()
    mkjob(store, scratch, tmp_path, {"README.md": "A\n"})
    mkjob(store, scratch, tmp_path, {"README.md": "B\n"})
    c = mkjob(store, scratch, tmp_path, {"c.txt": "c"})
    q.sync_accepted(store, scratch)
    seen = set()

    def flaky(wt, jid, files):                   # resolves for real the first time per job, never again
        if jid in seen:
            return False
        seen.add(jid)
        for f in files:
            (wt / f).write_text("resolved\n")
            git(wt, "add", f)
        return True

    out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=flaky, suite_fn=lambda wt: True,
                      deploy_fn=None, stop_fn=lambda _id: None, now=1000.0, notify=lambda t: True)
    assert seen, "the conflict path was never exercised"
    assert not set(out["landed"]) & set(out["dropped"])
    assert c.job_id in out["landed"]


def test_rerere_env_only_and_restored(scratch, tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.startswith("GIT_CONFIG_")]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "x")
    store = Store()
    mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    seen = {}
    lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False,
                suite_fn=lambda wt: seen.update(os.environ) or True, deploy_fn=None,
                stop_fn=lambda _id: None, now=1000.0, notify=lambda t: True)
    assert seen["GIT_CONFIG_COUNT"] == "3" and seen["GIT_CONFIG_KEY_1"] == "rerere.enabled"
    import subprocess
    assert subprocess.run(["git", "config", "--get", "rerere.enabled"], cwd=scratch).returncode == 1
    assert os.environ["GIT_CONFIG_COUNT"] == "1" and "GIT_CONFIG_KEY_1" not in os.environ


def test_failed_send_is_retried(scratch, tmp_path):
    store = Store()
    n = lt._once_per_head(store, lambda x: False)
    n("boom", "h1")
    assert not store.kv_get(lt.PSEUDO, "notified")


def test_all_green_batch_builds_once(scratch, tmp_path, monkeypatch):
    store = Store()
    mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    mkjob(store, scratch, tmp_path, {"y.txt": "y"})
    q.sync_accepted(store, scratch)
    real = lt.train_build.build_train
    calls = []
    monkeypatch.setattr(lt.train_build, "build_train", lambda *a, **k: calls.append(1) or real(*a, **k))
    out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False,
                      suite_fn=lambda wt: True, deploy_fn=None, stop_fn=lambda _id: None, now=1000.0,
                      notify=lambda t: True)
    assert len(out["landed"]) == 2 and len(calls) == 1


def test_tick_lock_busy_does_not_raise(scratch, tmp_path, monkeypatch):
    store = Store()
    mkjob(store, scratch, tmp_path, {"x.txt": "x"})

    def busy(*a, **k):
        raise lt.inc.LockBusy({"purpose": "x"})

    monkeypatch.setattr(lt.inc, "target_lock", busy)
    out = lt.tick(store, None, now=1000.0, settings=_settings(scratch, tmp_path), spawn=lambda *_: 4242)
    assert out == {"spawned": 4242}


def test_pid_with_different_start_time_is_dead(scratch, tmp_path):
    store = Store()
    store.kv_set(lt.PSEUDO, "run", {"pid": os.getpid(), "start": "1"})
    assert not lt.run_alive(store)
    store.kv_set(lt.PSEUDO, "run", {"pid": os.getpid(), "start": lt._start_time(os.getpid())})
    assert lt.run_alive(store)


def test_tick_never_sends_report(scratch, tmp_path, monkeypatch):
    store = Store()
    mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    monkeypatch.setattr(lt, "_tg", lambda text: (_ for _ in ()).throw(AssertionError("tick must not send")))
    called = []
    monkeypatch.setattr(lt.train_report, "maybe_send_daily", lambda *a, **k: called.append(1))
    lt.tick(store, None, now=1000.0, settings=_settings(scratch, tmp_path), spawn=lambda *_: 4242)
    assert not called


def test_base_red_waits_for_target_to_change(scratch, tmp_path):
    """C3 superseded by zero-touch spec §5.5 (base-red freeze removed): a red run never freezes the train; the next
    run at the SAME target head runs its suites again and lands."""
    store = Store()
    mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    s = _settings(scratch, tmp_path)
    kw = dict(resolve_fn=lambda *_: False, deploy_fn=None, stop_fn=lambda _id: None, now=1000.0,
              notify=lambda t: True)
    assert "aborted" not in lt.run_once(store, None, s, suite_fn=lambda wt: False, **kw)
    assert store.kv_get(lt.PSEUDO, "base_red_head") is None
    runs = []
    out = lt.run_once(store, None, s, suite_fn=lambda wt: runs.append(1) or True, **kw)
    assert runs and len(out["landed"]) == 1


def test_drop_reason_names_removed_tests(scratch, tmp_path):
    """m2: a gate drop says why: tests removed (with kinds) vs a red suite."""
    (scratch / "tests").mkdir()
    (scratch / "tests" / "test_x.py").write_text("def test_one():\n    assert 1\n\n\ndef test_two():\n    assert 2\n")
    git(scratch, "add", "-A")
    git(scratch, "commit", "-qm", "tests")
    store = Store()
    ok = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    weak = mkjob(store, scratch, tmp_path, {"tests/test_x.py": "def test_one():\n    assert 1\n"})
    q.sync_accepted(store, scratch)
    out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False,
                      suite_fn=lambda wt: True, deploy_fn=None, stop_fn=lambda _id: None, now=1000.0,
                      notify=lambda t: True)
    assert out["landed"] == [ok.job_id]
    assert out["dropped"][weak.job_id].startswith("tests removed: ")
    assert "removed_test_function" in out["dropped"][weak.job_id]
    assert q.get(store, weak.job_id).last_reason == out["dropped"][weak.job_id]


def test_preflight_not_possible_waits_before_suites(scratch, tmp_path):
    """C2a: health red / deploy impossible -> the run skips: no suites, no drops, reported once."""
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    runs = []
    for _ in range(2):
        out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False,
                          suite_fn=lambda wt: runs.append(1) or True, deploy_fn=lambda: {"ok": True},
                          preflight_fn=lambda: "health RED: mcp", stop_fn=lambda _id: None, now=1000.0,
                          notify=lambda t: True)
        assert out == {"skipped": "deploy not possible: health RED: mcp"}
    assert not runs and q.get(store, a.job_id).attempts == 0
    rep = list((store.home / "train-report").glob("*.md"))[0].read_text()
    assert rep.count("deploy not possible") == 1


def test_deferred_deploy_is_retried_without_using_the_resume_cap(scratch, tmp_path):
    """C2b: a refused deploy after the ff is retried every run (never 'unresolved'); it lands once allowed."""
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    refused = {"ok": False, "refused": True, "summary": "deploy-self refused: answer in flight"}
    kw = dict(resolve_fn=lambda *_: False, suite_fn=lambda wt: True, stop_fn=lambda _id: None, now=1000.0,
              notify=lambda t: True)
    out = lt.run_once(store, None, _settings(scratch, tmp_path), deploy_fn=lambda: refused, **kw)
    assert out["land"]["ok"] is False and not out["land"]["reverted"]
    for _ in range(3):
        out = lt.run_once(store, None, _settings(scratch, tmp_path), deploy_fn=lambda: refused, **kw)
        assert "deferred" in out, out
    out = lt.run_once(store, None, _settings(scratch, tmp_path), deploy_fn=lambda: {"ok": True}, **kw)
    assert store.get_job(a.job_id).state == JobState.RELEASED and q.get(store, a.job_id).status == "landed"


def test_run_once_rechecks_queue_before_building(scratch, tmp_path):
    """I2a: an entry whose job left ACCEPTED (or got incorporated another way) is not built."""
    store = Store()
    a = mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    b = mkjob(store, scratch, tmp_path, {"y.txt": "y"})
    q.sync_accepted(store, scratch)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.NEEDS_HUMAN.value, a.job_id))
    out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False,
                      suite_fn=lambda wt: True, deploy_fn=None, stop_fn=lambda _id: None, now=1000.0,
                      notify=lambda t: True)
    assert out["landed"] == [b.job_id] and a.job_id not in out["dropped"]
    assert q.get(store, a.job_id).status == "removed"


def test_parent_rides_its_queued_merge_fixup(scratch, tmp_path):
    """I2b/c: a parent with a queued ACCEPTED merge fix-up is left out; the fix-up carries it, and the parent's
    queue row is marked landed with it."""
    store = Store()
    parent = mkjob(store, scratch, tmp_path, {"p.txt": "p"})
    child = mkjob(store, scratch, tmp_path, {"c.txt": "c"}, goal="Merge `main` into the parent branch")
    git(tmp_path / f"wt-{child.job_id[:6]}", "merge", "-q", "--no-ff", "-m", "carry parent", f"steward/{parent.job_id}")
    store.kv_set(child.job_id, "autopilot_followup", parent.job_id)
    q.sync_accepted(store, scratch)
    built = []
    real = lt.train_build.build_train
    lt_build = lambda *a, **k: built.append(list(a[3])) or real(*a, **k)  # noqa: E731
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(lt.train_build, "build_train", lt_build)
    try:
        out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False,
                          suite_fn=lambda wt: True, deploy_fn=None, stop_fn=lambda _id: None, now=1000.0,
                          notify=lambda t: True)
    finally:
        mp.undo()
    assert built[0] == [child.job_id] and out["landed"] == [child.job_id]
    assert (scratch / "p.txt").exists()
    pe = q.get(store, parent.job_id)
    assert pe.status == "landed" and pe.landed_commit == git(scratch, "rev-parse", "main")
    assert store.kv_get(parent.job_id, "incorporated_commit")


def test_tick_spawns_report_when_queue_empty_and_due(scratch, tmp_path):
    """I3: no queue, report due and unsent -> tick spawns `report`; nothing when not due or already sent."""
    from datetime import datetime
    store = Store()
    s = _settings(scratch, tmp_path)
    seven = datetime(2026, 10, 8, 7, 5).timestamp()
    cmds = []
    out = lt.tick(store, None, now=seven - 3600, settings=s, spawn=lambda cmd: cmds.append(cmd) or 999999999)
    assert out == {"skipped": "empty"} and not cmds
    out = lt.tick(store, None, now=seven, settings=s, spawn=lambda cmd: cmds.append(cmd) or 999999999)
    assert cmds == ["report"] and out == {"spawned_report": 999999999}
    store.kv_set(lt.PSEUDO, "report_sent", "2026-10-08")
    store.kv_set(lt.PSEUDO, "run", {})
    out = lt.tick(store, None, now=seven + 600, settings=s, spawn=lambda cmd: cmds.append(cmd) or 1)
    assert out == {"skipped": "empty"} and cmds == ["report"]


def test_main_report_sends_daily(scratch, tmp_path, monkeypatch):
    store = Store()
    monkeypatch.setattr(lt, "Store", lambda: store)
    monkeypatch.setattr(lt.config, "train_settings", lambda: _settings(scratch, tmp_path, report_hour=0))
    sent = []
    monkeypatch.setattr(lt, "_tg", lambda text: sent.append(text) or True)
    assert lt.main(["report"]) == 0
    assert sent and store.kv_get(lt.PSEUDO, "report_sent") and store.kv_get(lt.PSEUDO, "run") == {}


def test_main_run_crash_is_reported_and_notified_once(scratch, tmp_path, monkeypatch):
    """I4: an uncaught exception in a run is written to the report, notified once per day per type, run cleared."""
    store = Store()
    monkeypatch.setattr(lt, "Store", lambda: store)
    monkeypatch.setattr(lt.config, "train_settings", lambda: _settings(scratch, tmp_path))
    sent = []
    monkeypatch.setattr(lt, "_tg", lambda text: sent.append(text) or True)

    def boom(*a, **k):
        raise ValueError("kaboom")

    monkeypatch.setattr(lt, "run_once", boom)
    assert lt.main(["run"]) == 1
    assert lt.main(["run"]) == 1
    rep = "".join(p.read_text() for p in (store.home / "train-report").glob("*.md"))
    assert rep.count("train run crashed: ValueError: kaboom") == 2
    assert len(sent) == 1 and "ValueError" in sent[0]
    assert store.kv_get(lt.PSEUDO, "run") == {}


def test_zombie_pid_is_dead():
    """I5: a zombie (Z) or dead (X) process is not a live run."""
    stat = lambda st: (lambda pid: f"{pid} (python3) {st} 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 777 19")  # noqa: E731
    assert lt._pid_alive(os.getpid(), "777", read_stat=stat("S")) is True
    assert lt._pid_alive(os.getpid(), "777", read_stat=stat("Z")) is False
    assert lt._pid_alive(os.getpid(), "777", read_stat=stat("X")) is False
    assert lt._pid_alive(os.getpid(), "", read_stat=stat("Z")) is False
    assert lt._pid_alive(os.getpid(), "778", read_stat=stat("R")) is False


def test_default_stop_relays_to_control_pane_lazily(scratch, tmp_path):
    """I6: fix-ups are stopped through the control pane relay (single writer), built only when needed."""
    store = Store()
    built, relayed = [], []

    def factory():
        built.append(1)
        return lambda op, args: relayed.append((op, args)) or {"accepted": True}

    stop = lt._default_stop(store, relay_factory=factory)
    assert not built
    stop("job-1")
    stop("job-2")
    assert built == [1] and relayed == [("stop", {"job_id": "job-1"}), ("stop", {"job_id": "job-2"})]
    out = lt.run_once(store, None, _settings(scratch, tmp_path), deploy_fn=None, now=1000.0, notify=lambda t: True)
    assert out == {"skipped": "nothing to land after the queue re-check"}


def test_base_red_wait_notifies_once_per_target_head(scratch, tmp_path):
    """N2 under zero-touch spec §5.5: a red base (pre-existing failures) is one tg notice per target head, no pause."""
    store = Store()
    mkjob(store, scratch, tmp_path, {"x.txt": "x"})
    q.sync_accepted(store, scratch)
    sent = []
    red = {"ok": False, "why": "new failures: t.py::x", "new_failures": ["t.py::x"], "preexisting": ["t.py::old"]}
    kw = dict(resolve_fn=lambda *_: False, gate_fn=lambda wt, base: red, deploy_fn=None, stop_fn=lambda _id: None,
              now=1000.0, notify=lambda t: sent.append(t) or True)
    sha = git(scratch, "rev-parse", "main")
    for _ in range(3):
        lt.run_once(store, None, _settings(scratch, tmp_path), **kw)
    notices = [t for t in sent if "pre-existing" in t]
    assert len(notices) == 1 and notices[0].startswith(f"main {sha[:7]} has 1 pre-existing failing test(s)")
    assert not [t for t in sent if "paused" in t]
