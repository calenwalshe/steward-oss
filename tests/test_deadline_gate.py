"""v2.2 Phase 22 Tasks 4–5 (DL-14, DL-15, DL-16, DL-17, DL-18): candidate report, Jev labels, calibration, read purity,
and the three-run fake-clock replay through the real Store, runner, closeout and control-pane sweep."""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest

from deadline_replay import T0, Replay
from steward import autopilot, deadline, deadline_outbox, deadline_status, efficiency_watch
from steward.api import StewardAPI
from steward.control import Controller
from steward.models import ControlRequest, JobState, Judgment
from steward.runner_exec import FakeRunner
from steward.store import Store


class FakeNotifier:
    def __init__(self, mode="ok"):
        self.sent, self.mode = [], mode

    def send(self, event):
        self.sent.append([event.kind, event.title, event.body, str(event.revision)])
        if self.mode == "raise":
            raise OSError("connection reset during send")
        return self.mode == "ok"


@pytest.fixture(autouse=True)
def _channel(monkeypatch):
    monkeypatch.setenv("STEWARD_NOTIFIER", "telegram")


def _fingerprint(store):
    ev = store._conn.execute("SELECT seq, what, evidence FROM events ORDER BY seq").fetchall()
    kv = store._conn.execute("SELECT job_id, key, value_json FROM kv ORDER BY job_id, key").fetchall()
    ob = store._conn.execute("SELECT * FROM deadline_outbox ORDER BY draft_key").fetchall()
    return [tuple(r) for r in ev], [tuple(r) for r in kv], [tuple(r) for r in ob]


# -- the full timeline ----------------------------------------------------------------------------------------

def timeline(home, tmp) -> tuple[Replay, dict]:
    """On time, then late; a plan revision; a paused job swept by the control pane; two racing control sweeps; a miss
    and a warning; downtime with catch-up; approvals with failure, crash-mid-send and success; a late candidate."""
    r = Replay(home, tmp, mode="active")
    log: dict = {"notifier": FakeNotifier()}
    r.at(1).tick()
    r.at(5).plan(m1=60, m2=120, cand=200)
    r.at(6).tick()
    r.at(50).implement(with_test=False)                       # M1 evidence (importer.py) before its target
    r.at(61).tick()                                           # M1 target: check-in, met (On track runs check in too)
    r.at(70).plan(m1=60, m2=150, cand=200)                    # M2 moves 21:00Z -> 22:30Z: both slots kept
    r.at(71).tick()
    Controller(r.store).submit(ControlRequest(request_id="pause", job_id=r.job_id, kind="pause"))
    r.at(121)
    barrier = threading.Barrier(2)

    def sweep_from_another_process():
        own = Store(r.home)
        barrier.wait()
        r.control(own)

    threads = [threading.Thread(target=sweep_from_another_process) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    Controller(r.store).submit(ControlRequest(request_id="resume", job_id=r.job_id, kind="resume"))
    r.at(161).tick()                                          # M2 @150 + 9 min grace: missed, warning drafted
    r.at(252).restart()                                       # down across the candidate and due targets
    r.tick()                                                  # both recorded individually + one catch-up draft
    log["catchup"] = [d["draft_key"] for d in r.drafts("catchup")]
    m1_key = "checkin:1:M1:2026-10-02T21:00:00Z"
    r.approve(m1_key, "approve-m1")                           # attempt 1: the notifier reports failure
    log["fail"] = r.deliver(FakeNotifier("fail"))
    log["fail_again"] = r.deliver(log["notifier"])            # no auto-retry on the old approval
    r.approve(m1_key, "approve-m1-retry")                     # attempt 2: a new exact approval
    claimed = deadline_outbox._claim(Store(r.home), "crashed-sender", r.t.timestamp(), 30.0)  # crash mid-send
    log["claimed"] = bool(claimed)
    r.at(254).restart()
    log["after_crash"] = r.deliver(log["notifier"])           # expired lease: unknown, never resent
    r.approve(log["catchup"][0], "approve-catchup")
    log["sent"] = r.deliver(log["notifier"])                  # exactly one call for the batch
    r.at(255).restart()
    log["after_success_restart"] = r.deliver(log["notifier"])
    r.at(258).commit({"tests/test_importer.py": "from importer import rows\n\n\ndef test_rows():\n"
                      "    assert rows('a,b') == [['a', 'b']]\n"}, "tests")
    r.at(260).cleanup()
    r.at(261).finish()                                        # closeout: per-item proof -> candidate ready (late)
    r.at(262).tick()
    return r, log


def test_three_replays_have_identical_event_and_notifier_digests(steward_home, tmp_path):
    digests = []
    for i in range(3):
        r, log = timeline(tmp_path / f"home{i}", tmp_path / f"run{i}")
        digests.append(r.digest(extra=[log["notifier"].sent, log["fail"], log["after_crash"], log["sent"]]))
    assert digests[0] == digests[1] == digests[2]


def test_timeline_facts(steward_home, tmp_path):
    r, log = timeline(tmp_path / "home", tmp_path / "run")
    checkins = [e.evidence["slot"] for e in r.whats("deadline_checkin")]
    assert checkins == ["1:M1:2026-10-02T21:00:00Z", "1:M2:2026-10-02T22:00:00Z", "1:M2:2026-10-02T22:30:00Z",
                        "1:candidate:2026-10-02T23:20:00Z", "1:due:2026-10-03T00:00:00Z"]
    assert len(set(checkins)) == len(checkins)  # each original and revised slot exactly once (racing sweeps too)
    m2_old = r.whats("deadline_checkin")[1].evidence
    assert m2_old["superseded"] and m2_old["replacement_target"] == "2026-10-02T22:30:00Z"
    missed = [e.evidence["slot"] for e in r.whats("deadline_milestone_missed")]
    assert "1:M2:2026-10-02T22:30:00Z" in missed
    assert r.drafts("warning") and len(log["catchup"]) == 1
    assert [x["state"] for x in log["fail"]] == ["failed"] and log["fail_again"] == []
    assert log["claimed"] and log["after_crash"] == [] and log["after_success_restart"] == []
    assert [x["state"] for x in log["sent"]] == ["sent"] and len(log["notifier"].sent) == 1
    assert log["notifier"].sent[0][3] == f"{log['catchup'][0]}#1"
    states = [a["state"] for a in deadline_outbox.attempts(r.store, "checkin:1:M1:2026-10-02T21:00:00Z")]
    assert states == ["failed", "unknown"]
    unapproved = [d for d in r.drafts() if d["state"] == "local"]
    assert unapproved  # many local drafts exist; none was ever sent
    assert r.state() == JobState.NEEDS_HUMAN
    assert any(e.what == "deadline_candidate_ready" for e in r.store.events(r.job_id))
    rep = deadline_status.report(r.store, r.job_id)
    assert rep["ready"] and rep["on_time"] is False and rep["late_by_min"] > 0
    assert [i["outcome"] for i in rep["items"] if i["item"] == "R1"] == ["proven"]
    assert any(m["missed"] for m in rep["milestones"]) and rep["commitment_revisions"][0]["judgment"] == "human"
    assert len(r.whats("deadline_candidate_report")) == 1


def test_candidate_never_auto_merges_even_after_human_accept_and_clear(steward_home, tmp_path):
    r, _ = timeline(tmp_path / "home", tmp_path / "run")
    from steward import closeout

    closeout.accept(r.store, r.job_id)
    deadline.revise(r.store, r.job_id, action="clear", judgment=Judgment.human, origin="cli", request_id="c",
                    reason="done")
    assert autopilot.ensure_attempted(r.store, r.job_id, FakeRunner())["outcome"] == "skipped"
    assert r.runner._terminal_exit(r.job_id) is not None and r.store.kv_get(r.job_id, autopilot.ATTEMPT_KEY) is None


def test_required_item_omission_blocks_ready_and_optional_drop_is_reported(steward_home, tmp_path):
    r = Replay(tmp_path / "home", tmp_path / "run", mode="active")
    r.at(5).plan(m1=60, cand=200, items=[
        {"id": "R1", "class": "required", "evidence": {"paths": ["importer.py"],
                                                       "tests": ["tests/test_importer.py::test_rows"]}},
        {"id": "O1", "class": "optional"}], extra={"dropped": ["O1"]})
    r.at(6).tick()
    r.at(30).commit({"tests/test_importer.py": "def test_rows():\n    assert True\n"}, "tests, no importer")
    r.at(40).cleanup()
    r.at(41).finish()
    rep = deadline_status.report(r.store, r.job_id)
    assert not rep["ready"] and rep["optional_dropped"] == ["O1"]
    assert any("R1" in u for u in rep["unresolved"])
    assert not any(e.what == "deadline_candidate_ready" for e in r.store.events(r.job_id))


# -- ETA ownership, read purity, Jev, calibration ------------------------------------------------------------------

def _eta_bytes(r):
    return json.dumps(r.store.kv_get(r.job_id, efficiency_watch.STATE_KEY), sort_keys=True)


def test_eta_state_is_byte_equal_with_deadline_supervision_on_and_off(steward_home, tmp_path, monkeypatch):
    import sys

    from steward import store as store_module

    clock = {"t": T0}
    real = store_module.now_iso
    for name, mod in list(sys.modules.items()):  # event/job stamps follow the replay clock, not the wall clock
        if name.startswith("steward") and getattr(mod, "now_iso", None) is real:
            monkeypatch.setattr(mod, "now_iso", lambda: clock["t"].isoformat())
    out = []
    for i, mode in enumerate(("active", "off")):
        clock["t"] = T0
        r = Replay(tmp_path / f"h{i}", tmp_path / f"r{i}", eta=True, mode=mode)
        r.at = (lambda rr: (lambda minutes: (clock.__setitem__("t", T0 + timedelta(minutes=minutes)),
                                             Replay.at(rr, minutes))[1]))(r)
        fake_clock = (lambda: clock["t"].timestamp())
        r.runner.efficiency = efficiency_watch.EfficiencyWatcher(r.store, notify=r.runner._notify, clock=fake_clock,
                                                                 spend_clock=fake_clock, tokens_fn=r.runner.spend_fn)
        if mode == "off":
            deadline.set_mode(r.store, r.job_id, "off", judgment=Judgment.human, origin="cli", request_id="off")
        r.at(5).plan(m1=60, cand=200)
        for m in (6, 30, 61, 90):
            r.at(m).tick()
        state = r.store.kv_get(r.job_id, efficiency_watch.STATE_KEY) or {}
        out.append(json.dumps(state.get("eta") or {}, sort_keys=True))
    assert out[0] == out[1]


def test_all_read_surfaces_including_notices_and_report_are_mutation_free(steward_home, tmp_path, capsys):
    from steward import cli, mcp_server, phone

    r, _ = timeline(tmp_path / "home", tmp_path / "run")
    store = Store(r.home)
    before = _fingerprint(store)
    api = StewardAPI(store)
    tools = {t.name: t for t in mcp_server.build_tools(api)}
    import os

    os.environ["STEWARD_HOME"] = str(r.home)
    try:
        for _ in range(2):
            for argv in (["deadline", r.job_id], ["deadline", r.job_id, "notices"], ["deadline", r.job_id, "report"],
                         ["deadline", r.job_id, "notice", "checkin:1:M1:2026-10-02T21:00:00Z", "show"],
                         ["status", r.job_id], ["explain", r.job_id]):
                assert cli.main(argv) == 0, argv
            got = api.get_job(r.job_id)["deadline"]
            tools["get_job"].handler({"job_id": r.job_id}, None)
            phone.home_lines(phone.gather(store, None), 80)
    finally:
        os.environ.pop("STEWARD_HOME", None)
    assert _fingerprint(store) == before
    assert got["candidate"]["ready"] and got["notices"] and got["slots"]
    out = capsys.readouterr().out
    assert "candidate" in out and "merge" in out


def test_jev_labels_ride_the_triage_budget_and_never_move_status(steward_home, tmp_path):
    r = Replay(tmp_path / "home", tmp_path / "run", mode="active")
    r.at(5).plan(m1=60, cand=200)
    r.at(6).tick()
    qs = deadline.jev_questions(r.store, r.job_id, used=0)
    assert set(qs) == {"deadline_milestone", "deadline_required_work", "deadline_kind"}
    assert set(deadline.jev_questions(r.store, r.job_id, used=5)) == {"deadline_milestone", "deadline_required_work"}
    assert deadline.jev_questions(r.store, r.job_id, used=7) == {}
    before = deadline_status.project(r.store, r.job_id, r.t)["status"]
    for _ in range(3):
        deadline.record_jev(r.store, r.job_id, {"deadline_required_work": {"choice": "yes", "confidence": 0.95},
                                                "deadline_milestone": {"choice": "candidate", "confidence": 0.9}},
                            r.t.timestamp())
    r.at(7).tick()
    p = deadline_status.project(r.store, r.job_id, r.t)
    assert p["jev_labels"]["required_work"] == "yes" and p["status"] == before  # a label alone changes nothing
    assert not r.whats("deadline_milestone_met")


def test_jev_outage_is_a_no_op(steward_home, tmp_path, monkeypatch):
    r = Replay(tmp_path / "home", tmp_path / "run", mode="active")
    monkeypatch.setattr(deadline, "jev_questions", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("jev down")))
    r.at(5).plan(m1=60, cand=200)
    r.at(6).tick()
    assert r.project()["plan"]["source"] == "worker" and not r.whats("deadline_error")


def test_calibration_is_descriptive_atomic_and_survives_corruption(steward_home, tmp_path):
    r, _ = timeline(tmp_path / "home", tmp_path / "run")
    rows = deadline_status.load_calibration(r.store)
    assert len(rows) == 1 and rows[0]["outcome"] == "late" and rows[0]["window_min"] == 240.0
    summary = deadline_status.calibration_summary(r.store)
    assert summary["n_jobs"] == 1 and "descriptive only" in summary["note"]
    (r.home / deadline_status.CALIBRATION_FILE).write_text("{not json")
    assert deadline_status.load_calibration(r.store) == []
    deadline_status.record_calibration(r.store, r.job_id, outcome="late")
    assert len(deadline_status.load_calibration(r.store)) == 1


def test_trust_floor_after_due_work_continues_and_due_time_is_fixed(steward_home, tmp_path):
    r = Replay(tmp_path / "home", tmp_path / "run", mode="active")
    r.at(5).plan(m1=60, cand=200)
    r.at(6).tick()
    report = r.at(250).tick()
    assert report.state == "RUNNING"  # authorized work continues after the due time
    p = r.project()
    assert p["banner"] == "Deadline passed · 10m over" and p["due_at_utc"] == "2026-10-03T00:00:00Z"
    assert T0 + timedelta(hours=4) == deadline_status._utc(p["due_at_utc"])


def test_calibration_failure_never_loses_the_candidate_report(steward_home, tmp_path, monkeypatch):
    monkeypatch.setattr(deadline_status, "record_calibration",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    r, _ = timeline(tmp_path / "home", tmp_path / "run")
    assert len(r.whats("deadline_candidate_report")) == 1
    assert any("disk full" in e.evidence.get("error", "") for e in r.whats("deadline_error"))
