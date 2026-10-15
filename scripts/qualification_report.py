#!/usr/bin/env python3
"""Final-qualification report generator (continuous job ownership; Plan 4 Tasks 1 and 16).

Reads the scenario registry (`docs/superpowers/evidence/scenarios.json`), every per-plan progress record and the
durable receipt + junit of the single background full-suite run (`scripts/final_candidate_check.py`), and writes
the evidence report: each spec §11 scenario and §13/§14/§15 qualification case with its owning plan, the tests that
prove it, their outcome in the final run, and the exact candidate commit. It never infers a pass: a test absent from
the final junit is "missing", a skipped one is "skipped", a run that timed out, is still running or was lost is
"not run", and a case with no mapped test is "unmapped". Pre-existing failures are attributed, never hidden.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "superpowers" / "evidence"
REGISTRY = EVIDENCE / "scenarios.json"
RECEIPT = EVIDENCE / "final-run" / "receipt.json"
JUNIT = EVIDENCE / "final-run" / "junit.xml"
REPORT = EVIDENCE / "2026-10-05-final-qualification.md"
COMPLETED = "completed"
REQUIRED_IDS = tuple([f"s{i}" for i in range(1, 20)] + [f"j{i}" for i in range(1, 17)] + [f"f{i}" for i in range(1, 11)]
                     + [f"l{i}" for i in range(1, 6)] + ["m1"])
_PROGRESS_RE = re.compile(r"^2026-\d\d-\d\d-plan-(\d)-progress\.md$")


def node_key(nodeid: str) -> str:
    """`tests/test_x.py::TestC::test_y[p]` -> `tests.test_x::TestC::test_y[p]` (junit classname form)."""
    path, _, rest = nodeid.partition("::")
    mod = path[:-3] if path.endswith(".py") else path
    return mod.replace("/", ".") + "::" + rest


def junit_outcomes(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not Path(path).is_file():
        return out
    for tc in ET.parse(path).getroot().iter("testcase"):
        cls, name = tc.get("classname") or "", tc.get("name") or ""
        outcome = "passed"
        for child in tc:
            if child.tag in ("failure", "error"):
                outcome = "failed"
                break
            if child.tag == "skipped":
                outcome = "skipped"
        out[f"{cls}::{name}"] = outcome
        mod, _, last = cls.rpartition(".")
        if mod and last[:1].isupper():  # a test class: index it as module::Class::test too
            out[f"{mod}::{last}::{name}"] = outcome
    return out


def load_registry(path: Path = REGISTRY) -> dict:
    return json.loads(Path(path).read_text())


def load_receipt(path: Path = RECEIPT) -> dict | None:
    p = Path(path)
    return json.loads(p.read_text()) if p.is_file() else None


def run_ok(receipt: dict | None) -> bool:
    if not receipt or receipt.get("state") != COMPLETED:
        return False
    att = receipt.get("attribution") or {}
    return (att.get("complete") is True
            and (receipt.get("exit_code") == 0 or bool(att.get("preexisting_failures")))
            and not att.get("new_failures")
            and not att.get("unattributed_failures"))


def progress_rows(evidence_dir: Path = EVIDENCE) -> list[dict]:
    rows: list[dict] = []
    for p in sorted(Path(evidence_dir).glob("*-progress.md")):
        m = _PROGRESS_RE.match(p.name)
        if not m:
            continue
        for line in p.read_text().splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not line.startswith("|") or len(cells) < 4 or cells[0] in ("Task", "") or set(cells[0]) <= {"-"}:
                continue
            rows.append({"plan": int(m.group(1)), "task": cells[0], "commit": cells[1], "command": cells[2],
                         "result": cells[3]})
    return rows


def scenario_status(tests: list[str], outcomes: dict[str, str], run_completed: bool) -> str:
    if not tests:
        return "unmapped"
    if not run_completed:
        return "not run"
    res = [outcomes.get(node_key(t), "missing") for t in tests]
    for bad in ("failed", "missing", "skipped"):
        if bad in res:
            return bad
    return "passed"


def verdict(receipt: dict | None, statuses: dict[str, str]) -> tuple[bool, list[str]]:
    why: list[str] = []
    if not receipt:
        why.append("no final-run receipt")
    elif receipt.get("state") != COMPLETED:
        why.append(f"final run {receipt.get('state')}: not passing evidence")
    else:
        att = receipt.get("attribution") or {}
        if receipt.get("exit_code") not in (0, 1):
            why.append(f"final run exited {receipt.get('exit_code')}: not passing evidence")
        elif receipt.get("exit_code") == 1 and not att.get("preexisting_failures"):
            why.append("nonzero final run has no baseline-attributed pre-existing failures")
        if att.get("new_failures"):
            why.append(f"{len(att['new_failures'])} new failure(s) against the baseline")
        if att.get("complete") is False:
            why.append(f"{len(att.get('unattributed_failures') or [])} test failure(s) lack baseline attribution")
        if att.get("complete") is not True:
            why.append("baseline attribution is incomplete")
        if not receipt.get("contains_source"):
            why.append(f"candidate does not contain {receipt.get('source_branch')} @ {str(receipt.get('source_head'))[:12]}")
    bad = sorted(k for k, v in statuses.items() if v != "passed")
    if bad:
        why.append(f"{len(bad)} scenario(s) not proven: {', '.join(bad)}")
    return (not why), why


def render(registry: dict, receipt: dict | None, outcomes: dict[str, str], rows: list[dict]) -> str:
    ok_run = run_ok(receipt)
    statuses = {s["id"]: scenario_status(s.get("tests") or [], outcomes, ok_run) for s in registry["scenarios"]}
    ok, why = verdict(receipt, statuses)
    r = receipt or {}
    att = r.get("attribution") or {}
    out = ["# Continuous job ownership — final qualification report", "",
           "Generated by `scripts/qualification_report.py` from `docs/superpowers/evidence/scenarios.json`, the "
           "per-plan progress records and the final-run receipt. Nothing here is inferred: see the status legend.", "",
           "## Verdict", "", ("QUALIFIED for owner review" if ok else "NOT QUALIFIED: " + "; ".join(why)), "",
           "## Candidate and final run", "",
           f"- Candidate commit: `{r.get('candidate_commit', '?')}` (tree `{r.get('tree', '?')}`)",
           f"- Final run: state `{r.get('state', 'absent')}`, exit {r.get('exit_code')}, {r.get('summary', '')}, "
           f"{r.get('duration_s')} s on `{r.get('host')}`",
           f"- Source branch: `{r.get('source_branch')}` @ `{r.get('source_head')}`; contained: {r.get('contains_source')}",
           f"- Baseline `{att.get('baseline_commit')}`: new failures {att.get('new_failures', [])}; "
           f"pre-existing failures (attributed to the baseline, not fixed here) {att.get('preexisting_failures', [])}; "
           f"fixed {att.get('fixed', [])}; baseline status {att.get('baseline_status', 'not recorded')}; "
           f"unattributed failures {att.get('unattributed_failures', [])}", "",
           "Status legend: passed = every mapped test passed in the final run; failed / missing / skipped = at least "
           "one mapped test did so; not run = the final run did not complete; unmapped = no test proves it.", "",
           "## Scenarios and qualification cases", "", "| Id | § | Case | Owner plan(s) | Tests | Result |",
           "|---|---|---|---|---|---|"]
    for s in registry["scenarios"]:
        tests = "<br>".join(f"`{t}`" for t in s.get("tests") or []) or "—"
        out.append(f"| {s['id']} | {s['section']} | {s['title']} | {', '.join(map(str, s['plans']))} | {tests} | "
                   f"{statuses[s['id']]} |")
    out += ["", "## Progress records", "", "| Plan | Task | Commit | Scoped command | Result |", "|---|---|---|---|---|"]
    out += [f"| {x['plan']} | {x['task']} | {x['commit']} | {x['command']} | {x['result']} |" for x in rows]
    out += ["", "## Gaps and limitations", ""]
    gaps = [f"- {k}: {v}" for k, v in statuses.items() if v != "passed"]
    gaps += [f"- {x}" for x in list(registry.get("limitations") or []) + list(r.get("limitations") or [])]
    out += gaps or ["- none recorded"]
    out += ["", "## Incorporation and activation", "",
            "Nothing was pushed, merged into the source branch, deployed or restarted by this job. Final acceptance, "
            "production incorporation and activation are separate owner decisions:", "",
            f"1. Accept: `steward accept <job>` (owner). 2. Incorporate: `steward incorporate <job>` merges "
            f"`{r.get('candidate_commit', '?')[:12]}` into `{r.get('source_branch')}` (serialized per target; the "
            "merged tree is verified post-merge when it differs from the tested tree). 3. Activate: `steward deploy-self` "
            "(or a restart), then `steward health`; activation is proven only when the running revision contains the "
            "merge commit and the required live checks are green.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--registry", default=str(REGISTRY))
    ap.add_argument("--receipt", default=str(RECEIPT))
    ap.add_argument("--junit", default=str(JUNIT))
    ap.add_argument("--evidence-dir", default=str(EVIDENCE))
    ap.add_argument("--out", default=str(REPORT))
    ap.add_argument("--check", action="store_true", help="exit 1 unless qualified for owner review")
    a = ap.parse_args(argv)
    reg, rec = load_registry(Path(a.registry)), load_receipt(Path(a.receipt))
    outcomes = junit_outcomes(Path(a.junit))
    text = render(reg, rec, outcomes, progress_rows(Path(a.evidence_dir)))
    Path(a.out).write_text(text + "\n")
    print(f"wrote {a.out}")
    return 1 if a.check and "NOT QUALIFIED" in text.split("## Candidate", 1)[0] else 0


if __name__ == "__main__":
    sys.exit(main())
