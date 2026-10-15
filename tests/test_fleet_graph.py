"""Phase 26 (FLEET-04): explicit task manifests, one-lane fallback, DAG and ownership admission."""

from __future__ import annotations

import pytest

from steward import fleet_graph as fg
from steward.fleet_store import FleetStore
from steward.store import Store

BLOB = "a" * 40


def task(tid="t1", **over):
    t = {
        "id": tid,
        "objective": f"do {tid}",
        "done_when": ["tests pass"],
        "depends_on": [],
        "owns": [f"src/{tid}/"],
        "evidence": ["pytest"],
        "required": True,
        "priority": 1,
    }
    t.update(over)
    return t


def manifest(*tasks, blob=BLOB):
    return {"schema_version": 1, "plan_blob_id": blob, "tasks": list(tasks or [task()])}


def compile_(m, blob=BLOB, **kw):
    return fg.compile_graph(m, committed_plan_blob_id=blob, goal="the goal", **kw)


def test_explicit_manifest_round_trips_through_store(steward_home):
    g = compile_(manifest(task("t1"), task("t2", depends_on=["t1"])))
    assert not g.implicit and g.errors == [] and [t.id for t in g.tasks] == ["t1", "t2"]
    fs = FleetStore(Store(steward_home))
    fs.store.create_job("g", "/repo", job_id="J1")
    fs.record_graph("J1", 1, g)
    view = fs.graph_view("J1")
    assert view["mode"] == "fleet" and view["graph_rev"] == 1 and view["graph_hash"] == g.graph_hash
    assert [t["id"] for t in view["tasks"]] == ["t1", "t2"]
    assert view["tasks"][1]["depends_on"] == ["t1"] and view["tasks"][0]["owns"] == ["src/t1"]


def test_legacy_job_projects_one_unknown_lane(steward_home):
    fs = FleetStore(Store(steward_home))
    fs.store.create_job("legacy goal", "/repo", job_id="J0")
    v = fs.graph_view("J0")
    assert v["mode"] == "legacy" and len(v["tasks"]) == 1
    t = v["tasks"][0]
    assert t["lane"] == "legacy" and t["model"] == "unknown" and t["cost"] == "unknown"
    assert "legacy" in v["reason"]


@pytest.mark.parametrize(
    "m,needle",
    [
        (None, "absent"),
        ("just prose", "malformed"),
        ({"schema_version": 1, "plan_blob_id": BLOB}, "malformed"),
        ({**manifest(), "schema_version": 9}, "malformed"),
        (manifest(task(objective="")), "malformed"),
        (manifest(task(owns=["/abs/path"])), "malformed"),
        (manifest(task(owns=["../escape"])), "malformed"),
        (manifest(blob="b" * 40), "stale"),
    ],
)
def test_bad_or_stale_metadata_yields_one_implicit_lane(m, needle):
    g = compile_(m)
    assert g.implicit and len(g.tasks) == 1 and g.tasks[0].owns == ()
    assert needle in g.fallback_reason
    assert [t.id for t in g.ready_tasks()] == [g.tasks[0].id]


def test_identity_is_deterministic_and_order_independent():
    a = compile_(manifest(task("t1"), task("t2")))
    b = compile_(manifest(task("t2"), task("t1")))
    assert a.graph_hash == b.graph_hash and [t.id for t in a.tasks] == [t.id for t in b.tasks]
    assert compile_(manifest(task("t1", objective="changed"), task("t2"))).graph_hash != a.graph_hash


# -- task 3: DAG, ownership and readiness ----------------------------------------

def ready_ids(g, **kw):
    return [t.id for t in g.ready_tasks(**kw)]


def test_duplicate_ids_unknown_deps_and_cycles_are_never_ready():
    cases = {
        "duplicate": manifest(task("a"), task("a", owns=["x"])),
        "unknown dependency": manifest(task("a", depends_on=["ghost"])),
        "cycle": manifest(task("a", depends_on=["b"]), task("b", depends_on=["a"])),
        "self": manifest(task("a", depends_on=["a"])),
    }
    for needle, m in cases.items():
        g = compile_(m)
        assert not g.implicit and g.errors and any(needle.split()[0] in e for e in g.errors), needle
        assert g.ready_tasks() == []


def test_dependencies_gate_readiness():
    g = compile_(manifest(task("a"), task("b", depends_on=["a"], owns=["src/b"])))
    assert ready_ids(g) == ["a"]
    assert ready_ids(g, done={"a"}) == ["b"]
    assert ready_ids(g, done={"a"}, active={"b"}) == []


def test_overlapping_and_empty_claims_serialize():
    g = compile_(manifest(task("a", owns=["src/core"]), task("b", owns=["src/core/util.py"]),
                          task("c", owns=["docs"]), task("d", owns=[])))
    # a (first by priority/id) wins; b overlaps a, d has empty ownership: only a and c run together
    assert ready_ids(g) == ["a", "c"]
    assert ready_ids(g, active={"a"}) == ["c"]
    assert ready_ids(g, done={"a", "c"}) == ["b"]  # b before d; d is empty-owns so it waits for b


def test_global_generated_and_cross_cutting_claims_serialize():
    for claim in (".", "**", "migrations/", "pkg/generated/x.py", "pyproject.toml", "src/*"):
        g = compile_(manifest(task("a", owns=[claim]), task("b", owns=["docs"], priority=2)))
        if claim == "src/*":
            assert ready_ids(g) == ["a", "b"]  # a bounded glob is only its literal prefix
        else:
            assert ready_ids(g) == ["a"], claim
    g = compile_(manifest(task("a", owns=["src/*"]), task("b", owns=["src/app"], priority=2)))
    assert ready_ids(g) == ["a"]


def test_dropping_or_reclassifying_required_work_is_refused():
    prior = compile_(manifest(task("a"), task("b")))
    dropped = compile_(manifest(task("a")), prior=prior)
    assert any("required task b" in e for e in dropped.errors) and dropped.ready_tasks() == []
    reclass = compile_(manifest(task("a"), task("b", required=False)), prior=prior)
    assert any("required task b" in e for e in reclass.errors)
    kept = compile_(manifest(task("a"), task("b"), task("c", required=False)), prior=prior)
    assert kept.errors == []


def test_committed_blob_id_uses_injected_runner():
    class R:
        returncode = 0
        stdout = BLOB + "\n"

    calls = []
    assert fg.committed_blob_id("/r", "p/PLAN.md", runner=lambda argv: calls.append(argv) or R()) == BLOB
    assert calls == [["git", "-C", "/r", "rev-parse", "HEAD:p/PLAN.md"]]
    R.returncode = 1
    assert fg.committed_blob_id("/r", "p", runner=lambda argv: R()) is None
