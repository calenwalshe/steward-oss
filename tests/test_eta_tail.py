"""The finishing tail: verify-work / verification / closeout after the last unit is in the estimate, and once the known
units are done the ETA says `finishing · <stage>` (never a stale countdown) until it is overdue past the tail's P90."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import eta_replay as rp  # noqa: E402
from test_eta_ladder import E, PRIOR, A7_AGENTS, est, labels, single  # noqa: E402
from steward import eta, eta_prior, eta_structure, tabnames  # noqa: E402
from steward.eta_structure import Structure  # noqa: E402


def multi(done_at=(), total=6, **kw) -> Structure:
    lab = labels(3, 16) + labels(3, 17)
    return Structure(start=E(0), units_source="planning", units_total=total, units_exact=True,
                     phases={"16": 3, "17": 3}, planned={u: E(30) for u in lab},
                     done={lab[i]: E(m) for i, m in enumerate(done_at)}, known_at=E(30), **kw)


def test_multi_phase_tail_comes_from_the_kind_history():
    assert PRIOR.tail_min == 15.0 and PRIOR.tail_p90_min == 25.0
    s = multi([40, 50])
    no_tail = est(s, [], 55, prior=dataclasses.replace(PRIOR, tail_min=None, tail_p90_min=None))
    with_tail = est(s, [], 55)
    assert with_tail.tail_min == 15.0 and no_tail.tail_min == 0.0
    assert with_tail.mid_min - no_tail.mid_min == pytest.approx(15.0, abs=1e-6)


def test_prior_tail_median_from_finished_jobs():
    rows = [{"job": f"j{i}", "kind": "gsd-build", "minutes": 100, "tail_min": t} for i, t in enumerate((10, 12, 25))]
    p = eta_prior.prior_for({"rows": rows}, "gsd-build")
    assert p.tail_min == 12 and p.tail_p90_min == 25


def test_all_done_verifying_counts_up_never_down():
    s = single([40, 50, 60, 70, 80, 90, 100])
    e = est(s, A7_AGENTS, 104)
    assert e.fin_stage == "verifying" and e.fin_since == E(100)
    r = eta.render(e.to_dict(), E(104))
    assert r["text"] == "finishing · verifying · 4m" and r["tab"] == "fin" and r["overdue_min"] is None
    assert eta.render(e.to_dict(), E(108))["text"].startswith("finishing")  # not frozen at "~20m left"
    late = eta.render(e.to_dict(), E(100 + 10 + 12))                       # single-phase tail 6 -> p90 10.2
    assert late["overdue_min"] == 12 and late["tab"] == "+12m" and late["header"].startswith("overdue 12m")


def test_worker_done_claim_is_closing_out():
    s = single([40, 50, 60, 70, 80, 90, 100], done_claimed_at=E(101))
    assert est(s, A7_AGENTS, 102).fin_stage == "closing out"


def test_sent_back_tail_synthetic():
    base = dict(done_claimed_at=E(110))
    s = single([40, 50, 60, 70, 80, 90, 100], gate={"at": E(112), "ok": False, "check": "npm pack"}, **base)
    e = est(s, A7_AGENTS, 115)
    assert e.fin_stage == "fixing npm pack" and e.fin_since == E(112)
    assert eta.render(e.to_dict(), E(115))["text"] == "finishing · fixing npm pack · 3m"
    assert eta.render(e.to_dict(), E(127))["tab"] == "fin"
    over = eta.render(e.to_dict(), E(112 + 15 + 30))                       # 30 min past the fix P90
    assert over["overdue_min"] == 30 and over["tab"] == "+30m" and "fixing npm pack" in over["text"]
    # the worker claims done again after the fix: recomputed as closing out, elapsed from the unit boundary
    again = single([40, 50, 60, 70, 80, 90, 100], gate={"at": E(112), "ok": False, "check": "npm pack"},
                   done_claimed_at=E(130))
    assert est(again, A7_AGENTS, 131).fin_stage == "closing out"
    # a failed gate with units still open is still `fixing`
    open_ = single([40, 50], gate={"at": E(60), "ok": False, "check": "pytest -q"})
    assert est(open_, A7_AGENTS, 61).fin_stage == "fixing pytest -q"


def test_job_facts_reads_the_gate_events():
    ev = [SimpleNamespace(what="worker reported done", ts="2026-09-30T18:40:00+00:00", evidence={}),
          SimpleNamespace(what="finish_line_not_met", ts="2026-09-30T18:41:00+00:00",
                          evidence={"command": "npm pack --dry-run --json", "first_failing_line": "x"})]
    f = eta_structure.job_facts(SimpleNamespace(created_at="2026-09-30T17:00:00+00:00"), ev)
    assert f["gate"]["ok"] is False and f["gate"]["check"] == "npm pack --dry-run"
    assert eta_structure.gate_check({"first_failing_line": "a" * 60}).endswith("…")


def test_tab_title_follows_the_same_text():
    assert tabnames.info_text(kind="live", eta={"tab": "fin"}) == "fin"
    assert tabnames.info_text(kind="live", eta={"tab": "+12m", "overdue_min": 12}) == "+12m"


@pytest.mark.parametrize("job", rp.JOBS)
def test_replay_backtest_has_no_stale_countdown_after_the_last_unit(job, tmp_path_factory):
    rep = rp.run_eta_replay(job, tmp_path_factory.mktemp(f"tail-{job}"))
    done = [r for r in rep.rows if r["units_total"] and r["units_done"] >= r["units_total"]]
    assert done, job
    for r in done:
        assert r["fin_stage"] in ("verifying", "closing out"), (job, r["fin_stage"])
        assert "left" not in eta.render(r, r["at"])["text"]
    last = rep.rows[-1]
    assert eta.render(last, last["at"])["text"].startswith(("finishing", "overdue"))
