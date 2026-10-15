"""Regression tests for the rest of the 2026-10-05 TUI speed plan (docs/perf/2026-10-05-tui-speed-plan.md, items 9-12b):
the resident status pane behind `steward dash`, the quieter event feed (one event per auto-allowed permission,
heartbeat hysteresis, deduped lead escalations), compact check results, the approvals table, the per-transcript
tokens cache, the control tick off the frame thread and the remaining filtered reads."""

from __future__ import annotations

import os
import sys
import time

import pytest

from steward import cli, cockpit, dashapp, version
from steward.models import JobState
from steward.store import Store

REPO_TEMPLATE = cli._dash_script()


# -- item 9: `steward dash` runs the resident pane ---------------------------------------------------------------


def _capture_exec(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "execv", lambda f, argv: calls.append(("execv", f, list(argv), dict(os.environ))))
    monkeypatch.setattr(os, "execvp", lambda f, argv: calls.append(("execvp", f, list(argv), dict(os.environ))))
    for name in ("STEWARD_PYTHON", "STEWARD_INSTALL_ROOT", "STEWARD_DASH_ONCE", "STEWARD_PUBLIC_URL",
                 "STEWARD_DASH_HOME", "PYTHONPATH"):
        monkeypatch.setenv(name, "x")  # recorded, so the test restores them
    return calls


def test_dash_execs_the_resident_pane(monkeypatch, tmp_path):
    calls = _capture_exec(monkeypatch)
    monkeypatch.delenv("STEWARD_DASH_LEGACY", raising=False)
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    cli.main(["dash"])
    kind, f, argv, env = calls[-1]
    root = str(version.install_root())
    assert kind == "execv" and f == sys.executable and argv == [sys.executable, "-m", "steward.dashapp"]
    assert env["STEWARD_INSTALL_ROOT"] == root and env["STEWARD_DASH_HOME"] == root
    assert env["PYTHONPATH"] == root + os.pathsep + "/elsewhere"
    assert env["STEWARD_PYTHON"] == sys.executable and env["STEWARD_PUBLIC_URL"] == ""
    assert "STEWARD_DASH_ONCE" not in env


def test_dash_once_and_legacy_keep_the_bash_loop(monkeypatch, tmp_path):
    calls = _capture_exec(monkeypatch)
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("STEWARD_DASH_LEGACY", raising=False)
    cli.main(["dash", "--once"])
    assert calls[-1][:3] == ("execvp", "bash", ["bash", str(REPO_TEMPLATE)]) and calls[-1][3]["STEWARD_DASH_ONCE"] == "1"
    monkeypatch.setenv("STEWARD_DASH_LEGACY", "1")
    cli.main(["dash"])
    assert calls[-1][:3] == ("execvp", "bash", ["bash", str(REPO_TEMPLATE)])
    assert "STEWARD_DASH_ONCE" not in calls[-1][3]


class _Runner:
    def __init__(self, argv):
        self.argv = argv

    def run(self, argv, cwd=None, **kw):
        from steward.runner_exec import CommandResult
        import json

        body = {"result": {"process_info": {"foreground_processes": [{"argv": self.argv}]}}}
        return CommandResult(argv, str(cwd), 0, json.dumps(body), "", 0.0)


@pytest.mark.parametrize("argv,alive", [
    (["/usr/bin/python3", "-m", "steward.dashapp"], True),
    (["bash", "/pkg/steward/templates/cockpit/steward-dash.sh"], True),
    (["bash"], False),
])
def test_cockpit_sees_either_status_pane_as_alive(argv, alive):
    assert cockpit._alive(_Runner(argv), "p1", ("steward.dashapp", "steward-dash.sh"), ".") is alive
    assert cockpit._alive(_Runner(argv), "p1", "steward-dash.sh", ".") is ("steward-dash.sh" in " ".join(argv))


def _app(tmp_path, **kw):
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    os.set_blocking(out_r, False)
    from steward import dashact

    app = dashapp.App(in_r, out_w, state_path=dashact.state_path(str(tmp_path / "map")), cols=100, rows=40,
                      run=lambda argv: (0, "done"), services=lambda: {"steward-mcp": "active"},
                      public=lambda: "200", **kw)
    app._fds = (in_r, in_w, out_r, out_w)
    return app


def _close(app):
    app.stop.set()
    app.wake()
    for t in app.threads:
        t.join(3)
    for fd in app._fds:
        os.close(fd)


def test_packaged_services_block_replaces_the_unit_dots(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HEALTH", "0")
    seen = []

    def block(pub):
        seen.append(pub)
        return ["  svc-a", "  svc-b", "  svc-c", "  svc-d", "  svc-e", "  svc-f", f"  connector {pub}"]

    app = _app(tmp_path, services_block=block)
    try:
        app.start_threads()
        assert app.first_data.wait(10)
        deadline = time.monotonic() + 5
        while app.cache.get("svcblock") is None and time.monotonic() < deadline:
            time.sleep(0.01)
        rows = app.frame()
        plain = [dashapp.ANSI.sub("", r) for r in rows]
        assert plain[2:9] == ["  svc-a", "  svc-b", "  svc-c", "  svc-d", "  svc-e", "  svc-f", "  connector 200"]
        assert plain[9] == "" and not any("steward-mcp" in r for r in plain)
        assert seen and seen[-1] == "200"
        # The renderer budgets for 5 service rows plus the blank line: 7 lines + blank leave 2 fewer job rows.
        assert app._jobs_rows() == 40 - (7 + 1 - 5)
    finally:
        _close(app)


def test_packaged_services_is_the_dash_services_block(monkeypatch):
    from steward import dash_services

    got = {}
    monkeypatch.setattr(dash_services, "lines", lambda state, probe=None: got.setdefault("probe", probe) and ["x"])
    dashapp.packaged_services("404")
    assert got["probe"]("https://example.org") == "404"


def _status_fetches(app):
    return sum(1 for k in app.cache.data if k == ("cli", ("status", "--json", "--recent", "7")))


def test_jobs_refetch_waits_for_a_commit(steward_home, tmp_path, monkeypatch):
    """With no commit (same data_version) and no tap, the per-pass data is not refetched until `idle_every`; a
    commit (new data_version) or a kick refetches on the next pass."""
    monkeypatch.setenv("STEWARD_HEALTH", "0")
    store = Store(steward_home)
    store.create_job("ship", str(tmp_path))
    version = {"v": 1}
    app = _app(tmp_path, db_version=lambda: version["v"], jobs_every=0.05, idle_every=30.0)
    key = ("cli", ("status", "--json", "--recent", "7"))
    try:
        app.start_threads()
        assert app.first_data.wait(10)
        stamp = lambda: app.cache.get(key)[0]  # noqa: E731
        first = stamp()
        time.sleep(1.6)
        assert stamp() == first and app.fresh.jobs_ttl == 30.0  # idle: nothing refetched past the 1 s ttl
        version["v"] = 2
        deadline = time.monotonic() + 5
        while stamp() == first and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stamp() != first  # a commit landed: refetched
        second = stamp()
        time.sleep(1.3)
        assert stamp() == second
        app.kick.set()
        deadline = time.monotonic() + 5
        while stamp() == second and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stamp() != second  # a tap's outcome: refetched
    finally:
        _close(app)


def test_no_gate_keeps_the_one_second_cadence(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HEALTH", "0")
    app = _app(tmp_path, jobs_every=0.05)
    key = ("cli", ("status", "--json", "--recent", "7"))
    try:
        app.start_threads()
        assert app.first_data.wait(10)
        first = app.cache.get(key)[0]
        deadline = time.monotonic() + 5
        while app.cache.get(key)[0] == first and time.monotonic() < deadline:
            time.sleep(0.02)
        assert app.cache.get(key)[0] != first and app.fresh.jobs_ttl == 1.0
    finally:
        _close(app)


def test_data_version_probe_sees_other_connections_commits(steward_home, monkeypatch):
    store = Store(steward_home)
    probe = dashapp.data_version_probe()
    v0 = probe()
    assert probe() == v0
    store.create_job("ship", "/tmp/x")
    assert probe() != v0


# -- item 10b: an auto-allowed permission is ONE event -------------------------------------------------------------

from steward import approvals as approvals_module  # noqa: E402
from steward import supervisors  # noqa: E402

CHAIN = ("approval opened", "worker permission requested", "approval approved", "permission_auto_allowed",
         "permission decision relayed", "permission answer delivered")


def _perm_frame(rid, command, seq=3):
    return type("F", (), {"seq": seq, "body": {"request_id": rid, "tool": "Bash", "input": {"command": command}}})()


def _perm_runner(steward_home):
    from test_authority_deciders import _runner
    from test_resolver import FakePopen, _setup

    FakePopen.instances, FakePopen.reply = [], None
    store, jid, eng = _setup(steward_home)
    runner, notes = _runner(store, eng)
    return store, jid, runner, notes


def _whats(store, jid, since):
    return [e.what for e in store.events(jid) if e.seq > since]


def _last_seq(store, jid):
    evs = store.events(jid)
    return evs[-1].seq if evs else 0


def test_auto_allowed_call_records_one_event_through_delivery(steward_home):
    store, jid, runner, notes = _perm_runner(steward_home)
    start = _last_seq(store, jid)
    runner._open_fabric_permission(jid, _perm_frame("r1", "ls -la"))
    [rec] = approvals_module.list_approvals(store, jid)
    assert rec["status"] == "approved" and rec["decided_by"] == "code"
    assert _whats(store, jid, start) == []  # nothing yet: the delivery result is not known
    approvals_module.mark_relayed(store, jid, rec["id"], ack_expected=True, relayed_by="i1")
    approvals_module.mark_delivery_acked(store, jid, rec["id"])
    assert _whats(store, jid, start) == ["permission_auto_allowed"]
    ev = store.events(jid)[-1]
    assert ev.rule == "permission_policy_allow" and ev.judgment.value == "code"
    assert ev.evidence["request_id"] == "r1" and ev.evidence["tool"] == "Bash" and ev.evidence["seq"] == 3
    assert ev.evidence["approval_id"] == rec["id"] and ev.evidence["policy_rule"]
    assert ev.evidence["delivery"] == "relayed" and ev.evidence["ack_expected"] is True
    rec = approvals_module.get_approval(store, jid, rec["id"])
    assert rec["delivery_acked_at"] and rec["relayed_at"] and not approvals_module.answer_in_flight(rec)
    assert notes == []


def test_auto_allowed_call_that_goes_stale_still_records_what_code_allowed(steward_home):
    store, jid, runner, _ = _perm_runner(steward_home)
    start = _last_seq(store, jid)
    runner._open_fabric_permission(jid, _perm_frame("r1", "ls"))
    [rec] = approvals_module.list_approvals(store, jid)
    approvals_module.mark_stale(store, jid, rec["id"], reason="worker no longer blocked")
    assert _whats(store, jid, start) == ["permission_auto_allowed", "approval stale"]
    assert store.events(jid)[-2].evidence["delivery"] == "stale"


def test_held_request_keeps_the_full_chain(steward_home):
    store, jid, runner, notes = _perm_runner(steward_home)
    start = _last_seq(store, jid)
    runner._open_fabric_permission(jid, _perm_frame("r1", "sudo tee /etc/passwd < x"))
    [rec] = approvals_module.list_approvals(store, jid)
    assert rec["status"] == "pending"
    assert _whats(store, jid, start)[:2] == ["approval opened", "worker permission requested"]
    approvals_module.decide(store, jid, rec["id"], "approve", decided_by="human")
    approvals_module.mark_relayed(store, jid, rec["id"], ack_expected=True)
    approvals_module.mark_delivery_acked(store, jid, rec["id"])
    whats = _whats(store, jid, start)
    for what in ("approval opened", "worker permission requested", "approval approved", "permission decision relayed",
                 "permission answer delivered"):
        assert what in whats, (what, whats)
    assert "permission_auto_allowed" not in whats


def test_code_denials_keep_the_full_chain(steward_home):
    from steward import grants  # noqa: F401
    from authority_fixtures import grant

    store, jid, runner, _ = _perm_runner(steward_home)
    grant(store, jid, "run_command:git push", kind="prohibit")
    start = _last_seq(store, jid)
    runner._open_fabric_permission(jid, _perm_frame("r1", "git push origin main"))
    assert _whats(store, jid, start) == ["approval opened", "worker permission requested", "approval denied",
                                         "owner prohibition denied permission"]


def test_supervisor_row_counts_single_events_as_requests(steward_home):
    store, jid, runner, _ = _perm_runner(steward_home)
    for i in range(3):
        runner._open_fabric_permission(jid, _perm_frame(f"r{i}", "ls", seq=10 + i))
    for rec in approvals_module.list_approvals(store, jid):
        approvals_module.mark_relayed(store, jid, rec["id"])
    runner._open_fabric_permission(jid, _perm_frame("held", "sudo tee /etc/passwd < x", seq=20))
    worker = next(r for r in supervisors.supervisor_rows(store, jid) if r["role"] == "worker")
    assert worker["stats"]["actions"] == 4


def test_old_history_is_not_double_counted(steward_home):
    """A pre-10b chain (request event + auto-allow event without `delivery`) still counts once."""
    from steward.models import Judgment

    store, jid, _, _ = _perm_runner(steward_home)
    store.append_event(jid, what="worker permission requested", evidence={"approval_id": "a"}, rule="r",
                       judgment=Judgment.code)
    store.append_event(jid, what="permission_auto_allowed", evidence={"approval_id": "a", "tool": "Bash"}, rule="r",
                       judgment=Judgment.code)
    worker = next(r for r in supervisors.supervisor_rows(store, jid) if r["role"] == "worker")
    assert worker["stats"]["actions"] == 1


# -- item 10c: heartbeat hysteresis, quiet inner verdicts ----------------------------------------------------------

from steward import heartbeat, inner, recovery  # noqa: E402
from steward.runner import record_class  # noqa: E402

C = heartbeat.Class


def _class_events(store, jid):
    return [(e.evidence["from"], e.evidence["to"]) for e in store.events(jid) if e.what == "heartbeat class changed"]


def _job_id(steward_home):
    store = Store(steward_home)
    return store, store.create_job("ship", "/tmp/x").job_id


def test_class_change_is_logged_after_two_polls_but_last_class_follows_every_poll(steward_home):
    store, jid = _job_id(steward_home)
    seen = []
    for cls in (C.progressing, C.quiet_known_process, C.progressing, C.quiet_known_process, C.progressing,
                C.waiting, C.waiting, C.waiting, C.progressing, C.progressing):
        record_class(store, jid, cls)
        seen.append(store.kv_get(jid, "last_class"))
    assert seen == ["progressing", "quiet_known_process", "progressing", "quiet_known_process", "progressing",
                    "waiting", "waiting", "waiting", "progressing", "progressing"]  # recovery reads this: unchanged
    assert _class_events(store, jid) == [(None, "progressing"), ("progressing", "waiting"), ("waiting", "progressing")]


def test_the_old_logging_is_what_a_flap_used_to_cost(steward_home):
    """Same sequence, pre-10c rule (log every change of last_class): 5 flaps -> 5 events instead of 1."""
    store, jid = _job_id(steward_home)
    flaps = (C.progressing, C.quiet_known_process, C.progressing, C.quiet_known_process, C.progressing)
    for cls in flaps:
        record_class(store, jid, cls)
    assert len(_class_events(store, jid)) == 1
    old = sum(1 for a, b in zip((None,) + flaps, flaps) if a != b)
    assert old == 5


@pytest.mark.parametrize("bad", [C.suspect_stall, C.dead])
def test_stall_and_dead_are_logged_at_once_and_so_is_leaving_them(steward_home, bad):
    store, jid = _job_id(steward_home)
    record_class(store, jid, C.progressing)
    record_class(store, jid, bad)
    assert _class_events(store, jid)[-1] == ("progressing", bad.value)
    record_class(store, jid, C.progressing)
    assert _class_events(store, jid)[-1] == (bad.value, "progressing")


def test_a_fresh_attempt_logs_its_first_class_at_once(steward_home):
    store, jid = _job_id(steward_home)
    record_class(store, jid, C.progressing)
    record_class(store, jid, C.progressing)
    store.kv_set(jid, "last_class", None)  # what a worker replacement / send-back does
    record_class(store, jid, C.progressing)
    assert _class_events(store, jid) == [(None, "progressing"), (None, "progressing")]


def test_existing_jobs_without_a_logged_class_start_from_last_class(steward_home):
    store, jid = _job_id(steward_home)
    store.kv_set(jid, "last_class", "waiting")  # recorded by the pre-10c runner
    record_class(store, jid, C.waiting)
    assert _class_events(store, jid) == []
    record_class(store, jid, C.progressing)
    record_class(store, jid, C.progressing)
    assert _class_events(store, jid) == [("waiting", "progressing")]


def _verdict(cls, kind="none", rule="no_recovery_needed"):
    from steward.models import Judgment

    return inner.inner_verdict(cls, recovery.Action(kind=kind, detail="", judgment=Judgment.code, rule=rule), False)


def test_continue_no_recovery_verdict_is_not_logged_on_a_state_flip(steward_home):
    store, jid = _job_id(steward_home)
    assert inner.record_if_changed(store, jid, _verdict(C.progressing)) is True  # the first verdict is recorded
    assert inner.record_if_changed(store, jid, _verdict(C.quiet_known_process)) is False
    assert inner.record_if_changed(store, jid, _verdict(C.progressing)) is False
    assert store.kv_get(jid, "last_inner_verdict") == {"state": "executing", "action": "continue"}
    assert inner.record_if_changed(store, jid, _verdict(C.suspect_stall, "guide", "stall_guidance")) is True
    assert inner.record_if_changed(store, jid, _verdict(C.progressing)) is True  # back to continue: a real change
    whats = [e.evidence["action"] for e in store.events(jid) if e.what == "inner_verdict"]
    assert whats == ["continue", "repair", "continue"]


# -- item 10d: a re-escalated owner decision is not a new tech_lead_escalated ------------------------------------


def test_repeated_lead_escalation_records_one_escalated_event(steward_home):
    from steward import blockers
    from steward import ownership
    from ownership_fixtures import Notes, control_lead, park_tracks
    from test_ownership_lead_actions import _cycle, _escalate, _ready

    store = Store(steward_home)
    notes = Notes()
    jid = park_tracks(store)
    lead, _ = control_lead(store, {"astra-tech-lead": [_escalate(), _escalate()]}, notifier=notes)
    t = _ready(store, jid)
    _cycle(store, lead, t, notes)
    tracks = next(b for b in blockers.open_blockers(store, jid) if b.kind == "worker_parked_unfinished")
    blockers.add_evidence(store, tracks.blocker_id, ["event:new"], actor=ownership.CONTROLLER, now=t + 10)
    _cycle(store, lead, t + 50, notes)  # the same decision again: same blocker, same approval
    escalated = [e for e in store.events(jid) if e.what == "tech_lead_escalated"]
    appended = [e for e in store.events(jid) if e.what == "tech_lead_escalation_appended"]
    assert len(escalated) == 1 and len(appended) == 1
    assert appended[0].evidence["approval_id"] == escalated[0].evidence["approval_id"]
    assert appended[0].evidence["kind"] == escalated[0].evidence["kind"] == "lead_escalation"
    assert notes.kinds() == ["NEEDS_YOU"]


# -- item 10e: compact check results ------------------------------------------------------------------------------

from steward.store import compact_results, expand_results  # noqa: E402

_RESULTS = {f"tests/test_{i // 50}.py::test_case_{i}": "passed" for i in range(2000)}
_RESULTS.update({"tests/test_x.py::test_broken": "failed", "tests/test_x.py::test_err": "error",
                 "tests/test_x.py::test_skip": "skipped", "tests/test_0.py::test_case_7": "failed"})


def test_check_results_round_trip_exactly_and_shrink(steward_home):
    store = Store(steward_home)
    store.add_check("j1", commit_sha="abc", command="pytest -q", exit_code=1, results=_RESULTS, label="baseline")
    [row] = store.get_checks("j1", label="baseline")
    assert row["results"] == _RESULTS and list(row["results"]) == list(_RESULTS)  # same outcomes, same order
    raw = store._conn.execute("SELECT results_json FROM checks").fetchone()[0]
    import json
    stored = json.loads(raw)
    assert stored["counts"] == {"passed": 2000 - 1, "failed": 2, "error": 1, "skipped": 1}
    assert stored["outcomes"] == {k: v for k, v in _RESULTS.items() if v != "passed"}
    assert len(raw) * 5 < len(json.dumps(_RESULTS))


def test_old_full_rows_still_read_and_compact_in_place(steward_home):
    import json
    store = Store(steward_home)
    with store._conn:
        store._conn.execute("INSERT INTO checks (job_id, commit_sha, command, label, exit_code, results_json, ts)"
                            " VALUES ('j1', 'abc', 'pytest', 'verify', 1, ?, 't')", (json.dumps(_RESULTS),))
        store._conn.execute("INSERT INTO checks (job_id, commit_sha, command, label, exit_code, results_json, ts)"
                            " VALUES ('j1', 'abc', 'ruff', 'verify', 0, '{}', 't')")
    before = store.get_checks("j1")
    assert before[0]["results"] == _RESULTS
    assert store.compact_checks(batch=1) == 1 and store.compact_checks() == 0
    assert store.get_checks("j1") == before
    assert json.loads(store._conn.execute("SELECT results_json FROM checks LIMIT 1").fetchone()[0])["v"] == 2


def test_empty_results_stay_empty(steward_home):
    store = Store(steward_home)
    store.add_check("j1", commit_sha="abc", command="ruff", exit_code=0, results={}, label="verify")
    assert store.get_checks("j1")[0]["results"] == {} and compact_results({}) == {} and expand_results({}) == {}


def test_counts_and_attribution_read_the_same_from_compact_rows(steward_home):
    """The readers that need more than the failing set: pass/fail counts, and closeout's re-attribution, which
    counts a baseline failure as fixed only when that test ran again and passed."""
    from steward import evidence

    store = Store(steward_home)
    base = dict(_RESULTS)
    now = {**_RESULTS, "tests/test_0.py::test_case_7": "passed", "tests/test_x.py::test_new": "failed"}
    store.add_check("j1", commit_sha="b0", command="pytest -q", exit_code=1, results=base, label="baseline")
    store.add_check("j1", commit_sha="h1", command="pytest -q", exit_code=1, results=now, label="verify")
    assert evidence._pass_fail(store, "j1", "verify", "h1") == (len(now) - 3, 3)
    assert cli._check_pass_fail(store, "j1", "baseline") == (len(base) - 3, 3)


# -- item 10a: approvals table ------------------------------------------------------------------------------------


def _legacy(jid, n, status="approved"):
    return [{"id": f"a{i}", "job_id": jid, "question": f"q{i}", "status": status, "kind": "permission",
             "created_at": f"2026-10-01T00:00:{i:02d}+00:00", "decided_at": None, "decided_by": "code",
             "relayed_at": None, "worker_request_id": f"r{i}"} for i in range(n)]


def _job_in(store, state):

    jid = store.create_job("ship", "/tmp/x").job_id
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, jid))
    return jid


def test_a_finished_jobs_legacy_list_moves_to_the_table_on_read(steward_home):
    store = Store(steward_home)
    jid = _job_in(store, JobState.ACCEPTED)
    store.kv_set(jid, "approvals", _legacy(jid, 5))
    assert approvals_module.list_approvals(store, jid) == _legacy(jid, 5)
    assert store.kv_get(jid, "approvals") is None and len(store.approval_rows(jid)) == 5
    assert approvals_module.list_approvals(store, jid) == _legacy(jid, 5)  # same records, same order


def test_bulk_migration_moves_finished_jobs_only(steward_home):
    store = Store(steward_home)
    done = [_job_in(store, s) for s in (JobState.ACCEPTED, JobState.RELEASED, JobState.STOPPED)]
    live = _job_in(store, JobState.RUNNING)
    for jid in done + [live]:
        store.kv_set(jid, "approvals", _legacy(jid, 3))
    assert approvals_module.migrate_finished(store) == 9
    assert all(store.kv_get(j, "approvals") is None for j in done)
    assert store.kv_get(live, "approvals") == _legacy(live, 3)  # a previous runner may still own it


def test_a_live_unmigrated_job_keeps_its_kv_list_current_for_a_previous_runner(steward_home):
    """Mixed versions after a deploy: the old runner reads only kv['approvals']. A decision on one of its records is
    written back to the kv list; a record new code opens goes to the table and leaves the list alone."""
    store = Store(steward_home)
    jid = _job_in(store, JobState.RUNNING)
    store.kv_set(jid, "approvals", _legacy(jid, 2, status="pending"))
    approvals_module.decide(store, jid, "a1", "approve", decided_by="human")
    assert [a["status"] for a in store.kv_get(jid, "approvals")] == ["pending", "approved"]
    rec = approvals_module.open_approval(store, jid, "tech lead: look", kind="tech_lead")
    assert [a["id"] for a in store.kv_get(jid, "approvals")] == ["a0", "a1"]
    assert [a["id"] for a in approvals_module.list_approvals(store, jid)] == ["a0", "a1", rec["id"]]
    assert approvals_module.get_approval(store, jid, "a1")["status"] == "approved"


def test_a_runner_of_this_version_migrates_the_job_it_takes(steward_home):
    store = Store(steward_home)
    jid = _job_in(store, JobState.RUNNING)
    store.kv_set(jid, "approvals", _legacy(jid, 2))
    assert approvals_module.migrate(store, jid) == 2 and approvals_module.migrate(store, jid) == 0
    assert store.kv_get(jid, "approvals") is None
    assert [a["id"] for a in approvals_module.list_approvals(store, jid)] == ["a0", "a1"]


def test_runner_start_migrates(steward_home, monkeypatch):
    from steward import runner as runner_module

    seen = []
    monkeypatch.setattr(approvals_module, "migrate", lambda store, jid: seen.append(jid) or 0)
    r = runner_module.Runner.__new__(runner_module.Runner)
    r.store = Store(steward_home)
    with pytest.raises(RuntimeError):  # no contract: start() stops right after taking the job
        r.start("job-x")
    assert seen == ["job-x"]


def test_a_decision_rewrites_one_row_not_the_history(steward_home, monkeypatch):
    store = Store(steward_home)
    jid = _job_in(store, JobState.RUNNING)
    for i in range(50):
        approvals_module.open_approval(store, jid, f"q{i}", kind="permission", worker_request_id=f"r{i}")
    target = approvals_module.list_approvals(store, jid)[25]["id"]
    calls = []
    real = store.approval_write
    monkeypatch.setattr(store, "approval_write", lambda j, ins, upd, kv=None: calls.append((ins, upd, kv)) or real(j, ins, upd, kv))
    approvals_module.decide(store, jid, target, "approve", decided_by="human")
    approvals_module.mark_relayed(store, jid, target, ack_expected=True)
    approvals_module.mark_delivery_acked(store, jid, target)
    assert [(len(i), [u[1]["id"] for u in up], kv) for i, up, kv in calls] == [(0, [target], None)] * 3
    assert approvals_module.get_approval(store, jid, target)["delivery_acked_at"]


def test_saving_the_same_list_twice_updates_the_inserted_row(steward_home):
    store = Store(steward_home)
    jid = _job_in(store, JobState.RUNNING)
    records = approvals_module._load(store, jid)
    records.append({"id": "x", "job_id": jid, "question": "q", "status": "pending", "kind": "general",
                    "created_at": "2026-10-05T00:00:00+00:00"})
    approvals_module._save(store, jid, records)
    records[0]["status"] = "approved"
    approvals_module._save(store, jid, records)
    assert [a["status"] for a in approvals_module.list_approvals(store, jid)] == ["approved"]
    assert len(store.approval_rows(jid)) == 1


# -- item 11: tokens cache, one row per transcript ----------------------------------------------------------------

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from steward import tokens  # noqa: E402
from test_perf_reads import NOW as T_NOW, _msg, _review_request, _worker_transcript, astra  # noqa: E402,F401


def _plain_result(store, jobs, proj):
    """The same pass over a plain in-memory dict (the long-lived efficiency_watch cache): the reference answer."""
    cache = {}
    return {j: tokens.job_tokens(store, j, T_NOW, proj, _cache=cache) for j in jobs}


def test_sqlite_cache_answers_like_a_plain_dict_cold_and_warm(astra):
    store, a, b, proj, review = astra
    _worker_transcript(store, a, proj).write_text(_msg("w1", T_NOW - 60) + "\n")
    _review_request(store, a, "r-a", T_NOW - 300)
    _review_request(store, b, "r-b", T_NOW - 120)
    review("ra", T_NOW - 298, "ma")
    review("rb", T_NOW - 118, "mb")
    for i in range(20):
        review(f"other{i}", T_NOW - 5000 - i, f"o{i}")  # nobody's
    ref = _plain_result(store, [a, b], proj)
    cold = tokens.all_tokens(store, [a, b], now=T_NOW, projects_dir=proj)["jobs"]
    warm = tokens.all_tokens(store, [a, b], now=T_NOW, projects_dir=proj)["jobs"]
    assert cold == warm == ref
    assert len(tokens.cache_entries(Path(store.home))) == 23


def test_a_warm_pass_decodes_only_the_entries_it_counts(astra, monkeypatch):
    store, a, b, proj, review = astra
    _review_request(store, a, "r-a", T_NOW - 300)
    review("ra", T_NOW - 298, "ma")
    for i in range(30):
        review(f"other{i}", T_NOW - 5000 - i, f"o{i}")
    tokens.all_tokens(store, [a, b], now=T_NOW, projects_dir=proj)
    loaded = []
    real = tokens._load_cache
    monkeypatch.setattr(tokens, "_load_cache", lambda home: loaded.append(real(home)) or loaded[-1])
    tokens.all_tokens(store, [a, b], now=T_NOW, projects_dir=proj)
    entries = [k for k in loaded[-1] if not k.startswith("__")]
    assert [Path(k).stem for k in entries] == ["ra"]  # 30 unmatched advisor transcripts: peeked, never decoded


def test_only_the_grown_transcript_is_written(astra, monkeypatch):
    store, a, b, proj, review = astra
    path = _worker_transcript(store, a, proj)
    path.write_text(_msg("m1", T_NOW - 60) + "\n")
    for i in range(10):
        review(f"other{i}", T_NOW - 5000 - i, f"o{i}")
    tokens.all_tokens(store, [a, b], now=T_NOW, projects_dir=proj)
    written = []
    real = tokens._row
    monkeypatch.setattr(tokens, "_row", lambda p, ent: written.append(p) or real(p, ent))
    with path.open("a") as fh:
        fh.write(_msg("m2", T_NOW - 30) + "\n")
    assert tokens.all_tokens(store, [a, b], now=T_NOW, projects_dir=proj)["jobs"][a]["by_source"]["worker"] == 20
    assert written == [str(path)]


def test_the_old_json_cache_is_imported_once(steward_home, tmp_path):
    home = Path(steward_home)
    old = {"/p/x.jsonl": {"sig": [1, 2], "rates": tokens.RATES_VERSION, "astra": False, "start": 0, "msgs": []},
           "__astra_requests__": []}
    (home / "tokens-cache.json").write_text(json.dumps(old))
    assert tokens.cache_entries(home) == {"/p/x.jsonl": old["/p/x.jsonl"]}
    (home / "tokens-cache.json").write_text(json.dumps({"/p/y.jsonl": {"sig": [3, 4]}}))
    tokens._DBS.clear()  # a new process
    assert list(tokens.cache_entries(home)) == ["/p/x.jsonl"]  # imported once, never again


# -- item 12: a ping never waits for a whole tick -----------------------------------------------------------------

from steward import control_pane  # noqa: E402
from steward.fabric import Frame  # noqa: E402


class _Inbox:
    def __init__(self, order):
        self.queue, self.order = [], order

    def read(self, timeout):
        out, self.queue = self.queue, []
        return out


class _NoApi:
    def call(self, method, params):
        return {}

    def send(self, *a):
        pass


def _ping(rid):
    return Frame(v=1, seq=1, job=rid, type="cmd", body={"request_id": rid, "op": "ping", "args": {}, "origin": "cli"})


def _ticking_pane(steward_home, order):
    return control_pane.ControlPane(Store(steward_home), {"ping": lambda cmd: order.append("ping") or {"accepted": True}},
                                    print_fn=order.append)


def _once(pane, inbox, tick):
    runs = iter([True, False])
    pane.run_forever(inbox, _NoApi(), own_pane=None, poll_s=0.0, running=lambda: next(runs), tick=tick)


def test_a_ping_during_a_slow_step_is_answered_before_the_next_step(steward_home):
    order = []
    inbox = _Inbox(order)
    pane = _ticking_pane(steward_home, order)

    def sweep():  # the slow step: a ping lands while it runs
        order.append("sweep")
        inbox.queue.append(_ping("p1"))

    _once(pane, inbox, control_pane.TickSteps(sweep, lambda: order.append("refresh"), lambda: order.append("tidy")))
    steps = [x for x in order if x in ("sweep", "ping", "refresh", "tidy")]
    assert steps == ["sweep", "ping", "refresh", "tidy"]
    assert control_pane.get_receipt(pane.store, "p1")["accepted"] is True


def test_a_plain_callable_tick_still_runs_whole(steward_home):
    order = []
    inbox = _Inbox(order)
    pane = _ticking_pane(steward_home, order)

    def tick():
        order.append("sweep")
        inbox.queue.append(_ping("p1"))
        order.append("refresh")

    _once(pane, inbox, tick)
    assert [x for x in order if x in ("sweep", "ping", "refresh")] == ["sweep", "refresh"]  # answered next step


def test_a_failing_step_ends_the_tick_like_before(steward_home):
    order = []
    pane = _ticking_pane(steward_home, order)

    def boom():
        raise RuntimeError("boom")

    _once(pane, _Inbox(order), control_pane.TickSteps(lambda: order.append("a"), boom, lambda: order.append("c")))
    assert "a" in order and "c" not in order and "tick error: boom" in order
    called = []
    with pytest.raises(RuntimeError):
        control_pane.TickSteps(lambda: called.append(1), boom, lambda: called.append(3))()
    assert called == [1]


# -- item 12b: the remaining full-history reads answer as the full scan did -------------------------------------

from steward import autopilot, ownership, questions  # noqa: E402
from steward import resolver as resolver_module  # noqa: E402
from steward import trajectory as trajectory_module  # noqa: E402
from steward.models import Judgment  # noqa: E402
from test_perf_reads import _noise  # noqa: E402


def _e(store, jid, what, rule="r", **ev):
    return store.append_event(jid, what=what, evidence=ev, rule=rule, judgment=Judgment.code)


def _two_jobs(steward_home):
    store = Store(steward_home)
    a, b = store.create_job("a", "/r").job_id, store.create_job("b", "/r").job_id
    _noise(store, a, b)
    return store, a, b


def test_events_where_and_prefixed_match_python_filters(steward_home):
    store, a, b = _two_jobs(steward_home)
    for i in range(3):
        _e(store, a, "resolver_decision", tier="floor", i=i)
        _e(store, b, "resolver_decision", tier="floor", i=i)
        _e(store, a, "trajectory_advice_received", tier="routine", i=i)
        _e(store, a, "floor_action", i=i)
        _noise(store, a, n=2)
    full = store.events(a)
    got = store.events_where(a, whats=("floor_action",), evidence={"tier": "floor"})
    assert got == [e for e in full if e.what == "floor_action" or e.evidence.get("tier") == "floor"]
    assert store.events_prefixed(a, "trajectory_") == [e for e in full if e.what.startswith("trajectory_")]
    assert store.events_where(a) == []


def _floor_reason_full_scan(store, job_id):  # the pre-12b loop, verbatim
    for e in store.events(job_id):
        aid = e.evidence.get("approval_id")
        if e.evidence.get("tier") == "floor" and aid:
            a = approvals_module.get_approval(store, job_id, str(aid))
            if a is not None and a.get("status") == "approved" and a.get("relayed_at"):
                return "job did a floor action"
        elif e.evidence.get("tier") == "floor" or e.what == "floor_action":
            return "job did a floor action"
    return None


@pytest.mark.parametrize("case", ["none", "held_floor", "approved_relayed", "floor_action", "tier_no_approval"])
def test_floor_reason_matches_the_full_scan(steward_home, case):
    store, a, b = _two_jobs(steward_home)
    _e(store, b, "floor_action")  # another job's
    rec = approvals_module.open_approval(store, a, "q", kind="permission")
    if case == "held_floor":
        _e(store, a, "resolver_held", tier="floor", approval_id=rec["id"])
    elif case == "approved_relayed":
        _e(store, a, "resolver_held", tier="floor", approval_id=rec["id"])
        approvals_module.decide(store, a, rec["id"], "approve", decided_by="human")
        approvals_module.mark_relayed(store, a, rec["id"])
    elif case == "floor_action":
        _e(store, a, "floor_action")
    elif case == "tier_no_approval":
        _e(store, a, "something", tier="floor")
    _noise(store, a)
    assert autopilot.floor_reason(store, a) == _floor_reason_full_scan(store, a)
    assert (autopilot.floor_reason(store, a) is None) == (case in ("none", "held_floor"))


def test_merged_branch_is_the_newest_incorporated(steward_home):
    store, a, b = _two_jobs(steward_home)
    assert questions.merged_branch(store, a) == ""
    _e(store, a, "incorporated", source_branch="one")
    _noise(store, a)
    _e(store, a, "incorporated", source_branch="two")
    _e(store, b, "incorporated", source_branch="other")
    assert questions.merged_branch(store, a) == "two"


def test_resolver_decisions_and_reopen_count_match_the_full_scan(steward_home):
    store, a, b = _two_jobs(steward_home)
    for i in range(3):
        _e(store, a, "resolver_decision", i=i)
        _e(store, a, "tech_lead_reopen", i=i)
        _noise(store, a)
    _e(store, a, ownership.STRATEGY_EVENT)
    _e(store, a, "tech_lead_reopen")
    _e(store, b, "tech_lead_reopen")
    assert resolver_module.decisions(store, a) == [e for e in store.events(a) if e.what == "resolver_decision"]
    assert ownership.reopens_since_strategy_review(store, a) == 1


def test_park_reads_match_the_full_scan(steward_home):
    store, a, b = _two_jobs(steward_home)
    for what, rule in (("worker said done", "fabric_done_frame"), ("checks passed; acceptance unresolved",
                                                                   "acceptance_unresolved"),
                       ("worker idle", "idle_after_final_message")):
        _e(store, a, what, rule=rule, reasons=["test-weakening detected: x"])
        _noise(store, a)
        full = store.events(a)
        assert ownership.park_event(ownership._park_events(store, a)) == ownership.park_event(full)
        assert ownership.classify_needs_human(store, a, ownership._park_events(store, a)) == \
            ownership.classify_needs_human(store, a, full)
    assert ownership._park_events(store, a)[-1] == store.events(a)[-1]
    fresh = store.create_job("c", "/r").job_id
    _noise(store, fresh)
    assert ownership.classify_needs_human(store, fresh, ownership._park_events(store, fresh)) == \
        ownership.classify_needs_human(store, fresh, store.events(fresh))
    assert ownership._park_events(store, store.create_job("d", "/r").job_id) == []


def test_review_history_matches_the_full_scan(steward_home, monkeypatch):
    store, a, b = _two_jobs(steward_home)
    ledger = trajectory_module.TrajectoryLedger(store, a)
    for i in range(3):
        ledger.add_review(f"rv{i}", ["cadence"], now=1000.0 + i)
        _e(store, a, "trajectory_review_requested", review_id=f"rv{i}")
        _e(store, a, "trajectory_advice_received", review_id=f"rv{i}")
        _e(store, a, "unrelated", review_id=f"rv{i}")
        _noise(store, a)
    got = trajectory_module.review_history(store, a)
    assert [r["decision"] for r in got] == ["trajectory_advice_received"] * 3
    monkeypatch.setattr(store, "events_prefixed", lambda jid, prefix: store.events(jid))
    assert got == trajectory_module.review_history(store, a)


def test_a_status_filtered_list_decodes_only_its_rows_and_matches_the_full_list(steward_home, monkeypatch):
    store = Store(steward_home)
    jid = _job_in(store, JobState.RUNNING)
    store.kv_set(jid, "approvals", _legacy(jid, 3, status="pending"))  # an unmigrated live job: union read
    for i in range(20):
        rec = approvals_module.open_approval(store, jid, f"q{i}", kind="permission", worker_request_id=f"t{i}")
        if i % 4:
            approvals_module.decide(store, jid, rec["id"], "approve", decided_by="code")
    full = approvals_module.list_approvals(store, jid)
    decoded = []
    real = json.loads
    monkeypatch.setattr(approvals_module.json, "loads", lambda raw, *a, **k: decoded.append(1) or real(raw, *a, **k))
    pending = approvals_module.list_approvals(store, jid, status="pending")
    assert pending == [a for a in full if a["status"] == "pending"] and len(pending) == 3 + 5
    assert len(decoded) == 5 + 1  # the 5 pending table rows, plus the legacy kv list (one decode)
