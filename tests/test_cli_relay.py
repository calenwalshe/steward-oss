"""Tests for the CLI's control-pane relay (OPX-01/02).

tests/conftest.py's autouse `_inprocess_control_relay` fixture replaces
`cli._relay` with an in-process fake for the rest of the suite (so existing
CLI tests don't need a real Herdr control pane); these tests exercise the
REAL relay path instead, via a fake `HerdrApi` (11-01-PLAN: "unit tests use
a fake HerdrApi and an in-process control loop").

`_real_relay` is captured here at import time — before any per-test fixture
has a chance to monkeypatch `cli._relay` — so it always refers to the
genuine Herdr-based implementation regardless of fixture ordering.
"""

from __future__ import annotations

import pytest

from steward import cli, control_pane
from steward.control_pane import ControlPane
from steward.fabric import Frame
from steward.models import JobState, Judgment
from steward.store import Store

_real_relay = cli._relay


def _running_job(store: Store, goal: str = "goal", repo: str = "/repo"):
    job = store.create_job(goal, repo)
    store.set_state(job.job_id, JobState.CONTRACTED, what="c", evidence={}, rule="r", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code)
    return job


class FakeHerdrApi:
    """Mirrors tests/test_control_pane.py's fake, plus a `send()` that
    synchronously feeds the frame to an in-process `ControlPane` — the
    "fake HerdrApi and an in-process control loop" the plan calls for."""

    def __init__(self, *, store: Store | None = None, handlers: dict | None = None, panes=None, send_raises=None):
        self.sent: list[tuple[str, Frame]] = []
        self.calls: list[tuple[str, dict]] = []
        self.panes = panes if panes is not None else [{"pane_id": "w1:control", "tokens": {"steward_role": "control"}}]
        self.send_raises = send_raises
        self._pane = None
        if store is not None:
            self._pane = ControlPane(store, handlers or {}, print_fn=lambda _line: None)

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "pane.list":
            return {"panes": self.panes}
        return {}

    def send(self, pane_id: str, frame: Frame) -> dict:
        if self.send_raises is not None:
            raise self.send_raises
        self.sent.append((pane_id, frame))
        if frame.type == "cmd" and self._pane is not None:
            self._pane.execute(frame.body)
        return {"type": "ok"}


def _patch_herdr_api(monkeypatch, fake: FakeHerdrApi) -> None:
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: fake)
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/fake.sock")


def test_write_verb_never_constructs_stewardapi_from_cli_process(steward_home, monkeypatch, capsys):
    """The CLI process's own dispatch code must never touch `StewardAPI`
    directly for a write verb — only `_build_control_handlers` (called by
    the control pane, never by `main()`) does. Proven by making `StewardAPI`
    raise if constructed, while `_relay` is a canned fake that never touches
    it either — if `main()`'s pause branch tried to build one itself before
    calling `_relay`, this would fail."""
    store = Store(steward_home)
    job = _running_job(store)

    def exploding_stewardapi(*args, **kwargs):
        raise AssertionError("StewardAPI must never be constructed by the CLI process directly")

    monkeypatch.setattr(cli, "StewardAPI", exploding_stewardapi)
    monkeypatch.setattr(
        cli, "_relay",
        lambda store, op, args, **kw: {
            "request_id": "r1", "job_id": args.get("job_id"), "kind": op,
            "accepted": True, "duplicate": False, "detail": "ok", "revision": 0,
        },
    )

    rc = cli.main(["pause", job.job_id])

    assert rc == 0
    assert "accepted=True" in capsys.readouterr().out


def test_real_relay_pause_round_trips_through_fake_control_pane(steward_home):
    """`control_pane.relay` is the guts of `cli._relay` (once it has a
    `HerdrApi`); exercised directly here with a fake socket API and an
    in-process `ControlPane`, no `cli.main` involved."""
    store = Store(steward_home)
    job = _running_job(store)
    handlers = cli._build_control_handlers(store)
    fake = FakeHerdrApi(store=store, handlers=handlers)

    receipt = control_pane.relay(store, fake, "pause", {"job_id": job.job_id}, timeout_s=1.0, poll_s=0.01)

    assert receipt["accepted"] is True
    assert store.get_job(job.job_id).state.value == "PAUSED"


def test_cli_relay_uses_real_herdr_api_and_control_pane_end_to_end(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = _running_job(store)
    handlers = cli._build_control_handlers(store)
    fake = FakeHerdrApi(store=store, handlers=handlers)
    monkeypatch.setattr(cli, "_relay", _real_relay)
    _patch_herdr_api(monkeypatch, fake)

    rc = cli.main(["pause", job.job_id])

    assert rc == 0
    assert "accepted=True" in capsys.readouterr().out
    assert store.get_job(job.job_id).state.value == "PAUSED"
    # The cmd frame really was sent into the control pane (SP-31 visibility).
    cmd_frames = [f for _, f in fake.sent if f.type == "cmd"]
    assert len(cmd_frames) == 1
    assert cmd_frames[0].body["op"] == "pause"


def test_no_control_pane_when_env_missing_exits_4(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    monkeypatch.setattr(cli, "_relay", _real_relay)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)

    rc = cli.main(["pause", job.job_id])

    assert rc == 4
    assert "no control pane" in capsys.readouterr().err


def test_no_control_pane_when_none_found_exits_4(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    fake = FakeHerdrApi(panes=[])  # no pane carries the control token
    monkeypatch.setattr(cli, "_relay", _real_relay)
    _patch_herdr_api(monkeypatch, fake)

    rc = cli.main(["pause", job.job_id])

    assert rc == 4
    assert "no control pane" in capsys.readouterr().err


def test_control_timeout_exits_5(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    # A control pane is found, but its `send` never actually executes
    # anything, so no receipt will ever land — `_relay`'s real default
    # timeout is 30s, far too slow for a unit test, so call the underlying
    # `control_pane.relay` with a fake clock instead of through `cli.main`.
    fake = FakeHerdrApi()
    fake_now = {"t": 0.0}
    with pytest.raises(control_pane.ControlTimeout):
        control_pane.relay(
            store, fake, "pause", {"job_id": job.job_id}, timeout_s=1.0, poll_s=0.1,
            clock=lambda: fake_now["t"], sleep=lambda dt: fake_now.__setitem__("t", fake_now["t"] + dt),
        )


def test_say_relays_and_prints_receipt(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    job = _running_job(store)
    handlers = cli._build_control_handlers(store)
    fake = FakeHerdrApi(store=store, handlers=handlers)
    monkeypatch.setattr(cli, "_relay", _real_relay)
    _patch_herdr_api(monkeypatch, fake)

    rc = cli.main(["say", f"pause {job.job_id}"])

    assert rc == 0
    assert "accepted=True" in capsys.readouterr().out
    assert store.get_job(job.job_id).state.value == "PAUSED"


def test_say_unsupported_command_errors(steward_home, capsys):
    rc = cli.main(["say", "run some-job"])

    assert rc == 2
    assert "doesn't relay" in capsys.readouterr().err


def test_say_empty_command_errors(steward_home, capsys):
    rc = cli.main(["say", "   "])

    assert rc == 2


def test_cmd_control_requires_sock(steward_home, monkeypatch, capsys):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)

    rc = cli.main(["control"])

    assert rc == 2
    assert "--sock" in capsys.readouterr().err


def test_cmd_control_requires_pane(steward_home, monkeypatch, capsys):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)

    rc = cli.main(["control", "--sock", "/tmp/fake.sock"])

    assert rc == 2
    assert "--pane" in capsys.readouterr().err


def test_cmd_control_ensure_noop_when_one_already_live(steward_home, monkeypatch, capsys):
    fake = FakeHerdrApi(panes=[{"pane_id": "w1:existing", "tokens": {"steward_role": "control"}}])
    _patch_herdr_api(monkeypatch, fake)

    rc = cli.main(["control", "--ensure"])

    assert rc == 0
    assert "w1:existing" in capsys.readouterr().out
    assert fake.sent == []  # never split a second one


def test_cmd_control_ensure_creates_a_steward_tab_when_none_live(steward_home, monkeypatch, capsys):
    """NB-04 (SP-36): `control --ensure` now goes through
    `launch.ensure_steward_up` (workspace resolved from `$HERDR_PANE_ID`
    when no `$HERDR_WORKSPACE_ID` is set) — a brand-new control pane lands
    in its own dedicated "steward" tab, not split off the caller's pane."""
    import json as _json

    from steward.runner_exec import CommandResult, FakeRunner

    fake = FakeHerdrApi(panes=[])
    _patch_herdr_api(monkeypatch, fake)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:caller")

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "tab", "create"]:
            assert "--workspace" in argv and "w1" in argv
            payload = _json.dumps(
                {"result": {"tab": {"tab_id": "w1:tS"}, "root_pane": {"pane_id": "w1:newctl"}}}
            )
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=payload, stderr="", duration_s=0.0)
        if argv[:3] == ["herdr", "pane", "split"]:
            # `ensure_steward_up` also ensures the advisor pane (default
            # trajectory mode is not off) off the freshly-created control pane.
            payload = _json.dumps({"result": {"pane": {"pane_id": "w1:newadv"}}})
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=payload, stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    monkeypatch.setattr(cli, "SubprocessRunner", lambda: FakeRunner(respond))

    rc = cli.main(["control", "--ensure"])

    assert rc == 0
    assert "w1:newctl" in capsys.readouterr().out


def test_relay_waits_for_a_freshly_started_control_pane_to_register(steward_home, monkeypatch):
    """Live bug (2026-09-28): after `ensure_steward_up` starts a control pane,
    its `steward_role` token takes ~1-3 s to appear, so the immediate retry
    still saw NoControlPane. The retry must poll until the pane registers."""
    calls = {"relay": 0, "slept": []}

    def fake_relay(*a, **kw):
        calls["relay"] += 1
        if calls["relay"] <= 3:
            raise control_pane.NoControlPane("no control pane — start one: `steward control`")
        return {"ok": True}

    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/fake.sock")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w1")
    monkeypatch.setattr(cli.control_pane, "relay", fake_relay)
    monkeypatch.setattr(cli.launch, "ensure_steward_up", lambda *a, **kw: {"control_pane": "w1:new", "created": ["control"]})
    monkeypatch.setattr(cli, "_relay_sleep", lambda s: calls["slept"].append(s))

    from steward.store import Store

    assert _real_relay(Store(), "new", {"goal": "g"}) == {"ok": True}
    assert calls["relay"] == 4
    assert calls["slept"]  # it waited between polls


def test_relay_gives_up_after_the_warmup_window(steward_home, monkeypatch):
    def always_missing(*a, **kw):
        raise control_pane.NoControlPane("no control pane — start one: `steward control`")

    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/fake.sock")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w1")
    monkeypatch.setattr(cli.control_pane, "relay", always_missing)
    monkeypatch.setattr(cli.launch, "ensure_steward_up", lambda *a, **kw: {"control_pane": "w1:new", "created": ["control"]})
    monkeypatch.setattr(cli, "_relay_sleep", lambda s: None)

    from steward.store import Store

    with pytest.raises(cli._RelayFailed):
        _real_relay(Store(), "new", {"goal": "g"})
