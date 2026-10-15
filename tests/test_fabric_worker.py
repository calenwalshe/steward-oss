"""Tests for steward/fabric_worker.py (WRK-03).

No real Herdr: `api` is a `FakeApi` recording sends/calls, `mailbox` is an
in-memory queue the test pushes `Frame`s into directly, `spawn_pane` is a
fake that never touches a real pane.
"""

from __future__ import annotations

import pytest

from steward.fabric import Frame
from steward.fabric_worker import FabricWorker, ack_kind
from steward.herdr_transport import HerdrApiError
from steward.herdr_worker import WorkerStartBlocked


class FakeApi:
    def __init__(
        self,
        *,
        call_raises: Exception | None = None,
        call_responses: dict[str, dict] | None = None,
        send_raises: Exception | None = None,
    ):
        self.sent: list[tuple[str, Frame]] = []
        self.calls: list[tuple[str, dict]] = []
        self.call_raises = call_raises
        self.call_responses = call_responses or {}
        self.send_raises = send_raises

    def send(self, pane_id: str, frame: Frame) -> dict:
        if self.send_raises is not None:
            raise self.send_raises
        self.sent.append((pane_id, frame))
        return {"type": "ok"}

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if self.call_raises is not None:
            raise self.call_raises
        return self.call_responses.get(method, {})


class FakeMailbox:
    def __init__(self):
        self._queue: list[Frame] = []

    def push(self, frame: Frame) -> None:
        self._queue.append(frame)

    def read(self, timeout: float) -> list[Frame]:
        drained, self._queue = self._queue, []
        return drained


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def _make_worker(api=None, mailbox=None, clock=None, spawn_pane=None, **kwargs) -> FabricWorker:
    return FabricWorker(
        api=api or FakeApi(),
        mailbox=mailbox or FakeMailbox(),
        runner_pane="w1:p1",
        job="jobA",
        spawn_pane=spawn_pane or (lambda worktree: "w1:p2"),
        clock=clock,
        **kwargs,
    )


def _sent_of_type(api: FakeApi, type_: str) -> list[Frame]:
    return [f for _, f in api.sent if f.type == type_]


def _push_ready(mailbox: FakeMailbox, seq: int = 1, pid: int = 123) -> None:
    """Simulates the shim's own first frame (10-03 finding 1): `start()`
    withholds the brief until it sees this."""
    mailbox.push(Frame(v=1, seq=seq, job="jobA", type="event", body={"kind": "ready", "pid": pid}))


# ------------------------------------------------------------- ack_kind()


def test_ack_kind_transport_shape():
    assert ack_kind({"acked_seq": 5}) == "transport"


def test_ack_kind_steer_receipt_shape():
    assert ack_kind({"rev": 3, "stage": "delivered"}) == "steer_receipt"


def test_ack_kind_neither_shape_raises():
    with pytest.raises(ValueError):
        ack_kind({"nonsense": True})


# --------------------------------------------------------------- start()


def test_start_spawns_pane_and_sends_brief():
    api = FakeApi()
    mailbox = FakeMailbox()
    spawned = {}

    def spawn_pane(worktree):
        spawned["worktree"] = worktree
        return "w1:p2"

    worker = _make_worker(api=api, mailbox=mailbox, spawn_pane=spawn_pane)
    _push_ready(mailbox)

    ref = worker.start("do the thing", "/repo/worktree")

    assert ref == "w1:p2"
    assert worker.pane_id == "w1:p2"
    assert str(spawned["worktree"]) == "/repo/worktree"
    briefs = _sent_of_type(api, "brief")
    assert len(briefs) == 1
    assert briefs[0].body == {"text": "do the thing"}


def test_kind_is_fabric_for_runner_dispatch():
    assert _make_worker().kind == "fabric"


def test_printed_lines_end_with_carriage_return_for_raw_tty():
    """10-03 finding 2: this process's own mailbox has its tty in raw mode
    (OPOST off), so every printed line must end `\\r\\n`, not a bare `\\n`."""
    printed = []
    worker = _make_worker(print_fn=printed.append, spawn_pane=lambda w: "w1:p2")
    mailbox = worker.mailbox
    _push_ready(mailbox)

    worker.start("brief", "/repo")

    assert printed
    assert all(line.endswith("\r") for line in printed)


def test_start_waits_for_ready_before_sending_brief():
    """10-03 finding 1: nothing is sent until the shim's own `ready` event
    is observed on the mailbox."""
    api = FakeApi()
    mailbox = FakeMailbox()
    worker = _make_worker(api=api, mailbox=mailbox, spawn_pane=lambda w: "w1:p2")
    _push_ready(mailbox, seq=1)

    worker.start("brief text", "/repo")

    briefs = _sent_of_type(api, "brief")
    assert len(briefs) == 1
    # the ready event's own transport ack went out before the brief did
    ack_seqs = [f.seq for _, f in api.sent if f.type == "ack"]
    assert ack_seqs and all(seq < briefs[0].seq for seq in ack_seqs)


def test_start_raises_worker_start_blocked_and_closes_pane_on_ready_timeout():
    """No `ready` ever arrives: `start()` gives up, closes the pane it just
    created (never one it didn't), and never sends the brief."""
    api = FakeApi()
    mailbox = FakeMailbox()  # never receives a ready frame
    worker = _make_worker(
        api=api, mailbox=mailbox, spawn_pane=lambda w: "w1:p2", ready_timeout_s=0.01
    )

    with pytest.raises(WorkerStartBlocked):
        worker.start("brief text", "/repo")

    assert _sent_of_type(api, "brief") == []
    assert ("pane.close", {"pane_id": "w1:p2"}) in api.calls


# ------------------------------------------------------ attach() (10-04 resume)


def _fg_response(*, foreground: bool) -> dict:
    """`pane.process_info` shape (matches the live gate's own probe): the
    shim is foreground when its pid differs from the bare shell's."""
    return {"process_info": {"foreground_process_group_id": 999 if foreground else 111, "shell_pid": 111}}


def test_attach_success_rebinds_peer_and_marks_working():
    """The `cmd` frame's own seq is seeded well past any low value the dead
    runner instance already used for this job (10-04: the shim's dedupe
    would otherwise silently drop a reused seq as a duplicate) — so the test
    doesn't assume a specific number, only that whatever it is gets acked."""
    api = FakeApi(call_responses={"pane.process_info": _fg_response(foreground=True)})
    mailbox = FakeMailbox()
    worker = FabricWorker(
        api=api, mailbox=mailbox, runner_pane="w9:pNew", job="jobA", spawn_pane=lambda w: "w1:p2"
    )

    # `attach()` sends the `cmd` frame before `_await_transport_ack()`'s poll
    # loop starts, so by the time this wrapper is asked to read, `api.sent`
    # already has it — queue its ack the first time nothing else is queued.
    real_read = mailbox.read
    acked = {"done": False}

    def read_with_ack(timeout):
        drained = real_read(timeout)
        if drained or acked["done"]:
            return drained
        cmds = _sent_of_type(api, "cmd")
        if not cmds:
            return drained
        acked["done"] = True
        mailbox.push(Frame(v=1, seq=1, job="jobA", type="ack", body={"acked_seq": cmds[0].seq}))
        return real_read(timeout)

    mailbox.read = read_with_ack

    ok = worker.attach("w1:pOld")

    assert ok is True
    assert worker.pane_id == "w1:pOld"
    cmds = _sent_of_type(api, "cmd")
    # rebind, then ask the shim what the worker is blocked on (a fresh object's memory is empty)
    assert [c.body for c in cmds] == [{"op": "rebind", "peer": "w9:pNew"}, {"op": "list_permissions"}]
    obs = worker.observe()
    assert obs.lifecycle == "working"


def test_attach_fails_when_shim_not_foreground():
    """The pane is back to a bare shell (the shim already exited) — treated
    as dead, never as a live second writer."""
    api = FakeApi(call_responses={"pane.process_info": _fg_response(foreground=False)})
    worker = _make_worker(api=api)

    ok = worker.attach("w1:pOld")

    assert ok is False
    assert _sent_of_type(api, "cmd") == []  # never even tried to rebind
    obs = worker.observe()
    assert obs.lifecycle == "exited"
    assert obs.pid_alive is False


def test_attach_fails_when_pane_process_info_unreachable():
    from steward.herdr_transport import HerdrUnavailable

    api = FakeApi(call_raises=HerdrUnavailable("no herdr"))
    worker = _make_worker(api=api)

    ok = worker.attach("w1:pOld")

    assert ok is False


def test_attach_fails_when_rebind_never_acks():
    """The shim looks foreground but never acks the rebind (e.g. it froze):
    bounded, never a hang."""
    api = FakeApi(call_responses={"pane.process_info": _fg_response(foreground=True)})
    worker = _make_worker(api=api, rebind_timeout_s=0.01)

    ok = worker.attach("w1:pOld")

    assert ok is False
    obs = worker.observe()
    assert obs.lifecycle == "exited"


def test_sends_target_the_worker_pane_not_the_runner_pane():
    """Mailbox model: to reach the shim you write into *its* pane
    (`self.pane_id`, from `spawn_pane`) — never `runner_pane` (that's this
    process's OWN pane, where the shim sends frames to us)."""
    api = FakeApi()
    mailbox = FakeMailbox()
    worker = _make_worker(api=api, mailbox=mailbox, spawn_pane=lambda w: "w1:p2")
    _push_ready(mailbox)

    worker.start("brief", "/repo")
    worker.deliver("[rev=1] steer text")

    assert worker.runner_pane == "w1:p1"
    assert all(pane_id == "w1:p2" for pane_id, _ in api.sent)


# --------------------------------------------------------------- deliver()


def test_deliver_splits_rev_prefix_into_steer_frame():
    api = FakeApi()
    worker = _make_worker(api=api)

    result = worker.deliver("[rev=4] focus on X")

    assert result.ok is True
    steers = _sent_of_type(api, "steer")
    assert steers[0].body == {"rev": 4, "text": "focus on X"}


def test_deliver_without_rev_prefix_defaults_rev_zero():
    api = FakeApi()
    worker = _make_worker(api=api)

    worker.deliver("just some text")

    steers = _sent_of_type(api, "steer")
    assert steers[0].body == {"rev": 0, "text": "just some text"}


# ----------------------------------------------------------- answer_prompt()


def test_answer_prompt_with_no_pending_request_fails():
    worker = _make_worker()
    result = worker.answer_prompt(approve=True)
    assert result.ok is False


def test_answer_prompt_approve_sends_approve_with_request_id():
    api = FakeApi()
    mailbox = FakeMailbox()
    worker = _make_worker(api=api, mailbox=mailbox)
    mailbox.push(Frame(v=1, seq=1, job="jobA", type="permission_request", body={"request_id": "r1", "tool": "Bash"}))

    worker.observe()  # drains the permission_request, sets pending

    result = worker.answer_prompt(approve=True)
    assert result.ok is True
    approves = _sent_of_type(api, "approve")
    assert approves[0].body == {"request_id": "r1"}


def test_answer_prompt_deny_sends_deny():
    api = FakeApi()
    mailbox = FakeMailbox()
    worker = _make_worker(api=api, mailbox=mailbox)
    mailbox.push(Frame(v=1, seq=1, job="jobA", type="permission_request", body={"request_id": "r1", "tool": "Bash"}))
    worker.observe()

    worker.answer_prompt(approve=False)

    denies = _sent_of_type(api, "deny")
    assert denies[0].body == {"request_id": "r1"}


# ------------------------------------------------------------- observe()/lifecycle


def test_observe_activity_event_sets_working():
    mailbox = FakeMailbox()
    worker = _make_worker(mailbox=mailbox)
    mailbox.push(Frame(v=1, seq=1, job="jobA", type="event", body={"kind": "activity", "tool": "Bash"}))

    obs = worker.observe()

    assert obs.lifecycle == "working"
    assert obs.events[0].type == "event"


def test_observe_permission_request_pending_sets_blocked():
    mailbox = FakeMailbox()
    worker = _make_worker(mailbox=mailbox)
    mailbox.push(Frame(v=1, seq=1, job="jobA", type="permission_request", body={"request_id": "r1", "tool": "Bash"}))

    obs = worker.observe()

    assert obs.lifecycle == "blocked"
    assert obs.events[0].type == "permission_request"


def test_observe_done_sets_idle():
    mailbox = FakeMailbox()
    worker = _make_worker(mailbox=mailbox)
    mailbox.push(Frame(v=1, seq=1, job="jobA", type="done", body={"sha": "abc123"}))

    obs = worker.observe()

    assert obs.lifecycle == "idle"
    assert obs.events[0].body["sha"] == "abc123"


def test_observe_error_sets_exited():
    mailbox = FakeMailbox()
    worker = _make_worker(mailbox=mailbox)
    mailbox.push(Frame(v=1, seq=1, job="jobA", type="error", body={"exit_code": 1}))

    obs = worker.observe()

    assert obs.lifecycle == "exited"
    assert obs.pid_alive is False


def test_observe_pane_exited_lifecycle_event_sets_exited():
    class _Ev:
        def __init__(self, kind, pane_id):
            self.kind = kind
            self.pane_id = pane_id

    class _Lifecycle:
        def poll(self, timeout):
            return [_Ev("pane_exited", "w1:p2")]

    mailbox = FakeMailbox()
    worker = _make_worker(mailbox=mailbox, spawn_pane=lambda w: "w1:p2", lifecycle=_Lifecycle())
    _push_ready(mailbox)
    worker.start("brief", "/repo")

    obs = worker.observe()

    assert obs.lifecycle == "exited"


def test_blocked_clears_once_answered_and_falls_back_to_prior_state():
    mailbox = FakeMailbox()
    worker = _make_worker(mailbox=mailbox)
    mailbox.push(Frame(v=1, seq=1, job="jobA", type="event", body={"kind": "activity", "tool": "Bash"}))
    worker.observe()  # lifecycle -> working

    mailbox.push(Frame(v=1, seq=2, job="jobA", type="permission_request", body={"request_id": "r1", "tool": "Bash"}))
    obs = worker.observe()
    assert obs.lifecycle == "blocked"

    worker.answer_prompt(approve=True)
    obs2 = worker.observe()
    assert obs2.lifecycle == "working"


# ------------------------------------------------------- ack disambiguation


def test_transport_ack_clears_outbox_and_is_never_surfaced():
    api = FakeApi()
    mailbox = FakeMailbox()
    worker = _make_worker(api=api, mailbox=mailbox, spawn_pane=lambda w: "w1:p2")
    _push_ready(mailbox)
    worker.start("brief", "/repo")  # sends "brief", tracked in outbox
    brief_seq = _sent_of_type(api, "brief")[0].seq

    assert worker.outbox.pending("jobA", brief_seq) is True
    mailbox.push(Frame(v=1, seq=99, job="jobA", type="ack", body={"acked_seq": brief_seq}))

    obs = worker.observe()

    assert worker.outbox.pending("jobA", brief_seq) is False
    assert obs.events == ()


def test_steer_receipt_ack_is_surfaced_and_transport_acked_back():
    api = FakeApi()
    mailbox = FakeMailbox()
    worker = _make_worker(api=api, mailbox=mailbox)
    mailbox.push(Frame(v=1, seq=5, job="jobA", type="ack", body={"rev": 2, "stage": "acknowledged"}))

    obs = worker.observe()

    assert len(obs.events) == 1
    assert obs.events[0].body == {"rev": 2, "stage": "acknowledged"}
    # transport-acked back to the shim for seq=5
    transport_acks = [f for _, f in api.sent if f.type == "ack"]
    assert any(f.body.get("acked_seq") == 5 for f in transport_acks)


# ------------------------------------------------------------- retransmit timing


def test_delayed_ack_is_drained_before_retransmit_check_no_spurious_retry():
    """10-03 finding 5: `observe()` runs only once per runner tick, which can
    be tens of seconds — long enough that a healthy ack sitting unread in the
    mailbox looked "overdue" to the retransmit check if that check ran first.
    `_drain()` now applies a waiting ack before checking for anything due, so
    a healthy run's log shows no retry even after a long gap."""
    api = FakeApi()
    mailbox = FakeMailbox()
    clock = FakeClock()
    worker = _make_worker(api=api, mailbox=mailbox, clock=clock, spawn_pane=lambda w: "w1:p2", timeout_s=2.0)
    _push_ready(mailbox)
    worker.start("brief", "/repo")
    brief_seq = _sent_of_type(api, "brief")[0].seq

    # a long gap between ticks (e.g. a 30s runner interval), during which the
    # shim's ack for the brief already arrived and is sitting in the mailbox
    clock.advance(30.0)
    mailbox.push(Frame(v=1, seq=50, job="jobA", type="ack", body={"acked_seq": brief_seq}))

    worker.observe()

    assert len(_sent_of_type(api, "brief")) == 1  # no retransmit
    assert worker.outbox.pending("jobA", brief_seq) is False


# ------------------------------------------------------------------- stop()


def test_stop_not_confirmed_until_error_or_pane_exited():
    api = FakeApi()
    mailbox = FakeMailbox()
    clock = FakeClock()
    worker = _make_worker(api=api, mailbox=mailbox, clock=clock, stop_grace_s=5.0)

    assert worker.stop() is False
    stops = _sent_of_type(api, "stop")
    assert len(stops) == 1

    mailbox.push(Frame(v=1, seq=1, job="jobA", type="error", body={"exit_code": 0}))
    assert worker.stop() is True


def test_stop_force_closes_pane_after_grace_expires():
    api = FakeApi()
    mailbox = FakeMailbox()
    clock = FakeClock()
    worker = _make_worker(
        api=api, mailbox=mailbox, clock=clock, spawn_pane=lambda w: "w1:p2", stop_grace_s=1.0,
    )
    _push_ready(mailbox)
    worker.start("brief", "/repo")

    assert worker.stop() is False
    clock.advance(2.0)
    assert worker.stop() is True
    assert ("pane.close", {"pane_id": "w1:p2"}) in api.calls


def test_stop_swallows_herdr_api_error_on_force_close():
    api = FakeApi(call_raises=HerdrApiError("boom", "detail"))
    mailbox = FakeMailbox()
    clock = FakeClock()
    worker = _make_worker(api=api, mailbox=mailbox, clock=clock, spawn_pane=lambda w: "w1:p2", stop_grace_s=1.0)
    _push_ready(mailbox)
    worker.start("brief", "/repo")

    worker.stop()
    clock.advance(2.0)
    assert worker.stop() is True


# ------------------------------------------------- gone-pane sends (11-01 carried fix)


def test_send_to_gone_pane_marks_exited_instead_of_raising():
    api = FakeApi(send_raises=HerdrApiError("not_found", "pane is gone"))
    worker = _make_worker(api=api)

    frame = worker._send("stop", {})

    assert frame.type == "stop"  # still returns a frame, never raises
    obs = worker.observe()
    assert obs.lifecycle == "exited"
    assert obs.pid_alive is False


def test_send_to_gone_pane_swallows_herdr_unavailable_too():
    from steward.herdr_transport import HerdrUnavailable

    api = FakeApi(send_raises=HerdrUnavailable("no socket"))
    worker = _make_worker(api=api)

    worker._send("stop", {})

    assert worker.observe().lifecycle == "exited"


def test_stop_on_gone_pane_confirmed_when_process_info_also_fails():
    """The attach()-failure fallback path (10-04 open issue): bounded
    recovery calls `stop()` on a placeholder worker whose pane is truly
    gone. `_send`'s failure must not raise out of `stop()`, and confirmed
    stopped must not require waiting out the full grace period."""
    from steward.herdr_transport import HerdrUnavailable

    api = FakeApi(send_raises=HerdrApiError("not_found", "pane is gone"), call_raises=HerdrUnavailable("no herdr"))
    worker = _make_worker(api=api, stop_grace_s=30.0)

    assert worker.stop() is True
    assert ("pane.process_info", {"pane_id": worker.pane_id}) in api.calls


def test_stop_on_gone_pane_confirmed_when_process_info_shows_no_shim():
    api = FakeApi(
        send_raises=HerdrApiError("not_found", "pane is gone"),
        call_responses={"pane.process_info": _fg_response(foreground=False)},
    )
    worker = _make_worker(api=api, stop_grace_s=30.0)

    assert worker.stop() is True


def test_stop_on_gone_pane_not_confirmed_falls_back_to_grace_period():
    """`_send` failed but `pane.process_info` says the shim is still there
    (foreground) — a transient send failure, not a confirmed exit — so
    `stop()` behaves like the ordinary not-yet-exited case: not confirmed
    until the grace period elapses."""
    api = FakeApi(
        send_raises=HerdrApiError("not_found", "transient"),
        call_responses={"pane.process_info": _fg_response(foreground=True)},
    )
    clock = FakeClock()
    worker = _make_worker(api=api, clock=clock, stop_grace_s=1.0)

    assert worker.stop() is False
    clock.advance(2.0)
    assert worker.stop() is True


def test_parallel_permission_requests_each_get_their_own_answer():
    # Regression: three parallel WebSearch calls asked at once; the single pending slot
    # answered one and stranded the other two, and the worker waited 30 min.
    api = FakeApi()
    mailbox = FakeMailbox()
    worker = _make_worker(api=api, mailbox=mailbox)
    for i, rid in enumerate(("r1", "r2", "r3"), 1):
        mailbox.push(Frame(v=1, seq=i, job="jobA", type="permission_request", body={"request_id": rid, "tool": "WebSearch"}))
    assert worker.observe().lifecycle == "blocked"

    assert worker.answer_prompt(approve=True, request_id="r2").ok
    assert worker.answer_prompt(approve=True, request_id="r3").ok
    assert worker.observe().lifecycle == "blocked"  # r1 still waiting
    assert worker.answer_prompt(approve=False).ok  # oldest remaining = r1
    # a named answer always goes to the shim (the authority: it acks a duplicate as already delivered)
    assert worker.answer_prompt(approve=True, request_id="r2").ok
    assert [f.body["request_id"] for f in _sent_of_type(api, "approve")] == ["r2", "r3", "r2"]
    assert [f.body["request_id"] for f in _sent_of_type(api, "deny")] == ["r1"]
    assert worker.observe().lifecycle != "blocked"
