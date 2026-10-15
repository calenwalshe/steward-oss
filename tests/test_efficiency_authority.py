"""The efficiency watcher's authority (16-05): the Phase 15 ladder under `watcher_authority:efficiency`, the T2 nudge
through Steering, the T1 escalation, and the bounded job efficiency policy. Synthetic transcripts and fakes only."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from efficiency_synth import T0, agent_lines, write_subagent, write_worker
from test_efficiency_watch import NOW, _job, _running_runner
from test_stability import Clock, _events

from steward import efficiency_detect as detect
from steward import efficiency_watch as ew
from steward import tokens
from steward import watcher_authority as wa
from steward.steering import Steering
from steward.store import Store

FID = "long_lived_agent:agent-e1"


# -- helpers ------------------------------------------------------------------------------------------------


def _spec(store, job_id):
    return ew.builtin_spec(detect.load_config(store, job_id))


def _tier(store, job_id, tier):
    spec = _spec(store, job_id)
    ew.ensure_record(store, job_id, spec)
    if tier:
        wa.promote(store, job_id, spec, actor="cli", tier=tier)


def _set_mode(store, job_id, m="active"):
    store.kv_set(job_id, ew.SWITCH_KEY, m)


def _watcher(store, proj=None, clock=None, **kw):
    kw.setdefault("tokens_fn", lambda st, jid, now=None: {"sources": {}, "last_hour": {"usd": 0.0}})
    return ew.EfficiencyWatcher(store, clock=clock or Clock(NOW), spend_clock=Clock(1000.0), projects_dir=proj, **kw)


def _finding(store, job_id, kind="long_lived_agent", subject="agent-e1", remedy="respawn_per_unit", *,
             saving_usd=3.0, saving_min=0.0, actions=None, policy=None, stage="gsd-executor", unit=1,
             evidence="81 calls × 260k ctx", metric=("ctx_sum", 21_000_000)):
    cfg = detect.load_config(store, job_id)
    f = detect._finding(cfg, kind, subject, evidence, metric, remedy, stage=stage, unit=unit,
                        saving_usd=saving_usd, saving_min=saving_min, actions=actions, policy=policy)
    f = ew._plain(f)
    f["epoch"] = store.get_revision(job_id)
    return f


def _eff_instructions(store, job_id):
    return [i for i in Steering(store).all_instructions(job_id) if i.get("origin") == "watcher:efficiency"]


def _t2_active(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    _tier(store, job.job_id, 2)
    _set_mode(store, job.job_id)
    return store, job, proj, d


def _long_actionable(d, finished=False):
    """A long-lived subagent whose ctx grows (so respawning saves > $1)."""
    write_subagent(d, "w", "e1", "gsd-executor", "Execute phase 1",
                   agent_lines(T0, calls=81, prefix="e1", finished=finished, ctx0=100_000, ctx_step=4_000,
                               gen_s=5, tool_s=1))


# -- Task 1: record, would-nudge, T2 nudge, gates, text, boundary --------------------------------------------


def test_record_starts_at_t0_not_legacy_t2(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    _long_actionable(d)
    assert wa.get_record(store, job.job_id, "efficiency") is None
    _watcher(store, proj).step(job.job_id)
    rec = wa.get_record(store, job.job_id, "efficiency")
    assert rec is not None and rec["tier"] == 0
    spec = _spec(store, job.job_id)
    assert spec["name"] == "efficiency" and spec["model"] == "code"
    assert spec["may"] == ["escalate", "nudge", "reconfigure"]
    assert spec["ask"] == "builtin:efficiency:v1:" + detect.thresholds_hash(detect.load_config(store, job.job_id))
    assert spec["every_s"] == detect.DEFAULTS["recompute_every_s"]
    assert wa.tier_of(store, job.job_id, spec) == 0
    assert ew.powers(store, job.job_id, spec) == set()


def test_t0_or_shadow_records_would_nudge_once(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    _long_actionable(d)
    clock, spend = Clock(NOW), Clock(1000.0)
    w = ew.EfficiencyWatcher(store, clock=clock, spend_clock=spend, projects_dir=proj,
                             tokens_fn=lambda st, jid, now=None: {"sources": {}, "last_hour": {"usd": 0.0}})
    w.step(job.job_id)
    state = store.kv_get(job.job_id, ew.STATE_KEY)
    f = next(f for f in state["findings"] if f["id"] == FID)
    assert f["actionable"] and "nudge" in f["actions"]
    [ev] = _events(store, job, "efficiency_would_nudge")
    assert ev.evidence["finding"] == FID and ev.evidence["at"] == NOW
    assert "mode_shadow" in ev.evidence["blocked"] and "tier_T0" in ev.evidence["blocked"]
    assert ev.evidence["text"].startswith("[efficiency] From now on, ")
    for _ in range(3):
        clock.t += 100
        spend.t += 100
        w.step(job.job_id)
    assert len(_events(store, job, "efficiency_would_nudge")) == 1
    assert _eff_instructions(store, job.job_id) == []
    assert _events(store, job, "efficiency_nudged") == []


def test_t2_active_nudges_once_through_steering(steward_home, tmp_path):
    store, job, proj, d = _t2_active(steward_home, tmp_path)
    w = _watcher(store)
    f = _finding(store, job.job_id)
    w._act(job.job_id, [f], None, {"agent-e1"}, NOW)
    [ins] = _eff_instructions(store, job.job_id)
    assert ins["verb"] == "nudge" and not ins.get("urgent") and ins["status"] == "received"
    assert ins["text"].startswith("[efficiency] ")
    [ev] = _events(store, job, "efficiency_nudged")
    assert ev.evidence["finding"] == FID and ev.evidence["rev"] == ins["rev"] and ev.evidence["text"] == ins["text"]
    assert ev.evidence["saving_usd"] == 3.0 and ev.evidence["saving_min"] == 0.0 and ev.evidence["at"] == NOW
    assert ev.judgment.value == "code" and ev.rule == "efficiency_gate"
    f2 = _finding(store, job.job_id)  # fresh epoch, same finding id
    Steering(store).mark_acknowledged(job.job_id, ins["rev"], "acknowledged")
    w._act(job.job_id, [f2], None, {"agent-e1"}, NOW + 4000)
    assert len(_eff_instructions(store, job.job_id)) == 1
    assert len(_events(store, job, "efficiency_nudged")) == 1


def test_runner_delivers_efficiency_nudge_next_tick(steward_home, tmp_path):
    store, job, proj, d = _t2_active(steward_home, tmp_path)
    _long_actionable(d, finished=True)
    runner = _running_runner(store, job, _watcher(store, proj))
    runner.tick(job.job_id)
    assert len(_events(store, job, "efficiency_nudged")) == 1
    worker = runner._workers[job.job_id]
    assert not any("[efficiency]" in t for t in worker.delivered)
    runner.tick(job.job_id)
    got = [t for t in worker.delivered if "[efficiency]" in t]
    assert len(got) == 1 and got[0].startswith("[rev=")


def _block_setup(case, store, job_id):
    """Apply one blocking condition; return (_act kwargs overrides, expected reason)."""
    kw = {"newly": {"agent-e1"}, "share": None}
    if case == "mode_shadow":
        _set_mode(store, job_id, "shadow")
    elif case == "tier_T1":
        spec = _spec(store, job_id)
        wa.promote(store, job_id, spec, actor="cli", tier=1)
    elif case == "observe_only_kv":
        store.kv_set(job_id, "efficiency_observe_only", True)
    elif case == "observe_only_share":
        kw["share"] = 0.30
    elif case == "quarantined":
        store.kv_set(job_id, wa.QUARANTINE_KEY, {"reason": "conflict"})
    elif case == "freeze":
        store.kv_set(job_id, "watcher_freeze_until", NOW + 100)
    elif case == "settle_lease":
        store.kv_set(job_id, "watcher_settle_until", NOW + 100)
    elif case == "unacked_pending":
        Steering(store).record(job_id, "focus", "look at the tests first")
    elif case == "not_boundary":
        kw["newly"] = set()
    elif case == "cooldown_finding":
        store.kv_set(job_id, "efficiency_nudged:" + FID, NOW - 5000)
    elif case == "cooldown_job":
        store.kv_set(job_id, "efficiency_last_nudge", NOW - 100)
    return kw


@pytest.mark.parametrize("case,reason", [
    ("mode_shadow", "mode_shadow"), ("tier_T1", "tier_T1"), ("observe_only_kv", "observe_only"),
    ("observe_only_share", "observe_only"), ("quarantined", "quarantined"), ("freeze", "freeze"),
    ("settle_lease", "settle_lease"), ("stale_epoch", "stale_epoch"), ("unacked_pending", "unacked_pending"),
    ("not_boundary", "not_boundary"), ("cooldown_finding", "cooldown_finding"), ("cooldown_job", "cooldown_job"),
])
def test_nudge_gates_block_and_are_listed(steward_home, tmp_path, case, reason):
    store, job, proj, d = _t2_active(steward_home, tmp_path)
    kw = _block_setup(case, store, job.job_id)
    f = _finding(store, job.job_id)
    if case == "stale_epoch":
        store.bump_revision(job.job_id)
    _watcher(store)._act(job.job_id, [f], None, kw["newly"], NOW, share=kw["share"])
    assert _eff_instructions(store, job.job_id) == []
    [ev] = _events(store, job, "efficiency_would_nudge")
    assert reason in ev.evidence["blocked"], ev.evidence["blocked"]
    if case not in ("mode_shadow", "tier_T1"):
        assert ev.evidence["blocked"] == [reason]


def test_transient_block_retried_silently(steward_home, tmp_path):
    store, job, proj, d = _t2_active(steward_home, tmp_path)
    w = _watcher(store)
    w._act(job.job_id, [_finding(store, job.job_id)], None, set(), NOW)
    assert _eff_instructions(store, job.job_id) == []
    w._act(job.job_id, [_finding(store, job.job_id)], None, set(), NOW + 10)
    assert len(_events(store, job, "efficiency_would_nudge")) == 1
    w._act(job.job_id, [_finding(store, job.job_id)], None, {"agent-e1"}, NOW + 20)
    assert len(_eff_instructions(store, job.job_id)) == 1


@pytest.mark.parametrize("kw", [{"saving_usd": 0.5, "saving_min": 1.0},
                                {"actions": ("escalate",)}])
def test_not_actionable_or_escalate_only_never_nudges(steward_home, tmp_path, kw):
    store, job, proj, d = _t2_active(steward_home, tmp_path)
    f = _finding(store, job.job_id, **kw)
    _watcher(store)._act(job.job_id, [f], None, {"agent-e1"}, NOW)
    assert _eff_instructions(store, job.job_id) == []
    assert _events(store, job, "efficiency_nudged") == []
    assert _events(store, job, "efficiency_would_nudge") == []


def test_nudge_text_shape(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    f = _finding(store, job.job_id, saving_usd=3.456, saving_min=7.4)
    text = ew.nudge_text(f)
    assert len(text) <= 600
    assert text.startswith("[efficiency] From now on, " + f["remedy_text"])
    assert "Evidence: gsd-executor#1 81 calls × 260k ctx" in text
    assert "Expected saving ≈$3.46 / ≈7 min per gsd-executor unit" in text
    assert "Measured by: ctx_sum on the next gsd-executor unit." in text
    huge = {**f, "remedy_text": "x" * 2000}
    assert len(ew.nudge_text(huge)) <= 600


def test_boundary_definition(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, calls=3, prefix="w", finished=False))
    trs = tokens.job_transcripts(store, job.job_id, proj, _cache={})
    assert not ew.worker_idle(trs)
    assert not ew.boundary(trs, set())
    assert ew.boundary(trs, {"agent-e1"})  # a subagent newly finished
    write_worker(d, agent_lines(T0, calls=3, prefix="w", finished=True))
    trs = tokens.job_transcripts(store, job.job_id, proj, _cache={})
    assert ew.worker_idle(trs)
    assert ew.boundary(trs, set())
    # a fabric worker whose turn is explicitly closed is an extra idle hint; anything else is ignored
    write_worker(d, agent_lines(T0, calls=3, prefix="w", finished=False))
    trs = tokens.job_transcripts(store, job.job_id, proj, _cache={})
    assert ew.boundary(trs, set(), SimpleNamespace(turn_open=False), fabric=True)
    assert not ew.boundary(trs, set(), SimpleNamespace(turn_open=False), fabric=False)
    assert not ew.boundary(trs, set(), SimpleNamespace(turn_open=True), fabric=True)
    assert not ew.boundary(trs, set(), SimpleNamespace(), fabric=True)


# -- Task 2: T1 escalation and the trust record in the watcher CLI ------------------------------------------


def _t_active(steward_home, tmp_path, tier, notes=None):
    store, job, proj, d = _job(steward_home, tmp_path)
    _tier(store, job.job_id, tier)
    _set_mode(store, job.job_id)
    notes = [] if notes is None else notes
    w = _watcher(store, notify=lambda *a: notes.append(a))
    return store, job, w, notes


def _pending(store, job_id):
    from steward import approvals

    return approvals.list_approvals(store, job_id, status="pending")


def test_t1_escalates_actionable_nudgeable_finding_once(steward_home, tmp_path):
    store, job, w, notes = _t_active(steward_home, tmp_path, 1)
    f = _finding(store, job.job_id)
    w._act(job.job_id, [f], None, {"agent-e1"}, NOW)
    [a] = _pending(store, job.job_id)
    assert a["question"].startswith(
        "efficiency: gsd-executor#1 81 calls × 260k ctx → respawn a fresh helper per unit (≈-$3.00")
    [n] = notes
    assert n[1] == "NEEDS_YOU" and n[2] == "Steward: efficiency" and n[3] == a["question"][:140]
    [ev] = _events(store, job, "efficiency_escalated")
    assert ev.evidence == {"finding": FID, "approval_id": a["id"], "at": NOW}
    assert ev.judgment.value == "code"
    assert _eff_instructions(store, job.job_id) == []
    w._act(job.job_id, [_finding(store, job.job_id)], None, {"agent-e1"}, NOW + 10)
    assert len(_pending(store, job.job_id)) == 1 and len(notes) == 1
    assert len(_events(store, job, "efficiency_escalated")) == 1


def _escalate_only(store, job_id, kind):
    if kind == "idle_wait":
        return _finding(store, job_id, "idle_wait", "job", "escalate_idle", saving_min=15.0, saving_usd=0.0,
                        stage="", unit=None, evidence="nothing generating or running for 15 min",
                        metric=("idle_s", 900))
    if kind == "cost_per_unit_trend":
        return _finding(store, job_id, "cost_per_unit_trend", "unit-3", "escalate_projection", saving_usd=4.0,
                        stage="", unit=3, evidence="unit 3 $9.00 = 1.8× median", metric=("unit_usd", 9.0))
    return _finding(store, job_id, "redundant_supervision", "drift", "escalate_remove_watcher", saving_usd=2.0,
                    stage="", unit=None, evidence="3 runs in a row duplicating other supervisors",
                    metric=("duplicate_runs", 3))


@pytest.mark.parametrize("tier", [1, 2])
@pytest.mark.parametrize("kind", ["idle_wait", "cost_per_unit_trend", "redundant_supervision"])
def test_escalate_only_kinds_escalate_at_t1_and_t2(steward_home, tmp_path, tier, kind):
    store, job, w, notes = _t_active(steward_home, tmp_path, tier)
    f = _escalate_only(store, job.job_id, kind)
    assert f["actions"] == ["escalate"] and f["actionable"]
    w._act(job.job_id, [f], None, {"agent-e1"}, NOW)
    assert len(_pending(store, job.job_id)) == 1
    assert len(_events(store, job, "efficiency_escalated")) == 1
    assert _eff_instructions(store, job.job_id) == []
    assert _events(store, job, "efficiency_nudged") == []


def test_escalate_dedupes_until_saving_doubles(steward_home, tmp_path):
    store, job, w, notes = _t_active(steward_home, tmp_path, 1)
    w._act(job.job_id, [_finding(store, job.job_id, saving_usd=3.0)], None, set(), NOW)
    w._act(job.job_id, [_finding(store, job.job_id, saving_usd=5.0)], None, set(), NOW + 10)
    assert len(_events(store, job, "efficiency_escalated")) == 1
    w._act(job.job_id, [_finding(store, job.job_id, saving_usd=6.0)], None, set(), NOW + 20)
    assert len(_events(store, job, "efficiency_escalated")) == 2
    assert len(_pending(store, job.job_id)) == 2
    # a pending approval with the same question is never duplicated, even with the saving record gone
    store.kv_set(job.job_id, "efficiency_escalated:" + FID, None)
    w._act(job.job_id, [_finding(store, job.job_id, saving_usd=6.0)], None, set(), NOW + 30)
    assert len(_pending(store, job.job_id)) == 2
    assert len(_events(store, job, "efficiency_escalated")) == 2


@pytest.mark.parametrize("defer", ["last_class", "stall diagnosis produced", "jev_flag", "jev_escalated"])
def test_idle_wait_defers_to_doctor_and_jev(steward_home, tmp_path, defer):
    import time as time_module

    from steward.models import Judgment

    store, job, w, notes = _t_active(steward_home, tmp_path, 1)
    if defer == "last_class":
        store.kv_set(job.job_id, "last_class", "suspect_stall")
    else:
        store.append_event(job.job_id, what=defer, evidence={}, rule="t", judgment=Judgment.code)
    f = _escalate_only(store, job.job_id, "idle_wait")
    w._act(job.job_id, [f], None, set(), time_module.time())
    assert _pending(store, job.job_id) == [] and notes == []
    state = store.kv_get(job.job_id, ew.STATE_KEY)
    assert "deferred_to_doctor" in state["blocked"][f["id"]]


def test_idle_wait_old_jev_event_does_not_defer(steward_home, tmp_path):
    from steward.models import Judgment

    store, job, w, notes = _t_active(steward_home, tmp_path, 1)
    store.append_event(job.job_id, what="jev_flag", evidence={}, rule="t", judgment=Judgment.code)
    w._act(job.job_id, [_escalate_only(store, job.job_id, "idle_wait")], None, set(), NOW)  # NOW is far later
    assert len(_pending(store, job.job_id)) == 1


@pytest.mark.parametrize("m,tier", [("shadow", 1), ("active", 0), ("shadow", 2)])
def test_escalate_needs_mode_active_and_tier_1(steward_home, tmp_path, m, tier):
    store, job, w, notes = _t_active(steward_home, tmp_path, tier)
    _set_mode(store, job.job_id, m)
    w._act(job.job_id, [_finding(store, job.job_id), _escalate_only(store, job.job_id, "cost_per_unit_trend")],
           None, {"agent-e1"}, NOW)
    assert _pending(store, job.job_id) == [] and notes == []
    assert _events(store, job, "efficiency_escalated") == []


def test_cli_watcher_trust_lists_efficiency(steward_home, tmp_path, capsys):
    import json

    from steward import cli

    store, job, proj, d = _job(steward_home, tmp_path)
    assert cli.main(["watcher", "trust", job.job_id]) == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.startswith("efficiency"))
    assert "T0 shadow" in row and "mode shadow" in row
    assert wa.get_record(store, job.job_id, "efficiency")["tier"] == 0
    assert cli.main(["watcher", "trust", job.job_id, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    eff = next(r for r in rows if r["name"] == "efficiency")
    assert eff["tier"] == 0 and eff["legacy"] is False and eff["mode"] == "shadow"


def test_cli_promote_efficiency_human_only_capped_at_t2(steward_home, tmp_path, capsys):
    from steward import cli

    store, job, proj, d = _job(steward_home, tmp_path)
    j = job.job_id
    assert cli.main(["watcher", "promote", j, "efficiency"]) == 0
    assert "T0 -> T1" in capsys.readouterr().out
    spec = _spec(store, j)
    assert wa.tier_of(store, j, spec) == 1
    [ev] = _events(store, job, "watcher_promoted")
    assert ev.evidence["name"] == "efficiency" and ev.judgment.value == "human"
    assert cli.main(["watcher", "promote", j, "efficiency"]) == 0
    out = capsys.readouterr().out
    assert "T1 -> T2" in out and "below the bar" in out  # Phase 15: the gap is recorded
    assert wa.get_record(store, j, "efficiency")["history"][-1]["gaps"]
    assert cli.main(["watcher", "promote", j, "efficiency"]) == 1
    assert "efficiency tops out at T2" in capsys.readouterr().err
    assert cli.main(["watcher", "promote", j, "efficiency", "--tier", "3"]) == 1
    assert "efficiency tops out at T2" in capsys.readouterr().err
    assert wa.tier_of(store, j, spec) == 2
    with pytest.raises(wa.AuthorityError):
        wa.promote(store, j, spec, actor="watcher:efficiency", tier=2)


def test_cli_adjudicate_efficiency_records_case(steward_home, tmp_path, capsys):
    from steward import cli

    store, job, proj, d = _job(steward_home, tmp_path)
    assert cli.main(["watcher", "adjudicate", job.job_id, "efficiency", "correct", "--kind", "nudge",
                     "--ref", "12"]) == 0
    rec = wa.get_record(store, job.job_id, "efficiency")
    assert rec["tier"] == 0 and rec["cases"][-1]["label"] == "correct" and rec["cases"][-1]["kind"] == "nudge"
    assert rec["cases"][-1]["ref"] == "12" and rec["cases"][-1]["by"] == "cli"


def test_threshold_change_demotes_efficiency_to_t0(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    _tier(store, job.job_id, 2)
    assert wa.tier_of(store, job.job_id, _spec(store, job.job_id)) == 2
    store.kv_set(job.job_id, "efficiency_overrides", {"long_lived_calls": 60})
    assert wa.tier_of(store, job.job_id, _spec(store, job.job_id)) == 0
    [ev] = _events(store, job, "watcher_demoted")
    assert ev.evidence["name"] == "efficiency" and ev.evidence["reason"] == "config_change"


# -- Task 3: job efficiency policy, reconfigure within the envelope, revert and overrides --------------------

from steward import efficiency_policy as ep  # noqa: E402


def _policy_finding(store, job_id, which):
    if which == "effort":
        return _finding(store, job_id, "hidden_thinking", "gsd-planner", "effort_step_down", saving_usd=2.0,
                        stage="gsd-planner", unit=None, evidence="thinking 70% of 200k output at effort xhigh",
                        metric=("think_share", 0.7), policy={"effort": {"gsd-planner": "high"}})
    if which == "plan_ahead":
        return _finding(store, job_id, "planning_serial", "gsd-planner", "plan_ahead", saving_min=30.0,
                        stage="gsd-planner", unit=3, evidence="plan started after build ended, units 2,3",
                        metric=("serial_plan_s", 600))
    if which == "respawn":
        return _finding(store, job_id)
    return _finding(store, job_id, "build_output", "pytest", "quiet_output", saving_usd=1.5, stage="gsd-executor",
                    unit=None, evidence="3 results > 20k chars from pytest", metric=("result_chars", 90_000))


def test_policy_defaults_and_line(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    assert ep.load(store, job.job_id) == {"respawn": "off", "effort": {}, "plan_ahead": "off", "output_hygiene": "off"}
    assert ep.policy_line(ep.load(store, job.job_id)) == ""
    assert ep.policy_line({"respawn": "per_unit", "effort": {"gsd-planner": "high"}, "plan_ahead": "on",
                           "output_hygiene": "on"}) == \
        "respawn per_unit; effort gsd-planner=high; plan ahead on; quiet output on"
    assert ep.policy_line({**ep.DEFAULT_POLICY, "plan_ahead": "on"}) == "plan ahead on"


@pytest.mark.parametrize("which,key,value", [
    ("effort", "effort", {"gsd-planner": "high"}), ("plan_ahead", "plan_ahead", "on"),
    ("respawn", "respawn", "per_unit"), ("output_hygiene", "output_hygiene", "on"),
])
def test_reconfigure_within_envelope_once(steward_home, tmp_path, which, key, value):
    store, job, proj, d = _t2_active(steward_home, tmp_path)
    w = _watcher(store)
    f = _policy_finding(store, job.job_id, which)
    assert "reconfigure" in f["actions"] and f["policy"] is not None
    w._act(job.job_id, [f], None, {"agent-e1"}, NOW)
    [ev] = _events(store, job, "efficiency_policy_changed")
    assert ev.evidence["actor"] == "watcher:efficiency" and ev.evidence["finding"] == f["id"]
    assert ev.evidence["after"] == {key: value} and ev.evidence["before"] == {key: ep.DEFAULT_POLICY[key]}
    assert ev.evidence["change_id"] == "pc-1" and ev.judgment.value == "code"
    assert store.kv_get(job.job_id, "efficiency_policy")[key] == value
    w._act(job.job_id, [_policy_finding(store, job.job_id, which)], None, {"agent-e1"}, NOW + 5000)
    assert len(_events(store, job, "efficiency_policy_changed")) == 1


@pytest.mark.parametrize("tier,m", [(1, "active"), (2, "shadow"), (0, "active")])
def test_reconfigure_needs_t2_and_active(steward_home, tmp_path, tier, m):
    store, job, w, notes = _t_active(steward_home, tmp_path, tier)
    _set_mode(store, job.job_id, m)
    w._act(job.job_id, [_policy_finding(store, job.job_id, "effort")], None, {"agent-e1"}, NOW)
    assert _events(store, job, "efficiency_policy_changed") == []
    assert ep.load(store, job.job_id) == ep.DEFAULT_POLICY
    if tier == 1 and m == "active":
        assert len(_events(store, job, "efficiency_escalated")) == 1


@pytest.mark.parametrize("patch", [
    {"model": "haiku"}, {"verifier": "off"}, {"plan_check": "off"}, {"nyquist": "off"}, {"tests": "off"},
    {"effort": {"x": "low"}}, {"effort": {"x": "xhigh"}}, {"respawn": "max_calls=10"}, {"kill_agent": "a1"},
    {"budget_usd": 5}, {"grant": "nudge"}, {"stop": True}, {"pause": True}, {"tier": 3},
])
def test_watcher_can_never_change_human_only_items(steward_home, tmp_path, patch):
    store, job, proj, d = _job(steward_home, tmp_path)
    with pytest.raises(ep.PolicyError):
        ep.change(store, job.job_id, patch, actor="watcher:efficiency", now=NOW)
    [ev] = _events(store, job, "efficiency_refused")
    assert ev.evidence["actor"] == "watcher:efficiency" and ev.evidence["request"] == patch
    assert ev.evidence["reasons"]
    assert ep.load(store, job.job_id) == ep.DEFAULT_POLICY
    assert _events(store, job, "efficiency_policy_changed") == []


def test_validate_envelope_rules():
    env = dict(detect.DEFAULTS["envelope"])
    assert ep.validate({"respawn": "max_calls=40"}, env, actor="watcher:efficiency") == []
    assert ep.validate({"effort": {"a": "medium", "b": "high"}}, env, actor="watcher:efficiency") == []
    assert ep.validate({"effort": {"a": "medium"}}, {**env, "effort_floor": "high"}, actor="watcher:efficiency")
    assert ep.validate({"respawn": "sometimes"}, env, actor="cli")
    assert ep.validate({"output_hygiene": "loud"}, env, actor="cli")
    assert ep.validate({"model": "haiku"}, env, actor="cli") == ["not_policy_key:model"]
    assert ep.validate({"effort": {"a": "low"}}, env, actor="cli") == []  # below the floor is Sam's call


def test_plan_ahead_refused_when_envelope_disallows(steward_home, tmp_path):
    store, job, w, notes = _t_active(steward_home, tmp_path, 2)
    store.kv_set(job.job_id, "efficiency_overrides", {"envelope.plan_ahead": False})
    assert wa.tier_of(store, job.job_id, _spec(store, job.job_id)) == 2  # the envelope is not a threshold
    f = _policy_finding(store, job.job_id, "plan_ahead")
    w._act(job.job_id, [f], None, {"agent-e1"}, NOW)
    [ref] = _events(store, job, "efficiency_refused")
    assert ref.evidence["request"] == {"plan_ahead": "on"} and "plan_ahead_not_allowed" in ref.evidence["reasons"]
    assert ep.load(store, job.job_id)["plan_ahead"] == "off"
    [a] = _pending(store, job.job_id)
    assert a["question"].startswith("efficiency (needs you): ")
    assert _eff_instructions(store, job.job_id) == []  # escalated instead
    w._act(job.job_id, [_policy_finding(store, job.job_id, "plan_ahead")], None, {"agent-e1"}, NOW + 10)
    assert len(_events(store, job, "efficiency_refused")) == 1 and len(_pending(store, job.job_id)) == 1


def test_human_may_set_below_floor(steward_home, tmp_path, capsys):
    from steward import cli

    store, job, proj, d = _job(steward_home, tmp_path)
    assert cli.main(["efficiency", job.job_id, "set", "policy.effort.gsd-executor=low"]) == 0
    assert ep.load(store, job.job_id)["effort"] == {"gsd-executor": "low"}
    [ev] = _events(store, job, "efficiency_policy_changed")
    assert ev.evidence["actor"] == "cli" and ev.judgment.value == "human"
    assert ev.evidence["after"] == {"effort": {"gsd-executor": "low"}}


def test_revert_restores_before(steward_home, tmp_path, capsys):
    from steward import cli

    store, job, proj, d = _job(steward_home, tmp_path)
    j = job.job_id
    first = ep.change(store, j, {"respawn": "per_unit"}, actor="watcher:efficiency", finding="f1", now=NOW)
    second = ep.change(store, j, {"effort": {"gsd-planner": "high"}}, actor="watcher:efficiency", finding="f2",
                       now=NOW + 1)
    assert (first["id"], second["id"]) == ("pc-1", "pc-2")
    assert cli.main(["efficiency", j, "revert"]) == 0  # the last change
    assert ep.load(store, j) == {**ep.DEFAULT_POLICY, "respawn": "per_unit"}
    rev = _events(store, job, "efficiency_policy_changed")[-1]
    assert rev.evidence["reverts"] == "pc-2" and rev.evidence["actor"] == "cli"
    assert rev.evidence["before"] == {"effort": {"gsd-planner": "high"}} and rev.evidence["after"] == {"effort": {}}
    assert rev.judgment.value == "human"
    assert cli.main(["efficiency", j, "revert", "pc-1"]) == 0
    assert ep.load(store, j) == ep.DEFAULT_POLICY
    log = store.kv_get(j, "efficiency_policy_log")
    assert [e["reverted"] for e in log] == [True, True]
    capsys.readouterr()
    assert cli.main(["efficiency", j, "revert", "pc-1"]) == 1
    assert cli.main(["efficiency", j, "revert"]) == 1  # nothing left to revert
    with pytest.raises(ep.PolicyError):
        ep.revert(store, j, actor="watcher:efficiency", now=NOW)


def test_cli_set_unset_overrides_and_envelope(steward_home, tmp_path, capsys):
    from steward import cli

    store, job, proj, d = _job(steward_home, tmp_path)
    j = job.job_id
    assert cli.main(["efficiency", j, "set", "long_lived_calls=60", "envelope.effort_floor=high"]) == 0
    ov = store.kv_get(j, "efficiency_overrides")
    assert ov == {"long_lived_calls": 60, "envelope.effort_floor": "high"}
    assert isinstance(ov["long_lived_calls"], int)
    cfg = detect.load_config(store, j)
    assert cfg["long_lived_calls"] == 60 and cfg["envelope"]["effort_floor"] == "high"
    [ev] = _events(store, job, "efficiency_overrides_changed")
    assert ev.evidence["set"] == {"long_lived_calls": 60, "envelope.effort_floor": "high"}
    assert ev.evidence["previous"] == {"long_lived_calls": None, "envelope.effort_floor": None}
    assert ev.judgment.value == "human"
    assert cli.main(["efficiency", j, "set", "thinking_share=0.5", "envelope.plan_ahead=false"]) == 0
    ov = store.kv_get(j, "efficiency_overrides")
    assert ov["thinking_share"] == 0.5 and ov["envelope.plan_ahead"] is False
    assert cli.main(["efficiency", j, "unset", "long_lived_calls"]) == 0
    assert "long_lived_calls" not in store.kv_get(j, "efficiency_overrides")
    before = store.kv_get(j, "efficiency_overrides")
    capsys.readouterr()
    for bad in (["set", "bogus=1"], ["set", "long_lived_calls=many"], ["set", "policy.model=haiku"],
                ["set", "envelope.effort_floor=turbo"], ["set", "noequals"], ["unset", "bogus"],
                ["set", "long_lived_calls=70", "bogus=1"]):
        assert cli.main(["efficiency", j, *bad]) == 1, bad
    assert store.kv_get(j, "efficiency_overrides") == before


def test_nudge_restates_policy(steward_home, tmp_path):
    store, job, proj, d = _t2_active(steward_home, tmp_path)
    w = _watcher(store)
    w._act(job.job_id, [_policy_finding(store, job.job_id, "effort")], None, {"agent-e1"}, NOW)
    [ins] = _eff_instructions(store, job.job_id)
    assert ins["text"].endswith(" Job efficiency policy: effort gsd-planner=high.")
    assert len(ins["text"]) <= 600
