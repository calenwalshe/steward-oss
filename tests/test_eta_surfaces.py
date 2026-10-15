"""The one stored ETA on every surface (phase 18-06, G16-G19): the pane header, the API, the phone and the MCP read
tools show the same text, counted down, from the same kv `efficiency_state['eta']`. Fakes only; no network."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from steward import dashact, eta, jobview  # noqa: E402
from steward import efficiency_watch as ew  # noqa: E402
from steward.api import StewardAPI  # noqa: E402
from steward.models import JobState  # noqa: E402
from steward.store import Store  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
ANSI = re.compile(r"\033\[[0-9;]*m")


def units_eta(now=None, *, p10=80, p50=100, p90=130, done=3, total=7, label="plan 16-04", source="units", **kw):
    """A stored units ETA whose shown band ends `p10/p50/p90` minutes after `now` (the seed time)."""
    now = time.time() if now is None else now
    e = eta.Eta(at=now, source=source, mid_min=float(p50), p10_min=float(p10), p90_min=float(p90),
                p50_min=float(p50) if source in eta.P50_SOURCES else None, units_done=done, units_total=total,
                units_exact=True, unit_label=label, **kw)
    e.shown = eta._empty_shown()
    e.shown.update(p10_end=now + p10 * 60, p50_end=now + p50 * 60, p90_end=now + p90 * 60, source=source, set_at=now,
                   units_done_at_set=done, band_units_done=done, p50_min_at_set=float(p50))
    return e.to_dict()


def seed(store, job_id, stored, *, mode=None):
    store.kv_set(job_id, ew.STATE_KEY, {"mode": "shadow", "eta": stored})
    if mode:
        store.kv_set(job_id, ew.SWITCH_KEY, mode)


def running(store, goal="Ship the widget", state=JobState.RUNNING):
    job = store.create_job(goal, "/tmp/somerepo")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))
    store.kv_set(job.job_id, "herdr_workspace", "w1")
    store.kv_set(job.job_id, "herdr_tab", "w1:t2")
    return job


def render_dash(steward_home, tmp_path, cols, rows=None):
    m = tmp_path / f"map{cols}"
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_DASH_COLS": str(cols), "STEWARD_HEALTH": "0",
           "STEWARD_DASH_MAP": str(m), "PYTHONPATH": str(_REPO)}
    if rows:
        env["STEWARD_DASH_ROWS"] = str(rows)
    res = subprocess.run([sys.executable, str(_REPO / "deploy/cockpit/steward-dash-jobs.py")],
                         capture_output=True, text=True, env=env, cwd=_REPO)
    return [ANSI.sub("", ln).replace("{SPIN}", "x") for ln in res.stdout.splitlines()]


# -- tracer --------------------------------------------------------------------------------------------


def test_tracer_pane_and_api_agree(steward_home, tmp_path):
    store = Store(steward_home)
    job = running(store)
    seed(store, job.job_id, units_eta())
    lines = render_dash(steward_home, tmp_path, 120)
    head = next(ln for ln in lines if job.job_id[:8] in ln)
    assert "RUNNING ~1h40 left (1h20–2h10) ·" in _no_steer(head), head  # the ETA comes first in the detail
    step = StewardAPI(store).get_current_step(job.job_id)["eta"]
    assert step["shown"] == "~1h40 left (1h20–2h10)"
    assert 99 <= step["p50_min"] <= 100 and 79 <= step["p10_min"] <= 80 and 129 <= step["p90_min"] <= 130
    assert step["source"] == "units" and step["units_done"] == 3 and step["units_total"] == 7
    assert step["unit_label"] == "plan 16-04" and step["updated_at"].endswith("+00:00")
    assert set(step) == {"p10_min", "p50_min", "p90_min", "shown", "units_done", "units_total", "unit_label", "source",
                         "stage", "overdue_min", "updated_at"}


def test_no_eta_when_not_running_or_off(steward_home, tmp_path):
    store = Store(steward_home)
    waiting = running(store, "Waiting job", JobState.NEEDS_HUMAN)
    off = running(store, "Switched off job")
    seed(store, waiting.job_id, units_eta())
    seed(store, off.job_id, units_eta(), mode="off")
    lines = render_dash(steward_home, tmp_path, 120)
    for j in (waiting, off):
        head = next(ln for ln in lines if j.job_id[:8] in ln)
        assert "left" not in head and "estimating" not in head, head
    api = StewardAPI(store)
    assert api.get_current_step(waiting.job_id)["eta"] is None
    assert api.get_current_step(off.job_id)["eta"] is None


def test_with_eta_drops_eta_first():
    detail, part = "live · working · 12s ago", "~1h40 left (1h20–2h10)"
    both = f"{detail} · {part}"
    assert jobview.with_eta(detail, part, len(both)) == both
    assert jobview.with_eta(detail, part, len(both) - 1) == detail
    assert jobview.with_eta(detail, part, len(detail)) == detail
    assert jobview.with_eta(detail, None, 500) == detail
    assert jobview.eta_part(None, 0) is None and jobview.eta_part("x", 0) is None
    now = time.time()
    assert jobview.eta_part(units_eta(now), now) == part


# -- efficiency row ------------------------------------------------------------------------------------


def test_efficiency_row_appends_units_and_source():
    base = {"summary": {"usd": 3.0, "calls": 10, "sub_share": 1.0, "peak_ctx": 1000, "think_share": 0.1, "tok_s": 50.0}}
    plain = ew.row_parts(base)
    assert ew.row_parts({**base, "eta": units_eta()}) == plain + ["units 3/7", "eta from cycle times"]
    prior = eta.Eta(at=1.0, source="prior", units_total=None).to_dict()
    assert ew.row_parts({**base, "eta": prior}) == plain + ["eta from job kind"]
    cr = eta.Eta(at=1.0, source="class-rate", units_done=0, units_total=7, units_exact=True).to_dict()
    assert ew.row_parts({**base, "eta": cr}) == plain + ["units 0/7", "eta from plan count"]
    trend = eta.Eta(at=1.0, source="units+trend", units_done=4, units_total=24, units_exact=False).to_dict()
    assert ew.row_parts({**base, "eta": trend})[-2:] == ["units 4/~24", "eta from cycle trend"]
    peers = eta.Eta(at=1.0, source="peers", stage="testing").to_dict()
    assert ew.row_parts({**base, "eta": peers})[-1] == "eta from peers + stage"
    assert ew.row_parts({"codex": True, "eta": eta.na_eta()}) == ["n/a (codex transcript)"]
    assert ew.row_parts({**base, "eta": eta.Eta(at=1.0).to_dict()}) == plain   # still estimating: nothing to add
    assert ew.row_parts(base) == plain and ew.row_parts(None) == ["no data yet"]


def test_supervisor_row_carries_the_eta_parts(steward_home):
    store = Store(steward_home)
    job = running(store)
    store.kv_set(job.job_id, ew.STATE_KEY, {"mode": "shadow", "computed_at": 1.0, "summary": {"usd": 1.0, "calls": 5},
                                            "eta": units_eta()})
    from steward import supervisors
    row = next(r for r in supervisors.supervisor_rows(store, job.job_id) if r["role"] == "efficiency")
    assert row["parts"][-2:] == ["units 3/7", "eta from cycle times"]


# -- list_jobs, get_job --------------------------------------------------------------------------------


def test_list_jobs_and_get_job(steward_home):
    store = Store(steward_home)
    job = running(store, "units")
    early = running(store, "early")
    waiting = running(store, "waiting", JobState.NEEDS_HUMAN)
    seed(store, job.job_id, units_eta())
    from test_eta_display import mk
    seed(store, early.job_id, mk(time.time(), source="class-rate", mid=145, half=50, done=0).to_dict())
    seed(store, waiting.job_id, units_eta())
    api = StewardAPI(store)
    rows = {r["job_id"]: r for r in api.list_jobs()}
    r = rows[job.job_id]
    assert r["eta"]["shown"] == r["eta_text"] == "~1h40 left (1h20–2h10)" and 99 <= r["eta_p50_min"] <= 100
    e = rows[early.job_id]
    assert e["eta"]["p50_min"] is None and e["eta_p50_min"] is None and e["eta_text"].startswith("≈")
    w = rows[waiting.job_id]
    assert w["eta"] is None and w["eta_p50_min"] is None and w["eta_text"] is None
    assert api.get_job(job.job_id)["eta"]["shown"] == r["eta_text"]
    assert api.get_job(waiting.job_id)["eta"] is None


# -- every surface, the same string --------------------------------------------------------------------


def _states(now):
    from test_eta_display import mk

    u = mk(now, mid=100, half=20)
    clock = mk(now, source="clock", mid=340, half=0, done=0, total=None, elapsed_min=20.0, kind="clock")
    peers = mk(now, source="peers", mid=10, half=8, done=0, total=None, elapsed_min=12.0, kind="gsd-quick",
               stage="testing", tail_min=5.0, jev_state="ok")
    return {
        "estimating": eta.Eta(at=now).to_dict(),
        "prior": mk(now, source="prior", mid=168, half=90, done=0, total=None, elapsed_min=13.0).to_dict(),
        "class-rate": mk(now, source="class-rate", mid=145, half=50, done=0).to_dict(),
        "units": u.to_dict(),
        "overdue": mk(now - 200 * 60, mid=100).to_dict(),
        "finishing": mk(now, mid=8, half=1, finishing=True).to_dict(),
        "peers": peers.to_dict(),
        "clock": clock.to_dict(),
        "n/a": eta.na_eta("gsd-quick"),
    }


def test_surfaces_agree_for_every_state(steward_home, monkeypatch):
    from datetime import datetime, timezone
    from steward import phone

    store = Store(steward_home)
    now = time.time()
    jobs = {}
    for name, stored in _states(now).items():
        job = running(store, name)
        seed(store, job.job_id, stored)
        jobs[name] = (job, stored)
    monkeypatch.setattr(time, "time", lambda: now)
    api = StewardAPI(store)
    rows = {r["job_id"]: r for r in api.list_jobs()}
    snap = {j.job_id: j for j in phone.gather(store, None, datetime.fromtimestamp(now, timezone.utc)).jobs}
    seen = set()
    for name, (job, stored) in jobs.items():
        pane = jobview.eta_part(stored, now)
        assert pane, name
        assert snap[job.job_id].eta_part == pane, name
        assert snap[job.job_id].hint.endswith(" · " + pane) and snap[job.job_id].hint_base, name
        step = api.get_current_step(job.job_id)["eta"]
        assert step["shown"] == pane == rows[job.job_id]["eta_text"] == api.get_job(job.job_id)["eta"]["shown"], name
        seen.add(pane)
    assert len(seen) == 9, seen   # nine distinct renderings, none empty
    assert {"estimating…", "eta n/a (codex transcript)"} <= seen


# -- narrow panes, height fit, no pushes ---------------------------------------------------------------


def _world(store, *, with_eta=True, long_hb=False):
    """Three running jobs: a units ETA, an overdue ETA and a peers ETA (plus optionally a long heartbeat class)."""
    from test_eta_display import mk

    now = time.time()
    stored = {
        "units": units_eta(now),
        "overdue": mk(now - 200 * 60, mid=100).to_dict(),
        "peers": mk(now, source="peers", mid=10, half=8, done=0, total=None, elapsed_min=12.0, kind="gsd-quick",
                    stage="testing", tail_min=5.0, jev_state="ok").to_dict(),
    }
    jobs = []
    for name, e in stored.items():
        job = running(store, f"Job {name} with a goal long enough to wrap onto a second line of the pane for sure yes")
        if with_eta:
            seed(store, job.job_id, e)
        if long_hb:
            from steward.models import Judgment
            store.append_event(job.job_id, what="heartbeat class changed",
                               evidence={"to": "waiting on a long running tool call"}, rule="r", judgment=Judgment.code)
        jobs.append(job)
    return jobs


def _no_steer(line):
    """The header row, without the trailing quick-steer / explain buttons (master) and padding."""
    return line.rstrip().removesuffix("[ explain ]").rstrip().removesuffix("[ steer ]").rstrip()


def _drop_etas(store, jobs):
    for j in jobs:
        store.kv_set(j.job_id, ew.STATE_KEY, {"mode": "shadow"})


@pytest.mark.parametrize("cols", [60, 80])
def test_narrow_cols_keeps_eta_and_cuts_liveness(steward_home, tmp_path, cols):
    # Sam 2026-09-30: the ETA is the most useful fact on a live job, so it comes first and survives narrow widths;
    # the liveness detail after it is what gets cut.
    store = Store(steward_home)
    _world(store, long_hb=True)
    heads = [ln for ln in render_dash(steward_home, tmp_path, cols) if "RUNNING" in ln]
    assert len(heads) == 3
    tails = [h.split("RUNNING", 1)[1] for h in heads]
    assert sum(1 for t in tails if "left" in t or "overdue" in t or "usually" in t) >= 2, tails


def test_wide_cols_shows_eta_first(steward_home, tmp_path):
    store = Store(steward_home)
    jobs = _world(store)
    lines = render_dash(steward_home, tmp_path, 140)  # room for both header buttons and the id
    heads = {j.job_id: next(ln for ln in lines if j.job_id[:8] in ln) for j in jobs}
    after_id = lambda j: _no_steer(heads[j.job_id]).split("RUNNING ", 1)[1]  # noqa: E731 - the detail after the state
    assert after_id(jobs[0]).startswith("~1h40 left (1h20–2h10) · ")
    assert after_id(jobs[1]).startswith("overdue 1h40 · ")
    assert after_id(jobs[2]).startswith("usually") and "testing" in after_id(jobs[2]), heads[jobs[2].job_id]


@pytest.mark.parametrize("cols", [60, 80, 100, 120])
def test_no_line_exceeds_cols_with_etas(steward_home, tmp_path, cols):
    _world(Store(steward_home), long_hb=cols % 40 == 20)
    lines = render_dash(steward_home, tmp_path, cols)
    assert lines and all(len(ln) <= cols for ln in lines), [ln for ln in lines if len(ln) > cols]
    assert not any(re.search(r"\w…\w", ln) for ln in lines)


def test_all_jobs_header_and_line_count_unchanged(steward_home, tmp_path):
    store = Store(steward_home)
    jobs = _world(store)
    for cols in (60, 100):
        a = render_dash(steward_home, tmp_path, cols)
        _drop_etas(store, jobs)
        b = render_dash(steward_home, tmp_path, cols)
        for j in jobs:
            seed(store, j.job_id, units_eta())
        assert len(a) == len(b)                                           # no new line per job
        assert [ln for ln in a if "ALL JOBS" in ln] == [ln for ln in b if "ALL JOBS" in ln]
        assert [ln for ln in a if "RUNNING" not in ln and "tokens" not in ln] == \
               [ln for ln in b if "RUNNING" not in ln and "tokens" not in ln]


def test_height_fit_unchanged_with_etas(steward_home, tmp_path):
    from questions_helpers import running_job, seed_queue
    from test_questions_pane import _check_map, _two_jobs_and_a_done
    from test_questions_pane import _render as qrender, _btns

    store, a, b, _done = _two_jobs_and_a_done(steward_home)
    for jid in (a, b):
        seed(store, jid, units_eta())
    lines, cmap = qrender(steward_home, tmp_path, 60, rows=16)
    assert len(lines) <= 16 - 9
    _check_map(lines, cmap)
    assert sorted(x[4] for x in _btns(cmap)) == ["accept_rec", "accept_rec", "answer", "answer"]
    for rows in (40, 30, 20, 16):                                         # identical frame shape with and without ETAs
        with_eta, cm1 = qrender(steward_home, tmp_path, 100, rows=rows)
        for jid in (a, b):
            store.kv_set(jid, ew.STATE_KEY, {"mode": "shadow"})
        without, cm2 = qrender(steward_home, tmp_path, 100, rows=rows)
        for jid in (a, b):
            seed(store, jid, units_eta())
        tappable = lambda cm: [c for c in cm if not c.startswith("more ")]  # a longer header may be cut: that adds a `more` line  # noqa: E731
        assert len(with_eta) == len(without) and len(tappable(cm1)) == len(tappable(cm2)), rows
        _check_map(with_eta, cm1)


def test_rendering_never_notifies(steward_home, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from steward import notify, phone

    store = Store(steward_home)
    jobs = _world(store)                                                  # includes an overdue ETA
    before = sum(len(store.events(j.job_id)) for j in jobs)

    def boom(*a, **k):
        raise AssertionError("rendering an ETA must never notify")

    for name in ("emit", "emit_questions"):
        monkeypatch.setattr(notify, name, boom)
    render_dash(steward_home, tmp_path, 100)
    phone.gather(store, None, datetime.now(timezone.utc))
    api = StewardAPI(store)
    api.list_jobs()
    for j in jobs:
        api.get_job(j.job_id)
        api.get_current_step(j.job_id)
    assert sum(len(store.events(j.job_id)) for j in jobs) == before
    assert all(store.kv_get(j.job_id, "eta_notify") is None for j in jobs)
