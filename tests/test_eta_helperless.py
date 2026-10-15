"""Helper-less, clock-bound and codex jobs (phase 18-05, G10, G20): ranges, stated ends and n/a, never a point estimate."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_eta_ladder import E, T0, est  # noqa: E402
from steward import eta, eta_prior  # noqa: E402
from steward.eta_structure import Structure  # noqa: E402

SEED = eta_prior.load("/nonexistent-eta-home")


def prior(kind):
    return eta_prior.prior_for(SEED, kind)


def peers(kind, at, *, jev=None, jev_state=None, prev=None):
    return est(Structure(start=T0), [], at, prior=prior(kind), kind=kind, jev=jev, jev_state=jev_state, prev=prev)


# -- clock-bound jobs: the stated end, exact -----------------------------------------------------------------


def clock_structure():
    return Structure(start=T0, units_source="goal", clock_min=360)


def test_clock_job_is_exact():
    e = est(clock_structure(), [], 20, prior=None, kind="clock")
    assert e.source == "clock"
    assert e.p10_min == e.p50_min == e.p90_min == pytest.approx(340.0)
    r = eta.render(e, E(20))
    assert r["header"] == r["text"] == "~5h40 left (stated 6 h)"
    assert r["detail"] == "eta ~5h40 left (stated 6 h) · from stated duration"
    later = eta.render(e, E(50))                                   # it counts down, the end does not move
    assert later["header"] == "~5h10 left (stated 6 h)"
    e2 = est(clock_structure(), [], 50, prior=None, kind="clock", prev=e.to_dict())
    assert e2.shown["p50_end"] == e.shown["p50_end"] == pytest.approx(E(360))
    assert eta.changes(e, e2) == []                                # no churn while it counts down


def test_clock_job_overdue_rule_applies_to_the_stated_end():
    e = est(clock_structure(), [], 20, prior=None, kind="clock")
    late = est(clock_structure(), [], 370, prior=None, kind="clock", prev=e.to_dict())
    r = eta.render(late, E(370))
    assert r["text"] == "overdue by 10m (was ~5h40)" and r["overdue_min"] == 10
    assert r["detail"].endswith("from stated duration")
    assert [w for w, _ in eta.changes(e, late)] == ["eta_overdue"]


def test_clock_stated_formats():
    for minutes, text in ((30, "30 min"), (60, "1 h"), (90, "1h30"), (120, "2 h")):
        e = est(Structure(start=T0, units_source="goal", clock_min=minutes), [], 1, prior=None, kind="clock")
        assert f"(stated {text})" in eta.render(e, E(1))["header"]


# -- helper-less jobs: a range plus the stage, never a point --------------------------------------------------


def test_quick_job_range_plus_stage():
    e = peers("gsd-quick", 12, jev={"stage": "testing"}, jev_state="ok")
    assert e.source == "peers" and e.p50_min is None and e.stage == "testing" and e.tail_min == 5.0
    r = eta.render(e, E(12))
    assert r["header"] == "usually 3–20 min · testing" and r["p50_min"] is None
    assert "stage testing (usually 5m left at this stage)" in r["detail"] and "from peers + stage" in r["detail"]
    committing = peers("gsd-quick", 12, jev={"stage": "committing"}, jev_state="ok")
    assert "stage committing (usually <5m left at this stage)" in eta.render(committing, E(12))["detail"]
    implementing = peers("gsd-quick", 12, jev={"stage": "implementing"}, jev_state="ok")
    assert eta.render(implementing, E(12))["detail"].endswith("· stage implementing · from peers + stage")
    bare = peers("gsd-quick", 12)
    assert eta.render(bare, E(12))["header"] == "usually 3–20 min" and bare.stage is None
    unclear = peers("gsd-quick", 12, jev={"stage": "unclear"}, jev_state="ok")
    assert unclear.stage is None and eta.render(unclear, E(12))["header"] == "usually 3–20 min"


def test_quick_job_never_a_point_at_any_elapsed_time():
    prev = None
    for at in (1, 5, 12, 25, 40, 90, 300):
        e = peers("gsd-quick", at, jev={"stage": "testing"}, jev_state="ok", prev=prev)
        r = eta.render(e, E(at))
        assert e.p50_min is None and r["p50_min"] is None and r["overdue_min"] is None
        assert r["header"] == "usually 3–20 min · testing"         # constant however long the job runs
        prev = e.to_dict()


def test_discussion_job_per_attempt():
    e = peers("gsd-discuss", 3)
    assert e.p50_min is None and eta.render(e, E(3))["header"] == "usually 5–6 min per attempt"


def test_research_and_other_ranges():
    assert eta.render(peers("research", 4), E(4))["header"] == "usually 15m–2h30"
    assert eta.render(peers("other", 4), E(4))["header"] == "usually 20–55 min"
    one = eta_prior.prior_for({"rows": [{"job": "x", "kind": "other", "minutes": 30}]}, "other")
    e = est(Structure(start=T0), [], 4, prior=one, kind="other")
    assert e.source == "none" and eta.render(e, E(4))["header"] == "estimating…"


def test_gsd_build_without_units_stays_a_prior():
    e = est(Structure(start=T0), [], 4)
    assert e.source == "prior"


# -- (no Jev) -----------------------------------------------------------------------------------------------------


def test_no_jev_detail():
    off = eta.render(peers("gsd-quick", 12, jev_state="off"), E(12))["detail"]
    gone = eta.render(peers("gsd-quick", 12, jev_state="unavailable"), E(12))["detail"]
    ok = eta.render(peers("gsd-quick", 12, jev={"stage": "testing"}, jev_state="ok"), E(12))["detail"]
    assert off.endswith("(no Jev)") and gone.endswith("(no Jev)") and "(no Jev)" not in ok
    assert eta.no_jev_note({"source": "units", "jev_state": "off"}) == " (no Jev)"
    assert eta.no_jev_note({"source": "units", "jev_state": "ok"}) == ""
    assert eta.no_jev_note({"source": "clock", "jev_state": "off"}) == ""      # Jev is never asked about a stated end
    assert eta.no_jev_note({"source": "n/a", "jev_state": "off"}) == ""


# -- codex (G20) --------------------------------------------------------------------------------------------------


def test_codex_na_render_and_api():
    d = eta.na_eta("gsd-build")
    assert d["source"] == "n/a" and d["at"] == 0.0 and d == eta.na_eta("gsd-build")
    r = eta.render(d, E(5))
    assert r["header"] == r["text"] == r["detail"] == "eta n/a (codex transcript)"
    v = eta.api_view(d, E(5))
    assert v["source"] == "n/a" and v["p50_min"] is None and v["p10_min"] is None and v["p90_min"] is None
    assert eta.changes(d, eta.na_eta("gsd-build")) == []
