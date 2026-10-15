# tests/test_parked_replay.py
"""Replay of the 2026-10-04 Circuit Tracks shape (job 8025f70f): the worker said it was NOT done and went idle
waiting for the owner; closeout parked it as an acceptance hold. With the parked pass: hold_kind == stuck, one
review within cadence, and the outcome is a reopen (confidence 0.9) through the send-back seam — no tg, no Herdr."""
from __future__ import annotations

import json
import time

from steward import fleet_runtime as rt
from steward import hold, questions_delivery as qd, sendback
from steward import tech_lead_actions as tla
from steward import tech_lead_policy as tpol
from steward.models import JobState, Judgment
from steward.runner import wire_control_lead
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from test_runner import _contracted_job
from test_tech_lead_no_clog import ASTRA

_LAST = ("I did not reach the finish line, and I'm not printing STEWARD-DONE. Step 3 hit a STOP rule on liveness... "
         "Waiting for Sam to check the unit.")


def _git(argv, cwd):
    return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="sha-1\n", stderr="", duration_s=0.0)


def test_tracks_shape_is_stuck_and_gets_one_reopen(steward_home):
    store = Store(steward_home)
    jid = _contracted_job(store, "Circuit Tracks Phase 2 finish. Finish line: verify --phase 2 passes").job_id
    (store.home / "worktrees" / jid).mkdir(parents=True)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.append_event(jid, what="yield_classified",
                       evidence={"lane": "STOPPED", "failed_preconditions": ["no_new_commit"], "seq": 4, "sha": "abc",
                                 "last_said": _LAST},
                       rule="yield_classification", judgment=Judgment.code)
    store.set_state(jid, JobState.WORKER_FINISHED, what="worker idle after its final message",
                    evidence={"marker": "idle_after_final_message"}, rule="idle_after_final_message",
                    judgment=Judgment.code)
    store.set_state(jid, JobState.NEEDS_HUMAN, what="acceptance unresolved",
                    evidence={"reasons": ["1 baseline failure(s) still failing", "test-weakening detected"]},
                    rule="acceptance_unresolved", judgment=Judgment.code)
    assert hold.kind(store, jid)["kind"] == "stuck"

    seen = []

    def lead_answer(entry, prompt, timeout):
        seen.append(prompt)
        assert "hold_kind" in prompt and "Waiting for Sam" in prompt
        return json.dumps({"proposal_id": "p", "trigger_id": "t", "confidence": 0.9, "judgment": False,
                           "signature": "stuck:tracks", "plan_item": "",
                           "actions": [{"type": "reopen", "params": {
                               "text": "The unit is not dead: check the Pi downloads dir before trusting pack_download_no_file; poll aconnect -l after stop()",
                               "evidence": "last_said", "confidence": 0.9}}]})

    calls = []

    def send_back_fn(job_id, text, origin, request_id, judgment):
        calls.append(origin)
        return sendback.send_back(store, job_id, text, origin=origin, judgment=judgment)

    lead = wire_control_lead(store, [ASTRA], send_back_fn=send_back_fn, policy_fn=lambda: tpol.Policy(),
                             runner_exec=FakeRunner(_git), invoke=lead_answer)
    lead.available = lambda e: None
    t0 = 1000.0
    qd.tick(store, FakeRunner(_git), now=t0, job_lead=lead, policy_fn=lambda: tpol.Policy())
    for i in range(300):
        if store.get_job(jid).state == JobState.RUNNING:
            break
        qd.tick(store, FakeRunner(_git), now=t0 + i, job_lead=lead, policy_fn=lambda: tpol.Policy())
        time.sleep(0.01)
    assert store.get_job(jid).state == JobState.RUNNING
    assert calls == [f"{tla.ORIGIN}:{ASTRA.id}"] and len(seen) == 1
    assert store.kv_get(jid, sendback.REWORK_KEY)["by"] == f"{tla.ORIGIN}:{ASTRA.id}"
    assert [e for e in store.events(jid) if e.what == "tech_lead_reopen"]
