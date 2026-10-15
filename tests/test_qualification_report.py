"""Plan 4 Task 1: the final evidence report never infers a pass (spec §11; handoff step 5)."""
from __future__ import annotations

import pytest

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("qualification_report", ROOT / "scripts" / "qualification_report.py")
qr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qr)

JUNIT = """<?xml version="1.0"?><testsuites><testsuite name="pytest">
<testcase classname="tests.test_a" name="test_ok"/>
<testcase classname="tests.test_a" name="test_red"><failure message="boom"/></testcase>
<testcase classname="tests.test_a" name="test_skip"><skipped message="no npm"/></testcase>
<testcase classname="tests.test_b.TestC" name="test_m"/>
</testsuite></testsuites>"""


def _receipt(**kw):
    base = {"state": "completed", "candidate_commit": "c" * 40, "tree": "t" * 40, "exit_code": 0,
            "summary": "3 passed", "duration_s": 2400.0, "started_at": 1.0, "finished_at": 2401.0, "host": "h",
            "source_branch": "fleet-registry", "source_head": "s" * 40, "contains_source": True,
            "attribution": {"new_failures": [], "preexisting_failures": [], "unattributed_failures": [],
                            "complete": True, "baseline_status": "not_needed_no_failures",
                            "fixed": [], "baseline_commit": "b" * 40},
            "limitations": []}
    base.update(kw)
    return base


def _write(tmp_path, junit=JUNIT):
    p = tmp_path / "junit.xml"
    p.write_text(junit)
    return qr.junit_outcomes(p)


def test_node_keys_match_junit_classnames(tmp_path):
    out = _write(tmp_path)
    assert out[qr.node_key("tests/test_a.py::test_ok")] == "passed"
    assert out[qr.node_key("tests/test_a.py::test_red")] == "failed"
    assert out[qr.node_key("tests/test_b.py::TestC::test_m")] == "passed"


def test_missing_skipped_and_failed_tests_are_not_passes(tmp_path):
    out = _write(tmp_path)
    assert qr.scenario_status(["tests/test_a.py::test_ok"], out, True) == "passed"
    assert qr.scenario_status(["tests/test_a.py::test_ok", "tests/test_a.py::test_gone"], out, True) == "missing"
    assert qr.scenario_status(["tests/test_a.py::test_ok", "tests/test_a.py::test_skip"], out, True) == "skipped"
    assert qr.scenario_status(["tests/test_a.py::test_red", "tests/test_a.py::test_gone"], out, True) == "failed"
    assert qr.scenario_status([], out, True) == "unmapped"


def test_timeout_or_running_receipt_is_never_passing_evidence(tmp_path):
    out = _write(tmp_path)
    for state in ("timeout", "running", "lost"):
        ok, why = qr.verdict(_receipt(state=state), {"s1": "passed"})
        assert not ok and any(state in w for w in why)
        assert qr.scenario_status(["tests/test_a.py::test_ok"], out, qr.run_ok(_receipt(state=state))) == "not run"


def test_new_failures_or_stale_source_block_qualification():
    ok, why = qr.verdict(_receipt(attribution={"new_failures": ["tests/test_x.py::test_y"], "preexisting_failures": [],
                                               "fixed": [], "baseline_commit": "b"}), {"s1": "passed"})
    assert not ok and any("new failure" in w for w in why)
    ok, why = qr.verdict(_receipt(contains_source=False), {"s1": "passed"})
    assert not ok and any("does not contain" in w for w in why)
    ok, why = qr.verdict(_receipt(attribution={"new_failures": [], "preexisting_failures": [], "fixed": [],
                                               "unattributed_failures": ["test_x"], "complete": False,
                                               "baseline_commit": "b"}), {"s1": "passed"})
    assert not ok and any("lack baseline attribution" in w for w in why)


def test_preexisting_failures_are_attributed_not_hidden():
    rec = _receipt(exit_code=1, attribution={"new_failures": [], "preexisting_failures": ["tests/test_q.py::test_env"],
                                             "unattributed_failures": [], "complete": True,
                                             "baseline_status": "compared", "fixed": [], "baseline_commit": "b"})
    ok, why = qr.verdict(rec, {"s1": "passed"})
    assert ok and why == []
    text = qr.render({"scenarios": [{"id": "s1", "section": "11", "title": "t", "plans": [4], "tests": []}],
                      "limitations": []}, rec, {}, [])
    assert "tests/test_q.py::test_env" in text and "pre-existing" in text


def test_completed_receipt_with_red_exit_or_incomplete_attribution_never_passes():
    for rec in (_receipt(exit_code=1), _receipt(attribution={"new_failures": [], "complete": None}),
                _receipt(attribution={"new_failures": [], "unattributed_failures": ["x"], "complete": False})):
        assert not qr.run_ok(rec)
        ok, _ = qr.verdict(rec, {"s1": "passed"})
        assert not ok


def test_every_unproven_scenario_is_listed_as_a_gap():
    reg = {"scenarios": [{"id": "s1", "section": "11", "title": "one", "plans": [1], "tests": []},
                         {"id": "s2", "section": "11", "title": "two", "plans": [4], "tests": ["tests/test_a.py::test_red"]}],
           "limitations": ["Plan 2 interfaces reconciled late"]}
    text = qr.render(reg, _receipt(), {qr.node_key("tests/test_a.py::test_red"): "failed"}, [])
    assert "NOT QUALIFIED" in text
    gaps = text.split("## Gaps and limitations", 1)[1]
    assert "s1" in gaps and "unmapped" in gaps and "s2" in gaps and "failed" in gaps and "reconciled late" in gaps


def test_completed_run_with_every_scenario_passed_is_qualified_for_owner_review(tmp_path):
    out = _write(tmp_path)
    reg = {"scenarios": [{"id": "s1", "section": "11", "title": "one", "plans": [4], "tests": ["tests/test_a.py::test_ok"]}],
           "limitations": []}
    text = qr.render(reg, _receipt(), out, [])
    assert "QUALIFIED for owner review" in text and "NOT QUALIFIED" not in text
    assert "c" * 40 in text and "Incorporation and activation" in text


def test_progress_rows_are_read_from_every_plan_record(tmp_path):
    (tmp_path / "2026-10-05-plan-1-progress.md").write_text(
        "# x\n\n| Task | Commit | Scoped command | Result |\n|---|---|---|---|\n| 1 | abc1234 | pytest a | 3 passed |\n")
    (tmp_path / "2026-10-05-plan-4-progress.md").write_text(
        "| Task | Commit | Scoped command | Result |\n|---|---|---|---|\n| 0 | def5678 | pytest b | 9 passed |\n")
    rows = qr.progress_rows(tmp_path)
    assert [(r["plan"], r["task"], r["commit"]) for r in rows] == [(1, "1", "abc1234"), (4, "0", "def5678")]


def test_committed_registry_covers_every_required_case_with_an_owner():
    if not qr.REGISTRY.exists():
        pytest.skip("the qualification registry is internal evidence, not part of every checkout")
    reg = qr.load_registry(qr.REGISTRY)
    ids = {s["id"] for s in reg["scenarios"]}
    assert set(qr.REQUIRED_IDS) <= ids
    for s in reg["scenarios"]:
        assert s["plans"] and set(s["plans"]) <= {1, 2, 3, 4}, s["id"]
        assert all("::" in t and t.startswith("tests/") for t in s["tests"]), s["id"]


def test_cli_writes_the_report_and_check_mode_fails_when_not_qualified(tmp_path):
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"scenarios": [{"id": "s1", "section": "11", "title": "one", "plans": [4], "tests": []}],
                               "limitations": []}))
    rec = tmp_path / "receipt.json"
    rec.write_text(json.dumps(_receipt(state="timeout")))
    out = tmp_path / "report.md"
    rc = qr.main(["--registry", str(reg), "--receipt", str(rec), "--junit", str(tmp_path / "none.xml"),
                  "--evidence-dir", str(tmp_path), "--out", str(out), "--check"])
    assert rc == 1 and "NOT QUALIFIED" in out.read_text()
