"""Phase 27 (FLEET-09): v2 lane/attempt frame identity, dedupe, stale rejection, steering, legacy isolation."""

from __future__ import annotations

import pytest
from test_fabric_worker import FakeApi, FakeMailbox
from test_fleet_lanes import JOB, TWO, controller, seed

from steward import fabric
from steward import fleet_lease as fl
from steward import fleet_runtime as rt
from steward.fabric import Frame, FrameReader, Outbox
from steward.fabric_worker import FabricWorker
from steward.store import Store


def v2(seq=1, *, lane="L1", task="t1", attempt=1, sender=None, epoch=1, type_="event", body=None, job=JOB):
    sender = sender if sender is not None else f"shim:{lane}"
    return Frame(v=2, seq=seq, job=job, type=type_, body=body if body is not None else {"kind": "x"},
                 task=task, lane=lane, attempt=attempt, sender=sender, epoch=epoch)


# ----------------------------------------------------------------------------- codec / dedupe

def test_v2_round_trip_and_required_identity():
    f = v2(7)
    assert fabric.decode(fabric.encode(f)) == f
    with pytest.raises(ValueError):
        fabric.encode(Frame(v=2, seq=1, job=JOB, type="event", body={}))  # no identity
    line = fabric.encode(f).replace('"lane":"L1",', "")
    with pytest.raises(ValueError):
        fabric.decode(line)
    v1 = Frame(v=1, seq=1, job=JOB, type="event", body={})
    assert '"lane"' not in fabric.encode(v1) and fabric.decode(fabric.encode(v1)) == v1


def test_same_sequence_in_two_lanes_is_delivered_twice_but_exact_duplicate_once():
    r = FrameReader()
    a, b = v2(1, lane="L1", task="t1"), v2(1, lane="L2", task="t2")
    text = "".join(fabric.encode(f) + "\n" for f in (a, b, a, v2(1, lane="L1", attempt=2), v2(1, lane="L1", epoch=2),
                                                     v2(1, lane="L1", sender="other")))
    got = r.feed(text)
    assert len(got) == 5 and r.dup == 1
    assert {(g.lane, g.attempt, g.epoch, g.sender) for g in got} == {
        ("L1", 1, 1, "shim:L1"), ("L2", 1, 1, "shim:L2"), ("L1", 2, 1, "shim:L1"),
        ("L1", 1, 2, "shim:L1"), ("L1", 1, 1, "other")}


def test_v1_dedupe_is_unchanged():
    r = FrameReader()
    f = Frame(v=1, seq=3, job=JOB, type="event", body={})
    assert len(r.feed(fabric.encode(f) + "\n" + fabric.encode(f) + "\n")) == 1 and r.dup == 1


def test_outbox_tracks_lane_frames_independently():
    ob = Outbox(timeout_s=1, max_retries=2)
    a, b = v2(1, lane="L1"), v2(1, lane="L2")
    ob.add(a, 0)
    ob.add(b, 0)
    ob.ack(JOB, 1, ident=a.ident)
    assert not ob.pending(JOB, 1, a.ident) and ob.pending(JOB, 1, b.ident)


# ----------------------------------------------------------------------------- FabricWorker lane route

def lane_worker(lane="L1", task="t1", attempt=1, epoch=1, check=None):
    api, mb = FakeApi(), FakeMailbox()
    w = FabricWorker(api=api, mailbox=mb, runner_pane="r", job=JOB, spawn_pane=lambda p: "p2", task=task, lane=lane,
                     attempt=attempt, epoch=epoch, lease_check=check, ready_timeout_s=0.01)
    w.pane_id = "p2"
    return w, api, mb


def test_lane_worker_stamps_identity_and_acks_only_its_own_frames():
    w, api, mb = lane_worker()
    w.deliver("[rev=2] hello")
    sent = api.sent[-1][1]
    assert (sent.v, sent.lane, sent.task, sent.attempt, sent.epoch) == (2, "L1", "t1", 1, 1)
    # transport ack from the shim for that exact frame clears the outbox
    mb.push(v2(9, type_="ack", body={"acked_seq": sent.seq}))
    w.observe()
    assert not w.outbox.pending(JOB, sent.seq, w._ident())


def test_lane_worker_drops_foreign_stale_and_legacy_frames_without_surfacing():
    w, api, mb = lane_worker(attempt=2, epoch=2)
    mb.push(v2(1, attempt=1, epoch=1, type_="done", body={}))       # fenced prior attempt
    mb.push(v2(2, lane="L2", task="t2", attempt=2, epoch=2, type_="done", body={}))  # sibling lane
    mb.push(Frame(v=1, seq=3, job=JOB, type="done", body={}))     # legacy frame on a lane route
    good = v2(4, attempt=2, epoch=2, type_="done", body={})
    mb.push(good)
    obs = w.observe()
    assert [f.seq for f in obs.events] == [4] and w.stale_dropped == 3


def test_lease_check_hook_gates_events_and_legacy_worker_rejects_v2():
    w, api, mb = lane_worker(check=lambda f: False)
    mb.push(v2(1, type_="done", body={}))
    assert w.observe().events == () and w.stale_dropped == 1
    legacy = FabricWorker(api=FakeApi(), mailbox=FakeMailbox(), runner_pane="r", job=JOB, spawn_pane=lambda p: "p")
    legacy.pane_id = "p"
    legacy.mailbox.push(v2(1, type_="done", body={}))
    legacy.mailbox.push(Frame(v=1, seq=2, job=JOB, type="done", body={}))
    assert [f.seq for f in legacy.observe().events] == [2]
    assert legacy.api.sent[0][1].v == 1  # legacy route still emits v1


def test_lane_worker_requires_the_expected_shim_sender():
    w, _api, mb = lane_worker()
    mb.push(v2(1, sender="spoofed-endpoint", type_="done", body={"candidate_sha": "a" * 40}))
    assert w.observe().events == ()
    assert w.stale_dropped == 1


# ----------------------------------------------------------------------------- controller routing

def started(tmp_path, home, lanes=2):
    store, repo, _ = seed(tmp_path, home, TWO, max_lanes=lanes)
    ctl, world = controller(tmp_path, store, repo)
    ctl.admit(JOB)
    return store, ctl, world


def f(lane, task, seq=1, epoch=1, attempt=1, **kw):
    return v2(seq, lane=f"lane-{lane}", task=task, epoch=epoch, attempt=attempt, **kw)


def test_current_frames_apply_independently_per_lane_even_with_equal_sequence(steward_home, tmp_path):
    store, ctl, world = started(tmp_path, steward_home)
    d1 = ctl.accept_frame(JOB, f("t1", "t1", type_="done", body={}))
    assert d1.accepted
    assert fl.task_states(store, JOB) == {"t1": "finished"}  # t2 untouched
    assert ctl.accept_frame(JOB, f("t2", "t2", type_="event", body={"kind": "activity"})).accepted
    assert fl.task_states(store, JOB) == {"t1": "finished"}


def test_controller_fences_correct_lane_identity_from_wrong_sender(steward_home, tmp_path):
    store, ctl, _world = started(tmp_path, steward_home, lanes=1)
    spoofed = f("t1", "t1", type_="done", body={}, sender="other-lane-endpoint")
    assert not ctl.frame_is_current(JOB, spoofed)
    assert not ctl.accept_frame(JOB, spoofed).accepted
    assert fl.task_states(store, JOB).get("t1") != "finished"


def test_controller_routes_current_lane_permission_to_runner_and_persists_lease_route(steward_home, tmp_path):
    store, repo, _ = seed(tmp_path, steward_home, TWO, max_lanes=1)
    from test_fleet_lanes import World
    from steward.runner_exec import SubprocessRunner

    world, routed = World(), []
    ctl = rt.JobController(store, repo, SubprocessRunner(), world.factory, base_dir=tmp_path / "wts",
                           probe=world.probe, frame_handler=lambda job, frame, adapter: routed.append(
                               (job, frame.body["request_id"], adapter)))
    assert ctl.admit(JOB)[0]["status"] == "started"
    frame = f("t1", "t1", type_="permission_request", body={"request_id": "request-1", "tool": "Bash",
                                                               "input": {"command": "ls"}})

    assert ctl.accept_frame(JOB, frame).accepted
    assert routed == [(JOB, "request-1", ctl.adapter(JOB, "lane-t1"))]
    routes = store.kv_get(JOB, "fleet_permission_routes")
    assert routes["lane-t1:1:1:request-1"] == {"request_id": "request-1", "lane": "lane-t1",
                                               "attempt": 1, "epoch": 1}


def test_late_frame_from_fenced_attempt_changes_nothing(steward_home, tmp_path):
    store, ctl, world = started(tmp_path, steward_home)
    ctl.steer(JOB, "be careful", request_id="s1", lane_id="lane-t1", rev=3)
    assert ctl.receipts(JOB)["s1:lane-t1"]["stage"] == "delivered"
    out = ctl.recover_lane(JOB, "lane-t1", "r1", reason="x")  # attempt 2 / epoch 2
    assert out["status"] == "started" and fl.get(store, JOB, "lane-t1")["epoch"] == 2
    before = (fl.task_states(store, JOB), dict(ctl.receipts(JOB)), fl.get(store, JOB, "lane-t1"))
    for late in (f("t1", "t1", seq=50, type_="done", body={"sha": "abc"}),
                 f("t1", "t1", seq=51, type_="ack", body={"rev": 3, "stage": "acknowledged"}),
                 f("t1", "t1", seq=52, type_="error", body={"exit_code": 1})):
        dec = ctl.accept_frame(JOB, late)
        assert (dec.accepted, dec.reason) == (False, "stale_identity")
    assert (fl.task_states(store, JOB), dict(ctl.receipts(JOB)), fl.get(store, JOB, "lane-t1")) == before
    assert any(e["what"] == "frame_rejected" for e in ctl.fleet.fleet_events(JOB))
    # same for a stopped lane with no replacement, and for a mismatched task or an unknown lane
    assert not ctl.accept_frame(JOB, f("t2", "t1", type_="done", body={})).accepted
    assert not ctl.accept_frame(JOB, v2(1, lane="nope", task="t9", type_="done", body={})).accepted
    ctl.stop(JOB, "stop-1")
    assert not ctl.accept_frame(JOB, f("t2", "t2", type_="done", body={})).accepted


def test_current_ack_upgrades_only_the_matching_lane_receipt(steward_home, tmp_path):
    store, ctl, world = started(tmp_path, steward_home)
    ctl.steer(JOB, "x", request_id="s1", lane_id="lane-t1", rev=3)
    ctl.steer(JOB, "x", request_id="s2", lane_id="lane-t2", rev=3)
    assert ctl.accept_frame(JOB, f("t1", "t1", type_="ack", body={"rev": 3, "stage": "acknowledged"})).accepted
    r = ctl.receipts(JOB)
    assert r["s1:lane-t1"]["stage"] == "acknowledged" and r["s2:lane-t2"]["stage"] == "delivered"
    assert (r["s1:lane-t1"]["lane"], r["s1:lane-t1"]["attempt"], r["s1:lane-t1"]["epoch"]) == ("lane-t1", 1, 1)


def test_lane_targeted_steering_reaches_only_that_lane(steward_home, tmp_path):
    store, ctl, world = started(tmp_path, steward_home)
    out = ctl.steer(JOB, "focus", request_id="s1", lane_id="lane-t2", rev=1)
    assert [r["lane"] for r in out] == ["lane-t2"]
    assert world.adapters[("lane-t2", 1)].delivered == ["[rev=1] focus"]
    assert world.adapters[("lane-t1", 1)].delivered == []
    assert ctl.steer(JOB, "focus", request_id="s1", lane_id="lane-t2", rev=1) == out  # idempotent
    assert len(world.adapters[("lane-t2", 1)].delivered) == 1
    missing = ctl.steer(JOB, "x", request_id="s9", lane_id="lane-zz")
    assert missing[0]["stage"] == "undeliverable"


def test_global_steering_fans_out_separately_and_is_inherited_by_later_lanes(steward_home, tmp_path):
    three = TWO + [{**TWO[0], "id": "t3", "objective": "do t3", "owns": ["src/c/"], "depends_on": ["t1"]}]
    store, repo, _ = seed(tmp_path, steward_home, three, max_lanes=2)
    ctl, world = controller(tmp_path, store, repo)
    ctl.admit(JOB)
    out = ctl.steer(JOB, "never touch prod", request_id="g1", rev=4)
    assert sorted(r["lane"] for r in out) == ["lane-t1", "lane-t2"]
    assert all(r["stage"] == "delivered" and r["epoch"] == 1 for r in out)
    assert len(ctl.receipts(JOB)) == 2
    ctl.accept_frame(JOB, f("t1", "t1", type_="done", body={}))
    ctl.tick(JOB)  # t1's lane stops, t3 becomes ready and is admitted
    brief = world.adapters[("lane-t3", 1)].brief
    assert "Constraint: never touch prod" in brief


def test_legacy_v1_frames_only_for_legacy_jobs(steward_home, tmp_path):
    store, ctl, world = started(tmp_path, steward_home)
    v1 = Frame(v=1, seq=1, job=JOB, type="done", body={})
    dec = ctl.accept_frame(JOB, v1)
    assert (dec.accepted, dec.reason) == (False, "legacy_frame_in_fleet_job")
    assert fl.task_states(store, JOB) == {}
    store.create_job("g", "/r", job_id="LEG")
    assert ctl.accept_frame("LEG", Frame(v=1, seq=1, job="LEG", type="done", body={})).accepted
    assert not rt.is_fleet_job(store, "LEG")
    # a v2 frame for a legacy job has no lease and is rejected too
    assert not ctl.accept_frame("LEG", v2(1, job="LEG")).accepted


def test_observe_routes_adapter_events_through_identity_checks(steward_home, tmp_path):
    store, ctl, world = started(tmp_path, steward_home)
    world.adapters[("lane-t1", 1)].events = [f("t1", "t1", type_="done", body={}),
                                              f("t1", "t1", seq=2, epoch=7, type_="done", body={})]
    decisions = ctl.observe(JOB)
    assert [d.accepted for d in decisions] == [True, False]


def test_controller_tick_returns_per_lane_activity_and_persists_last_activity(steward_home, tmp_path):
    store, ctl, world = started(tmp_path, steward_home, lanes=1)
    world.adapters[("lane-t1", 1)].events = [f("t1", "t1", type_="event", body={"kind": "activity"})]

    result = ctl.tick(JOB)

    lane = result["observations"]["lane-t1"]
    assert lane["lifecycle"] == "working" and lane["activity"] is True
    assert lane["last_activity_at"] > 0
    saved = store.kv_get(JOB, "fleet_last_activity")["lane-t1"]
    assert saved == {"attempt_no": 1, "epoch": 1, "at": lane["last_activity_at"]}


def test_rejected_stale_lane_activity_does_not_refresh_heartbeat(steward_home, tmp_path):
    store, ctl, world = started(tmp_path, steward_home, lanes=1)
    world.adapters[("lane-t1", 1)].events = [f("t1", "t1", type_="event", body={"kind": "activity"},
                                                  sender="spoofed-lane")]

    result = ctl.tick(JOB)

    assert result["frames"][0].accepted is False
    assert result["observations"]["lane-t1"]["activity"] is False


def test_rejected_stale_permission_does_not_mark_lane_waiting(steward_home, tmp_path):
    store, ctl, world = started(tmp_path, steward_home, lanes=1)
    world.adapters[("lane-t1", 1)].events = [f("t1", "t1", type_="permission_request",
                                                  body={"request_id": "old"}, sender="spoofed-lane")]

    result = ctl.tick(JOB)

    assert result["frames"][0].accepted is False
    assert result["observations"]["lane-t1"]["waiting"] is False


def test_last_activity_clock_restarts_for_a_new_lane_attempt(steward_home, tmp_path):
    store, ctl, _world = started(tmp_path, steward_home, lanes=1)
    ctl.clock = lambda: 100.0
    first = ctl.tick(JOB)["observations"]["lane-t1"]
    assert (first["attempt_no"], first["last_activity_at"]) == (1, 100.0)

    replacement = ctl.recover_lane(JOB, "lane-t1", "replace-1", reason="test")
    assert replacement["status"] == "started"
    ctl.clock = lambda: 200.0
    second = ctl.tick(JOB)["observations"]["lane-t1"]

    assert (second["attempt_no"], second["epoch"], second["last_activity_at"]) == (2, 2, 200.0)
    assert store.kv_get(JOB, "fleet_last_activity")["lane-t1"] == {
        "attempt_no": 2, "epoch": 2, "at": 200.0,
    }


def test_legacy_runner_ignores_v2_frames(steward_home):
    """The single-worker Runner path never acts on a lane (v2) frame, and records that it ignored one."""
    import test_runner_fabric as trf
    from steward.models import JobState
    from steward.runner import Runner
    store = Store(steward_home)
    job = trf._contracted_job(store)
    lane_done = v2(1, job=job.job_id, type_="done", body={"sha": "abc"})
    factory = trf._FabricFactory(scripts=[[trf._obs(events=(lane_done,))]])
    runner = Runner(store, factory, trf._git_runner(), trf._fake_diagnoser(), clock=trf._Clock())
    runner.start(job.job_id)
    runner.tick(job.job_id)
    assert store.get_job(job.job_id).state == JobState.RUNNING  # a v2 `done` did not finish the job
    assert any(e.what == "v2_frame_ignored_by_legacy_runner" for e in store.events(job.job_id))
