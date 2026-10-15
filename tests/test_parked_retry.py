# tests/test_parked_retry.py
"""Task 14 (spec 2026-10-08 §5.4 "Parked entries"): a parked landing-train entry retries when the target moves
(attempts kept); after the second park the train spawns exactly one rebase fix-up (repair depth capped at 1); a
fix-up that ends without carrying the parent is the ladder's ask. No real workers, no live DB."""
from __future__ import annotations

from steward import autopilot, incorporate, liveness
from steward import landing_queue as q
from steward import landing_train as lt
from steward import repair_episode as rep
from steward.models import JobState
from steward.store import Store
from test_incorporate import git, mkjob, scratch  # noqa: F401 - scratch is a fixture
from test_repair_episode import OK, Attempts, conflicted_root, set_state

S = {"idle_s": 600.0}
DEAD = lambda _jid: False  # noqa: E731


def test_parked_requeues_when_target_moves(steward_home, tmp_path):
    store = Store(); j = store.create_job("g", str(tmp_path))
    q.enqueue(store, j.job_id, tmp_path)
    for _ in range(q.MAX_DROPS):
        q.mark_dropped(store, j.job_id, "conflict: x", target_head="aaa")
    assert q.get(store, j.job_id).status == "parked" and q.get(store, j.job_id).last_target == "aaa"
    assert q.requeue_parked(store, tmp_path, "aaa") == []
    assert q.requeue_parked(store, tmp_path, "bbb") == [j.job_id]
    assert q.get(store, j.job_id).status == "queued" and q.get(store, j.job_id).attempts == q.MAX_DROPS


def test_second_park_spawns_one_fixup_then_asks(steward_home, tmp_path):
    store = Store(); j = store.create_job("g", str(tmp_path)); spawned = []
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.ACCEPTED.value, j.job_id))
    q.enqueue(store, j.job_id, tmp_path)
    q.park(store, j.job_id, "conflict", target_head="aaa"); q.set_parks(store, j.job_id, 2)
    assert lt.maybe_rebase_fixup(store, j.job_id, spawn=lambda jid: spawned.append(jid) or "fixup-1") == "fixup-1"
    assert lt.maybe_rebase_fixup(store, j.job_id, spawn=lambda jid: spawned.append(jid) or "fixup-2") is None
    assert spawned == [j.job_id] and q.get(store, j.job_id).fixup == "fixup-1"


def test_parks_count_entries_into_parked_only(steward_home, tmp_path):
    store = Store(); j = store.create_job("g", str(tmp_path))
    q.enqueue(store, j.job_id, tmp_path)
    q.mark_dropped(store, j.job_id, "red", target_head="a1")
    assert q.get(store, j.job_id).parks == 0
    for _ in range(2):
        q.mark_dropped(store, j.job_id, "red", target_head="a1")
    assert q.get(store, j.job_id).parks == 1
    q.park(store, j.job_id, "again while parked", target_head="a1")       # already parked: not a new park
    assert q.get(store, j.job_id).parks == 1
    assert q.requeue_parked(store, tmp_path, "b2") == [j.job_id]
    e = q.mark_dropped(store, j.job_id, "red", target_head="b2")          # attempts kept: one drop parks again
    assert e.status == "parked" and e.parks == 2 and e.last_target == "b2" and e.attempts == q.MAX_DROPS + 1


def test_entry_with_a_fixup_is_not_requeued_and_park_keeps_target_default(steward_home, tmp_path):
    store = Store(); j = store.create_job("g", str(tmp_path))
    q.enqueue(store, j.job_id, tmp_path)
    q.park(store, j.job_id, "conflict", target_head="aaa")
    q.set_fixup(store, j.job_id, "child")
    assert q.requeue_parked(store, tmp_path, "bbb") == []
    assert q.get(store, j.job_id).status == "parked"


def test_re_accepted_entry_starts_fresh(steward_home, tmp_path):
    store = Store(); j = store.create_job("g", str(tmp_path))
    q.enqueue(store, j.job_id, tmp_path)
    q.park(store, j.job_id, "conflict", target_head="aaa"); q.set_parks(store, j.job_id, 2)
    q.set_fixup(store, j.job_id, "child")
    q.remove(store, j.job_id, "job is running")
    assert q.enqueue(store, j.job_id, tmp_path)
    e = q.get(store, j.job_id)
    assert (e.status, e.attempts, e.parks, e.fixup, e.last_target) == ("queued", 0, 0, "", "")


def _accepted(store, tmp_path, goal="g"):
    j = store.create_job(goal, str(tmp_path))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.ACCEPTED.value, j.job_id))
    return store.get_job(j.job_id)


def test_actor_is_fixup_while_the_rebase_fixup_is_live(steward_home, tmp_path):
    store = Store(); parent = _accepted(store, tmp_path); child = store.create_job("Merge `main`", str(tmp_path))
    q.enqueue(store, parent.job_id, tmp_path)
    q.park(store, parent.job_id, "conflict", target_head="aaa")
    assert liveness.actor(store, parent, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None
    q.set_fixup(store, parent.job_id, child.job_id)
    for st in (JobState.RUNNING, JobState.NEEDS_HUMAN, JobState.ACCEPTED):
        set_state(store, child.job_id, st)
        assert liveness.actor(store, parent, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) == "fixup"
    set_state(store, child.job_id, JobState.FAILED)
    assert liveness.actor(store, parent, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None
    q.set_fixup(store, parent.job_id, "spawning")                          # a marker, not a job: no actor
    assert liveness.actor(store, parent, now=1.0, settings=S, runner_alive=DEAD, check_runs_live=DEAD) is None


class _Asks:
    def __init__(self, result="blk-1"):
        self.calls, self.result = [], result

    def __call__(self, store, job, *, tried, now, **_kw):
        self.calls.append((job.job_id, list(tried)))
        return self.result


def test_parked_pass_requeues_spawns_and_asks_once(steward_home, tmp_path):
    store = Store(); spawned = []; asks = _Asks()
    moved, stuck, gone = _accepted(store, tmp_path), _accepted(store, tmp_path), _accepted(store, tmp_path)
    for j in (moved, stuck, gone):
        q.enqueue(store, j.job_id, tmp_path)
        q.park(store, j.job_id, "conflict: a.txt", target_head="aaa")
    q.set_parks(store, stuck.job_id, 2)
    set_state(store, gone.job_id, JobState.RUNNING)                       # left ACCEPTED: never requeued or fixed
    child = store.create_job("Merge `main` into YOUR branch", str(tmp_path))
    set_state(store, child.job_id, JobState.RUNNING)

    def spawn(jid):
        spawned.append(jid)
        return child.job_id

    out = lt._parked_pass(store, tmp_path, "bbb", spawn=spawn, ask_fn=asks, now=10.0)
    assert out["requeued"] == [moved.job_id] and out["fixups"] == {stuck.job_id: child.job_id}
    assert out["removed"] == [gone.job_id] and not asks.calls
    assert q.get(store, moved.job_id).status == "queued" and q.get(store, stuck.job_id).status == "parked"
    assert q.get(store, gone.job_id).status == "removed"
    lt._parked_pass(store, tmp_path, "ccc", spawn=spawn, ask_fn=asks, now=20.0)
    assert spawned == [stuck.job_id] and not asks.calls                   # the live fix-up is the actor: wait
    set_state(store, child.job_id, JobState.FAILED)
    out = lt._parked_pass(store, tmp_path, "ccc", spawn=spawn, ask_fn=asks, now=30.0)
    assert asks.calls == [(stuck.job_id, ["train x3", "rebase fix-up"])] and out["asked"] == [stuck.job_id]
    lt._parked_pass(store, tmp_path, "ddd", spawn=spawn, ask_fn=asks, now=40.0)
    assert len(asks.calls) == 1 and spawned == [stuck.job_id]             # asked once per fix-up, never respawned


def test_fixup_that_could_not_start_is_asked(steward_home, tmp_path):
    store = Store(); asks = _Asks(); j = _accepted(store, tmp_path)
    q.enqueue(store, j.job_id, tmp_path)
    q.park(store, j.job_id, "conflict", target_head="aaa"); q.set_parks(store, j.job_id, 2)
    out = lt._parked_pass(store, tmp_path, "aaa", spawn=lambda jid: None, ask_fn=asks, now=1.0)
    assert out["fixups"] == {} and q.get(store, j.job_id).fixup == lt.FIXUP_NONE
    assert asks.calls == [(j.job_id, ["train x3", "rebase fix-up (could not start)"])]


def test_spawn_that_raises_never_breaks_the_pass(steward_home, tmp_path):
    store = Store(); asks = _Asks(); a, b = _accepted(store, tmp_path), _accepted(store, tmp_path)
    for j in (a, b):
        q.enqueue(store, j.job_id, tmp_path)
        q.park(store, j.job_id, "conflict", target_head="aaa"); q.set_parks(store, j.job_id, 2)

    def boom(jid):
        raise RuntimeError("no control pane")

    out = lt._parked_pass(store, tmp_path, "aaa", spawn=boom, ask_fn=asks, now=1.0)
    assert sorted(out["asked"]) == sorted([a.job_id, b.job_id])
    assert q.get(store, a.job_id).fixup == lt.FIXUP_SPAWNING        # the claim stays: never a second spawn


def test_incorporated_parked_parent_is_marked_landed(steward_home, tmp_path):
    store = Store(); asks = _Asks(); j = _accepted(store, tmp_path)
    q.enqueue(store, j.job_id, tmp_path)
    q.park(store, j.job_id, "conflict", target_head="aaa"); q.set_fixup(store, j.job_id, "gone-child")
    store.kv_set(j.job_id, "incorporated_commit", "c0ffee")
    out = lt._parked_pass(store, tmp_path, "bbb", spawn=lambda jid: None, ask_fn=asks, now=1.0)
    assert q.get(store, j.job_id).status == "landed" and out["landed"] == [j.job_id] and not asks.calls


def test_decide_max_depth_refuses_a_second_attempt(steward_home, scratch, tmp_path):
    store = Store(steward_home); att = Attempts(scratch, tmp_path)
    root = conflicted_root(store, scratch, tmp_path)
    job = store.get_job(root); pf = incorporate.preflight(store, root)
    first = autopilot._repair(store, job, pf, "rebase for the landing train", pf.conflicts, OK, att, None, max_depth=1)
    assert first["outcome"] == "followup" and len(att.calls) == 1
    set_state(store, first["follow_up"], JobState.FAILED)
    d = rep.decide(store, job, pf, detail="again", files=pf.conflicts, now=2.0, pf_target_head=None, max_depth=1)
    assert d["action"] == "ask"
    again = autopilot._repair(store, job, pf, "again", pf.conflicts, OK, att, None, max_depth=1)
    assert again["outcome"] == "ask" and len(att.calls) == 1
    # default (no cap) keeps today's behaviour: the same failure goes to strategy review, not the ask
    assert rep.decide(store, job, pf, detail="again", files=pf.conflicts, now=3.0, pf_target_head=None)["action"] \
        != "ask"


def test_default_spawn_starts_one_capped_repair(steward_home, scratch, tmp_path, monkeypatch):
    """The default spawner goes through autopilot._repair with max_depth=1 and a MERGE_GOAL_PREFIX goal."""
    from steward.train_housekeeping import MERGE_GOAL_PREFIX
    store = Store(steward_home); att = Attempts(scratch, tmp_path)
    root = conflicted_root(store, scratch, tmp_path)
    set_state(store, root, JobState.ACCEPTED)
    q.enqueue(store, root, scratch)
    q.park(store, root, "conflict: a.txt", target_head="aaa"); q.set_parks(store, root, 2)
    seen = {}
    real = autopilot._repair

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(autopilot, "_repair", spy)
    spawn = lt._default_rebase_spawn(store, OK, "main", start_followup=att)
    child = lt.maybe_rebase_fixup(store, root, spawn=spawn)
    assert child == att.calls[0]["child"] and seen.get("max_depth") == 1
    assert store.get_job(child).goal.startswith(f"{MERGE_GOAL_PREFIX}main` into YOUR branch")
    assert "rebase for the landing train" in store.get_job(child).goal
    assert store.kv_get(child, "autopilot_followup") == root


def test_second_park_is_never_retried_by_target_moves(steward_home, tmp_path):
    """An active target moves every train: from the second park the entry waits on its fix-up, not another retry."""
    store = Store(); j = store.create_job("g", str(tmp_path))
    q.enqueue(store, j.job_id, tmp_path)
    q.park(store, j.job_id, "conflict", target_head="aaa"); q.set_parks(store, j.job_id, 2)
    assert q.requeue_parked(store, tmp_path, "bbb") == [] and q.requeue_parked(store, tmp_path, "") == []


def _settings(scratch, tmp_path, **kw):
    s = {"enabled": True, "repo": str(scratch), "target": "main", "every_s": 180.0, "max_batch": 8,
         "resolver_timeout_s": 5.0, "suite_cmd": "true", "worktree_root": str(tmp_path / "mroot"), "report_hour": 7}
    s.update(kw)
    return s


def test_tick_wakes_a_run_for_parked_work_only(scratch, tmp_path):
    store = Store(); j = mkjob(store, scratch, tmp_path, {"x.txt": "x"}); cmds = []
    q.enqueue(store, j.job_id, scratch)
    q.park(store, j.job_id, "conflict", target_head=git(scratch, "rev-parse", "main"))
    s = _settings(scratch, tmp_path, report_hour=99)
    assert lt.tick(store, None, now=1000.0, settings=s, spawn=lambda c: cmds.append(c) or 999999999) == \
        {"skipped": "empty"}                                   # parked against today's head: nothing to do
    (scratch / "z.txt").write_text("z\n"); git(scratch, "add", "-A"); git(scratch, "commit", "-qm", "move")
    out = lt.tick(store, None, now=2000.0, settings=s, spawn=lambda c: cmds.append(c) or 999999999)
    assert cmds == ["run"] and out == {"spawned": 999999999}


def test_run_once_lands_a_parked_entry_after_the_target_moves(scratch, tmp_path):
    store = Store(); j = mkjob(store, scratch, tmp_path, {"x.txt": "x"}); asks = _Asks()
    q.enqueue(store, j.job_id, scratch)
    for _ in range(q.MAX_DROPS):
        q.mark_dropped(store, j.job_id, "red suite", target_head="0" * 40)
    out = lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False,
                      suite_fn=lambda wt: True, deploy_fn=None, stop_fn=lambda _id: None, now=1000.0,
                      notify=lambda t: True, rebase_spawn=lambda jid: None, ask_fn=asks)
    assert out["landed"] == [j.job_id] and q.get(store, j.job_id).status == "landed" and not asks.calls
    ev = [e for e in store.events_of(j.job_id, ("landing_enqueued",)) if e.evidence.get("origin") == "parked_retry"]
    assert len(ev) == 1 and ev[0].evidence["attempts"] == q.MAX_DROPS


def test_run_once_drop_records_the_target_it_was_built_on(scratch, tmp_path):
    store = Store(); j = mkjob(store, scratch, tmp_path, {"BREAKS": "1"})
    q.enqueue(store, j.job_id, scratch)
    lt.run_once(store, None, _settings(scratch, tmp_path), resolve_fn=lambda *_: False,
                suite_fn=lambda wt: not (wt / "BREAKS").exists(), deploy_fn=None, stop_fn=lambda _id: None,
                now=1000.0, notify=lambda t: True, rebase_spawn=lambda jid: None, ask_fn=_Asks())
    assert q.get(store, j.job_id).last_target == git(scratch, "rev-parse", "main")


def test_migration_adds_parks_and_fixup_to_an_old_queue(steward_home, tmp_path):
    store = Store(steward_home); j = store.create_job("g", str(tmp_path))
    q.enqueue(store, j.job_id, tmp_path)
    with store._conn:
        store._conn.execute("ALTER TABLE landing_queue DROP COLUMN fixup")
        store._conn.execute("ALTER TABLE landing_queue DROP COLUMN parks")
    store._conn.close()
    again = Store(steward_home)
    e = q.get(again, j.job_id)
    assert (e.status, e.parks, e.fixup) == ("queued", 0, "")
