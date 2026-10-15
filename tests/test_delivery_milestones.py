"""Plan 4 Task 7: accepted is not delivered; authority is looked up, never inferred; owner decisions are exact
and asked once (spec §2, §4, §9; P4-9/10/14/24/25/26)."""
from __future__ import annotations

from steward import approvals, autopilot, blockers, deadline, delivery, fleet_closeout, incorporate, ownership
from steward.store import Store
from owner_surface import OwnerSurface
from test_incorporate import git, mkjob, scratch  # noqa: F401 - scratch is a fixture


def human_mode(monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "human")  # [autopilot] merge defaults off in human mode


def accepted(store, scratch, tmp_path, goal="Add the thing to the repo"):
    return mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}, goal=goal).job_id


def test_acceptance_without_incorporation_authority_asks_exactly_once(steward_home, scratch, tmp_path, monkeypatch):
    human_mode(monkeypatch)
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = accepted(store, scratch, tmp_path)
    head_before = git(scratch, "rev-parse", "HEAD")
    for t in (100.0, 200.0, 300.0):
        out = delivery.plan(store, jid, now=t, notifier=surf.notes)
    assert out["step"]["kind"] == "owner_decision" and out["step"]["milestone"] == "incorporation"
    (b,) = [b for b in blockers.open_blockers(store, jid) if b.kind == delivery.DECISION_KIND]
    assert b.disposition == "confirmed_human"
    decision = b.human_request["decision"]
    assert decision.startswith(f"Incorporate job {jid[:8]}") and "into main" in decision
    assert "no incorporation authority" in b.human_request["why_authority_insufficient"]
    phone = [r for r in surf.owner_requests(jid) if r["channel"] == "phone"]
    assert len(phone) == 1 and phone[0]["kind"] == "NEEDS_YOU"
    assert git(scratch, "rev-parse", "HEAD") == head_before and not incorporate.is_incorporated(store, jid)
    assert surf.snapshot(jid)["liveness"][0] != "done" and not surf.concealed(jid)


def test_with_authority_the_controller_incorporates_and_nobody_is_asked(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    jid = accepted(store, scratch, tmp_path)
    out = delivery.plan(store, jid, now=100.0)
    assert out["step"] == {"kind": "incorporate", "milestone": "incorporation", "authority": "steward.toml [autopilot] merge"}
    assert not [b for b in blockers.open_blockers(store, jid) if b.kind == delivery.DECISION_KIND]


def test_owner_no_merge_instruction_is_binding_and_never_asked(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = accepted(store, scratch, tmp_path, goal="Add the thing. Do not merge this branch.")
    out = delivery.plan(store, jid, now=100.0, notifier=surf.notes)
    inc_m = next(m for m in delivery.milestones(store, jid) if m.name == "incorporation")
    assert (inc_m.required, inc_m.status) == (False, "not_required") and "not to merge" in inc_m.reason
    assert out["step"]["kind"] == "none" and surf.owner_requests(jid) == []
    assert "not merged" in delivery.summary(store, jid)["text"]


def test_floor_goal_and_deadline_gate_need_exactly_one_owner_merge_decision(steward_home, scratch, tmp_path,
                                                                             monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    floor = accepted(store, scratch, tmp_path, goal="Build it and publish the package")
    dl = accepted(store, scratch, tmp_path)
    monkeypatch.setattr(deadline, "merge_protected", lambda st, j: "deadline_mode" if j == dl else None)
    for jid, why in ((floor, "floor action"), (dl, "deadline")):
        delivery.plan(store, jid, now=100.0)
        delivery.plan(store, jid, now=200.0)
        decs = [b for b in blockers.open_blockers(store, jid) if b.kind == delivery.DECISION_KIND]
        assert len(decs) == 1 and why in decs[0].human_request["why_authority_insufficient"]


def test_fleet_job_incorporation_is_always_an_owner_decision(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    monkeypatch.setattr(fleet_closeout, "is_fleet", lambda st, j: True)
    store = Store(steward_home)
    jid = accepted(store, scratch, tmp_path)
    assert delivery.plan(store, jid, now=100.0)["step"]["kind"] == "owner_decision"


def test_approval_authorizes_and_denial_retires_the_milestone(steward_home, scratch, tmp_path, monkeypatch):
    human_mode(monkeypatch)
    store = Store(steward_home)
    a, d = accepted(store, scratch, tmp_path), accepted(store, scratch, tmp_path)
    for jid in (a, d):
        delivery.plan(store, jid, now=100.0)
    aid = lambda jid: next(x["id"] for x in approvals.list_approvals(store, jid, status="pending")  # noqa: E731
                           if x.get("kind") == delivery.APPROVAL_KIND)
    approvals.decide(store, a, aid(a), "approve", decided_by="human")
    approvals.decide(store, d, aid(d), "deny", decided_by="human")
    auth = delivery.incorporation_authority(store, a)
    assert auth.allowed and auth.source.startswith("owner approval")
    assert delivery.plan(store, a, now=200.0)["step"]["kind"] == "incorporate"
    m = next(m for m in delivery.milestones(store, d) if m.name == "incorporation")
    assert (m.status, m.reason) == ("not_required", "owner declined incorporation")


def test_accepted_with_pending_delivery_is_never_shown_done(steward_home, scratch, tmp_path, monkeypatch):
    human_mode(monkeypatch)
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = accepted(store, scratch, tmp_path)
    delivery.plan(store, jid, now=100.0, notifier=surf.notes)
    snap = surf.snapshot(jid)
    assert snap["liveness"][0] == "waiting" and "incorporation" in snap["liveness"][1]
    assert snap["label"].startswith("delivery: incorporation")
    incorporate.incorporate(store, jid)  # the owner merges by hand
    ownership.reconcile_job(store, jid, now=200.0)
    delivery.plan(store, jid, now=200.0)
    assert not [b for b in blockers.open_blockers(store, jid) if b.kind == delivery.DECISION_KIND]
    assert delivery.summary(store, jid)["delivered"] and surf.snapshot(jid)["liveness"][0] == "done"


def test_delivery_decision_pages_once_on_every_surface(steward_home, scratch, tmp_path, monkeypatch):
    human_mode(monkeypatch)
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = accepted(store, scratch, tmp_path)
    for t in (100.0, 200.0):
        delivery.plan(store, jid, now=t, notifier=surf.notes)
        surf.alerts(jid)
    chans = sorted(r["channel"] for r in surf.owner_requests(jid))
    assert chans == ["phone", "telegram"]
    assert any(k == "delivery" for k, _ in surf.snapshot(jid)["alert_reasons"])


def test_milestones_are_in_the_ownership_projection(steward_home, scratch, tmp_path, monkeypatch):
    human_mode(monkeypatch)
    store = Store(steward_home)
    jid = accepted(store, scratch, tmp_path)
    delivery.plan(store, jid, now=100.0)
    ownership.reconcile_job(store, jid, now=101.0)
    ms = ownership.projection(store, jid)["milestones"]
    assert ms["incorporation"]["status"] == "pending" and ms["activation"]["status"] == "not_required"
