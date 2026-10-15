"""`steward deploy-self`: fakes for systemctl, the control relay, exec, and health; never the live Steward."""

import fcntl
import os
import sys
import time

from steward import approvals as approvals_module
from steward import autopilot, cli, deploy_self
from steward.control_pane import ControlPane
from steward.fabric import Frame
from steward.models import JobState
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from test_autopilot import git, go, mkjob, runner, scratch, whats  # noqa: F401


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Rig:
    def __init__(self, tmp_path, store, *, systemctl_code=0, relay_ok=True, import_err=None, health=(True, "ok")):
        self.root = tmp_path / "repo"
        (self.root / "deploy" / "cockpit").mkdir(parents=True)
        (self.root / "deploy" / "cockpit" / "steward-dash").write_text("new dash\n")
        (self.root / "deploy" / "cockpit" / "steward-cockpit").write_text("cockpit\n")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "steward-cockpit").write_text("cockpit\n")  # unchanged: must not be re-copied
        (self.bin / "steward-dash").write_text("old dash\n")
        self.signal = str(tmp_path / "dash.reexec")
        self.calls, self.relays, self.clock = [], [], Clock()
        self.store = store

        def respond(argv, cwd):
            self.calls.append(argv)
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=systemctl_code, stdout="", stderr="boom", duration_s=0)

        self.runner = FakeRunner(respond)
        self.relay_ok, self.import_err, self.health = relay_ok, import_err, health
        self.health_calls = 0
        # the control pane as its `ping` reports it: re-exec (when `reloads`) = a new start time + `reload_head`
        self.reloads, self.reload_head = True, "new"
        self.control = {"accepted": True, "pid": 42, "head": "old", "started_at": 1.0}
        self.pings = 0

    def relay(self, op, args):
        self.relays.append(op)
        if op == "self_exec" and self.relay_ok and self.reloads:
            self.control = {**self.control, "head": self.reload_head, "started_at": self.control["started_at"] + 1}
        return {"accepted": self.relay_ok, "detail": "control pane re-executing in place" if self.relay_ok else "no pane"}

    def ping(self):
        self.pings += 1
        return dict(self.control)

    def health_fn(self):
        self.health_calls += 1
        return self.health

    def deploy(self, **kw):
        kw.setdefault("ping_fn", self.ping)
        kw.setdefault("head_fn", lambda: "new")
        return deploy_self.deploy(self.store, root=self.root, bin_dir=self.bin, runner=self.runner, relay_fn=self.relay,
                                  import_check_fn=lambda: self.import_err, health_fn=self.health_fn,
                                  signal_file=self.signal, clock=self.clock, sleep=self.clock.sleep, **kw)


def test_dry_run_plans_and_changes_nothing(steward_home, tmp_path):
    rig = Rig(tmp_path, Store(steward_home))
    res = rig.deploy(dry_run=True)
    assert res["ok"] and res["dry_run"] and any("steward-dash" in s for s in res["steps"])
    assert not rig.calls and not rig.relays and not os.path.exists(rig.signal)
    assert (rig.bin / "steward-dash").read_text() == "old dash\n"


def test_deploy_copies_changed_only_restarts_all_and_reports_health(steward_home, tmp_path):
    rig = Rig(tmp_path, Store(steward_home))
    res = rig.deploy()
    assert res["ok"] and res["health"] == "ok" and rig.health_calls == 1
    assert (rig.bin / "steward-dash").read_text() == "new dash\n"
    assert res["done"][0] == "copied steward-dash"
    assert ["systemctl", "--user", "restart", "steward-mcp"] in rig.calls
    assert rig.relays == ["self_exec"] and os.path.exists(rig.signal)
    assert res["done"][1:] == ["steward-mcp", "control", "dash"]
    assert "restarted copied steward-dash, steward-mcp, control, dash; health ok" in res["summary"]


def test_import_failure_refuses_before_touching_anything(steward_home, tmp_path):
    rig = Rig(tmp_path, Store(steward_home), import_err="SyntaxError: bad")
    res = rig.deploy()
    assert not res["ok"] and res["refused"] and "does not import" in res["detail"]
    assert not rig.calls and not rig.relays and not os.path.exists(rig.signal)
    assert (rig.bin / "steward-dash").read_text() == "old dash\n"


def test_lock_allows_one_deploy_at_a_time(steward_home, tmp_path):
    store = Store(steward_home)
    rig = Rig(tmp_path, store)
    fh = open(os.path.join(store.home, deploy_self.LOCK_NAME), "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
        res = rig.deploy()
    finally:
        fh.close()
    assert res["refused"] and "already running" in res["detail"] and not rig.calls
    assert rig.deploy()["ok"]  # lock released: the next deploy goes


def test_never_restarts_while_a_permission_answer_is_in_flight(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("goal", str(tmp_path))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    rec = approvals_module.open_approval(store, job.job_id, "worker permission: Bash x", kind="permission",
                                         worker_request_id="r1")
    approvals_module.decide(store, job.job_id, rec["id"], "approve", decided_by="human")
    rig = Rig(tmp_path, store)
    res = rig.deploy()
    assert res["refused"] and "in flight" in res["detail"]
    assert not rig.calls and not rig.relays and not os.path.exists(rig.signal)
    assert rig.clock.t > 1000.0  # it waited (debounce window) before refusing
    approvals_module.mark_relayed(store, job.job_id, rec["id"], ack_expected=False, relayed_by="t")
    assert rig.deploy()["ok"]


def test_failed_pieces_and_red_health_are_reported(steward_home, tmp_path):
    rig = Rig(tmp_path, Store(steward_home), systemctl_code=1, relay_ok=False, health=(False, "RED: control"))
    res = rig.deploy()
    assert not res["ok"] and rig.health_calls == deploy_self.HEALTH_TRIES
    assert "health RED: control" in res["summary"] and "steward-mcp restart failed" in res["summary"]
    assert "control re-exec failed" in res["summary"]


def test_deploy_verifies_the_control_pane_really_reloaded(steward_home, tmp_path):
    rig = Rig(tmp_path, Store(steward_home))
    res = rig.deploy()
    assert res["ok"] and "control" in res["done"] and res["deployed_head"] == "new"
    assert rig.pings >= 2  # one before the self_exec (the old start time), one proving the new process


def test_deploy_fails_loudly_when_the_control_pane_does_not_reload(steward_home, tmp_path):
    """Job f257aa7b: an accepted `self_exec` receipt was taken as proof of a reload. Same start time = same process."""
    rig = Rig(tmp_path, Store(steward_home))
    rig.reloads = False
    res = rig.deploy()
    assert not res["ok"] and "control" not in res["done"]
    assert "control did not reload" in res["summary"] and "old" in res["summary"] and "new" in res["summary"]
    assert rig.clock.t >= 1000.0 + deploy_self.RELOAD_WAIT_S - deploy_self.RELOAD_POLL_S  # it waited, then gave up


def test_deploy_fails_when_the_reloaded_control_runs_other_code(steward_home, tmp_path):
    """A re-exec that imports a stale tree (another checkout on its path) is a new process on the WRONG code."""
    rig = Rig(tmp_path, Store(steward_home))
    rig.reload_head = "elsewhere"
    res = rig.deploy()
    assert not res["ok"] and "runs elsewhere" in res["summary"] and "deployed HEAD new" in res["summary"]


def test_deploy_fails_when_the_control_pane_never_answers_after_the_exec(steward_home, tmp_path):
    rig = Rig(tmp_path, Store(steward_home))
    rig.ping = lambda: None
    res = rig.deploy()
    assert not res["ok"] and "no ping answer" in res["summary"]


def test_verify_reload_waits_for_the_new_process():
    clock = Clock()
    answers = iter([None, {"head": "old", "started_at": 1.0}, {"head": "new", "started_at": 2.0, "pid": 7}])
    ok, seen = deploy_self.verify_reload({"head": "old", "started_at": 1.0}, "new", lambda: next(answers),
                                         clock=clock, sleep=clock.sleep)
    assert ok and "new" in seen and "pid 7" in seen


def test_cli_dry_run_prints_plan(steward_home, capsys):
    assert cli.main(["deploy-self", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "systemctl --user restart steward-mcp" in out and "re-exec the control pane" in out


def test_control_self_exec_op_execs_after_the_receipt(steward_home, tmp_path):
    store = Store(steward_home)
    execs = []
    pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda *_: None,
                       reexec_fn=lambda: execs.append("exec"))

    class Box:
        def read(self, timeout):
            return [Frame(v=1, seq=1, job="r1", type="cmd", body={"request_id": "r1", "op": "self_exec", "args": {},
                                                                   "origin": "deploy-self"})]

    (res,) = pane.step(Box(), None)
    assert res["accepted"] and res["reexec"] and execs == ["exec"]
    from steward.control_pane import get_receipt
    assert get_receipt(store, "r1")["accepted"]  # the receipt was written before the exec
    pane.step(Box(), None)  # a duplicate request must not exec again
    assert execs == ["exec"]


def test_dashapp_reexecs_on_signal_file(tmp_path):
    from steward import dashapp

    sig = tmp_path / "sig"
    execs = []
    app = dashapp.App(in_fd=0, out_fd=1, cols=80, rows=24, reexec_file=str(sig),
                      exec_fn=lambda path, argv: execs.append((path, argv)))
    app.restore_terminal = lambda: None
    app.check_reexec()
    assert execs == []  # no file
    sig.write_text("")
    os.utime(sig, (app.started_at - 5, app.started_at - 5))
    app.check_reexec()
    assert execs == []  # a stale file from before this process started
    os.utime(sig, (app.started_at + 5, app.started_at + 5))
    app.check_reexec()
    assert len(execs) == 1 and execs[0][0] == execs[0][1][0]


def test_autopilot_deploys_after_merging_steward_itself(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_AUTODEPLOY", "1")
    store = Store(steward_home)
    (scratch / "steward").mkdir()
    (scratch / "steward" / "x.py").write_text("x=1\n")
    git(scratch, "add", "-A"); git(scratch, "commit", "-qm", "pkg")
    job = mkjob(store, scratch, tmp_path, {"steward/y.py": "y=1\n"})
    calls, pushes = [], []
    monkeypatch.setattr(autopilot, "_push", lambda s, j, ev, r, **facts: pushes.append((ev, facts)))

    def fake_deploy(s, **kw):
        calls.append(kw)
        return {"ok": True, "summary": "deploy-self: restarted steward-mcp, control, dash; health ok"}

    res = go(store, job, steward_root=scratch, deploy_self=fake_deploy)
    assert res["outcome"] == "merged" and len(calls) == 1
    ev = whats(store, job)
    assert "auto_deployed" in ev and "restart_needed" not in ev
    assert [e for e, _ in pushes].count("auto_deployed") == 1


def test_autopilot_holds_runners_from_before_the_self_merge_lands_until_its_deploy(steward_home, scratch, tmp_path,
                                                                                    monkeypatch):
    """Sam's B (job f257aa7b): the mark is set BEFORE the merge reaches the live checkout (runners self-restart
    within a second of HEAD moving) and stays until the deploy has run."""
    from steward import deploy_pending, incorporate

    monkeypatch.setenv("STEWARD_AUTODEPLOY", "1")
    store = Store(steward_home)
    (scratch / "steward").mkdir()
    (scratch / "steward" / "x.py").write_text("x=1\n")
    git(scratch, "add", "-A"); git(scratch, "commit", "-qm", "pkg")
    job = mkjob(store, scratch, tmp_path, {"steward/y.py": "y=1\n"})
    seen = {}
    real_incorporate = incorporate.incorporate

    def spy_incorporate(*a, **kw):
        seen["at_merge"] = deploy_pending.pending(store)
        return real_incorporate(*a, **kw)

    monkeypatch.setattr(incorporate, "incorporate", spy_incorporate)

    def fake_deploy(s, **kw):
        seen["at_deploy"] = deploy_pending.pending(store)
        return {"ok": False, "summary": "deploy-self: control did not reload"}

    assert go(store, job, steward_root=scratch, deploy_self=fake_deploy)["outcome"] == "merged"
    assert seen["at_merge"]["job_id"] == job.job_id and seen["at_deploy"]["job_id"] == job.job_id
    assert deploy_pending.pending(store) is None  # the attempt ended (even a failed deploy): runners stop waiting


def test_a_successful_deploy_releases_waiting_runners(steward_home, tmp_path):
    from steward import deploy_pending

    store = Store(steward_home)
    deploy_pending.mark(store, "j1")
    assert Rig(tmp_path, store).deploy()["ok"] and deploy_pending.pending(store) is None


def test_a_failed_deploy_keeps_runners_waiting(steward_home, tmp_path):
    from steward import deploy_pending

    store = Store(steward_home)
    deploy_pending.mark(store, "j1")
    rig = Rig(tmp_path, store)
    rig.reloads = False
    assert not rig.deploy()["ok"] and deploy_pending.pending(store) is not None


def test_autopilot_failed_deploy_falls_back_to_restart_needed(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_AUTODEPLOY", "1")
    store = Store(steward_home)
    (scratch / "steward").mkdir()
    (scratch / "steward" / "x.py").write_text("x=1\n")
    git(scratch, "add", "-A"); git(scratch, "commit", "-qm", "pkg")
    job = mkjob(store, scratch, tmp_path, {"steward/y.py": "y=1\n"})
    res = go(store, job, steward_root=scratch,
             deploy_self=lambda s, **kw: {"ok": False, "summary": "deploy-self refused: the new code does not import"})
    assert res["outcome"] == "merged"
    ev = whats(store, job)
    assert "auto_deploy_failed" in ev and "restart_needed" in ev


def test_autodeploy_off_keeps_the_restart_needed_flag(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_AUTODEPLOY", "0")
    store = Store(steward_home)
    (scratch / "steward").mkdir()
    (scratch / "steward" / "x.py").write_text("x=1\n")
    git(scratch, "add", "-A"); git(scratch, "commit", "-qm", "pkg")
    job = mkjob(store, scratch, tmp_path, {"steward/y.py": "y=1\n"})

    def boom(s, **kw):
        raise AssertionError("deploy must not run with STEWARD_AUTODEPLOY=0")

    assert go(store, job, steward_root=scratch, deploy_self=boom)["outcome"] == "merged"
    assert "restart_needed" in whats(store, job)


def test_refusals_say_whether_they_are_transient(steward_home, tmp_path):
    """The landing train defers a transient refusal and reverts on a refusal about the code itself."""
    store = Store(steward_home)
    res = Rig(tmp_path / "a", store, import_err="SyntaxError: bad").deploy()
    assert res["refused"] and res["transient"] is False
    fh = open(os.path.join(store.home, deploy_self.LOCK_NAME), "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
        res = Rig(tmp_path / "b", store).deploy()
    finally:
        fh.close()
    assert res["refused"] and res["transient"] is True


def test_preflight_reports_why_a_deploy_is_not_possible(steward_home, tmp_path):
    store = Store(steward_home)
    assert deploy_self.preflight(store, health_fn=lambda: (True, "ok")) is None
    assert "RED: mcp" in deploy_self.preflight(store, health_fn=lambda: (False, "RED: mcp"))
    fh = open(os.path.join(store.home, deploy_self.LOCK_NAME), "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
        assert "already running" in deploy_self.preflight(store, health_fn=lambda: (True, "ok"))
    finally:
        fh.close()
    job = store.create_job("goal", str(tmp_path))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    rec = approvals_module.open_approval(store, job.job_id, "worker permission: Bash x", kind="permission",
                                         worker_request_id="r1")
    approvals_module.decide(store, job.job_id, rec["id"], "approve", decided_by="human")
    slept = []
    why = deploy_self.preflight(store, health_fn=lambda: (True, "ok"), sleep=slept.append)
    assert "in flight" in why and not slept          # a pre-flight never waits out the debounce


# --- train 30 (2026-10-13): the train's deploy must run at the new HEAD in a fresh interpreter --------------------

def _mini_tree(root, contract: str, cli_src: str):
    """A stand-in `steward` package at `root`: its deploy_self imports cli (as the real relay does) and reports."""
    pkg = root / "steward"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "contract.py").write_text(contract)
    (pkg / "cli.py").write_text(cli_src)
    (pkg / "deploy_self.py").write_text(
        "import json, sys\n"
        "from steward import cli\n"
        f"print({deploy_self.RESULT_PREFIX!r} + json.dumps({{'ok': True, 'summary': 'deploy-self: ' + cli.WHAT}}))\n")


def _swap_in(root, monkeypatch):
    """Make the mini tree the process's `steward` (restored afterwards), like a train process started before a landing."""
    import importlib

    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    saved = {k: v for k, v in sys.modules.items() if k == "steward" or k.startswith("steward.")}
    for k in saved:
        del sys.modules[k]
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()

    def restore():
        for k in [k for k in sys.modules if k == "steward" or k.startswith("steward.")]:
            del sys.modules[k]
        sys.modules.update(saved)
        sys.path.remove(str(root))
        importlib.invalidate_caches()
    return restore


def test_fresh_deploy_is_green_when_only_the_in_process_module_is_stale(tmp_path, monkeypatch):
    import importlib

    from steward import train_land

    store = Store(tmp_path / "home")
    fresh_deploy = deploy_self.fresh_deploy
    root = tmp_path / "repo"
    _mini_tree(root, "X = 1\n", "WHAT = 'old'\n")
    restore = _swap_in(root, monkeypatch)
    try:
        importlib.import_module("steward.contract")                     # cached before the landing
        # the landing: contract gains a name, the new cli imports it
        (root / "steward" / "contract.py").write_text("X = 1\nPAUSE_VALUES = ('a', 'b')\n")
        (root / "steward" / "cli.py").write_text("from steward.contract import PAUSE_VALUES\nWHAT = 'new'\n")
        importlib.invalidate_caches()
        try:                                                              # the old in-process path: false red
            importlib.import_module("steward.cli")
            raise AssertionError("the stale in-process import should have failed")
        except ImportError as exc:
            assert "PAUSE_VALUES" in str(exc)
        res = fresh_deploy(store, root=root)                             # the train's path: new interpreter
    finally:
        restore()
    assert res["ok"] is True, res
    assert res["summary"] == "deploy-self: new"
    assert train_land._deploy(lambda: res)["ok"] is True


def test_fresh_deploy_is_red_when_the_new_head_really_does_not_import(tmp_path):
    from steward import train_land

    store = Store(tmp_path / "home")
    root = tmp_path / "repo"
    _mini_tree(root, "X = 1\n", "from steward.contract import PAUSE_VALUES\nWHAT = 'new'\n")   # name not on disk
    res = deploy_self.fresh_deploy(store, root=root)
    assert res["ok"] is False and res["refused"] is True and res["transient"] is False
    assert "does not import" in res["summary"] and "PAUSE_VALUES" in res["summary"]
    assert not train_land._deferred(res)                                # red (reverted), never deferred


def test_fresh_preflight_reports_the_child_answer(tmp_path):
    store = Store(tmp_path / "home")
    root = tmp_path / "repo"
    pkg = root / "steward"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "deploy_self.py").write_text(
        f"import json\nprint({deploy_self.RESULT_PREFIX!r} + json.dumps({{'why': 'an answer is in flight'}}))\n")
    assert deploy_self.fresh_preflight(store, root=root) == "an answer is in flight"
    (pkg / "deploy_self.py").write_text("raise ImportError('broken')\n")
    assert deploy_self.fresh_preflight(store, root=root).startswith("pre-flight could not run")


def test_landing_train_self_deploy_runs_fresh_never_in_process(tmp_path, monkeypatch):
    from steward import landing_train

    store = Store(tmp_path / "home")
    calls = []
    monkeypatch.setattr(deploy_self, "deploy", lambda *a, **k: calls.append("in-process deploy"))
    monkeypatch.setattr(deploy_self, "preflight", lambda *a, **k: calls.append("in-process preflight"))
    monkeypatch.setattr(deploy_self, "fresh_deploy", lambda st, *, root: calls.append(("fresh", str(root))) or {"ok": True})
    monkeypatch.setattr(deploy_self, "fresh_preflight", lambda st, *, root: calls.append(("pre", str(root))))
    dep, pre = landing_train._self_deploy_fns(store, "/r")
    assert dep() == {"ok": True} and pre() is None
    assert calls == [("fresh", "/r"), ("pre", "/r")]
