#!/usr/bin/env python3
"""Pure-wait turns per job and steer -> ack latency (v2.7 Phase 42, SRD-15). Read-only: the store is opened with
sqlite's `mode=ro` URI, nothing is written, no Steward process is touched.

  python3 scripts/wait_metrics.py [--home DIR] [--job PREFIX ...] [--json]
         [--baseline-turns-per-hour N] [--baseline-p95-s S]

What is counted
  * pure-wait turns: every Bash/observer permission request a worker raised whose call `wait_policy.classify_call`
    labels `pure_wait` (a sleep/until loop, a Monitor/TaskOutput observer, `steward check wait`). Each is one model
    turn spent waiting. The shim reports the in-flight batch as kv `inflight_batch`, but that key holds only the
    LATEST batch, so history comes from the approvals table (one row per permission ask, carrying the call). Calls
    that never ask (pre-authorized by `--allowedTools`) are invisible to both sources, so the count is a lower bound
    on both sides of the comparison; `inflight_now` shows whether the batch in flight at read time is a pure wait.
    A row whose question text was truncated before the command could be parsed is counted under `unparsed`.
  * steer -> ack: per steering instruction (contract boundaries excluded), `acknowledged_at - received_at`.

SRD-15 verdict (needs a baseline): >= 80 % fewer pure-wait turns per job-hour than the baseline AND steer -> ack
p95 <= 600 s. Without a baseline the report only measures.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steward import wait_policy  # noqa: E402

P95_LIMIT_S = 600.0
REDUCTION_TARGET = 0.80
_QUESTION = re.compile(r"^worker permission: (\w+) (\{.*)$", re.S)


def default_home() -> Path:
    env = os.environ.get("STEWARD_HOME")
    if env:
        return Path(env)
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "steward"


def connect_ro(home: Path | str) -> sqlite3.Connection:
    path = Path(home) / "steward.db"
    if not path.is_file():
        raise FileNotFoundError(f"no store at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _kv(conn, job_id: str, key: str):
    row = conn.execute("SELECT value_json FROM kv WHERE job_id = ? AND key = ?", (job_id, key)).fetchone()
    return None if row is None else json.loads(row["value_json"])


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def pure_wait_turns(conn, job_id: str) -> dict:
    turns, unparsed, forms = 0, 0, {}
    for r in conn.execute("SELECT record_json FROM approvals WHERE job_id = ? AND kind = 'permission' ORDER BY rowid",
                          (job_id,)):
        try:
            question = str(json.loads(r["record_json"]).get("question") or "")
        except (ValueError, AttributeError):
            continue
        m = _QUESTION.match(question)
        if not m:
            continue
        tool = m.group(1)
        try:
            tool_input = ast.literal_eval(m.group(2))
        except (ValueError, SyntaxError):
            if tool in ("Bash", "Monitor", "BashOutput", "TaskOutput"):
                unparsed += 1
            continue
        if wait_policy.classify_call(tool, tool_input if isinstance(tool_input, dict) else {}) == "pure_wait":
            turns += 1
            label = (wait_policy.analyze(str(tool_input.get("command") or "")).waits or [tool])[0] \
                if tool == "Bash" else tool
            forms[label] = forms.get(label, 0) + 1
    batch = _kv(conn, job_id, "inflight_batch")
    return {"turns": turns, "unparsed": unparsed, "forms": forms, "inflight_now": wait_policy_all_pure(batch)}


def wait_policy_all_pure(batch) -> bool:
    from steward import inflight

    return inflight.all_pure_waits(batch if isinstance(batch, dict) else None)


def _ts(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def steer_latencies(conn, job_id: str) -> list[float]:
    out = []
    for i in _kv(conn, job_id, "instructions") or []:
        if i.get("origin") == "contract":
            continue
        a, b = _ts(i.get("received_at")), _ts(i.get("acknowledged_at"))
        if a is not None and b is not None and b >= a:
            out.append(b - a)
    return out


def span_hours(conn, job_id: str) -> float:
    row = conn.execute("SELECT MIN(ts) AS a, MAX(ts) AS b FROM events WHERE job_id = ?", (job_id,)).fetchone()
    a, b = _ts(row["a"]), _ts(row["b"])
    return 0.0 if a is None or b is None else max(0.0, (b - a) / 3600.0)


def job_ids(conn, prefixes: list[str] | None) -> list[str]:
    ids = [r["job_id"] for r in conn.execute("SELECT job_id FROM jobs ORDER BY created_at")]
    return [j for j in ids if not prefixes or any(j.startswith(p) for p in prefixes)]


def measure(conn, prefixes: list[str] | None = None) -> dict:
    jobs, all_lat = [], []
    for jid in job_ids(conn, prefixes):
        w, lat, hours = pure_wait_turns(conn, jid), steer_latencies(conn, jid), span_hours(conn, jid)
        all_lat += lat
        jobs.append({"job": jid, **w, "hours": round(hours, 3),
                     "turns_per_hour": round(w["turns"] / hours, 2) if hours > 0 else None,
                     "steers_acked": len(lat), "ack_p50_s": percentile(lat, 0.5), "ack_p95_s": percentile(lat, 0.95)})
    turns, hours = sum(j["turns"] for j in jobs), sum(j["hours"] for j in jobs)
    return {"jobs": jobs,
            "total": {"jobs": len(jobs), "turns": turns, "hours": round(hours, 3),
                      "turns_per_hour": round(turns / hours, 2) if hours > 0 else None,
                      "steers_acked": len(all_lat), "ack_p50_s": percentile(all_lat, 0.5),
                      "ack_p95_s": percentile(all_lat, 0.95)}}


def verdict(total: dict, *, baseline_turns_per_hour: float | None, baseline_p95_s: float | None = None) -> dict:
    """SRD-15: >= 80 % fewer pure-wait turns per job-hour than the baseline and steer->ack p95 <= 600 s."""
    out = {"reduction": None, "turns_ok": None, "p95_ok": None, "pass": None}
    cur = total.get("turns_per_hour")
    if baseline_turns_per_hour and cur is not None:
        out["reduction"] = round(1 - cur / baseline_turns_per_hour, 3)
        out["turns_ok"] = out["reduction"] >= REDUCTION_TARGET
    p95 = total.get("ack_p95_s")
    if p95 is not None:
        out["p95_ok"] = p95 <= P95_LIMIT_S
    if baseline_p95_s is not None:
        out["baseline_p95_s"] = baseline_p95_s
    checks = [v for v in (out["turns_ok"], out["p95_ok"]) if v is not None]
    out["pass"] = all(checks) if checks and out["turns_ok"] is not None else None
    return out


def render(report: dict, v: dict | None) -> str:
    lines = [f"{'job':10} {'turns':>6} {'hours':>7} {'turns/h':>8} {'acked':>5} {'p50 s':>8} {'p95 s':>8}  forms"]
    fmt = lambda x: "-" if x is None else f"{x:.1f}"  # noqa: E731
    for j in report["jobs"]:
        lines.append(f"{j['job'][:8]:10} {j['turns']:>6} {j['hours']:>7.2f} {fmt(j['turns_per_hour']):>8} "
                     f"{j['steers_acked']:>5} {fmt(j['ack_p50_s']):>8} {fmt(j['ack_p95_s']):>8}  "
                     f"{json.dumps(j['forms']) if j['forms'] else ''}"
                     f"{' unparsed=' + str(j['unparsed']) if j['unparsed'] else ''}")
    t = report["total"]
    lines.append(f"{'TOTAL':10} {t['turns']:>6} {t['hours']:>7.2f} {fmt(t['turns_per_hour']):>8} {t['steers_acked']:>5} "
                 f"{fmt(t['ack_p50_s']):>8} {fmt(t['ack_p95_s']):>8}")
    if v is not None:
        lines.append(f"SRD-15: reduction={v['reduction']} (target >= {REDUCTION_TARGET}) turns_ok={v['turns_ok']} "
                     f"p95_ok={v['p95_ok']} (limit {P95_LIMIT_S:g}s) pass={v['pass']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="pure-wait turns per job and steer->ack latency (read-only)")
    ap.add_argument("--home", default=None, help="STEWARD_HOME (default: $STEWARD_HOME or the XDG state dir)")
    ap.add_argument("--job", action="append", default=None, metavar="PREFIX")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--baseline-turns-per-hour", type=float, default=None)
    ap.add_argument("--baseline-p95-s", type=float, default=None)
    args = ap.parse_args(argv)
    try:
        conn = connect_ro(args.home or default_home())
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"wait_metrics: {exc}", file=sys.stderr)
        return 1
    report = measure(conn, args.job)
    v = None
    if args.baseline_turns_per_hour is not None or args.baseline_p95_s is not None:
        v = verdict(report["total"], baseline_turns_per_hour=args.baseline_turns_per_hour,
                    baseline_p95_s=args.baseline_p95_s)
    if args.json:
        print(json.dumps({**report, "verdict": v}, indent=2))
    else:
        print(render(report, v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
