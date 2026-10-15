"""The G21 replay (phase 18-07): three finished GSD builds through the REAL watcher and ETA.

`run_eta_replay(job, base_dir)` drives `EfficiencyWatcher.step` (with its ETA), `tokens`, `efficiency.compute`,
`efficiency_detect`, `eta_structure` over a real tmp git worktree that receives the job's planning commits at their
recorded times (`eta_fixtures`), `eta_prior`'s shipped seed, `eta.estimate` and `eta.changes`, on the committed transcript
fixtures (tests/fixtures/efficiency/*.json.gz) and timelines (tests/fixtures/eta/*.json). The only fakes are the clock
(including `steward.store.now_iso`), the tmp Store and repo, the projects dir and a subprocess guard that raises on herdr
or claude; there is no Jev.

    python3 tests/eta_replay.py --table [--loo]
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import efficiency_replay as er  # noqa: E402
import eta_fixtures as ef  # noqa: E402
from steward import chat as chat_module  # noqa: E402
from steward import efficiency_watch, eta, eta_prior  # noqa: E402
from steward import store as store_module  # noqa: E402
from steward.runner_exec import SubprocessRunner  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "efficiency"
JOBS = ("06ea6a04", "a7d292d8", "ed11ba3c")
SLICES = (0.25, 0.50, 0.75)
SLICE_LIMITS = {0.25: 25.0, 0.50: 15.0, 0.75: 8.0}
BAND_MIN_HITS = 22


@dataclass
class EtaReplay:
    job: str
    t0: float
    truth_min: float
    rows: list                      # every stored ETA (kv efficiency_state['eta']) once per recompute, in order
    events: list                    # [(what, evidence)] eta_* and efficiency_* events, tmp paths cleaned
    event_ts: list = field(default_factory=list)   # [(what, seconds after t0)] of the same events
    argv0: set = field(default_factory=set)
    wall_s: float = 0.0
    report: str = ""                # efficiency_watch.render_table at the end of the replay
    notifications: list = field(default_factory=list)
    res: object = None              # the efficiency_replay.ReplayResult
    timeline: dict = field(default_factory=dict)
    head: str = ""                  # "<commit count>:<HEAD committer epoch>" of the worktree at the end

    def digest(self) -> str:
        """Everything the ETA and the watcher decided; no tmp path, pid or real time can appear."""
        doc = {"rows": self.rows, "events": self.events, "event_ts": self.event_ts, "report": self.report,
               "notifications": self.notifications}
        return json.dumps(_round(doc), sort_keys=True, default=str)


def _round(node):
    if isinstance(node, float):
        return round(node, 4)
    if isinstance(node, dict):
        return {k: _round(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_round(v) for v in node]
    return node


def sha_prefix(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@contextmanager
def _fake_store_clock(getter):
    """steward.store.now_iso answers the ISO form of getter() (a fake epoch); restored afterwards."""
    real = store_module.now_iso
    store_module.now_iso = lambda: ef._iso(getter())
    try:
        yield
    finally:
        store_module.now_iso = real


def seed_minus(job: str) -> dict:
    """The shipped seed without one job's row (leave-one-out)."""
    seed = eta_prior._seed()
    return {**seed, "rows": [r for r in seed["rows"] if r["job"] != job]}


def run_eta_replay(job: str, base_dir, *, priors: dict | None = None) -> EtaReplay:
    base_dir = Path(base_dir)
    tl = ef.load_timeline(job)
    t0 = float(tl["t0"])
    files = er.from_fixture(FIXTURES / f"{job}.json.gz")
    rows: list = []
    holder: dict = {"clock": None}

    def now() -> float:
        c = holder["clock"]
        return c.t if c is not None else t0

    def setup(store, job_id, clock):
        holder["clock"] = clock
        clock.t = t0
        if priors is not None:
            (Path(store.home)).mkdir(parents=True, exist_ok=True)
            (Path(store.home) / eta_prior.PRIORS_FILE).write_text(json.dumps(priors), encoding="utf-8")
        created, _base = ef.setup_job(store, tl, until=t0)
        return created

    def before_step(store, job_id, t):
        repo = chat_module.worktree_path(store, job_id)
        ef.advance(repo, tl, t)
        ef.replay_events(store, job_id, tl, t)

    def after_step(store, job_id, t):
        stored = (store.kv_get(job_id, efficiency_watch.STATE_KEY) or {}).get("eta")
        if isinstance(stored, dict) and (not rows or rows[-1]["at"] != stored.get("at")):
            rows.append(copy.deepcopy(stored))

    with _fake_store_clock(now):
        res = er.run_replay(files, base_dir, job_id=job, goal=tl["goal"], setup=setup, before_step=before_step,
                            after_step=after_step, watcher_kwargs={"eta_runner": SubprocessRunner()})
        store = res.store
        evs, ev_ts = [], []
        for e in store.events(job):
            if e.what.startswith("eta_") or e.what.startswith("efficiency_"):
                evs.append((e.what, er._clean(e.evidence, base_dir)))
                ev_ts.append((e.what, round(_epoch(e.ts) - t0, 3)))
        report = efficiency_watch.render_table(
            efficiency_watch.report(store, job, projects_dir=res.proj, now=res.end))
        repo = chat_module.worktree_path(store, job)
        head = f"{ef._git(repo, 'rev-list', '--count', 'HEAD')}:{ef._git(repo, 'log', '-1', '--format=%ct')}"
    argv0 = {c.split()[0] if c.split() else "" for c in res.subprocess_calls}
    return EtaReplay(job=job, t0=t0, truth_min=float(tl["truth_min"]), rows=rows, events=evs, event_ts=ev_ts,
                     argv0=argv0, wall_s=res.wall_s, report=er._clean(report, base_dir),
                     notifications=res.notifications, res=res, timeline=tl, head=head)


def _epoch(ts: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(ts).timestamp()


# -- reading the stored ETAs ------------------------------------------------------------------------------


def estimate_at(rows: list, t: float):
    """The latest stored ETA computed at or before the epoch `t`, or None."""
    best = None
    for r in rows:
        if r["at"] <= t + 1e-6:
            best = r
        else:
            break
    return best


def predicted_total(row, t0: float):
    """Predicted total job length in minutes: elapsed at compute + the P50 remaining (mid_min); None without one."""
    if row is None or row.get("mid_min") is None:
        return None
    return row["elapsed_min"] + row["mid_min"]


def band_total(row, t0: float):
    """(low, high) of the predicted total in minutes from the stored P10 / P90, or None."""
    if row is None or row.get("p10_min") is None or row.get("p90_min") is None:
        return None
    return row["elapsed_min"] + row["p10_min"], row["elapsed_min"] + row["p90_min"]


def step_time(rep: EtaReplay, frac: float) -> float:
    return rep.t0 + frac * rep.truth_min * 60.0


def p50_error(rep: EtaReplay, frac: float):
    """(signed error in percent or None, the row). No ETA or no mid_min is None (the gate counts it as 100 %)."""
    row = estimate_at(rep.rows, step_time(rep, frac))
    pred = predicted_total(row, rep.t0)
    if pred is None:
        return None, row
    return 100.0 * (pred - rep.truth_min) / rep.truth_min, row


def slice_means(reps: list) -> dict:
    out = {}
    for f in SLICES:
        errs = []
        for r in reps:
            e, _row = p50_error(r, f)
            errs.append(100.0 if e is None else abs(e))
        out[f] = sum(errs) / len(errs)
    return out


def table(replays: list) -> tuple:
    """(rows, means): one row per job and 10 %-step (27 for three jobs) with the stored ETA at that time and whether the
    truth lay inside its band; `means` = {"p50": {0.25: pct, 0.5: pct, 0.75: pct}, "in_band": n, "steps": n}."""
    out = []
    for rep in replays:
        for k in range(1, 10):
            frac = k / 10.0
            t = step_time(rep, frac)
            row = estimate_at(rep.rows, t)
            band = band_total(row, rep.t0)
            pred = predicted_total(row, rep.t0)
            err = None if pred is None else 100.0 * (pred - rep.truth_min) / rep.truth_min
            inside = bool(band and band[0] <= rep.truth_min <= band[1])
            out.append({"job": rep.job, "step": k * 10, "t_min": round((t - rep.t0) / 60.0, 1),
                        "done": None if row is None else row["units_done"],
                        "U": None if row is None else row["units_total"],
                        "p10": None if row is None else row["p10_min"],
                        "p50": None if row is None else row["mid_min"],
                        "p90": None if row is None else row["p90_min"],
                        "source": None if row is None else row["source"],
                        "in_band": inside, "error_pct": err})
    means = {"p50": slice_means(replays), "in_band": sum(1 for r in out if r["in_band"]), "steps": len(out)}
    return out, means


def _f(x, nd=1):
    return "-" if x is None else f"{x:.{nd}f}"


def format_table(rows: list, means: dict) -> str:
    lines = [f"{'job':<9}{'step':>5}{'t min':>8}{'done':>6}{'U':>5}{'P10':>8}{'P50':>8}{'P90':>8}  "
             f"{'source':<12}{'in band':<8}{'error %':>8}"]
    for r in rows:
        lines.append(f"{r['job']:<9}{str(r['step']) + '%':>5}{r['t_min']:>8.1f}{str(r['done']):>6}{str(r['U']):>5}"
                     f"{_f(r['p10']):>8}{_f(r['p50']):>8}{_f(r['p90']):>8}  {str(r['source']):<12}"
                     f"{'yes' if r['in_band'] else 'NO':<8}{_f(r['error_pct']):>8}")
    lines.append("")
    lines.append("mean |P50 error| by slice (limit): " + ", ".join(
        f"{int(f * 100)}% {means['p50'][f]:.1f}% (<= {SLICE_LIMITS[f]:.0f})" for f in SLICES))
    lines.append(f"truth inside the P10-P90 band: {means['in_band']} of {means['steps']} steps "
                 f"(>= {BAND_MIN_HITS})")
    return "\n".join(lines)


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    loo = "--loo" in argv
    # A private home and no ambient Jev: the replay never touches the live Steward.
    os.environ["STEWARD_HOME"] = tempfile.mkdtemp(prefix="eta-replay-home-")
    for name in ("TYPESAFE_API_KEY", "STEWARD_JEV", "TYPESAFE_MODEL", "STEWARD_EFFICIENCY"):
        os.environ.pop(name, None)
    reps = []
    with tempfile.TemporaryDirectory(prefix="eta-replay-") as tmp:
        for job in JOBS:
            reps.append(run_eta_replay(job, Path(tmp) / job, priors=seed_minus(job) if loo else None))
        rows, means = table(reps)
        print(("leave-one-out priors (the seed without the replayed job)" if loo
               else "shipped seed priors (contains these three jobs)"))
        print(format_table(rows, means))
        print()
        for rep in reps:
            errs = []
            for f in SLICES:
                e, _r = p50_error(rep, f)
                errs.append(f"{int(f * 100)}% {'none' if e is None else f'{e:+.1f}%'}")
            print(f"{rep.job}: signed P50 error " + ", ".join(errs) + f"; wall {rep.wall_s:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
