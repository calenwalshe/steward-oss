"""Continuous delivery scheduling stays supervised and owner-gated."""

from steward import autopilot, blockers, check_runs, delivery, ownership, questions_delivery
from steward import ownership
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from owner_surface import OwnerSurface
from test_incorporate import git, mkjob, scratch  # noqa: F401 - scratch is a fixture


def accepted(store, scratch, tmp_path):
    return mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}).job_id


def test_sweep_asks_once_when_incorporation_has_no_authority(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "human")
    store = Store(steward_home)
    surf = OwnerSurface(store, monkeypatch)
    jid = accepted(store, scratch, tmp_path)
    spawn_calls = []

    def spawn(*args):
        spawn_calls.append(args)
        return 99999999

    first = delivery.sweep(store, now=1000.0, spawn=spawn, notifier=surf.notes)
    second = delivery.sweep(store, now=1031.0, spawn=spawn, notifier=surf.notes)

    assert first["owner_decisions"] == [jid]
    assert second["owner_decisions"] == [jid]
    assert spawn_calls == []
    assert len([b for b in blockers.open_blockers(store, jid) if b.kind == delivery.DECISION_KIND]) == 1
    assert len(surf.owner_requests(jid)) == 1


def test_sweep_claims_one_worker_for_authorized_snapshot(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    jid = accepted(store, scratch, tmp_path)
    spawn_calls = []

    def spawn(*args):
        spawn_calls.append(args)
        return 99999999  # deliberately absent PID; a fresh heartbeat lease still prevents a duplicate launch

    first = delivery.sweep(store, now=1000.0, spawn=spawn)
    second = delivery.sweep(store, now=1031.0, spawn=spawn)

    assert first["launched"] == [{"job": jid, "step": "incorporate", "pid": 99999999}]
    assert second["active"] == [{"job": jid, "step": "incorporate"}]
    assert len(spawn_calls) == 1
    assert store.kv_get(delivery.PSEUDO_JOB, delivery.WORKER_KEY)["job_id"] == jid


def test_worker_verifies_then_incorporates_the_accepted_branch(steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    monkeypatch.setattr(autopilot, "_check_lines", lambda tree: [])
    store = Store(steward_home)
    jid = accepted(store, scratch, tmp_path)

    result = delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0)

    merged = delivery.inc.is_incorporated(store, jid)
    assert result["outcome"] == "incorporated"
    assert merged and git(scratch, "merge-base", "--is-ancestor", merged, "HEAD") == ""
    rec = delivery.receipts(store, jid, "incorporation")[-1]
    assert rec["evidence"]["tested_tree_matches"] is True
    assert delivery.summary(store, jid)["delivered"]


def test_existing_delivery_authority_reaches_incorporation_and_activation_continuous_ownership_recovery_acceptance(
        steward_home, scratch, tmp_path, monkeypatch):
    from steward import activation
    from test_activation import GREEN, NO_SLEEP, control_at

    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    monkeypatch.setattr(delivery, "activation_targets", lambda _store, _job: ("control",))
    store = Store(steward_home)
    jid = accepted(store, scratch, tmp_path)
    result = delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0)

    assert result["outcome"] == "incorporated"
    merge = delivery.receipts(store, jid, "incorporation")[-1]["revision"]
    activated = activation.activate(store, jid, merge,
                                    deploy=lambda _store, **_kw: {"ok": True, "summary": "fixture restart"},
                                    probes=control_at(merge), health_fn=GREEN, now=1100.0, sleep=NO_SLEEP,
                                    authorized=True)

    assert activated["activated"] is True
    assert delivery.receipts(store, jid, "activation")[-1]["revision_contains"] == merge
    assert delivery.summary(store, jid)["delivered"] is True


def test_dirty_overlapping_source_waits_without_touching_it_and_retries_when_clean(
        steward_home, scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "enabled", lambda: True)
    store = Store(steward_home)
    jid = mkjob(store, scratch, tmp_path, {"a.txt": "job side\n"}).job_id
    (scratch / "a.txt").write_text("owner edit\n")

    result = delivery.run_delivery(store, jid, SubprocessRunner(), now=1000.0)

    assert result["outcome"] == "waiting_external"
    assert (scratch / "a.txt").read_text() == "owner edit\n"
    assert len([b for b in blockers.open_blockers(store, jid) if b.kind == delivery.WAITING_KIND]) == 1
    (scratch / "a.txt").write_text("a\n")
    ownership.reconcile_job(store, jid, now=1031.0)
    assert not [b for b in blockers.open_blockers(store, jid) if b.kind == delivery.WAITING_KIND]
    assert delivery.next_step(store, jid, now=1031.0)["kind"] == "incorporate"


def test_control_tick_schedules_delivery_and_reconciles_shared_checks(steward_home, monkeypatch):
    store = Store(steward_home)
    monkeypatch.setattr(ownership, "enabled", lambda: True)
    calls = []
    monkeypatch.setattr(delivery, "sweep", lambda _store, **kw: calls.append(("delivery", kw)) or
                        {"launched": [], "errors": []})
    monkeypatch.setattr(check_runs, "reconcile", lambda _store, **kw: calls.append(("checks", kw)) or {})
    spawn = lambda *_: 123

    result = questions_delivery.tick(store, SubprocessRunner(), now=1020.0, delivery_spawn=spawn)

    assert result["delivery"]["launched"] == []
    assert calls == [("delivery", {"now": 1020.0, "spawn": spawn, "notifier": None}),
                     ("checks", {"now": 1020.0})]
