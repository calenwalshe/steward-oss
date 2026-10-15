"""`steward up` and the self-heal-into-a-steward-tab behavior (NB-04, NB-05,
SP-36 "defaults, not blockers"): from a Herdr session with no Steward panes,
`steward new` then `steward launch` just work — no write verb ever errors
"no control pane" while inside Herdr; it ensures one instead.

`_real_relay` (captured at import time, before conftest's autouse fixture
patches `cli._relay`) is used the same way tests/test_cli_relay.py uses it —
these tests exercise the real Herdr-based relay path with a fake `HerdrApi`.
"""

from __future__ import annotations

import json

from steward import cli, exceptions, launch
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

_real_relay = cli._relay


class _FakeHerdrApi:
    def __init__(self, *, panes=None):
        self.panes = panes if panes is not None else []
        self.calls: list[tuple[str, dict]] = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "pane.list":
            return {"panes": self.panes}
        return {}


def _tab_and_split_responder(*, control_pane="w3T:pctl", advisor_pane="w3T:padv", tab_id="w3T:tS"):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "tab", "create"]:
            payload = json.dumps(
                {"result": {"tab": {"tab_id": tab_id}, "root_pane": {"pane_id": control_pane}}}
            )
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=payload, stderr="", duration_s=0.0)
        if argv[:3] == ["herdr", "pane", "split"]:
            payload = json.dumps({"result": {"pane": {"pane_id": advisor_pane}}})
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=payload, stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    return respond


# ------------------------------------------------------------- steward up


def test_cmd_up_requires_sock(steward_home, monkeypatch, capsys):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)

    rc = cli.main(["up"])

    assert rc == 2
    assert "--sock" in capsys.readouterr().err


def test_cmd_up_requires_workspace(steward_home, monkeypatch, capsys):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    monkeypatch.delenv("HERDR_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)

    rc = cli.main(["up"])

    assert rc == 2
    assert "--workspace" in capsys.readouterr().err


def test_cmd_up_creates_a_steward_tab_with_control_and_advisor(steward_home, monkeypatch, capsys):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(steward_home / "cfg-does-not-exist"))
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: _FakeHerdrApi(panes=[]))
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: FakeRunner(_tab_and_split_responder()))

    rc = cli.main(["up", "--workspace", "w3T"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "steward tab: w3T:tS" in out
    assert "control pane started: w3T:pctl" in out
    assert "advisor pane started: w3T:padv" in out


def test_cmd_up_is_idempotent_when_both_already_live(steward_home, monkeypatch, capsys):
    panes = [
        {"pane_id": "w3T:pctl", "tokens": {"steward_role": "control"}},
        {"pane_id": "w3T:padv", "tokens": {"steward_role": "advisor"}},
    ]
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(steward_home / "cfg-does-not-exist"))
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: _FakeHerdrApi(panes=panes))
    monkeypatch.setattr(
        cli, "SubprocessRunner",
        lambda: FakeRunner(lambda argv, cwd: (_ for _ in ()).throw(AssertionError("must not create anything"))),
    )

    rc = cli.main(["up", "--workspace", "w3T"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "control pane already running: w3T:pctl" in out
    assert "advisor pane already running: w3T:padv" in out


def test_cmd_up_workspace_derived_from_pane_id_env(steward_home, monkeypatch, capsys):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(steward_home / "cfg-does-not-exist"))
    monkeypatch.delenv("HERDR_WORKSPACE_ID", raising=False)
    monkeypatch.setenv("HERDR_PANE_ID", "w3T:caller")
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: _FakeHerdrApi(panes=[]))
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: FakeRunner(_tab_and_split_responder()))

    rc = cli.main(["up"])

    assert rc == 0
    assert "control pane started: w3T:pctl" in capsys.readouterr().out


def test_cmd_up_syncs_exceptions_registry_additively(steward_home, monkeypatch, capsys):
    registry = steward_home / "exceptions.toml"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(exceptions._DEFAULT_HEADER + "\n" + exceptions._DEFAULT_SECTIONS["git"])
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(registry))
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    panes = [
        {"pane_id": "w3T:pctl", "tokens": {"steward_role": "control"}},
        {"pane_id": "w3T:padv", "tokens": {"steward_role": "advisor"}},
    ]
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: _FakeHerdrApi(panes=panes))
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: FakeRunner(lambda argv, cwd: CommandResult(
        argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0
    )))

    rc = cli.main(["up", "--workspace", "w3T"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "exceptions registry: added" in out
    remaining = set(exceptions._DEFAULT_SECTIONS) - {"git"}
    for name in remaining:
        assert name in out


def test_cmd_control_ensure_syncs_exceptions_registry(steward_home, monkeypatch, capsys):
    registry = steward_home / "exceptions.toml"
    registry.write_text(exceptions._DEFAULT_HEADER + "\n" + exceptions._DEFAULT_SECTIONS["git"])
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(registry))
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    panes = [
        {"pane_id": "w3T:pctl", "tokens": {"steward_role": "control"}},
        {"pane_id": "w3T:padv", "tokens": {"steward_role": "advisor"}},
    ]
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: _FakeHerdrApi(panes=panes))

    rc = cli.main(["control", "--ensure"])

    assert rc == 0
    assert "exceptions registry: added" in capsys.readouterr().out


def test_cmd_advisor_ensure_syncs_exceptions_registry(steward_home, monkeypatch, capsys):
    registry = steward_home / "exceptions.toml"
    registry.write_text(exceptions._DEFAULT_HEADER + "\n" + exceptions._DEFAULT_SECTIONS["git"])
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(registry))
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    panes = [{"pane_id": "w3T:padv", "tokens": {"steward_role": "advisor"}}]
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: _FakeHerdrApi(panes=panes))

    rc = cli.main(["advisor", "--ensure"])

    assert rc == 0
    assert "exceptions registry: added" in capsys.readouterr().out


# --------------------------------------------------- self-heal in `_relay`


def test_relay_self_heals_missing_control_pane_when_workspace_is_known(steward_home, monkeypatch, capsys):
    """NB-04: a write verb inside Herdr (sock + workspace known) never
    errors "no control pane" — it calls `launch.ensure_steward_up` and
    retries the same cmd."""
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    store.set_state(job.job_id, JobState.CONTRACTED, what="c", evidence={}, rule="r", judgment=Judgment.code)
    store.set_state(job.job_id, JobState.RUNNING, what="r", evidence={}, rule="r", judgment=Judgment.code)
    monkeypatch.setattr(cli, "_relay", _real_relay)
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w3T")

    fake = _FakeHerdrApi(panes=[])

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "tab", "create"]:
            payload = json.dumps(
                {"result": {"tab": {"tab_id": "w3T:tS"}, "root_pane": {"pane_id": "w3T:pctl"}}}
            )
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=payload, stderr="", duration_s=0.0)
        if argv[:3] == ["herdr", "pane", "split"]:
            payload = json.dumps({"result": {"pane": {"pane_id": "w3T:padv"}}})
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=payload, stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    monkeypatch.setattr(cli, "SubprocessRunner", lambda: FakeRunner(respond))

    def fake_herdr_api(sock):
        # `ensure_steward_up`'s freshly-started control pane immediately
        # "reports" its role, so the retried relay finds it.
        return fake

    original_start = launch._start_control_on

    def start_and_register(runner_exec, sock, pane_id):
        original_start(runner_exec, sock, pane_id)
        fake.panes.append({"pane_id": pane_id, "tokens": {"steward_role": "control"}})

    monkeypatch.setattr(launch, "_start_control_on", start_and_register)
    monkeypatch.setattr(cli, "HerdrApi", fake_herdr_api)

    # The retried relay needs a receipt to poll for — a real control pane
    # would execute the cmd and store one; here the fake's `send()` doesn't
    # run anything, so seed the receipt directly once the pane exists, via
    # a thin FakeHerdrApi.send that mirrors test_cli_relay.FakeHerdrApi.
    from steward.control_pane import ControlPane

    handlers = cli._build_control_handlers(store)
    pane = ControlPane(store, handlers, print_fn=lambda _line: None)

    def send(pane_id, frame):
        if frame.type == "cmd":
            pane.execute(frame.body)
        return {"type": "ok"}

    fake.send = send

    rc = cli.main(["pause", job.job_id])

    assert rc == 0
    assert "accepted=True" in capsys.readouterr().out
    assert store.get_job(job.job_id).state.value == "PAUSED"


def test_relay_keeps_old_error_when_workspace_unknown(steward_home, monkeypatch, capsys):
    """Outside Herdr (no workspace to create a "steward" tab in), the
    original error text is unchanged."""
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    monkeypatch.setattr(cli, "_relay", _real_relay)
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/x.sock")
    monkeypatch.delenv("HERDR_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: _FakeHerdrApi(panes=[]))

    rc = cli.main(["pause", job.job_id])

    assert rc == 4
    assert "no control pane" in capsys.readouterr().err
