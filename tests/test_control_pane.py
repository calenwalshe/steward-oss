import pytest

from steward.control_pane import (
    ControlPane,
    ControlTimeout,
    NoControlPane,
    find_control_pane,
    get_receipt,
    relay,
    send_cmd,
)
from steward.fabric import Frame
from steward.herdr_transport import HerdrApiError, HerdrUnavailable
from steward.store import Store


class FakeHerdrApi:
    def __init__(self, *, panes=None, call_raises=None, send_raises=None):
        self.sent: list[tuple[str, Frame]] = []
        self.calls: list[tuple[str, dict]] = []
        self.panes = panes if panes is not None else []
        self.call_raises = call_raises
        self.send_raises = send_raises

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if self.call_raises is not None:
            raise self.call_raises
        if method == "pane.list":
            return {"panes": self.panes}
        return {}

    def send(self, pane_id: str, frame: Frame) -> dict:
        if self.send_raises is not None:
            raise self.send_raises
        self.sent.append((pane_id, frame))
        return {"type": "ok"}


class FakeMailbox:
    def __init__(self, frames: list[Frame] | None = None):
        self._queue = list(frames or [])

    def push(self, frame: Frame) -> None:
        self._queue.append(frame)

    def read(self, timeout: float) -> list[Frame]:
        drained, self._queue = self._queue, []
        return drained


def _cmd(op: str, args: dict, *, request_id: str = "req-1", origin: str = "cli", reply_to: str | None = None) -> dict:
    body = {"request_id": request_id, "op": op, "args": args, "origin": origin}
    if reply_to:
        body["reply_to"] = reply_to
    return body


# ------------------------------------------------------------- ControlPane


def test_execute_dispatches_to_handler_and_records_receipt(steward_home):
    store = Store(steward_home)
    calls = []

    def handler(cmd):
        calls.append(cmd)
        return {"accepted": True, "detail": "did it"}

    pane = ControlPane(store, {"pause": handler}, print_fn=lambda _line: None)
    result = pane.execute(_cmd("pause", {"job_id": "j1"}))

    assert result["accepted"] is True
    assert result["detail"] == "did it"
    assert result["duplicate"] is False
    assert len(calls) == 1
    assert get_receipt(store, "req-1") == result


def test_execute_unknown_op_is_not_accepted(steward_home):
    store = Store(steward_home)
    pane = ControlPane(store, {}, print_fn=lambda _line: None)

    result = pane.execute(_cmd("teleport", {}))

    assert result["accepted"] is False
    assert "unknown control op" in result["detail"]


def test_execute_duplicate_request_id_never_recalls_handler(steward_home):
    store = Store(steward_home)
    calls = []

    def handler(cmd):
        calls.append(cmd)
        return {"accepted": True, "detail": "did it"}

    pane = ControlPane(store, {"pause": handler}, print_fn=lambda _line: None)
    first = pane.execute(_cmd("pause", {"job_id": "j1"}))
    second = pane.execute(_cmd("pause", {"job_id": "j1"}))

    assert len(calls) == 1
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["detail"] == first["detail"]


def test_execute_handler_exception_is_recorded_not_raised(steward_home):
    store = Store(steward_home)

    def handler(cmd):
        raise RuntimeError("boom")

    pane = ControlPane(store, {"pause": handler}, print_fn=lambda _line: None)
    result = pane.execute(_cmd("pause", {"job_id": "j1"}))

    assert result["accepted"] is False
    assert "boom" in result["detail"]


def test_step_processes_cmd_frames_and_ignores_other_types(steward_home):
    store = Store(steward_home)
    handled = []
    pane = ControlPane(
        store,
        {"pause": lambda cmd: handled.append(cmd) or {"accepted": True, "detail": "ok"}},
        print_fn=lambda _line: None,
    )
    mailbox = FakeMailbox(
        [
            Frame(v=1, seq=1, job="req-1", type="cmd", body=_cmd("pause", {"job_id": "j1"})),
            Frame(v=1, seq=2, job="other", type="ack", body={"acked_seq": 1}),
        ]
    )
    api = FakeHerdrApi()

    results = pane.step(mailbox, api)

    assert len(results) == 1
    assert len(handled) == 1
    assert api.sent == []  # no reply_to on this cmd


def test_step_sends_reply_only_when_reply_to_given(steward_home):
    store = Store(steward_home)
    pane = ControlPane(store, {"pause": lambda cmd: {"accepted": True, "detail": "ok"}}, print_fn=lambda _line: None)
    mailbox = FakeMailbox(
        [Frame(v=1, seq=1, job="req-1", type="cmd", body=_cmd("pause", {"job_id": "j1"}, reply_to="w1:p9"))]
    )
    api = FakeHerdrApi()

    pane.step(mailbox, api)

    assert len(api.sent) == 1
    reply_pane, reply_frame = api.sent[0]
    assert reply_pane == "w1:p9"
    assert reply_frame.type == "reply"
    assert reply_frame.body["request_id"] == "req-1"


def test_step_swallows_reply_send_failure(steward_home):
    store = Store(steward_home)
    pane = ControlPane(store, {"pause": lambda cmd: {"accepted": True, "detail": "ok"}}, print_fn=lambda _line: None)
    mailbox = FakeMailbox(
        [Frame(v=1, seq=1, job="req-1", type="cmd", body=_cmd("pause", {"job_id": "j1"}, reply_to="w1:p9"))]
    )
    api = FakeHerdrApi(send_raises=HerdrApiError("not_found", "gone"))

    pane.step(mailbox, api)  # must not raise


def test_run_forever_reports_control_role_once_then_steps_until_not_running(steward_home):
    store = Store(steward_home)
    handled = []
    pane = ControlPane(
        store,
        {"pause": lambda cmd: handled.append(cmd) or {"accepted": True, "detail": "ok"}},
        print_fn=lambda _line: None,
    )
    mailbox = FakeMailbox([Frame(v=1, seq=1, job="req-1", type="cmd", body=_cmd("pause", {"job_id": "j1"}))])
    api = FakeHerdrApi()
    ticks = {"n": 0}

    def running():
        ticks["n"] += 1
        return ticks["n"] <= 1  # exactly one step() call, then stop

    pane.run_forever(mailbox, api, own_pane="w1:control", poll_s=0.0, running=running)

    assert len(handled) == 1
    report_calls = [c for c in api.calls if c[0] == "pane.report_metadata"]
    assert report_calls == [
        (
            "pane.report_metadata",
            {
                "pane_id": "w1:control", "source": "steward",
                "tokens": {"steward_role": "control"}, "title": "steward control",
            },
        )
    ]


def test_run_forever_skips_report_when_no_own_pane_given(steward_home):
    store = Store(steward_home)
    pane = ControlPane(store, {}, print_fn=lambda _line: None)
    mailbox = FakeMailbox()
    api = FakeHerdrApi()

    pane.run_forever(mailbox, api, own_pane=None, poll_s=0.0, running=lambda: False)

    assert api.calls == []


# --------------------------------------------------------- find_control_pane


def test_find_control_pane_returns_matching_pane_id():
    api = FakeHerdrApi(
        panes=[
            {"pane_id": "w1:p1", "tokens": {}},
            {"pane_id": "w1:p2", "tokens": {"steward_role": "control"}},
        ]
    )
    assert find_control_pane(api) == "w1:p2"


def test_find_control_pane_returns_none_when_no_match():
    api = FakeHerdrApi(panes=[{"pane_id": "w1:p1", "tokens": {}}])
    assert find_control_pane(api) is None


def test_find_control_pane_returns_none_when_herdr_unreachable():
    api = FakeHerdrApi(call_raises=HerdrUnavailable("no socket"))
    assert find_control_pane(api) is None


# ----------------------------------------------------------------- send_cmd


def test_send_cmd_uses_request_id_as_frame_job_for_uniqueness():
    api = FakeHerdrApi()
    send_cmd(api, "w1:p2", "req-xyz", "pause", {"job_id": "j1"}, "cli")

    assert len(api.sent) == 1
    pane_id, frame = api.sent[0]
    assert pane_id == "w1:p2"
    assert frame.type == "cmd"
    assert frame.job == "req-xyz"
    assert frame.body == {"request_id": "req-xyz", "op": "pause", "args": {"job_id": "j1"}, "origin": "cli"}


def test_send_cmd_includes_reply_to_when_given():
    api = FakeHerdrApi()
    send_cmd(api, "w1:p2", "req-xyz", "pause", {"job_id": "j1"}, "cli", reply_to="w1:p9")

    _, frame = api.sent[0]
    assert frame.body["reply_to"] == "w1:p9"


# -------------------------------------------------------------------- relay


def _in_process_control_api(store: Store, handlers: dict) -> FakeHerdrApi:
    """A `FakeHerdrApi` whose `send()` synchronously feeds the frame to an
    in-process `ControlPane` (11-01-PLAN: "unit tests use a fake HerdrApi
    and an in-process control loop") — so `relay()`'s poll loop finds the
    receipt on its very first check, no real waiting needed."""
    pane = ControlPane(store, handlers, print_fn=lambda _line: None)

    class SyncApi(FakeHerdrApi):
        def send(self, pane_id, frame):
            super().send(pane_id, frame)
            pane.execute(frame.body)
            return {"type": "ok"}

    return SyncApi(panes=[{"pane_id": "w1:control", "tokens": {"steward_role": "control"}}])


def test_relay_happy_path_returns_receipt(steward_home):
    store = Store(steward_home)
    api = _in_process_control_api(store, {"pause": lambda cmd: {"accepted": True, "detail": "paused"}})

    result = relay(store, api, "pause", {"job_id": "j1"}, request_id="req-1", timeout_s=1.0, poll_s=0.01)

    assert result["accepted"] is True
    assert result["detail"] == "paused"


def test_relay_raises_no_control_pane_when_none_found(steward_home):
    store = Store(steward_home)
    api = FakeHerdrApi(panes=[])

    with pytest.raises(NoControlPane):
        relay(store, api, "pause", {"job_id": "j1"}, timeout_s=0.05, poll_s=0.01)


def test_relay_raises_no_control_pane_when_send_fails(steward_home):
    store = Store(steward_home)
    api = FakeHerdrApi(
        panes=[{"pane_id": "w1:control", "tokens": {"steward_role": "control"}}],
        send_raises=HerdrUnavailable("no socket"),
    )

    with pytest.raises(NoControlPane):
        relay(store, api, "pause", {"job_id": "j1"}, timeout_s=0.05, poll_s=0.01)


def test_relay_raises_timeout_when_no_receipt_ever_appears(steward_home):
    store = Store(steward_home)
    # A plain FakeHerdrApi's send() never actually executes anything, so no
    # receipt will ever land in the store — a fake clock makes the timeout
    # deterministic and instant instead of a real 30s wait.
    api = FakeHerdrApi(panes=[{"pane_id": "w1:control", "tokens": {"steward_role": "control"}}])
    fake_now = {"t": 0.0}

    def clock():
        return fake_now["t"]

    def fake_sleep(dt):
        fake_now["t"] += dt

    with pytest.raises(ControlTimeout):
        relay(
            store, api, "pause", {"job_id": "j1"}, timeout_s=1.0, poll_s=0.1,
            clock=clock, sleep=fake_sleep,
        )


def test_relay_is_idempotent_by_request_id_via_control_pane(steward_home):
    """The control-pane-level dedupe (CORE-04) applies end to end: two
    `relay()` calls with the same request_id never re-run the handler, and
    both callers see the identical stored result (the DB receipt itself
    only ever records the first execution — a second `relay()` call reads
    the exact same row, it never rewrites `duplicate` into it)."""
    store = Store(steward_home)
    calls = []
    api = _in_process_control_api(
        store, {"pause": lambda cmd: calls.append(cmd) or {"accepted": True, "detail": "paused"}}
    )

    first = relay(store, api, "pause", {"job_id": "j1"}, request_id="dup-1", timeout_s=1.0, poll_s=0.01)
    second = relay(store, api, "pause", {"job_id": "j1"}, request_id="dup-1", timeout_s=1.0, poll_s=0.01)

    assert len(calls) == 1
    assert second == first


def test_execute_ignores_cmd_sent_for_a_different_steward_home(steward_home, tmp_path):
    store = Store(steward_home)
    calls = []
    pane = ControlPane(store, {"pause": lambda cmd: calls.append(cmd) or {"accepted": True}}, print_fn=lambda _line: None)
    result = pane.execute({"request_id": "r-other", "op": "pause", "args": {}, "origin": "cli", "home": str(tmp_path / "scratch")})
    assert result["accepted"] is False and "ignored" in result["detail"]
    assert calls == [] and get_receipt(store, "r-other") is None  # nothing written to this store
    # same home (or none, for older senders) still executes
    assert pane.execute({"request_id": "r-same", "op": "pause", "args": {}, "origin": "cli", "home": str(steward_home)})["accepted"]
    assert pane.execute({"request_id": "r-old", "op": "pause", "args": {}, "origin": "cli"})["accepted"]
