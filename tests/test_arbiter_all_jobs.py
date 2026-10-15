"""SUP-07: the Arbiter routes facts for every job (single-worker and fleet) to exactly one rung.

v2.5's fleet routes and the single-lane doctor path are unchanged; the new general facts (the code-fact floor and
Jev's general `permission_confusion` / `thrash`) route to the tech lead (`outer`)."""

from __future__ import annotations

import pytest

from steward import code_facts, jev_watch
from steward import fleet_runtime as rt
from steward.store import Store
from test_runner import _contracted_job

RUNGS = {"inner", "doctor", "advisory", "outer"}


@pytest.fixture
def job(steward_home):
    store = Store(steward_home)
    return store, _contracted_job(store, "single-worker job").job_id


def test_single_worker_job_facts_route_to_exactly_one_rung(job):
    store, jid = job
    arb = rt.Arbiter(store)
    for kind in code_facts.KINDS:
        fact = arb.submit(jid, code_facts.SOURCE, kind, key="k")
        assert fact["route"] == "outer"
    for kind in ("permission_confusion", "thrash"):
        assert arb.submit(jid, "jev", kind)["route"] == "outer"
    kinds = [f["kind"] for f in arb.pending_for_tech_lead(jid)]
    assert sorted(kinds) == sorted([*code_facts.KINDS, "permission_confusion", "thrash"])
    assert all(f["route"] in RUNGS for f in store.kv_get(jid, rt._ARBITER)["facts"])


def test_v25_routing_matrix_is_unchanged(job):
    store, jid = job
    arb = rt.Arbiter(store)
    matrix = {
        ("heartbeat", "progressing"): "inner", ("recovery", "replace"): "inner", ("lifecycle", "lane_started"): "inner",
        ("heartbeat", "stall"): "doctor", ("doctor", "diagnosed"): "doctor",
        ("jev", "flag"): "advisory", ("jev", "lanes_overlapping"): "advisory", ("jev", "looping"): "advisory",
        ("jev", "done_unproven"): "advisory", ("efficiency", "finding"): "advisory",
        ("trajectory", "drift"): "outer", ("deadline", "deadline_risk"): "outer",
        ("jev_fleet", "lanes_overlapping"): "outer", ("jev_fleet", "runtime_misbehaving"): "outer",
        ("jev_fleet", "lane_stalled"): "doctor", ("jev_fleet", "ok"): "advisory",
        ("somebody", "new_thing"): "advisory",
        ("code_facts", "not_a_floor_kind"): "advisory",  # the floor's source cannot smuggle in arbitrary triggers
    }
    for (source, kind), route in matrix.items():
        assert arb.submit(jid, source, kind, lane="lane-a" if source == "jev_fleet" else None)["route"] == route, \
            (source, kind)


def test_legacy_jev_routes_are_unchanged():
    assert jev_watch.ROUTES == {
        "stalled": "doctor", "hung_call": "doctor+permission", "looping": "watcher", "off_track": "watcher",
        "risky_action": "needs_you",
        "permission_confusion": "tech_lead", "thrash": "tech_lead"}


def test_runner_feeds_job_level_facts_for_a_single_worker_job(job):
    from test_stability import ScriptedRunner, _runner

    store, jid = job
    runner = _runner(store, ScriptedRunner(), [[]])
    runner.job_fact(jid, code_facts.SOURCE, "finish_check_unverified", key="e1")
    assert [f["kind"] for f in runner.arbiter.pending_for_tech_lead(jid)] == ["finish_check_unverified"]


def test_job_fact_never_raises(job):
    from test_stability import ScriptedRunner, _runner

    store, jid = job
    runner = _runner(store, ScriptedRunner(), [[]])

    class Broken:
        def submit(self, *a, **k):
            raise RuntimeError("boom")

    runner.arbiter = Broken()
    runner.job_fact(jid, code_facts.SOURCE, "thrash")  # recorded, never raised
    assert any(e.what == "job_fact_error" for e in store.events(jid))
