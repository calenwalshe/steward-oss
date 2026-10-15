"""Plan 3 Task 2: limits carry provenance; defaults are advisory; owner caps are hard everywhere; legacy caps migrate
without softening an owner cap or hardening a default; unknown price is never zero."""
from __future__ import annotations

import pytest
from recovery_fixtures import research_job

from ownership_fixtures import Clock, contracted
from steward import cli, research, spend_policy as sp
from steward.models import Judgment
from steward.store import Store


def test_default_research_cap_is_advisory_owner_cap_binds(steward_home):
    store = Store(steward_home)
    default_job = research_job(store, None)
    owner_job = research_job(store, 2.0)
    d = sp.effective(store, default_job, "research")
    o = sp.effective(store, owner_job, "research")
    assert (d.binding, d.provenance, d.usd) == ("advisory", "system_default", 5.0)
    assert (o.binding, o.provenance, o.usd) == ("hard", "owner_explicit", 2.0)
    assert sp.hard_limits(store, default_job) == []
    assert [lim.scope for lim in sp.hard_limits(store, owner_job)] == ["research"]


def test_research_default_cap_alerts_and_continues(steward_home):
    """G32 at the module seam (the runner path is Task 4): past the default cap a call is still allowed."""
    store = Store(steward_home)
    jid = research_job(store, None)
    allowed = [research.charge(store, jid, "mcp__research__research_deep", {})[0] for _ in range(12)]
    assert all(allowed)  # 12 x $0.50 = $6 > $5 default: advisory, never a deny
    info = research.charge(store, jid, "mcp__research__research_quick", {})[1]
    assert info["binding"] == "advisory" and info["over"] is True and info["trip"] is False


def test_owner_research_cap_still_denies(steward_home):
    store = Store(steward_home)
    jid = research_job(store, 1.0)
    got = [research.charge(store, jid, "mcp__research__research_deep", {})[0] for _ in range(3)]
    assert got == [True, True, False]


@pytest.mark.legacy_ownership_off
def test_legacy_switch_keeps_default_cap_hard(steward_home):
    store = Store(steward_home)
    jid = research_job(store, None)
    got = [research.charge(store, jid, "mcp__research__research_deep", {})[0] for _ in range(11)]
    assert got[-1] is False


def _legacy_job(store, *, cap, goal="fix it. Finish line: `pytest -q`", raised: float | None = None):
    """A pre-Plan-3 job: capabilities in kv, the legacy capability_granted event, no spend_limits row."""
    jid = contracted(store, goal)
    store.kv_set(jid, "capabilities", {"research": True, "keys": [], "cap_usd": cap})
    store.append_event(jid, what="capability_granted", evidence={"capability": "research", "cap_usd": cap},
                       rule="job_capability", judgment=Judgment.human)
    if raised is not None:
        store.kv_set(jid, "capabilities", {"research": True, "keys": [], "cap_usd": raised})
        store.append_event(jid, what="research_cap_raised", evidence={"cap_usd": raised, "previous_cap_usd": cap},
                           rule="research_cap_raised", judgment=Judgment.human)
    store._conn.execute("DELETE FROM spend_limits WHERE job_id = ?", (jid,))
    store._conn.commit()
    return jid


def test_legacy_non_default_cap_migrates_hard(steward_home):
    store = Store(steward_home)
    jid = _legacy_job(store, cap=3.0)
    (lim,) = sp.migrate_legacy(store, jid, now=1.0)
    assert (lim.binding, lim.provenance, lim.usd) == ("hard", "owner_legacy", 3.0)


def test_legacy_raised_cap_migrates_owner_explicit(steward_home):
    store = Store(steward_home)
    jid = _legacy_job(store, cap=5.0, raised=8.0)
    (lim,) = sp.migrate_legacy(store, jid, now=1.0)
    assert (lim.binding, lim.provenance, lim.usd) == ("hard", "owner_explicit", 8.0)
    assert lim.source["via"] == "research_cap_raised"


def test_legacy_default_cap_with_owner_text_stays_hard(steward_home):
    store = Store(steward_home)
    jid = _legacy_job(store, cap=5.0, goal="Survey the vendors. Keep research spend under $5. Finish line: report.md")
    (lim,) = sp.migrate_legacy(store, jid, now=1.0)
    assert (lim.binding, lim.provenance) == ("hard", "owner_legacy")
    assert "research spend under $5" in lim.source["text"]


def test_legacy_default_cap_without_owner_source_becomes_advisory_and_is_disclosed_once(steward_home):
    store = Store(steward_home)
    jid = _legacy_job(store, cap=5.0)
    (lim,) = sp.migrate_legacy(store, jid, now=1.0)
    assert (lim.binding, lim.provenance) == ("advisory", "migrated_default")
    assert sp.migrate_legacy(store, jid, now=2.0) == [lim]  # idempotent
    disclosed = [e for e in store.events(jid) if e.what == "spend_limit_migrated"]
    assert len(disclosed) == 1 and disclosed[0].evidence["binding"] == "advisory"


def test_owner_hard_limit_is_never_softened_by_a_default_write(steward_home):
    store = Store(steward_home)
    jid = research_job(store, 2.0)
    with pytest.raises(sp.SpendPolicyError, match="owner"):
        sp.set_limit(store, jid, "research", 5.0, binding="advisory", provenance="system_default",
                     source={"via": "default"}, now=1.0)
    with pytest.raises(sp.SpendPolicyError, match="owner source"):
        sp.set_limit(store, jid, "job_total", 5.0, binding="hard", provenance="migrated_default",
                     source={"via": "x"}, now=1.0)
    assert sp.effective(store, jid, "research").binding == "hard"


def test_unknown_price_is_disclosed_and_unbounded_work_waits_under_hard_cap(steward_home):
    store = Store(steward_home)
    clock = Clock()
    jid = contracted(store)
    sp.set_limit(store, jid, "job_total", 1.00, binding="hard", provenance="owner_explicit",
                 source={"via": "spend-limit", "actor": "owner:cli"}, now=clock())
    sp.record(store, entry_id="jev:1", job_id=jid, source="jev", usd=None, price_basis="unknown", now=clock())
    sp.record(store, entry_id="lead:1", job_id=jid, source="tech_lead", usd=0.40, price_basis="reported", now=clock())
    fits = sp.check_hard(store, jid, source="flow_investigator", bound_usd=0.50, now=clock())
    assert fits.ok and fits.known_usd == pytest.approx(0.40) and fits.unknown_calls == 1
    assert "1 call(s) with unknown price" in fits.reason
    unbounded = sp.check_hard(store, jid, source="jev", bound_usd=None, now=clock())
    assert not unbounded.ok and unbounded.reason.startswith("unbounded_under_hard_cap")
    held = sp.commit(store, jid, source="flow_investigator", bound_usd=0.50, now=clock())
    too_big = sp.check_hard(store, jid, source="tech_lead", bound_usd=0.20, now=clock())
    assert not too_big.ok and too_big.committed_usd == pytest.approx(0.50)
    sp.settle(store, held, now=clock())
    assert sp.check_hard(store, jid, source="tech_lead", bound_usd=0.20, now=clock()).ok


def test_without_hard_limits_everything_fits_and_unknown_is_still_unknown(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    sp.record(store, entry_id="jev:1", job_id=jid, source="jev", usd=None, price_basis="unknown", now=1.0)
    chk = sp.check_hard(store, jid, source="jev", bound_usd=None, now=2.0)
    assert chk.ok and chk.limit is None and chk.unknown_calls == 1


def test_ledger_entries_are_idempotent(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    assert sp.record(store, entry_id="x", job_id=jid, source="resolver", usd=0.1, price_basis="reported", now=1.0)
    assert not sp.record(store, entry_id="x", job_id=jid, source="resolver", usd=0.1, price_basis="reported", now=2.0)
    assert sp.known_spend(store, jid) == (pytest.approx(0.1), 0)


def test_spend_limit_verb_records_an_owner_hard_limit_and_refuses_workers(steward_home):
    store = Store(steward_home)
    jid = contracted(store)
    handlers = cli._build_control_handlers(store)
    ok = handlers["spend_limit"]({"args": {"job_id": jid, "usd": 12.0, "scope": "job_total", "hard": True},
                                  "origin": "cli"})
    assert ok["accepted"] and "hard" in ok["detail"]
    assert sp.effective(store, jid, "job_total").provenance == "owner_explicit"
    bad = handlers["spend_limit"]({"args": {"job_id": jid, "usd": 99.0, "scope": "job_total", "hard": False},
                                   "origin": "worker"})
    assert not bad["accepted"] and sp.effective(store, jid, "job_total").usd == 12.0
