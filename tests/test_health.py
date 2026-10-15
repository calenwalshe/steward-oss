"""System health: `steward health`, the control `ping` op, the 30s dash check, self-heal once, gated alerts.
Fake Herdr / systemd / notifier only: nothing here touches a real socket, pane, unit or network."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from steward import alerts, cli, control_pane, health
from steward.control_pane import ControlPane
from steward.herdr_transport import HerdrUnavailable
from steward.models import JobState, Judgment
from steward.store import Store
from test_runner import _contracted_job

REPO = Path(__file__).resolve().parent.parent


def _pane(pid, role=None, label=None, ws="w1"):
    p = {"pane_id": pid, "workspace_id": ws}
    if role:
        p["tokens"] = {"steward_role": role}
    if label:
        p["label"] = label
    return p


GOOD_PANES = [_pane("w1:p1", "control"), _pane("w1:p2", "advisor")]


class FakeHerdr:
    """`pane.list` plus `send`, which executes the cmd in-process on a REAL ControlPane (so `ping` is the real op)."""

    def __init__(self, store, panes=GOOD_PANES, answers=True, down=False):
        self.store, self.panes, self.answers, self.down = store, list(panes), answers, down
        self.sent = []

    def call(self, method, params):
        if self.down:
            raise HerdrUnavailable("no socket")
        assert method == "pane.list"
        return {"panes": self.panes}

    def send(self, pane_id, frame):
        self.sent.append((pane_id, frame.body["op"]))
        if self.answers:
            ControlPane(self.store, cli._build_control_handlers(self.store), print_fn=lambda _l: None).execute(frame.body)


class Time:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def clock(self):
        return self.t


def _check(store, api=None, *, units=lambda u: True, pids=lambda p: True, ping=True, trajectory_on=True):
    tm = Time()
    return health.check_all(store, api=api or FakeHerdr(store), ping=ping, unit_active=units, pid_alive=pids,
                            sleep=tm.sleep, clock=tm.clock, trajectory_on=trajectory_on)


def _by(comps):
    return {c.name: c for c in comps}


def _running_job(store, **kv):
    job = _contracted_job(store)
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    for k, v in kv.items():
        store.kv_set(job.job_id, k, v)
    return job


def test_control_pane_handles_a_noop_ping(steward_home):
    store = Store(steward_home)
    handlers = cli._build_control_handlers(store)
    assert "ping" in handlers
    r = ControlPane(store, handlers, print_fn=lambda _l: None).execute({"request_id": "p1", "op": "ping", "args": {}})
    assert r["accepted"] and r["detail"] == "pong"


def test_everything_green_and_ping_leaves_no_receipts_behind(steward_home):
    store = Store(steward_home)
    api = FakeHerdr(store)
    comps = _by(_check(store, api))
    assert [c.name for c in comps.values()] == ["control", "advisor", "herdr", "runners", "mcp", "budget"]
    assert all(c.ok is True for n, c in comps.items() if n != "budget"), comps
    assert comps["budget"].ok is None  # no usage.db in tests: unknown, never red
    assert comps["control"].detail.startswith("answers ping") and api.sent == [("w1:p1", "ping")]
    left = store._conn.execute("SELECT COUNT(*) FROM kv WHERE job_id = '_control'").fetchone()[0]
    assert left == 0  # a ping every 30s must not accumulate receipts


def test_control_ping_reports_the_code_it_loaded(steward_home):
    from steward import stability

    store = Store(steward_home)
    r = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _l: None).execute(
        {"request_id": "p1", "op": "ping", "args": {}})
    assert r["head"] == stability.PROCESS_HEAD and r["started_at"] == stability.PROCESS_STARTED_AT
    assert r["root"] == str(stability.REPO_ROOT) and r["pid"] == os.getpid()


def _check_heads(store, head_fn):
    tm = Time()
    return _by(health.check_all(store, api=FakeHerdr(store), unit_active=lambda u: True, pid_alive=lambda p: True,
                                sleep=tm.sleep, clock=tm.clock, trajectory_on=True, head_fn=head_fn))


def test_control_running_stale_code_is_red_with_both_shas(steward_home, monkeypatch):
    """Job f257aa7b: 233d178 landed in the control's tree (runners re-exec'd onto it within a second) while the control
    kept running 6e76e71 for 49 min; health only pinged, so it stayed green and approvals failed on the code skew."""
    from steward import stability

    monkeypatch.setattr(stability, "PROCESS_HEAD", "6e76e719cf9953001352999c68490853fa6b3b8a")
    seen = []
    comps = _check_heads(Store(steward_home), lambda root: seen.append(root) or "233d178b5ee586db3b6fbb7cb5c5a528f71e3de8")
    ctl = comps["control"]
    assert ctl.ok is False and "6e76e719cf99" in ctl.detail and "233d178b5ee5" in ctl.detail
    assert ctl.problems == [f"control: {ctl.detail}"] and "deploy-self" in ctl.detail
    assert seen == [str(stability.REPO_ROOT)]  # the HEAD of the tree the control imported from, not the caller's


def test_control_running_the_deployed_head_is_green(steward_home, monkeypatch):
    from steward import stability

    monkeypatch.setattr(stability, "PROCESS_HEAD", "abc123")
    comps = _check_heads(Store(steward_home), lambda root: "abc123")
    assert comps["control"].ok is True and "abc123" in comps["control"].detail


def test_stale_control_while_a_self_merge_awaits_its_deploy_is_not_red(steward_home, monkeypatch):
    """Sam's B: during the merged-tree re-test runners wait too, so the old control is the intended state."""
    import time

    from steward import deploy_pending, stability

    monkeypatch.setattr(stability, "PROCESS_HEAD", "6e76e719cf99")
    store = Store(steward_home)
    deploy_pending.mark(store, "33157637-job")
    ctl = _check_heads(store, lambda root: "233d178b5ee5")["control"]
    assert ctl.ok is True and "awaiting deploy-self" in ctl.detail and "33157637" in ctl.detail
    deploy_pending.mark(store, "33157637-job", now=time.time() - deploy_pending.MAX_AGE_S - 1)  # forgotten mark
    assert _check_heads(store, lambda root: "233d178b5ee5")["control"].ok is False


def test_unknown_code_sha_is_not_called_stale():
    assert health.stale_code({"accepted": True}, lambda root: "abc") is None  # a pre-fix control: no head
    assert health.stale_code({"head": "abc", "root": "/r"}, lambda root: "") is None  # git failed: unknown
    assert health.stale_code({"head": "abc", "root": "/r"}, lambda root: "abc") is None
    assert "loaded abc" in health.stale_code({"head": "abc", "root": "/r"}, lambda root: "def")


def test_no_control_pane_is_red(steward_home):
    store = Store(steward_home)
    comps = _by(_check(store, FakeHerdr(store, panes=[_pane("w1:p2", "advisor")])))
    assert comps["control"].ok is False and comps["control"].problems == ["control: no control pane is running"]
    assert comps["advisor"].ok is True


def test_control_pane_present_but_not_answering_is_red(steward_home):
    store = Store(steward_home)
    comps = _by(_check(store, FakeHerdr(store, answers=False)))
    assert comps["control"].ok is False and "not answering" in comps["control"].detail
    left = store._conn.execute("SELECT COUNT(*) FROM kv WHERE job_id = '_control'").fetchone()[0]
    assert left == 0
    assert _by(_check(store, FakeHerdr(store, answers=False), ping=False))["control"].ok is True  # --no-ping: presence only


def test_advisor_missing_is_red_unless_trajectory_is_off(steward_home):
    store = Store(steward_home)
    api = FakeHerdr(store, panes=[_pane("w1:p1", "control")])
    assert _by(_check(store, api))["advisor"].ok is False
    off = _by(_check(store, api, trajectory_on=False))["advisor"]
    assert off.ok is True and "off" in off.detail


def test_herdr_socket_unreachable_is_red_and_the_rest_unknown(steward_home):
    store = Store(steward_home)
    comps = _by(_check(store, FakeHerdr(store, down=True)))
    assert comps["herdr"].ok is False
    assert comps["control"].ok is None and comps["advisor"].ok is None and comps["runners"].ok is None  # unknown, not red
    assert comps["mcp"].ok is True


def test_running_job_runner_and_worker_liveness(steward_home):
    store = Store(steward_home)
    ok = _running_job(store, runner_code={"pid": 111, "head": "a", "started_at": 1}, herdr_pane="w1:p9")
    bad_runner = _running_job(store, runner_code={"pid": 222, "head": "a", "started_at": 1})
    bad_worker = _running_job(store, runner_code={"pid": 111, "head": "a", "started_at": 1}, herdr_pane="w1:p404")
    untracked = _running_job(store)  # started before pid tracking, no pane: unknown -> never red
    panes = [*GOOD_PANES, _pane("w1:p9")]
    comps = _by(_check(store, FakeHerdr(store, panes=panes), pids=lambda pid: pid == 111))
    r = comps["runners"]
    assert r.ok is False
    assert r.problems == [f"runner {bad_runner.job_id[:8]}: runner process 222 is gone",
                          f"runner {bad_worker.job_id[:8]}: worker pane w1:p404 is gone"]
    assert all(ok.job_id[:8] not in p and untracked.job_id[:8] not in p for p in r.problems)


def test_mcp_units_inactive_is_red(steward_home):
    store = Store(steward_home)
    c = _by(_check(store, units=lambda u: u != "steward-tunnel"))["mcp"]
    assert c.ok is False and c.problems == ["mcp: steward-tunnel is not active"]


def test_cli_health_json_and_exit_code(steward_home, monkeypatch, capsys):
    store = Store(steward_home)
    seen = {}

    def fake_check(st, **kw):
        seen.update(kw)
        return [health.Component("control", True, "answers ping"), health.Component("mcp", False, "1 of 3 inactive",
                                                                                     ["mcp: steward-tunnel is not active"])]

    monkeypatch.setattr(health, "check_all", fake_check)
    assert cli.main(["health", "--json", "--no-ping"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and seen["ping"] is False
    assert [(c["name"], c["ok"]) for c in data["components"]] == [("control", True), ("mcp", False)]
    assert data["components"][1]["problems"] == ["mcp: steward-tunnel is not active"]
    monkeypatch.setattr(health, "check_all", lambda st, **kw: [health.Component("control", True, "fine")])
    assert cli.main(["health"]) == 0 and "ok  control" in capsys.readouterr().out


# -- the 30s dash check: state, self-heal once, gated alerts --------------------------------------------


def _red_control():
    return [health.Component("control", False, "no control pane", ["control: no control pane is running"]),
            health.Component("mcp", True, "ok")]


def _green():
    return [health.Component("control", True, "ok"), health.Component("mcp", True, "ok")]


def test_tick_caches_for_30_seconds(steward_home):
    store = Store(steward_home)
    calls = []
    fn = lambda: calls.append(1) or _green()
    health.tick(store, now=1000.0, check_fn=fn)
    health.tick(store, now=1029.0, check_fn=fn)
    assert len(calls) == 1
    health.tick(store, now=1031.0, check_fn=fn)
    assert len(calls) == 2


def test_control_down_self_heals_once_per_outage_and_records_it(steward_home):
    store = Store(steward_home)
    heals = []
    heal = lambda: heals.append(1) or {"control_pane": "w1:p5", "created": ["control"]}
    state = None
    for t in (1000.0, 1040.0, 1080.0, 1120.0):
        state = health.tick(store, now=t, check_fn=_red_control, heal_fn=heal, alert=False)
    assert len(heals) == 1  # once, however long it stays down
    assert state["heals"] == [{"ts": 1000.0, "component": "control", "control_pane": "w1:p5", "created": ["control"]}]
    health.tick(store, now=1160.0, check_fn=_green, heal_fn=heal)  # recovered: the outage is over
    health.tick(store, now=1200.0, check_fn=_red_control, heal_fn=heal, alert=False)
    assert len(heals) == 2  # a NEW outage gets its own one attempt


def test_heal_failure_is_recorded_not_raised(steward_home):
    store = Store(steward_home)

    def boom():
        raise RuntimeError("herdr said no")

    st = health.tick(store, now=1000.0, check_fn=_red_control, heal_fn=boom, alert=False)
    assert "herdr said no" in st["heals"][0]["error"]


def test_red_over_two_minutes_alerts_once_through_the_gate(steward_home):
    store = Store(steward_home)
    sent = []
    alert = lambda text: sent.append(text) or True
    for t in (1000.0, 1060.0, 1119.0):
        health.tick(store, now=t, check_fn=_red_control, alert_fn=alert, heal_fn=None)
    assert sent == []  # not yet: red for < 2 minutes
    health.tick(store, now=1125.0, check_fn=_red_control, alert_fn=alert)
    health.tick(store, now=1200.0, check_fn=_red_control, alert_fn=alert)
    assert len(sent) == 1 and "control red" in sent[0] and "no control pane is running" in sent[0]  # once
    health.tick(store, now=1300.0, check_fn=_green, alert_fn=alert)
    health.tick(store, now=1340.0, check_fn=_red_control, alert_fn=alert)
    health.tick(store, now=1500.0, check_fn=_red_control, alert_fn=alert)
    assert len(sent) == 2  # a new outage may alert again


def test_failed_alert_retries_a_bounded_number_of_times(steward_home):
    store = Store(steward_home)
    tries = []
    for t in range(1000, 1000 + 40 * 12, 40):
        health.tick(store, now=float(t), check_fn=_red_control, alert_fn=lambda text: tries.append(1) and False)
    assert len(tries) == health.MAX_ALERT_TRIES


def test_default_config_sends_nothing(steward_home):
    class Boom:
        def send_text(self, text):
            raise AssertionError("must not send")

    assert alerts.telegram_enabled() is False
    assert alerts.system_alert("control red", object(), notifier=Boom()) is False  # gated off by default
    sent = []

    class Ok:
        def send_text(self, text):
            sent.append(text)
            return True

    assert alerts.system_alert("control red", object(), notifier=Ok(), enabled=True) is True and sent == ["control red"]
    (Path(os.environ["STEWARD_CONFIG_DIR"]) / "steward.toml").write_text("[alerts]\ntelegram = true\n")
    assert alerts.system_alert("x", object(), notifier=Ok()) is True  # opted in through steward.toml


def test_a_broken_check_never_breaks_the_dash(steward_home):
    store = Store(steward_home)

    def boom():
        raise RuntimeError("x")

    assert health.tick(store, now=1005.0, check_fn=boom) == {}  # swallowed; nothing persisted


# -- the pane header -------------------------------------------------------------------------------------


def _strip(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_header_has_a_dot_per_component_and_a_line_for_anything_red():
    st = health.update_state({}, [
        health.Component("control", False, "no control pane", ["control: no control pane is running"]),
        health.Component("advisor", True, "ok"), health.Component("herdr", True, "ok"),
        health.Component("runners", None, "unknown"), health.Component("mcp", True, "ok")], 1.0)
    lines = health.render_header(st, 88)
    plain = [_strip(ln) for ln in lines]
    assert plain[0] == "HEALTH ● control ● advisor ● herdr ● runners ● mcp"
    assert plain[1] == "  control: no control pane is running" and len(plain) == 2
    assert "\033[31m●" in lines[0] and "\033[32m●" in lines[0] and "\033[2m●" in lines[0]  # red, green, unknown
    assert all(len(p) <= 88 for p in plain)
    assert all(len(_strip(ln)) <= 40 for ln in health.render_header(st, 40)[1:])
    green = health.render_header(health.update_state({}, [health.Component("control", True, "ok")], 1.0), 88)
    assert len(green) == 1


def test_dash_renderer_shows_the_header_and_probes_only_when_asked(steward_home, tmp_path):
    """The renderer runs the 30s check unless STEWARD_HEALTH=0 (which the test suite sets everywhere)."""
    store = Store(steward_home)
    state = health.update_state({}, [health.Component("control", False, "x", ["control: no control pane is running"]),
                                     health.Component("mcp", True, "ok")], 4102444800.0)  # checked "in the future": cached
    health.save_state(store, state)
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_DASH_COLS": "88", "PYTHONPATH": str(REPO),
           "HOME": str(tmp_path)}
    env.pop("STEWARD_HEALTH", None)
    res = subprocess.run([sys.executable, str(REPO / "deploy/cockpit/steward-dash-jobs.py")], capture_output=True,
                         text=True, env=env, cwd=REPO)
    lines = [_strip(ln) for ln in res.stdout.splitlines()]
    assert "HEALTH ● control ● mcp" in lines and "  control: no control pane is running" in lines
    quiet = subprocess.run([sys.executable, str(REPO / "deploy/cockpit/steward-dash-jobs.py")], capture_output=True,
                           text=True, env={**env, "STEWARD_HEALTH": "0"}, cwd=REPO)
    assert "HEALTH" not in quiet.stdout
