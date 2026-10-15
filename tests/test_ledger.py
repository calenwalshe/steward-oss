"""Tests for steward/ledger.py (TRJ-02, TRJ-03).

The ledger is kv-backed, capped, and structurally cannot hold transcripts:
every fact API takes only short summary fields, capped at 200 chars and
redacted before persistence.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field

import pytest

from steward.ledger import TrajectoryLedger
from steward.models import Judgment
from steward.store import Store

_FORBIDDEN_PARAM_NAMES = {"output_tail", "transcript", "tool_output", "assistant_text", "pane_text"}


@dataclass(frozen=True)
class _FakeDiffSample:
    changed_lines: int = 0
    files: list = field(default_factory=list)
    deleted_or_renamed: int = 0
    new_top_level_dirs: list = field(default_factory=list)
    manifests_changed: list = field(default_factory=list)
    summary_commits: list = field(default_factory=list)
    public_api_changes: list = field(default_factory=list)
    interface_files: list = field(default_factory=list)
    weakening: list = field(default_factory=list)


def _ledger(store, job_id, cap=200):
    return TrajectoryLedger(store, job_id, cap=cap)


# -- round trip --------------------------------------------------------


def test_round_trip_across_a_new_store_instance(steward_home):
    store_a = Store(steward_home)
    job = store_a.create_job("goal", "/repo")
    ledger_a = _ledger(store_a, job.job_id)
    ledger_a.set_intent("build the thing", "tests pass", now=100.0)
    ledger_a.add_plan(["step one", "step two"], now=101.0)

    store_b = Store(steward_home)
    ledger_b = _ledger(store_b, job.job_id)
    snap = ledger_b.snapshot()

    assert snap["intent"]["goal"] == "build the thing"
    assert snap["lists"]["plan"][0]["items"] == ["step one", "step two"]


# -- set_intent: first call wins, later calls become revisions ----------


def test_set_intent_first_call_wins(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)

    ledger.set_intent("goal A", "done A", now=1.0)
    ledger.set_intent("goal B", "done B", now=2.0)

    snap = ledger.snapshot()
    assert snap["intent"]["goal"] == "goal A"
    revisions = snap["lists"]["acceptance_revisions"]
    assert len(revisions) == 1
    assert "goal B" in revisions[0]["text"]
    assert revisions[0]["source"] == "intent_update"


def test_add_acceptance_revision_appends_directly(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)
    ledger.set_intent("g", "d", now=1.0)

    ledger.add_acceptance_revision("scope narrowed", "goal_steer", now=2.0)

    revisions = ledger.snapshot()["lists"]["acceptance_revisions"]
    assert revisions[-1]["text"] == "scope narrowed"
    assert revisions[-1]["source"] == "goal_steer"


# -- milestone / cost-point validation ------------------------------------


def test_add_milestone_rejects_unknown_kind(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)
    with pytest.raises(ValueError):
        ledger.add_milestone("not_a_real_kind", ref="x", now=1.0)


def test_add_milestone_accepts_each_known_kind(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)
    for kind in ("done", "closeout_round", "summary_commit", "contract_revision"):
        ledger.add_milestone(kind, ref="r", now=1.0)
    kinds = [m["kind"] for m in ledger.snapshot()["lists"]["milestones"]]
    assert kinds == ["done", "closeout_round", "summary_commit", "contract_revision"]


def test_add_cost_point_rejects_unknown_source(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)
    with pytest.raises(ValueError):
        ledger.add_cost_point(1.0, attempt_no=1, source="bogus", now=1.0)


# -- diff sample -----------------------------------------------------------


def test_add_diff_sample_persists_all_fields(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)
    sample = _FakeDiffSample(
        changed_lines=42,
        files=["a.py", "b.py"],
        deleted_or_renamed=1,
        new_top_level_dirs=["newdir"],
        manifests_changed=["pyproject.toml"],
        summary_commits=["12-01-SUMMARY.md"],
        public_api_changes=["a.py:foo"],
        interface_files=["schema.proto"],
        weakening=["deleted_test_file:tests/test_x.py"],
    )
    ledger.add_diff_sample(sample, now=5.0)

    stored = ledger.snapshot()["lists"]["diff_samples"][0]
    assert stored["changed_lines"] == 42
    assert stored["files"] == ["a.py", "b.py"]
    assert stored["manifests_changed"] == ["pyproject.toml"]
    assert stored["weakening"] == ["deleted_test_file:tests/test_x.py"]
    assert stored["ts"] == 5.0


# -- cap + drop counter ------------------------------------------------


def test_cap_thins_oldest_half_and_increments_drop_counter(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id, cap=4)

    for i in range(10):
        ledger.add_test_point(passed=i, failed=0, errors=0, source="worker_observed", now=float(i))

    snap = ledger.snapshot()
    assert len(snap["lists"]["test_points"]) <= 6  # thinning keeps it well under a runaway 10
    assert snap["dropped"]["test_points"] > 0


def test_cap_never_drops_the_most_recent_entries(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id, cap=4)

    for i in range(10):
        ledger.add_test_point(passed=i, failed=0, errors=0, source="worker_observed", now=float(i))

    passed_values = [p["passed"] for p in ledger.snapshot()["lists"]["test_points"]]
    assert passed_values[-1] == 9  # the newest fact is never thinned away


# -- cumulative vs per-turn cost points ------------------------------------


def test_worker_spend_treats_non_decreasing_attempt_points_as_cumulative(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)

    for cost in (0.1, 0.3, 0.3, 0.5):
        ledger.add_cost_point(cost, attempt_no=1, source="worker", now=1.0)

    assert ledger.worker_spend_usd() == pytest.approx(0.5)


def test_worker_spend_sums_per_turn_points_when_they_decrease(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)

    for cost in (0.2, 0.1, 0.3):  # a decrease means these are per-turn deltas
        ledger.add_cost_point(cost, attempt_no=1, source="worker", now=1.0)

    assert ledger.worker_spend_usd() == pytest.approx(0.6)


def test_worker_spend_sums_across_attempts(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)

    for cost in (0.1, 0.3, 0.3, 0.5):
        ledger.add_cost_point(cost, attempt_no=1, source="worker", now=1.0)
    for cost in (0.2, 0.1, 0.3):
        ledger.add_cost_point(cost, attempt_no=2, source="worker", now=1.0)

    assert ledger.worker_spend_usd() == pytest.approx(0.5 + 0.6)


def test_supervision_spend_is_a_plain_sum_and_excludes_worker(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)

    ledger.add_cost_point(0.5, attempt_no=1, source="worker", now=1.0)
    ledger.add_cost_point(0.05, attempt_no=1, source="supervision", now=1.0)
    ledger.add_cost_point(0.02, attempt_no=1, source="supervision", now=2.0)

    assert ledger.supervision_spend_usd() == pytest.approx(0.07)


# -- elapsed / spend rate ------------------------------------------------


def test_elapsed_s_from_first_fact(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)
    ledger.set_intent("g", "d", now=1000.0)

    assert ledger.elapsed_s(now=1100.0) == pytest.approx(100.0)


def test_elapsed_s_zero_with_no_facts_yet(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)
    assert ledger.elapsed_s(now=500.0) == 0.0


def test_spend_rate_only_counts_the_trailing_window(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)
    ledger.add_cost_point(1.0, attempt_no=1, source="worker", now=-100.0)  # outside window
    ledger.add_cost_point(2.0, attempt_no=1, source="worker", now=1500.0)  # inside window

    rate = ledger.spend_rate(window_s=1800, now=1800.0)
    assert rate == pytest.approx(2.0 / 1800)


# -- since() ---------------------------------------------------------------


def test_since_returns_only_entries_after_the_marker(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)
    ledger.add_failed_approach("guide", ref="r1", now=100.0)
    ledger.add_failed_approach("replace", ref="r2", now=200.0)

    deltas = ledger.since(review_marker=150.0)
    assert [e["ref"] for e in deltas["failed_approaches"]] == ["r2"]


# -- sentinel: no transcript can ever reach the ledger ----------------------


def test_no_public_fact_api_accepts_an_output_tail_style_parameter(steward_home):
    for name, method in inspect.getmembers(TrajectoryLedger, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        params = set(inspect.signature(method).parameters)
        assert not (params & _FORBIDDEN_PARAM_NAMES), f"{name} accepts a transcript-shaped param: {params}"


def test_sentinel_never_survives_into_persisted_kv_json(steward_home):
    """A long sentinel string, standing in for a raw pane tail (Signals.
    output_tail), is fed through every public fact API's text-bearing
    field. None of them can hold a transcript: the 200-char cap alone
    guarantees a string this long never appears intact in the stored JSON."""
    sentinel = "TRANSCRIPT-LEAK-SENTINEL-" + ("Z" * 300)
    assert len(sentinel) > 200

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)

    ledger.set_intent(sentinel, sentinel, now=1.0)
    ledger.add_acceptance_revision(sentinel, sentinel, now=2.0)
    ledger.add_plan([sentinel], now=3.0)
    ledger.add_decision(sentinel, sentinel, sentinel, Judgment.code, sentinel, now=4.0)
    ledger.add_discarded_approach(sentinel, sentinel, now=5.0)
    ledger.add_milestone("done", sentinel, now=6.0)
    ledger.add_diff_sample(
        _FakeDiffSample(files=[sentinel], manifests_changed=[sentinel], weakening=[sentinel]), now=7.0
    )
    ledger.add_test_point(1, 0, 0, sentinel, now=8.0)
    ledger.add_cost_point(0.1, attempt_no=1, source="worker", now=9.0)
    ledger.add_failed_approach(sentinel, sentinel, now=10.0)
    ledger.add_review(sentinel, [sentinel], now=11.0)

    raw = store.kv_get(job.job_id, "trajectory_ledger")
    blob = json.dumps(raw)
    assert sentinel not in blob


# -- size bound: a 4h synthetic job stays under 256 KB ----------------------


def test_four_hour_synthetic_job_stays_under_256kb(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    ledger = _ledger(store, job.job_id)

    now = 0.0
    ledger.set_intent("build a thing", "tests pass", now=now)
    tick_s = 30.0
    ticks = int(4 * 3600 / tick_s)  # one heartbeat tick every 30s for 4h
    for i in range(ticks):
        now += tick_s
        ledger.add_diff_sample(
            _FakeDiffSample(changed_lines=i, files=[f"file_{i % 20}.py"], manifests_changed=[]), now=now
        )
        if i % 10 == 0:
            ledger.add_test_point(passed=i, failed=0, errors=0, source="worker_observed", now=now)
        if i % 5 == 0:
            ledger.add_cost_point(0.01 * i, attempt_no=1, source="worker", now=now)

    raw = store.kv_get(job.job_id, "trajectory_ledger")
    size = len(json.dumps(raw).encode("utf-8"))
    assert size < 256 * 1024, f"ledger grew to {size} bytes for a 4h synthetic job"
