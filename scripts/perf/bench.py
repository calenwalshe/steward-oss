"""Steward TUI/control perf bench (docs/perf/2026-10-05-tui-speed-plan.md). Run ONLY through scripts/perf/sandbox.sh,
which points STEWARD_HOME at the DB copy in .perf/home and cuts the live system off (no network, no Herdr socket,
read-only filesystem outside this worktree).

  scripts/perf/sandbox.sh python scripts/perf/bench.py status  [--n 3] [--prof out.prof]
  scripts/perf/sandbox.sh python scripts/perf/bench.py tokens  [--n 3]
  scripts/perf/sandbox.sh python scripts/perf/bench.py frame   [--n 3] [--prof out.prof]   # one dash_jobs frame, health off
  scripts/perf/sandbox.sh python scripts/perf/bench.py tick    [--n 3] [--prof out.prof]   # one control-pane tick
  scripts/perf/sandbox.sh python scripts/perf/bench.py resident [--n 12]                    # resident dashapp refresh passes, 1 s apart
  scripts/perf/sandbox.sh python scripts/perf/bench.py sweep   [--n 3] [--prof out.prof]   # a tick that runs the 30 s delivery sweep
  scripts/perf/sandbox.sh python scripts/perf/bench.py steps   [--n 3]   # a sweep tick: its longest step (what a ping can wait for)
  scripts/perf/sandbox.sh python scripts/perf/bench.py dashapp [--secs 30]   # the resident pane's own threads: CPU share
  scripts/perf/sandbox.sh python scripts/perf/bench.py permission [--n 5] --job <prefix>   # one auto-allowed call

Times are per call, in-process, after imports (wall and process CPU)."""
from __future__ import annotations

import argparse
import contextlib
import cProfile
import io
import os
import sys
import time

if "/.perf/" not in os.environ.get("STEWARD_HOME", ""):
    sys.exit("refusing: run through scripts/perf/sandbox.sh (STEWARD_HOME must be a .perf/ copy)")


def _quiet(fn):
    def run():
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                fn()
            except SystemExit:
                pass
    return run


def status_fn(days):
    from steward import cli
    return _quiet(lambda: cli.main(["status", "--json", "--recent", str(days)]))


def tokens_fn():
    from steward import cli
    return _quiet(lambda: cli.main(["tokens", "--json"]))


def frame_fn():
    os.environ["STEWARD_HEALTH"] = "0"  # the health ping needs the live control pane; measured separately
    from steward import dashrender

    def run():
        dashrender.Pane(80, 0, None).render(refresh=True)  # a fresh Pane each time, like one steward-dash.sh frame
    return run


def tick_fn():
    """The exact tick `steward control` runs every 5 s, built by `_cmd_control` itself: its run_forever is swapped for
    one that hands the tick back, and the Herdr side is a dead socket (the sandbox has none)."""
    from steward import cli, control_pane

    captured = {}

    class _Mailbox:
        def close(self):
            pass

    def _capture(self, mailbox, herdr_api, *, tick=None, **kw):
        captured["tick"] = tick

    cli.Mailbox = _Mailbox
    control_pane.ControlPane.run_forever = _capture
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["control", "--sock", "/tmp/no-herdr.sock", "--pane", "w0:p0"])
    run = _quiet(captured["tick"])
    run.raw = captured["tick"]
    return run


def resident_fn():
    """What the resident pane (steward.dashapp) does every second: one long-lived Pane, `render(refresh=True)`, so
    only data past its ttl is refetched (jobs 1 s, tokens 10 s, slow 5 s). Passes are spaced 1 s apart."""
    os.environ["STEWARD_HEALTH"] = "0"
    from steward import dashrender

    pane = dashrender.Pane(80, 0, None)
    state = {"first": True}

    def run():
        if not state["first"]:
            time.sleep(1.0)  # the pane's cadence; main() subtracts it from the wall time
        state["first"] = False
        pane.render(refresh=True)
    run.sleeps = state
    return run


def sweep_fn():
    """A control tick on which the delivery sweep is due (it runs every 30 s): its cadence stamp is cleared first."""
    from steward.store import Store

    tick = tick_fn()
    store = Store()

    def run():
        with store._conn:
            store._conn.execute("DELETE FROM kv WHERE job_id = '_delivery' AND key = 'delivery_sweep_at'")
        tick()
    return run


def steps_fn():
    """A sweep tick, step by step: (longest step, whole tick). A ping is answered between steps when the tick is a
    TickSteps (plan item 12), so it waits for the longest step; a plain callable tick makes it wait for all of it."""
    from steward.store import Store

    raw = tick_fn().raw
    steps = getattr(raw, "steps", None) or (raw,)
    store = Store()
    stats = {"longest": [], "total": []}

    def run():
        with store._conn:
            store._conn.execute("DELETE FROM kv WHERE job_id = '_delivery' AND key = 'delivery_sweep_at'")
        longest, t0 = 0.0, time.perf_counter()
        for step in steps:
            w = time.perf_counter()
            _quiet(step)()
            longest = max(longest, time.perf_counter() - w)
        stats["longest"].append(longest * 1000)
        stats["total"].append((time.perf_counter() - t0) * 1000)
    run.stats = stats
    return run


def dashapp_cpu(secs: float) -> None:
    """The resident pane's refresh + probe threads (no terminal, services stubbed) for `secs`: process CPU share."""
    os.environ["STEWARD_HEALTH"] = "0"
    from steward import dashapp

    kw = dict(cols=80, rows=0, services=lambda: {"steward-mcp": "active"}, public=lambda: "-")
    try:
        app = dashapp.App(os.open(os.devnull, os.O_RDONLY), os.open(os.devnull, os.O_WRONLY),
                          db_version=dashapp.data_version_probe(), **kw)
        gate = "data_version gate"
    except (TypeError, AttributeError):
        app = dashapp.App(os.open(os.devnull, os.O_RDONLY), os.open(os.devnull, os.O_WRONLY), **kw)
        gate = "no gate"
    app.start_threads()
    app.first_data.wait(60)
    c0, w0 = time.process_time(), time.perf_counter()
    time.sleep(secs)
    cpu, wall = time.process_time() - c0, time.perf_counter() - w0
    app.stop.set()
    print(f"dashapp ({gate}): {100 * cpu / wall:.1f}% of a core over {wall:.0f} s (no commits landing)")


def permission_fn(prefix: str):
    """One auto-allowed worker permission through the runner, as the fabric delivers it: request frame -> code
    decision -> relayed -> shim ack. Reports events written and WAL frames per call (the first call warms up)."""
    from steward import approvals
    from steward.diagnosis import Diagnosis, FakeDiagnoser
    from steward.runner import Runner
    from steward.runner_exec import FakeRunner
    from steward.store import Store

    store = Store()
    store._conn.execute("PRAGMA wal_autocheckpoint=0")
    jid = store._conn.execute("SELECT job_id FROM jobs WHERE job_id LIKE ?", (prefix + "%",)).fetchone()[0]
    runner = Runner(store, lambda *_: None, FakeRunner(), FakeDiagnoser(Diagnosis("needs_human", "", "")))
    runner._notify = lambda *a: None
    wal = os.path.join(os.environ["STEWARD_HOME"], "steward.db-wal")
    n = {"i": 0}
    out = {"events": [], "frames": []}

    def call():
        n["i"] += 1
        rid = f"perf-{os.getpid()}-{n['i']}"
        frame = type("F", (), {"seq": n["i"], "body": {"request_id": rid, "tool": "Bash",
                                                       "input": {"command": "ls"}}})()
        store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = store._conn.execute("SELECT COUNT(*) FROM events WHERE job_id = ?", (jid,)).fetchone()[0]
        runner._open_fabric_permission(jid, frame)
        rec = approvals.by_request_id(store, jid, rid)
        approvals.mark_relayed(store, jid, rec["id"], ack_expected=True, relayed_by="perf")
        approvals.mark_delivery_acked(store, jid, rec["id"])
        out["events"].append(store._conn.execute("SELECT COUNT(*) FROM events WHERE job_id = ?",
                                                 (jid,)).fetchone()[0] - before)
        out["frames"].append((os.path.getsize(wal) - 32) // (4096 + 24))
    call()  # warm-up (on this tree: the job's legacy approvals list moves to the table here)
    out["events"].clear(), out["frames"].clear()
    call.out = out
    return call


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=("status", "tokens", "frame", "tick", "sweep", "resident", "steps", "dashapp",
                                     "permission"))
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--secs", type=float, default=30)
    ap.add_argument("--job", default="f98915b9")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--prof")
    a = ap.parse_args()
    if a.what == "dashapp":
        dashapp_cpu(a.secs)
        return 0
    t0 = time.perf_counter()
    fn = {"status": lambda: status_fn(a.days), "tokens": tokens_fn, "frame": frame_fn, "tick": tick_fn,
          "sweep": sweep_fn, "resident": resident_fn, "steps": steps_fn,
          "permission": lambda: permission_fn(a.job)}[a.what]()
    print(f"setup {time.perf_counter() - t0:.2f}s")
    walls, cpus = [], []
    prof = cProfile.Profile() if a.prof else None
    for i in range(a.n):
        w, c = time.perf_counter(), time.process_time()
        if prof and i == a.n - 1:
            prof.enable()
        fn()
        if prof and i == a.n - 1:
            prof.disable()
        walls.append(time.perf_counter() - w - (1.0 if a.what == "resident" and i else 0.0))
        cpus.append(time.process_time() - c)
    if prof:
        prof.dump_stats(a.prof)
    if a.what == "steps":
        st = fn.stats
        print("steps: longest step " + " ".join(f"{x:.0f}" for x in st["longest"]) + " ms; whole tick "
              + " ".join(f"{x:.0f}" for x in st["total"]) + " ms")
    if a.what == "permission":
        print(f"permission ({a.job}): events per call {fn.out['events']}; WAL frames per call {fn.out['frames']}")
    print(f"{a.what}: wall " + " ".join(f"{x * 1000:.0f}" for x in walls) + " ms; cpu "
          + " ".join(f"{x * 1000:.0f}" for x in cpus) + " ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
