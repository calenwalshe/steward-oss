"""The control pane's tick hook (Phase 17-03, G7): the answer flusher runs on the control loop."""

from __future__ import annotations

from steward import cli, control_pane, questions_delivery
from steward.control_pane import ControlPane
from steward.store import Store


class _Mailbox:
    def read(self, timeout):
        return []


class _Api:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return {}


def _run(pane, times, tick, *, steps=None, every=None):
    """Run the loop for len(times) steps with a scripted monotonic clock."""
    it = iter(times)
    remaining = {"n": steps if steps is not None else len(times)}

    def running():
        remaining["n"] -= 1
        return remaining["n"] >= 0

    kw = {} if every is None else {"tick_every_s": every}
    pane.run_forever(_Mailbox(), _Api(), own_pane=None, poll_s=0.0, running=running, tick=tick,
                     clock=lambda: next(it), **kw)


def test_run_forever_calls_tick_on_its_schedule(steward_home):
    printed = []
    pane = ControlPane(Store(steward_home), {}, print_fn=printed.append)
    calls = []
    _run(pane, [0.0, 3.0, 6.0, 6.5, 11.0], lambda: calls.append(1))
    assert len(calls) == 3  # at 0, 6 and 11: never twice inside tick_every_s (5.0), and with no cmd frames at all
    calls.clear()
    _run(pane, [0.0, 1.0, 2.0, 3.0], lambda: calls.append(1), every=2.0)
    assert len(calls) == 2
    assert printed == []


def test_no_tick_means_the_old_loop(steward_home):
    pane = ControlPane(Store(steward_home), {}, print_fn=lambda line: None)
    pane.run_forever(_Mailbox(), _Api(), own_pane=None, poll_s=0.0, running=iter([True, True, False]).__next__)


def test_a_failing_tick_is_reported_and_never_ends_the_loop(steward_home):
    printed = []
    pane = ControlPane(Store(steward_home), {}, print_fn=printed.append)
    seen = {"steps": 0, "ticks": 0}

    def boom():
        seen["ticks"] += 1
        raise RuntimeError("boom")

    class Mailbox:
        def read(self, timeout):
            seen["steps"] += 1
            return []

    times = iter([0.0, 10.0, 20.0])
    left = {"n": 3}

    def running():
        left["n"] -= 1
        return left["n"] >= 0

    pane.run_forever(Mailbox(), _Api(), own_pane=None, poll_s=0.0, running=running, tick=boom,
                     clock=lambda: next(times))
    assert seen == {"steps": 3, "ticks": 3}
    assert printed == ["tick error: boom"] * 3


def test_control_command_wires_the_tick(steward_home, monkeypatch):
    captured = {}

    class Mailbox:
        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(cli, "Mailbox", Mailbox)
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: object())
    monkeypatch.setattr(control_pane.ControlPane, "run_forever",
                        lambda self, mailbox, api, **kw: captured.update(kw))
    calls = []
    monkeypatch.setattr(questions_delivery, "tick", lambda store, runner, **kw: calls.append((store, kw)) or {})
    rc = cli.main(["control", "--sock", "/tmp/none.sock", "--pane", "w1:p1"])
    assert rc == 0 and captured["closed"]
    tick = captured["tick"]
    assert callable(tick)
    tick()
    assert len(calls) == 1 and "notifier" in calls[0][1]


def test_the_control_tick_refreshes_tab_labels_before_the_sweep(steward_home, monkeypatch):
    from steward import herdr_tabs

    captured, order = {}, []

    class Mailbox:
        def close(self):
            pass

    monkeypatch.setattr(cli, "Mailbox", Mailbox)
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: "the-api")
    monkeypatch.setattr(control_pane.ControlPane, "run_forever", lambda self, mailbox, api, **kw: captured.update(kw))
    monkeypatch.setattr(questions_delivery, "tick", lambda store, runner, **kw: order.append("questions") or {})
    monkeypatch.setattr(herdr_tabs, "refresh", lambda store, runner, api, **kw: order.append(("refresh", api)) or {})
    monkeypatch.setattr(herdr_tabs, "sweep", lambda store, runner, **kw: order.append("sweep") or [])
    assert cli.main(["control", "--sock", "/tmp/none.sock", "--pane", "w1:p1"]) == 0
    captured["tick"]()
    assert order == ["questions", ("refresh", "the-api"), "sweep"]
