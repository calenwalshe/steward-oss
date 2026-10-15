"""18-07 G21 acceptance gate: a deterministic replay of the three finished GSD builds through the real watcher and ETA.

The replay drives the real EfficiencyWatcher.step (with its ETA), tokens, efficiency.compute, efficiency_detect,
eta_structure over a real tmp git worktree that receives the job's planning commits at their recorded times, eta_prior's
shipped seed, eta.estimate and eta.changes (see tests/eta_replay.py). Thresholds come from 18-CONTEXT G21 and G8 verbatim.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import eta_replay as rp  # noqa: E402
from steward import efficiency_watch, eta  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_CACHE: dict = {}  # job -> [EtaReplay, ...], computed lazily inside the first test that needs it (conftest isolation active)


def runs(job: str, n: int, factory) -> list:
    """At least `n` independent replays of `job`, each in its own tmp dir."""
    lst = _CACHE.setdefault(job, [])
    while len(lst) < n:
        lst.append(rp.run_eta_replay(job, factory.mktemp(f"eta-{job}-{len(lst)}")))
    return lst


def one(job: str, factory) -> "rp.EtaReplay":
    return runs(job, 1, factory)[0]


def all_jobs(factory) -> list:
    return [one(j, factory) for j in rp.JOBS]


def test_gate_a7d292d8_units_7_of_7(tmp_path_factory):
    rep = one("a7d292d8", tmp_path_factory)
    last = rep.rows[-1]
    assert (last["units_done"], last["units_total"], last["units_source"]) == (7, 7, "planning")
    after35 = next(r for r in rep.rows if r["at"] >= rep.t0 + 35 * 60)
    assert after35["units_total"] == 7  # known without a hint, at 35 minutes
    assert after35["units_source"] == "planning"
    assert "units: 7/7 complete" in rep.report
    seen = []
    for r in rep.rows:
        if not seen or seen[-1] != r["source"]:
            seen.append(r["source"])
    assert seen == ["prior", "class-rate", "units", "units+trend"]
    print("a7d292d8 sources in order:", seen, "| units at 35 min:", after35["units_done"], "/", after35["units_total"])


def test_gate_harness_fakes_only_the_clock(tmp_path_factory):
    for job in rp.JOBS:
        rep = one(job, tmp_path_factory)
        lo, hi = rep.t0, rep.t0 + rep.truth_min * 60 + 5 * 60
        assert rep.rows
        assert all(lo <= r["at"] <= hi for r in rep.rows), job
        assert rep.event_ts and all(0 <= s <= hi - lo for _w, s in rep.event_ts), job  # store.now_iso is the fake clock
        commits = rep.timeline["commits"]
        count, _, ct = rep.head.partition(":")
        assert int(count) == len(commits) + 1  # the base commit plus every planning commit, replayed at its time
        assert int(ct) == int(commits[-1]["at"])
        assert rep.argv0 <= {"git"}


def test_phase16_gate_unaffected():
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests/test_efficiency_gate.py"],
                         cwd=str(ROOT), capture_output=True, text=True)
    assert res.returncode == 0, res.stdout[-1500:] + res.stderr[-500:]
    assert "passed" in res.stdout


# -- Task 2: the G21 numbers ----------------------------------------------------------------------------------------


def _header(row) -> str:
    return eta.render(row, row["at"])["header"]


def _first_units(rows: list) -> int:
    return next(i for i, r in enumerate(rows) if r["source"] in ("units", "units+trend"))


def test_gate_p50_error_by_slice(tmp_path_factory):
    reps = all_jobs(tmp_path_factory)
    means = rp.slice_means(reps)
    for f in rp.SLICES:
        parts = []
        for rep in reps:
            e, row = rp.p50_error(rep, f)
            parts.append(f"{rep.job} {'none' if e is None else f'{e:+.1f}%'} ({'-' if row is None else row['source']})")
        print(f"{int(f * 100)}% mean |P50 error| {means[f]:.1f}% (limit {rp.SLICE_LIMITS[f]:.0f}): " + ", ".join(parts))
    for f in rp.SLICES:
        assert means[f] <= rp.SLICE_LIMITS[f], (f, means)


def test_gate_band_coverage(tmp_path_factory):
    reps = all_jobs(tmp_path_factory)
    rows, means = rp.table(reps)
    assert means["steps"] == 27
    misses = [f"{r['job']} {r['step']}% ({r['source']}, band {r['p10']}-{r['p90']} left, err {r['error_pct']})"
              for r in rows if not r["in_band"]]
    print(f"truth inside the P10-P90 band at {means['in_band']} of 27 steps; misses: {misses}")
    assert means["in_band"] >= rp.BAND_MIN_HITS


def test_gate_no_p50_before_units(tmp_path_factory):
    for rep in all_jobs(tmp_path_factory):
        i = _first_units(rep.rows)
        for r in rep.rows[:i]:
            assert r["p50_min"] is None, (rep.job, r["elapsed_min"], r["source"])
            assert not re.search(r"~\d", _header(r)), (rep.job, _header(r))
        for r in rep.rows[i:]:
            assert isinstance(r["p50_min"], (int, float)), (rep.job, r["elapsed_min"], r["source"])
        print(f"{rep.job}: first P50 at {rep.rows[i]['elapsed_min']:.1f} min, source {rep.rows[i]['source']}, "
              f"{i} range-only rows before it")


def test_gate_false_done_and_send_back(tmp_path_factory):
    rep = one("ed11ba3c", tmp_path_factory)
    tl = rep.timeline
    done1, back = tl["done"][0], tl["sent_back"][0]
    assert (done1 - rep.t0) / 60 == pytest.approx(3.1, abs=0.1) and (back - rep.t0) / 60 == pytest.approx(5.3, abs=0.1)
    between = [r for r in rep.rows if done1 <= r["at"] < back]
    assert between, "no ETA was stored while the false done stood"
    for r in between:
        assert r["done_claimed"] is True and _header(r) == "estimating…", (r["at"], _header(r))
        assert r["units_done"] == 0 and r["finishing"] is False
    attempt2 = tl["attempts"][1]["at"]
    later = [r for r in rep.rows if r["at"] >= back + 60]
    assert later and all(r["attempt_no"] == 2 and r["done_claimed"] is False for r in later)
    assert all(r["attempt_no"] == 1 for r in rep.rows if r["at"] < attempt2)
    for job in rp.JOBS:
        rows = one(job, tmp_path_factory).rows
        assert all(r["source"] not in ("prior", "none") for r in rows[_first_units(rows):]), job  # never back to the prior
        assert all(not r["finishing"] or r["units_done"] >= (r["units_total"] or 0) - 1 for r in rows), job
    print(f"false done at 3.1 min: {len(between)} rows showed `estimating…`; attempt 2 from 5.3 min")


def test_gate_finishing_near_the_end_only(tmp_path_factory):
    for rep in all_jobs(tmp_path_factory):
        summaries = sorted(c["at"] for c in rep.timeline["commits"]
                           if any(re.search(r"-\d+-SUMMARY\.md$", p) for p in c["add"]))
        total = rep.rows[-1]["units_total"]
        assert len(summaries) >= total - 1, (rep.job, len(summaries), total)
        boundary = summaries[total - 2]  # the last-but-one unit boundary
        fin = [r for r in rep.rows if r["finishing"]]
        assert all(r["at"] >= boundary for r in fin), rep.job
        print(f"{rep.job}: {len(fin)} finishing rows, all after the unit {total - 1} boundary "
              f"({(boundary - rep.t0) / 60:.1f} min)")


def test_gate_events_are_evidence_driven(tmp_path_factory):
    for rep in all_jobs(tmp_path_factory):
        changed = [(ev, ts) for (w, ev), (_w, ts) in zip(rep.events, rep.event_ts) if w == "eta_changed"]
        assert 0 < len(changed) < len(rep.rows), rep.job
        by_at = {round(r["at"] - rep.t0, 3): i for i, r in enumerate(rep.rows)}
        for ev, ts in changed:
            if ev["from"] != ev["to"]:
                continue  # a source change
            i = next(j for k, j in by_at.items() if abs(k - ts) < 0.01)
            prev, cur = rep.rows[i - 1]["shown"], rep.rows[i]["shown"]
            at = rep.rows[i]["at"]
            old, new = (prev["p50_end"] - at) / 60, (cur["p50_end"] - at) / 60
            assert abs(new - old) / max(old, 1.0) > 0.30, (rep.job, ev, old, new)
        assert not [n for n in rep.notifications if n[1] == "BUDGET" and "overdue" in " ".join(map(str, n)).lower()]
        assert not [w for w, _ in rep.events if w == "eta_notify_set"]
        print(f"{rep.job}: {len(changed)} eta_changed for {len(rep.rows)} stored ETAs; notifications "
              f"{[n[1] for n in rep.notifications]}")


def test_gate_three_runs_identical(tmp_path_factory):
    for job in rp.JOBS:
        three = runs(job, 3, tmp_path_factory)[:3]
        digests = [r.digest() for r in three]
        assert digests[0] == digests[1] == digests[2], job
        assert len(digests[0]) > 1000
        print(f"{job}: 3 identical digests, length {len(digests[0])}, sha256 {rp.sha_prefix(digests[0])}, "
              f"runtimes {[round(r.wall_s, 1) for r in three]} s")


def test_gate_only_git_subprocesses(tmp_path_factory):
    for rep in all_jobs(tmp_path_factory):
        assert rep.res.subprocess_calls, "the replay launches git (worktree, structure)"
        assert rep.argv0 <= {"git"}, (rep.job, rep.argv0)  # the guard raises on herdr, claude, codex, tg


def test_gate_runtime_budget(tmp_path_factory):
    for rep in all_jobs(tmp_path_factory):
        print(f"{rep.job}: {len(rep.rows)} ETAs in {rep.wall_s:.1f}s")
        assert rep.wall_s < 60
