"""`steward run --trajectory/--trajectory-config`, `steward trajectory
status|mode|off`, and the `explain-state` trajectory line (TRJ-11/12).
`tests/conftest.py`'s autouse `_inprocess_control_relay` fixture gives every
`cli.main([...])` write verb (including `trajectory mode`/`off`) the real
`_build_control_handlers` dispatch table without a real Herdr socket.
"""

from __future__ import annotations

import argparse
import json

from steward import cli
from steward import trajectory as trajectory_module
from steward.models import Judgment
from steward.runner_exec import SubprocessRunner
from steward.store import Store


def _isolate_config(monkeypatch, steward_home):
    """`load_trajectory_config()`'s default path is `~/.config/steward/
    trajectory.toml`; point it at a directory that never exists so these
    tests never depend on (or race) a real operator config file."""
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(steward_home / "no-such-config-dir"))


# ------------------------------------------------------- _build_trajectory_supervisor


def test_build_trajectory_supervisor_defaults_to_config_mode(steward_home, monkeypatch):
    _isolate_config(monkeypatch, steward_home)
    store = Store(steward_home)
    args = argparse.Namespace(trajectory=None, trajectory_config=None, sock=None)

    supervisor = cli._build_trajectory_supervisor(store, args, SubprocessRunner())

    assert supervisor._current_cfg().mode == "shadow"  # TrajectoryConfig()'s own default


def test_build_trajectory_supervisor_flag_overrides_mode(steward_home, monkeypatch):
    _isolate_config(monkeypatch, steward_home)
    store = Store(steward_home)
    args = argparse.Namespace(trajectory="active", trajectory_config=None, sock=None)

    supervisor = cli._build_trajectory_supervisor(store, args, SubprocessRunner())

    assert supervisor._current_cfg().mode == "active"


def test_build_trajectory_supervisor_reads_trajectory_config_path(steward_home, tmp_path):
    path = tmp_path / "custom-trajectory.toml"
    path.write_text('mode = "active"\nmax_reviews_per_job = 3\n')
    store = Store(steward_home)
    args = argparse.Namespace(trajectory=None, trajectory_config=str(path), sock=None)

    supervisor = cli._build_trajectory_supervisor(store, args, SubprocessRunner())

    cfg = supervisor._current_cfg()
    assert cfg.mode == "active"
    assert cfg.max_reviews_per_job == 3


def test_build_trajectory_supervisor_no_sock_uses_no_advisor_link(steward_home, monkeypatch):
    _isolate_config(monkeypatch, steward_home)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    store = Store(steward_home)
    args = argparse.Namespace(trajectory=None, trajectory_config=None, sock=None)

    supervisor = cli._build_trajectory_supervisor(store, args, SubprocessRunner())

    assert isinstance(supervisor.advisor_link, trajectory_module.NoAdvisorLink)
    assert supervisor.advisor_link.find() is None


def test_build_trajectory_supervisor_with_sock_uses_herdr_advisor_link(steward_home, monkeypatch):
    _isolate_config(monkeypatch, steward_home)
    store = Store(steward_home)
    args = argparse.Namespace(trajectory=None, trajectory_config=None, sock="/tmp/x.sock")

    supervisor = cli._build_trajectory_supervisor(store, args, SubprocessRunner())

    # NB-05 (SP-36): a real socket wraps the production link in
    # `_SelfHealingAdvisorLink` so a missing advisor pane gets restored
    # instead of staying absent for the rest of the run.
    assert isinstance(supervisor.advisor_link, cli._SelfHealingAdvisorLink)
    assert isinstance(supervisor.advisor_link._inner, trajectory_module.HerdrAdvisorLink)


# ------------------------------------------------------- _SelfHealingAdvisorLink (NB-05)


class _FakeInnerLink:
    def __init__(self, found=None):
        self._found = found
        self.find_calls = 0

    def find(self):
        self.find_calls += 1
        return self._found

    def request(self, pane_id, job_id, review_id):
        pass


class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_self_healing_advisor_link_passes_through_when_pane_found(steward_home):
    store = Store(steward_home)
    inner = _FakeInnerLink(found="w1:adv")
    link = cli._SelfHealingAdvisorLink(inner, store, "job-1", object(), SubprocessRunner(), "/tmp/x.sock", "w1")

    assert link.find() == "w1:adv"


def test_self_healing_advisor_link_self_heals_and_records_event(steward_home, monkeypatch):
    from steward import launch

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    inner = _FakeInnerLink(found=None)
    clock = _Clock(1000.0)

    monkeypatch.setattr(
        launch, "ensure_steward_up",
        lambda *a, **k: {"control_pane": "w1:ctl", "advisor_pane": "w1:adv", "created": ["advisor"]},
    )

    link = cli._SelfHealingAdvisorLink(
        inner, store, job.job_id, object(), SubprocessRunner(), "/tmp/x.sock", "w1", clock=clock
    )

    result = link.find()

    assert result == "w1:adv"
    events = [e for e in store.events(job.job_id) if e.what == "steward_panes_restored"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.code
    assert events[0].evidence["advisor_pane"] == "w1:adv"


def test_self_healing_advisor_link_is_debounced(steward_home, monkeypatch):
    from steward import launch

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    inner = _FakeInnerLink(found=None)
    clock = _Clock(1000.0)
    calls = []
    monkeypatch.setattr(
        launch, "ensure_steward_up",
        lambda *a, **k: calls.append(1) or {"control_pane": "w1:ctl", "advisor_pane": None, "created": []},
    )

    link = cli._SelfHealingAdvisorLink(
        inner, store, job.job_id, object(), SubprocessRunner(), "/tmp/x.sock", "w1", clock=clock
    )

    assert link.find() is None
    clock.t += 10  # well within the 5-minute debounce window
    assert link.find() is None

    assert len(calls) == 1  # second call was debounced, not retried


def test_self_healing_advisor_link_retries_after_debounce_window(steward_home, monkeypatch):
    from steward import launch

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    inner = _FakeInnerLink(found=None)
    clock = _Clock(1000.0)
    calls = []
    monkeypatch.setattr(
        launch, "ensure_steward_up",
        lambda *a, **k: calls.append(1) or {"control_pane": "w1:ctl", "advisor_pane": None, "created": []},
    )

    link = cli._SelfHealingAdvisorLink(
        inner, store, job.job_id, object(), SubprocessRunner(), "/tmp/x.sock", "w1", clock=clock
    )

    assert link.find() is None
    clock.t += 301  # past the 5-minute debounce window
    assert link.find() is None

    assert len(calls) == 2


def test_self_healing_advisor_link_skips_self_heal_without_workspace(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    inner = _FakeInnerLink(found=None)

    link = cli._SelfHealingAdvisorLink(inner, store, job.job_id, object(), SubprocessRunner(), "/tmp/x.sock", None)

    assert link.find() is None
    assert store.events(job.job_id) == []


def test_run_parser_accepts_trajectory_flags():
    parser = cli._build_parser()
    ns = parser.parse_args(["run", "job-1", "--trajectory", "active", "--trajectory-config", "/tmp/t.toml"])
    assert ns.trajectory == "active"
    assert ns.trajectory_config == "/tmp/t.toml"


# ------------------------------------------------------------- trajectory mode / off


def test_cli_trajectory_off_sets_the_global_kill_switch(steward_home, monkeypatch):
    _isolate_config(monkeypatch, steward_home)
    store = Store(steward_home)

    rc = cli.main(["trajectory", "off"])

    assert rc == 0
    eff = trajectory_module.effective_mode(store, "any-job-id", trajectory_module.TrajectoryConfig())
    assert eff.mode == "off"
    assert eff.source == "kill_switch"


def test_cli_trajectory_mode_sets_a_per_job_override_and_records_an_event(steward_home, monkeypatch):
    _isolate_config(monkeypatch, steward_home)
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    rc = cli.main(["trajectory", "mode", "active", "--job", job.job_id])

    assert rc == 0
    eff = trajectory_module.effective_mode(store, job.job_id, trajectory_module.TrajectoryConfig(mode="shadow"))
    assert eff.mode == "active"
    assert eff.source == "job"
    events = [e for e in store.events(job.job_id) if e.what == "trajectory_mode_changed"]
    assert len(events) == 1
    assert events[0].judgment == Judgment.human


def test_cli_trajectory_mode_unknown_job_is_rejected(steward_home, monkeypatch):
    _isolate_config(monkeypatch, steward_home)
    store = Store(steward_home)

    rc = cli.main(["trajectory", "mode", "active", "--job", "no-such-job"])

    assert rc == 1


def test_cli_trajectory_off_is_effective_on_the_next_supervisor_step(steward_home, monkeypatch):
    """The kill switch is re-read every `effective_mode()` call (D9): a
    supervisor built BEFORE the CLI write still sees `off` on its next step,
    with no restart needed."""
    _isolate_config(monkeypatch, steward_home)
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    args = argparse.Namespace(trajectory="active", trajectory_config=None, sock=None)
    supervisor = cli._build_trajectory_supervisor(store, args, SubprocessRunner())

    before = trajectory_module.effective_mode(store, job.job_id, supervisor._current_cfg())
    assert before.mode == "active"

    rc = cli.main(["trajectory", "off"])
    assert rc == 0

    after = trajectory_module.effective_mode(store, job.job_id, supervisor._current_cfg())
    assert after.mode == "off"

    report = supervisor.step(job.job_id)
    assert report.mode == "off"


# ------------------------------------------------------------- trajectory status


def test_cli_trajectory_status_json_reports_defaults_for_a_fresh_job(steward_home, monkeypatch, capsys):
    _isolate_config(monkeypatch, steward_home)
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    rc = cli.main(["trajectory", "status", job.job_id, "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "shadow"
    assert payload["mode_source"] == "config"
    assert payload["reviews"] == []
    assert payload["current_approach"] is None
    assert payload["discarded_approaches"] == []
    assert payload["open_concerns"] == {}


def test_cli_trajectory_status_human_readable_output(steward_home, monkeypatch, capsys):
    _isolate_config(monkeypatch, steward_home)
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    rc = cli.main(["trajectory", "status", job.job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "mode: shadow" in out
    assert "last reviews:" in out
    assert "next review due in:" in out
    assert "worker spend:" in out


def test_cli_trajectory_status_reflects_the_job_mode_override(steward_home, monkeypatch, capsys):
    _isolate_config(monkeypatch, steward_home)
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    trajectory_module.set_job_mode(store, job.job_id, "active")

    rc = cli.main(["trajectory", "status", job.job_id, "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "active"
    assert payload["mode_source"] == "job"


def test_cli_trajectory_status_unknown_job_exits_1(steward_home, monkeypatch, capsys):
    _isolate_config(monkeypatch, steward_home)
    Store(steward_home)

    rc = cli.main(["trajectory", "status", "no-such-job"])

    assert rc == 1


# ------------------------------------------------------------- explain-state


def test_explain_state_gains_a_trajectory_line(steward_home, monkeypatch, capsys):
    _isolate_config(monkeypatch, steward_home)
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    rc = cli.main(["explain-state", job.job_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Trajectory: shadow; last review none yet; next due in" in out
