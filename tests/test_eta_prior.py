"""The ETA reference-class prior (phase 18-03, G4): job kinds, the seed, prior_for, and the store rebuild."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from steward import eta_prior
from steward import store as store_module
from steward.models import Judgment
from steward.store import Store

GOALS = {
    "gsd-quick": ["Use gsd-quick to fix the flaky heartbeat test"],
    "gsd-discuss": ["Start a new Steward capability with a GSD discussion phase only (no plans, no code)",
                    "UX/UI discussion for the Steward Android app, before any detailed build work"],
    "gsd-build": ['Build Steward Phase 16 "Efficiency watcher" with GSD, end to end.',
                  "Take the Steward Android app from spec to Play Store autonomously, using GSD."],
    "research": ["Conduct a one-time, comprehensive evidence review of Meta Muse",
                 "RIL Steward research for Pixel 8 Pro",
                 "Research and design a full-screen Android app"],
    "clock": ["Monitor the live temperature in Muscat, Oman for 6 hours.",
              "Smoke test for Steward: a 30-minute ticker."],
    "other": ["Design and build continuous deployment for the Steward Android app",
              "Upgrade Steward itself: build phase 15 'watcher authority'"],
}


@pytest.mark.parametrize("kind", sorted(GOALS))
def test_job_kind_patterns(kind):
    for goal in GOALS[kind]:
        assert eta_prior.job_kind(goal) == kind, goal
        assert eta_prior.job_kind(goal.upper()) == kind, goal
    assert eta_prior.job_kind("") == "other"
    assert eta_prior.job_kind(None) == "other"


def test_job_kind_order():
    # clock beats everything; quick beats discuss beats build beats research
    assert eta_prior.job_kind("gsd-quick research for 2 hours") == "clock"
    assert eta_prior.job_kind("gsd-quick discussion") == "gsd-quick"
    assert eta_prior.job_kind("a GSD discussion of research") == "gsd-discuss"
    assert eta_prior.job_kind("use GSD to research the thing") == "gsd-build"
    assert eta_prior.job_kind("the gsdx tool") == "other"  # whole-word gsd only


def test_seed_prior_values(tmp_path):
    priors = eta_prior.load(tmp_path)
    assert priors["rows"] and priors["built_at"] is None
    b = eta_prior.prior_for(priors, "gsd-build")
    assert b.peers == 3 and b.median_min == 181
    assert b.lo_min == pytest.approx(108.6) and b.hi_min == pytest.approx(289.6)
    assert b.per_unit_min == 17.7 and b.first_plan_min == 30
    assert b.plans_per_phase == pytest.approx(2.875)
    assert eta_prior.prior_for(priors, "gsd-quick").range_min == (3.1, 20.5)
    assert eta_prior.prior_for(priors, "gsd-quick").stage_left == {"testing": 5, "committing": 2}
    assert eta_prior.prior_for(priors, "gsd-discuss").range_min == (5.1, 6.2)
    assert eta_prior.prior_for(priors, "clock") is None            # recorded, never an estimate
    one = {"rows": [{"job": "x", "kind": "other", "minutes": 5}]}
    assert eta_prior.prior_for(one, "other") is None               # fewer than 2 peers -> estimating…
    assert eta_prior.prior_for({"rows": []}, "research") is None
    q = eta_prior.prior_for(priors, "research")
    assert q.per_unit_min is None and q.first_plan_min == 30.0 and q.plans_per_phase == 3.0


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _finished_job(store, monkeypatch, job_id, goal, created, done, *, eta=None):
    monkeypatch.setattr(store_module, "now_iso", lambda: _iso(created))
    store.create_job(goal, "/nonexistent/x", job_id=job_id)
    monkeypatch.setattr(store_module, "now_iso", lambda: _iso(done))
    store.append_event(job_id, what="worker reported done (typed frame)", evidence={}, rule="t", judgment=Judgment.code)
    if eta is not None:
        store.kv_set(job_id, "efficiency_state", {"eta": eta})


def test_rebuild_merges_store_jobs(tmp_path, monkeypatch):
    store = Store(tmp_path)
    t0 = 1_790_000_000.0
    _finished_job(store, monkeypatch, "aaaa1111-0000", "Use gsd-quick to fix A", t0, t0 + 7 * 60)
    _finished_job(store, monkeypatch, "bbbb2222-0000", "Use gsd-quick to fix B", t0, t0 + 11 * 60)
    _finished_job(store, monkeypatch, "a7d292d8-1234-5678", 'Build Steward Phase 16 with GSD', t0, t0 + 130 * 60,
                  eta={"units_done": 7, "phases": {"16": 7}, "first_build_at": t0 + 36 * 60, "first_plan_min": 31.5})
    # a job that never reported done is not a finished peer
    monkeypatch.setattr(store_module, "now_iso", lambda: _iso(t0))
    store.create_job("Use gsd-quick to fix C", "/nonexistent/x", job_id="cccc3333-0000")

    before = eta_prior.prior_for(eta_prior.load(tmp_path), "gsd-quick").peers
    data = eta_prior.rebuild(store, now=t0 + 9999)
    assert data is not None
    target = tmp_path / eta_prior.PRIORS_FILE
    assert target.is_file() and not (tmp_path / (eta_prior.PRIORS_FILE + ".tmp")).exists()
    priors = eta_prior.load(tmp_path)
    assert eta_prior.prior_for(priors, "gsd-quick").peers == before + 2
    rows = priors["rows"]
    assert [r["job"] for r in rows if r["kind"] == "gsd-quick"] == sorted(r["job"] for r in rows if r["kind"] == "gsd-quick")
    assert [(r["kind"], r["job"]) for r in rows] == sorted((r["kind"], r["job"]) for r in rows)
    # the seed row a7d292d8 is replaced field by field by the store row
    a7 = [r for r in rows if r["job"].startswith("a7d292d8")]
    assert len(a7) == 1 and a7[0]["job"] == "a7d292d8-1234-5678"
    assert a7[0]["minutes"] == 130 and a7[0]["plans"] == 7 and a7[0]["phases"] == 1
    assert a7[0]["per_unit_min"] == pytest.approx((130 - 36) / 7, abs=0.01)
    assert a7[0]["first_plan_min"] == 31.5
    assert sum(1 for r in rows if r["kind"] == "gsd-build") == 3          # replaced, not added
    first = target.read_bytes()
    eta_prior.rebuild(store, now=t0 + 9999)
    assert target.read_bytes() == first
    eta_prior.rebuild(store)                                              # no clock given: reproducible as well
    second = target.read_bytes()
    eta_prior.rebuild(store)
    assert target.read_bytes() == second


def test_rebuild_keeps_seed_value_where_store_has_none(tmp_path, monkeypatch):
    store = Store(tmp_path)
    t0 = 1_790_000_000.0
    _finished_job(store, monkeypatch, "ed11ba3c-9999", "Build Steward Phase 17 with GSD", t0, t0 + 200 * 60)
    eta_prior.rebuild(store)
    row = [r for r in eta_prior.load(tmp_path)["rows"] if r["job"].startswith("ed11ba3c")][0]
    assert row["minutes"] == 200 and row["per_unit_min"] == 20.5 and row["plans"] == 7 and row["first_plan_min"] == 31


def test_load_falls_back_to_the_seed(tmp_path):
    seed_kinds = {r["kind"] for r in eta_prior.load(tmp_path)["rows"]}
    (tmp_path / eta_prior.PRIORS_FILE).write_text("{not json", encoding="utf-8")
    assert {r["kind"] for r in eta_prior.load(tmp_path)["rows"]} == seed_kinds
    (tmp_path / eta_prior.PRIORS_FILE).write_text(json.dumps({"rows": [{"kind": "nope"}]}), encoding="utf-8")
    assert eta_prior.prior_for(eta_prior.load(tmp_path), "gsd-build").median_min == 181
    (tmp_path / eta_prior.PRIORS_FILE).write_text(json.dumps([1, 2]), encoding="utf-8")
    assert eta_prior.prior_for(eta_prior.load(tmp_path), "gsd-build").median_min == 181


def test_rebuild_never_raises(tmp_path):
    class Broken:
        home = tmp_path

        def list_jobs(self):
            raise RuntimeError("db gone")

    assert eta_prior.rebuild(Broken()) is None
