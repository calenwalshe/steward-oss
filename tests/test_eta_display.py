"""The ETA display rules (phase 18-03, G14, G15): range first, rounding, countdown, hysteresis, overdue, change events."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_eta_ladder import A7_AGENTS, E, agent, est, single  # noqa: E402
from steward import eta  # noqa: E402

AT = 1_790_100_000.0


def mk(at=AT, *, source="units", mid=100.0, half=20.0, done=3, total=7, exact=True, prev=None, attempt=0, **kw):
    """An Eta whose shown numbers are computed by the real hysteresis rules from `prev`."""
    p50 = mid if source in eta.P50_SOURCES else None
    e = eta.Eta(at=at, source=source, mid_min=mid, p10_min=max(mid - half, 0.0), p90_min=mid + half, p50_min=p50,
                units_done=done, units_total=total, units_exact=exact, attempt_no=attempt, **kw)
    e.shown = eta._shown(eta._d(prev), e)
    return e


# -- formatting -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("x,text", [
    (0, "<5m"), (3, "<5m"), (4.99, "<5m"), (5, "5m"), (12, "10m"), (13, "15m"), (58, "1h"), (59.9, "1h"),
    (99.9, "1h40"), (126, "2h10"), (125, "2h10"), (120, "2h"), (61, "1h"), (-4, "<5m")])
def test_fmt_minutes(x, text):
    assert eta.fmt_minutes(x) == text
    assert "s" not in text.replace("h", "")  # never seconds


@pytest.mark.parametrize("lo,hi,text", [
    (108.6, 289.6, "2–5 h"), (92.94, 196.86, "2–3 h"), (3.1, 20.5, "3–20 min"), (5.1, 6.2, "5–6 min"),
    (35, 80, "35m–1h20"), (70, 100, "1h10–1h40"), (3, 3.2, "3–4 min"), (20, 21, "20–25 min")])
def test_fmt_range(lo, hi, text):
    assert eta.fmt_range(lo, hi) == text


def test_fmt_range_equal_hours_widen():
    a, b = eta.fmt_range(120, 181).split(" ")[0].split("–")
    assert int(b) > int(a)


# -- render ---------------------------------------------------------------------------------------------


def test_render_texts():
    none = eta.render(eta.Eta(at=AT), AT)
    assert none["text"] == none["header"] == "estimating…" and none["p50_min"] is None

    prior = eta.Eta(at=AT, source="prior", elapsed_min=13.0, mid_min=168.0, p10_min=95.6, p90_min=276.6)
    prior.shown = eta._shown(None, prior)
    r = eta.render(prior, AT)
    assert r["header"] == "usually 2–5 h" and r["detail"] == "eta usually 2–5 h · from job kind" and r["p50_min"] is None
    assert eta.render(prior, AT + 1800)["header"] == "usually 2–5 h"         # the class range does not count down

    cr = mk(source="class-rate", mid=144.9, half=51.96, done=0)
    r = eta.render(cr, AT)
    assert r["header"] == "≈2–3 h left" and r["detail"] == "eta ≈2–3 h left · units 0/7 · from plan count"
    assert r["p50_min"] is None

    u = mk(mid=100, half=0)
    u.shown.update(p10_end=AT + 80 * 60, p90_end=AT + 130 * 60)
    r = eta.render(u, AT)
    assert r["header"] == r["text"] == "~1h40 left (1h20–2h10)"
    assert r["detail"] == "eta ~1h40 (1h20–2h10) · units 3/7 · from cycle times"
    t = mk(source="units+trend", mid=100, done=4)
    assert eta.render(t, AT)["detail"].endswith("units 4/7 · from cycle trend")
    inexact = mk(total=24, exact=False)
    assert "units 3/~24" in eta.render(inexact, AT)["detail"]

    fin = mk(mid=5, half=1, finishing=True)
    assert eta.render(fin, AT)["text"] == "finishing · ~5m left"
    bare = mk(source="class-rate", mid=60, half=10, done=6, finishing=True)
    assert eta.render(bare, AT)["text"] == "finishing"
    claimed = mk(done_claimed=True)
    assert eta.render(claimed, AT)["text"] == "estimating…" and eta.render(claimed, AT)["p10_min"] is None


def test_countdown():
    u = mk(mid=100, half=20)
    at2 = eta.render(u, AT + 120)
    assert at2["p50_min"] == pytest.approx(98.0) and at2["p10_min"] == pytest.approx(78.0)
    assert at2["p90_min"] == pytest.approx(118.0)
    assert at2["header"].startswith("~1h40 left")          # 98 rounds to 1h40
    short = mk(mid=12, half=2)
    assert eta.render(short, AT + 120)["header"].startswith("~10m left")
    assert eta.render(short, AT)["header"].startswith("~10m left")
    over = eta.render(short, AT + 12 * 60 + 1)
    assert over["p10_min"] == 0.0 and over["p50_min"] == 0.0 and over["p90_min"] >= 0.0


# -- hysteresis -----------------------------------------------------------------------------------------


def test_hysteresis():
    prev = mk(AT, mid=100)
    base = prev.shown["p50_end"]
    assert base == AT + 6000
    keep = mk(AT + 60, mid=104, prev=prev)        # |104 - 99| = 5: neither over 5 min nor over 10 %
    assert keep.shown["p50_end"] == base and keep.shown["set_at"] == AT
    moved = mk(AT + 60, mid=105.5, prev=prev)
    assert moved.shown["p50_end"] == pytest.approx(AT + 60 + 105.5 * 60) and moved.shown["set_at"] == AT + 60
    small = mk(AT, mid=20)
    over_pct = mk(AT, mid=22.5, prev=small)
    assert over_pct.shown["p50_end"] == pytest.approx(AT + 22.5 * 60)
    kept_small = mk(AT, mid=21.5, prev=small)       # 1.5 min and 7.5 %: keep
    assert kept_small.shown["p50_end"] == small.shown["p50_end"]
    src = mk(AT + 60, source="units+trend", mid=100, done=4, prev=prev)
    assert src.shown["source"] == "units+trend" and src.shown["set_at"] == AT + 60
    attempt = mk(AT + 60, mid=100, attempt=2, prev=prev)
    assert attempt.shown["attempt_no"] == 2 and attempt.shown["set_at"] == AT + 60
    assert attempt.shown["p50_min_at_set"] == 100


def test_band_narrows_only_at_boundaries():
    prev = mk(AT, mid=100, half=20)
    p10, p90 = prev.shown["p10_end"], prev.shown["p90_end"]
    narrow = mk(AT + 60, mid=99, half=10, prev=prev)
    assert narrow.shown["p10_end"] == p10 and narrow.shown["p90_end"] == p90       # same units_done: kept
    wide = mk(AT + 60, mid=99, half=40, prev=prev)
    assert wide.shown["p10_end"] == pytest.approx(AT + 60 + 59 * 60) and wide.shown["p90_end"] == pytest.approx(AT + 60 + 139 * 60)
    edge = mk(AT + 60, mid=99, half=10, prev=prev)
    lop = eta._shown(eta._d(prev), eta.Eta(at=AT + 60, source="units", mid_min=99, p50_min=99, p10_min=24.0, p90_min=109.0,
                                          units_done=3, units_total=7))
    assert lop["p10_end"] == pytest.approx(AT + 60 + 24 * 60) and lop["p90_end"] == p90   # each edge independently
    assert edge.shown["band_units_done"] == 3
    boundary = mk(AT + 60, mid=99, half=10, done=4, prev=prev)
    assert boundary.shown["p10_end"] == pytest.approx(AT + 60 + 89 * 60)
    assert boundary.shown["p90_end"] == pytest.approx(AT + 60 + 109 * 60) and boundary.shown["band_units_done"] == 4
    assert boundary.shown["p50_end"] == prev.shown["p50_end"]       # the P50 itself did not move
    assert boundary.shown["p10_end"] <= boundary.shown["p50_end"] <= boundary.shown["p90_end"]


def test_estimate_does_not_jump_between_recomputes():
    s = single([40.8])
    running = A7_AGENTS + [agent("build", (16, 2), 40.9, None)]   # the running unit's remainder shrinks with the clock
    prev = est(s, running, 41)
    ends = {prev.shown["p50_end"]}
    for m in range(42, 50):
        cur = est(s, running, m, prev=prev)
        ends.add(cur.shown["p50_end"])
        r = eta.render(cur, E(m))
        assert r["p50_min"] == pytest.approx((cur.shown["p50_end"] - E(m)) / 60.0)
        prev = cur
    assert len(ends) == 1        # a consistent estimate is shown once and then only counts down


# -- overdue (G15) --------------------------------------------------------------------------------------


def test_overdue():
    prev = mk(AT, mid=100)
    now = AT + 6000 + 12 * 60
    r = eta.render(prev, now)
    assert r["header"] == "overdue 12m" and r["text"] == "overdue by 12m (was ~1h40)" and r["overdue_min"] == 12
    assert r["detail"].startswith("eta overdue by 12m (was ~1h40)")
    frozen = mk(now, mid=30, prev=prev)                    # a recompute with no new boundary: never a silent extension
    assert frozen.shown["p50_end"] == prev.shown["p50_end"] and frozen.shown["set_at"] == AT
    assert frozen.shown["overdue_noted"] is True
    assert eta.render(frozen, now + 300)["overdue_min"] == 17
    later = mk(now + 60, mid=8, prev=frozen)
    assert later.shown["p50_end"] == prev.shown["p50_end"]
    fresh = mk(now + 120, mid=25, done=4, prev=frozen)     # the next unit boundary re-anchors
    assert fresh.shown["p50_end"] == pytest.approx(now + 120 + 25 * 60) and fresh.shown["overdue_noted"] is False
    assert eta.render(fresh, now + 120)["overdue_min"] is None
    hours = eta.render(prev, AT + 6000 + 75 * 60)
    assert hours["header"] == "overdue 1h15" and hours["text"] == "overdue by 1h15 (was ~1h40)"


# -- events ---------------------------------------------------------------------------------------------


def test_changes_events():
    prior = eta.Eta(at=AT, source="prior", mid_min=168, p10_min=95, p90_min=276)
    prior.shown = eta._shown(None, prior)
    cr = mk(AT + 60, source="class-rate", mid=145, half=50, done=0, total=7, prev=prior)
    (what, ev), = eta.changes(prior, cr)
    assert what == "eta_changed" and ev["from"] == "prior" and ev["to"] == "class-rate"
    assert set(ev) == {"from", "to", "p50_min", "p10_min", "p90_min", "units_done", "units_total"}
    assert ev["p50_min"] is None and ev["units_total"] == 7 and ev["units_done"] == 0

    prev = mk(AT, mid=100)
    twenty = mk(AT + 60, mid=119, prev=prev)
    assert twenty.shown["set_at"] == AT + 60 and eta.changes(prev, twenty) == []            # re-anchored, 20 %: silent
    thirtyfive = mk(AT + 60, mid=134, prev=prev)
    (what, ev), = eta.changes(prev, thirtyfive)
    assert what == "eta_changed" and ev["from"] == ev["to"] == "units" and ev["p50_min"] == pytest.approx(134.0, abs=0.1)
    assert eta.changes(prev, mk(AT + 60, mid=104, prev=prev)) == []                           # not even re-anchored

    grown = mk(AT + 60, source="class-rate", mid=90, done=3, total=8, prev=prev, scope_grown_at=AT + 60, grown_units_done=3)
    evs = dict(eta.changes(prev, grown))
    assert evs["eta_scope_grown"] == {"from_units": 7, "to_units": 8}
    again = mk(AT + 120, source="class-rate", mid=89, done=3, total=8, prev=grown, scope_grown_at=AT + 60, grown_units_done=3)
    assert "eta_scope_grown" not in dict(eta.changes(grown, again))                             # once per growth

    now = AT + 6000 + 120
    over = mk(now, mid=30, prev=prev)
    (what, ev), = eta.changes(prev, over)
    assert what == "eta_overdue" and ev["overdue_min"] == pytest.approx(2.0) and ev["was_min"] == 100.0
    over2 = mk(now + 60, mid=29, prev=over)
    assert eta.changes(over, over2) == []                                                       # once per shown estimate
    renew = mk(now + 120, mid=25, done=4, prev=over2)
    assert renew.shown["overdue_noted"] is False
    assert [w for w, _ in eta.changes(over2, renew)] == ["eta_changed"]                          # a 25 min vs overdue move
    assert eta.changes(None, prev) == []


# -- api_view -------------------------------------------------------------------------------------------


def test_api_view():
    u = mk(mid=100, half=0, done=3, unit_label="plan 16-04")
    u.shown.update(p10_end=AT + 80 * 60, p90_end=AT + 130 * 60)
    v = eta.api_view(u, AT + 120)
    assert set(v) == {"p10_min", "p50_min", "p90_min", "shown", "units_done", "units_total", "unit_label", "source",
                      "stage", "overdue_min", "updated_at"}
    assert (v["p10_min"], v["p50_min"], v["p90_min"]) == (78, 98, 128)
    assert v["shown"].startswith("~1h40 left") and v["units_done"] == 3 and v["units_total"] == 7
    assert v["unit_label"] == "plan 16-04" and v["source"] == "units" and v["stage"] is None and v["overdue_min"] is None
    assert v["updated_at"] == datetime.fromtimestamp(AT, timezone.utc).isoformat()
    json.dumps(v)
    cr = mk(source="class-rate", mid=145, half=50, done=0)
    w = eta.api_view(cr, AT)
    assert w["p50_min"] is None and w["p10_min"] == 95 and w["p90_min"] == 195 and w["shown"].startswith("≈")
    late = eta.api_view(u, AT + 6000 + 600)
    assert late["p50_min"] == 0 and late["overdue_min"] == 10 and late["shown"] == "overdue 10m"
    est_only = eta.api_view(eta.Eta(at=AT), AT)
    assert est_only["shown"] == "estimating…" and est_only["p10_min"] is None and est_only["p50_min"] is None
