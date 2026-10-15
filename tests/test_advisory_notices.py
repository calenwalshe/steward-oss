"""Plan 3 Task 4 (spec §7, scenario 7): every default spend threshold alerts once per meaningful update and work
continues without acknowledgment; an owner hard cap pauses the job with one exact owner decision and resumes it when
the owner raises the cap. Through the real call sites, not only the module."""
from __future__ import annotations

import pytest
from recovery_fixtures import research_job
from test_research import _research_runner
from test_resolver import LOW_HOLD, _hold, _run, _setup, popen

from ownership_fixtures import Clock, Notes, contracted
from steward import approvals as approvals_module
from steward import blockers, custom_watchers as cw
from steward import efficiency_watch as ew
from steward import resolver as rs
from steward import spend_policy as sp
from steward.models import JobState, Judgment
from steward.store import Store


class Asked:
    def __init__(self):
        self.calls = []

    def __call__(self, job_id, kind, title, body):
        self.calls.append((kind, title, body))
        return True


def test_advise_bands_dedup_and_spacing_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    jid = research_job(store, None)            # research advisory $5
    asked, clock = Asked(), Clock()
    assert not sp.advise(store, jid, "research", observed_usd=4.0, notify_fn=asked, now=clock())
    assert sp.advise(store, jid, "research", observed_usd=5.2, notify_fn=asked, now=clock())
    assert not sp.advise(store, jid, "research", observed_usd=6.0, notify_fn=asked, now=clock.advance(60))  # band 0
    assert not sp.advise(store, jid, "research", observed_usd=7.6, notify_fn=asked, now=clock.advance(60))  # band 1 < 1 h
    assert sp.advise(store, jid, "research", observed_usd=10.1, notify_fn=asked, now=clock.advance(60))  # band 2: +2
    assert not sp.advise(store, jid, "research", observed_usd=7.9, notify_fn=asked, now=clock.advance(4000))
    assert sp.advise(store, jid, "research", observed_usd=12.6, notify_fn=asked, now=clock.advance(10))  # band 3
    assert [k for k, _, _ in asked.calls] == ["BUDGET", "BUDGET", "BUDGET"]
    assert all("no action needed" in body for _, _, body in asked.calls)
    assert sp.estimate(store, jid)["revision"] == 3


def test_owner_hard_scope_never_gets_an_advisory_notice(steward_home):
    store = Store(steward_home)
    jid = research_job(store, 2.0)
    assert not sp.advise(store, jid, "research", observed_usd=9.0, notify_fn=Asked(), now=1.0)


def test_undelivered_notice_is_retried_not_lost(steward_home):
    store = Store(steward_home)
    jid = research_job(store, None)
    clock = Clock()
    assert not sp.advise(store, jid, "research", observed_usd=5.5, notify_fn=lambda *a: False, now=clock())
    assert not sp.advise(store, jid, "research", observed_usd=5.5, notify_fn=Asked(), now=clock.advance(100))
    assert sp.advise(store, jid, "research", observed_usd=5.5, notify_fn=Asked(), now=clock.advance(300))


def test_research_default_cap_crossed_in_the_runner_alerts_and_keeps_approving_continuous_ownership_recovery_acceptance(steward_home):
    reqs = [("mcp__research__research_deep", f"q{i}") for i in range(12)]
    store, job, runner, factory, sent = _research_runner(steward_home, 1.0, reqs)
    store._conn.execute("DELETE FROM spend_limits WHERE job_id = ?", (job.job_id,))  # make it the default cap
    store._conn.commit()
    sp.set_limit(store, job.job_id, "research", 1.0, binding="advisory", provenance="system_default",
                 source={"via": "test-default"}, now=1.0)
    runner.tick(job.job_id)
    statuses = [a["status"] for a in approvals_module.list_approvals(store, job.job_id)]
    assert statuses == ["approved"] * 12
    kinds = [ev.kind for ev in sent]
    assert "NEEDS_YOU" not in kinds and kinds.count("BUDGET") == 1
    assert not [e for e in store.events(job.job_id) if e.what == "research_cap_tripped"]


def test_spend_rate_is_advisory_and_realerts_only_on_meaningful_change(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    asked = Asked()
    apply = lambda usd: ew.apply_spend_shape(store, jid, {"sources": {}, "last_hour": {"usd": usd}}, asked)
    apply(6.2)
    apply(6.4)
    apply(9.0)  # band 1 ($7.50+) within the hour of the band-0 notice: not yet meaningful
    kinds = [k for k, _, _ in asked.calls]
    assert kinds == ["BUDGET"] and "NEEDS_YOU" not in kinds
    assert len([e for e in store.events(jid) if e.what == "spend_rate_high"]) == 1


def test_watcher_observe_only_keeps_reduction_but_notice_is_advisory(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    notes = []
    w = cw.CustomWatchers(store, notify=lambda *a: notes.append(a), spend_fn=lambda job_id: (1.0, 0.5))
    assert w._observe_only(jid, 1.0) is True
    assert store.kv_get(jid, "watcher_observe_only") is True
    assert [n[1] for n in notes] == ["BUDGET"]


def test_resolver_spend_over_default_is_advisory(steward_home, popen):
    popen.cost = 5.0
    notes = []
    store, jid, eng = _setup(steward_home, notes, cfg=rs.ResolverConfig(max_usd_per_hour=1.0))
    _hold(store, jid, eng, *LOW_HOLD)
    _run(eng, jid)
    rec2, _ = _hold(store, jid, eng, "git commit -m 'b.pem'", "reads a credential path: b.pem")
    _run(eng, jid)
    assert len(popen.instances) == 2  # the second resolution still ran
    assert "spend cap" not in str(approvals_module.get_approval(store, jid, rec2["id"]).get("resolver"))
    assert any(n[1] == "BUDGET" for n in notes) and not any(n[1] == "NEEDS_YOU" for n in notes)
    assert sp.report(store, jid, tokens_data={"sources": {}}).by_source["resolver"]["usd"] == pytest.approx(10.0)


def test_resolver_start_rate_defers_instead_of_escalating(steward_home, popen):
    notes = []
    store, jid, eng = _setup(steward_home, notes, cfg=rs.ResolverConfig(max_per_hour=2))
    recs = [_hold(store, jid, eng, f"git commit -m 'x{i}.pem'", "reads a credential path: x.pem")[0] for i in range(3)]
    _run(eng, jid)
    assert len(popen.instances) == 2
    third = approvals_module.get_approval(store, jid, recs[2]["id"])
    assert third["status"] == "pending" and not any("budget" in str(n) for n in notes)
    assert [e for e in store.events(jid) if e.what == "resolver_deferred"]
    eng.clock = lambda: 10**10  # an hour later the slot is free again
    _run(eng, jid)
    assert len(popen.instances) == 3


def test_owner_hard_cap_pauses_with_one_exact_decision_and_resumes_on_raise_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    notes, clock = Notes(), Clock()
    sp.set_limit(store, jid, "job_total", 1.00, binding="hard", provenance="owner_explicit",
                 source={"via": "spend-limit"}, now=clock())
    sp.record(store, entry_id="lead:1", job_id=jid, source="tech_lead", usd=1.10, price_basis="reported", now=clock())
    assert sp.enforce(store, jid, now=clock(), notify_fn=lambda *args: True) == "paused"
    assert store.get_job(jid).state == JobState.PAUSED
    assert sp.enforce(store, jid, now=clock.advance(30), notify_fn=lambda *args: True) is None   # no second request
    (b,) = [b for b in blockers.open_blockers(store, jid) if b.kind == "hard_cap_reached"]
    assert b.disposition == "confirmed_human" and "Raise the $1.00" in b.human_request["decision"]
    sp.set_limit(store, jid, "job_total", 5.00, binding="hard", provenance="owner_explicit",
                 source={"via": "spend-limit"}, now=clock.advance(60))
    assert sp.enforce(store, jid, now=clock.advance(1)) == "resumed"
    assert store.get_job(jid).state == JobState.RUNNING
    assert blockers.get(store, b.blocker_id).disposition == "resolved"


def test_an_owner_pause_is_never_lifted_by_a_cap_raise(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.set_state(jid, JobState.PAUSED, what="operator pause", evidence={}, rule="operator_pause",
                    judgment=Judgment.human)
    sp.set_limit(store, jid, "job_total", 5.00, binding="hard", provenance="owner_explicit",
                 source={"via": "spend-limit"}, now=1.0)
    assert sp.enforce(store, jid, now=2.0) is None and store.get_job(jid).state == JobState.PAUSED
