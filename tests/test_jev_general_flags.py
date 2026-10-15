"""SUP-05: Jev's general (non-fleet) choices `permission_confusion` and `thrash`, for every job.

The existing gate is unchanged (confidence >= 0.7, the same choice twice, per-choice cooldown). A gated flag of
either new choice goes to the Arbiter as a tech-lead fact; the legacy choices route exactly as before."""

from __future__ import annotations

import json

from steward import jev_observer, jev_watch
from steward.jev_observer import CHOICES, JevObserver
from steward.store import Store
from test_jev_watch import FakeActions, _resp, _running, _sig, _watch


class TLActions(FakeActions):
    def tech_lead_fact(self, job_id, choice, evidence):
        self.calls.append(("tech_lead", choice, evidence.get("confidence")))
        return "fact queued"


def test_choices_and_question_text():
    assert "permission_confusion" in CHOICES and "thrash" in CHOICES
    for old in ("ok", "stalled", "hung_call", "looping", "off_track", "risky_action"):
        assert old in CHOICES
    assert "done_unproven" not in CHOICES and "finished" not in CHOICES
    body = json.loads(JevObserver(enabled=True)._build_body({}, ""))
    crit = body["questions"]["triage"]["criteria"]
    assert set(crit) == set(CHOICES)
    assert "asks for, refuses, or waits on" in crit["permission_confusion"]
    assert "done" in crit["thrash"] and "same failing step" in crit["thrash"]


def test_gate_is_unchanged_for_the_new_choices(steward_home):
    store = Store(steward_home)
    job = _running(store)
    acts = TLActions()
    w, t, _, now = _watch(store, [_resp("permission_confusion", 0.65), _resp("permission_confusion", 0.9),
                                  _resp("thrash", 0.9), _resp("permission_confusion", 0.9),
                                  _resp("permission_confusion", 0.9)], actions=acts)
    for _ in range(3):  # below confidence; then one each of two different choices: no streak of 2
        w.step(job.job_id, _sig(), force=True)
    assert acts.calls == []
    w.step(job.job_id, _sig(), force=True)
    w.step(job.job_id, _sig(), force=True)  # same choice twice at >= 0.7
    assert acts.calls == [("tech_lead", "permission_confusion", 0.9)]
    for _ in range(2):  # inside the 300 s cooldown: no second flag
        w.step(job.job_id, _sig(), force=True)
    assert len(acts.calls) == 1


def test_new_flags_reach_the_arbiter_in_shadow_too(steward_home):
    """D-03: judgment-based fixes are active; a gated flag is a tech-lead fact (the tech lead still decides)."""
    store = Store(steward_home)
    job = _running(store)
    acts = TLActions()
    w, *_ = _watch(store, [_resp("thrash", 0.85)], mode="shadow", actions=acts)
    w.step(job.job_id, _sig(), force=True)
    w.step(job.job_id, _sig(), force=True)
    assert acts.calls == [("tech_lead", "thrash", 0.85)]
    flag = next(e for e in store.events(job.job_id) if e.what == "jev_flag")
    assert flag.evidence["routed_to"] == "tech_lead" and flag.rule == "jev_flag_tech_lead"


def test_legacy_choices_route_exactly_as_before(steward_home):
    store = Store(steward_home)
    job = _running(store)
    acts = TLActions()
    w, *_ = _watch(store, [_resp("looping", 0.9)], mode="shadow", actions=acts)
    w.step(job.job_id, _sig(), force=True)
    w.step(job.job_id, _sig(), force=True)
    assert acts.calls == []  # shadow: record only, as before
    flag = next(e for e in store.events(job.job_id) if e.what == "jev_flag")
    assert flag.evidence["would_escalate_to"] == "watcher"

    store2 = Store(steward_home)
    job2 = _running(store2, "second")
    acts2 = TLActions()
    w2, *_ = _watch(store2, [_resp("off_track", 0.9)], mode="active", actions=acts2)
    store2.kv_set(job2.job_id, "supervisor_jev", "active")
    w2.step(job2.job_id, _sig(), force=True)
    w2.step(job2.job_id, _sig(), force=True)
    assert ("watcher", "off_track") in acts2.calls and not any(c[0] == "tech_lead" for c in acts2.calls)


def test_runner_actions_submit_the_flag_to_the_arbiter(steward_home):
    from steward.runner import _JevActions
    from test_stability import ScriptedRunner, _runner

    store = Store(steward_home)
    job = _running(store)
    runner = _runner(store, ScriptedRunner(), [[]])
    _JevActions(runner).tech_lead_fact(job.job_id, "permission_confusion", {"confidence": 0.91})
    facts = runner.arbiter.pending_for_tech_lead(job.job_id)
    assert [(f["source"], f["kind"], f["route"]) for f in facts] == [("jev", "permission_confusion", "outer")]


def test_actions_without_a_tech_lead_seam_record_and_never_raise(steward_home):
    store = Store(steward_home)
    job = _running(store)
    w, *_ = _watch(store, [_resp("thrash", 0.9)], actions=FakeActions())
    w.step(job.job_id, _sig(), force=True)
    w.step(job.job_id, _sig(), force=True)
    assert any(e.what == "jev_flag" for e in store.events(job.job_id))
    assert jev_watch.ROUTES["thrash"] == "tech_lead" and jev_observer.FLAG_CONFIDENCE == 0.7
