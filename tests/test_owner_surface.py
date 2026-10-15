"""Plan 4 Task 2: the harness sees what the owner sees (spec §11 owner-visible qualification)."""
from __future__ import annotations

from steward import jobview, ownership
from steward.store import Store
from owner_surface import OwnerSurface
from ownership_fixtures import park_done, park_tracks


def test_technical_park_makes_no_owner_request_on_any_surface(steward_home, monkeypatch):
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = park_tracks(store)
    ownership.reconcile_job(store, jid, now=100.0, notifier=surf.notes)
    surf.alerts(jid)
    snap = surf.snapshot(jid)
    assert snap["owner_view"]["status"] == "awaiting_technical_review"
    assert surf.owner_requests(jid) == []
    assert not surf.concealed(jid)


def test_human_acceptance_is_exactly_one_request_per_channel(steward_home, monkeypatch):
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = park_done(store)
    ownership.reconcile_job(store, jid, now=100.0, notifier=surf.notes)
    ownership.reconcile_job(store, jid, now=200.0, notifier=surf.notes)
    surf.alerts(jid)
    surf.alerts(jid)
    reqs = surf.owner_requests(jid)
    assert [r["channel"] for r in reqs].count("phone") == 1
    assert [r["channel"] for r in reqs].count("telegram") == 1
    assert surf.snapshot(jid)["owner_view"]["status"] == "needs_owner"


def test_concealment_is_detected_when_a_surface_reads_done_for_unfinished_work(steward_home, monkeypatch):
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = park_done(store)
    ownership.reconcile_job(store, jid, now=100.0, notifier=surf.notes)
    monkeypatch.setattr(jobview, "liveness", lambda state, events, now=None, **kw: ("done", "0s ago"))
    assert surf.concealed(jid)


def test_pane_pushes_and_runner_notices_are_recorded(steward_home, monkeypatch):
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = park_done(store)
    from steward import push
    push.notify_creator(store, jid, "auto_escalated", surf.pane_runner, facts={"reason": "merge not done"})
    surf.notify_fn(jid, "NEEDS_YOU", "Steward: needs you", "merge not done")
    chans = sorted(r["channel"] for r in surf.owner_requests(jid))
    assert chans == ["pane", "runner"]
