"""Event and table volume on a DB copy, and what plan items 10a-10e change about it (docs/perf/2026-10-05-tui-speed-plan.md).
Read-only except `--compact` (which runs Store.compact_checks on the copy). Run through scripts/perf/sandbox.sh.

  scripts/perf/sandbox.sh python scripts/perf/event_volume.py [--compact]

* 10b: every auto-allowed chain (6 events) becomes 1: counted from the snapshot's `permission_auto_allowed` events.
* 10c: a `heartbeat class changed` whose class lasted under 2 polls (< 2 x 15 s until the next change) is not logged;
  an `inner_verdict` continue/no_recovery_needed following another continue is not logged. From the timestamps.
* 10d: a repeat `tech_lead_escalated` with the same (kind, approval_id) as an earlier one of the job.
* 10e: checks.results_json bytes, full form vs the compact form.
"""
import json
import os
import sys
from collections import Counter
from datetime import datetime

assert "/.perf/" in os.environ.get("STEWARD_HOME", ""), "run through scripts/perf/sandbox.sh"
from steward.store import Store, compact_results  # noqa: E402

POLL_S = 15.0
store = Store()
c = store._conn
total = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
whats = Counter(dict(c.execute("SELECT what, COUNT(*) FROM events GROUP BY what").fetchall()))
auto = whats["permission_auto_allowed"]
saved_b = 5 * auto  # the five other events of each auto-allowed chain


def _ts(s):
    return datetime.fromisoformat(s).timestamp()


saved_c_hb = saved_c_iv = 0
for (jid,) in c.execute("SELECT DISTINCT job_id FROM events WHERE what = 'heartbeat class changed'").fetchall():
    rows = c.execute("SELECT ts, evidence FROM events WHERE job_id = ? AND what = 'heartbeat class changed' "
                     "ORDER BY seq", (jid,)).fetchall()
    for (ts, ev), (nxt, _) in zip(rows, rows[1:]):
        to = json.loads(ev).get("to")
        if to not in ("suspect_stall", "dead") and _ts(nxt) - _ts(ts) < 2 * POLL_S:
            saved_c_hb += 1
    prev = None
    for (ev,) in c.execute("SELECT evidence FROM events WHERE job_id = ? AND what = 'inner_verdict' ORDER BY seq",
                           (jid,)).fetchall():
        e = json.loads(ev)
        if e.get("action") == "continue" and e.get("reason") == "no_recovery_needed" and prev == "continue":
            saved_c_iv += 1
        prev = e.get("action")

seen, saved_d = set(), 0
for jid, ev in c.execute("SELECT job_id, evidence FROM events WHERE what = 'tech_lead_escalated' ORDER BY seq"):
    e = json.loads(ev)
    key = (jid, e.get("kind"), e.get("approval_id"))
    if e.get("approval_id") and key in seen:
        saved_d += 1
    seen.add(key)

print(f"events: {total}; auto-allowed chains {auto} -> 10b saves {saved_b} ({100 * saved_b / total:.0f}%)")
print(f"10c: heartbeat changes logged {whats['heartbeat class changed']}, under 2 polls {saved_c_hb}; "
      f"inner_verdict {whats['inner_verdict']}, continue->continue {saved_c_iv}; "
      f"saves {saved_c_hb + saved_c_iv} ({100 * (saved_c_hb + saved_c_iv) / total:.0f}%)")
print(f"10d: tech_lead_escalated {whats['tech_lead_escalated']}, same (kind, approval) repeats {saved_d} "
      f"(the legacy-path repeats were already folded by e6fd957 into tech_lead_escalation_appended: "
      f"{whats['tech_lead_escalation_appended']})")

rows = c.execute("SELECT results_json FROM checks").fetchall()
full = sum(len(r[0] or "") for r in rows)
compact = sum(len(json.dumps(compact_results(json.loads(r[0])))) if r[0] and r[0] not in ("{}", "null") else len(r[0] or "")
              for r in rows)
print(f"10e: checks.results_json {len(rows)} rows, {full / 1e6:.1f} MB as stored -> {compact / 1e6:.1f} MB compact")
blob = c.execute("SELECT COALESCE(SUM(length(value_json)), 0), COUNT(*) FROM kv WHERE key = 'approvals'").fetchone()
rows_a = c.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] if c.execute(
    "SELECT 1 FROM sqlite_master WHERE name = 'approvals'").fetchone() else 0
print(f"10a: kv['approvals'] lists {blob[1]} ({blob[0] / 1e6:.1f} MB); approvals table rows {rows_a}")
if "--compact" in sys.argv:
    n = store.compact_checks()
    after = sum(len(r[0] or "") for r in c.execute("SELECT results_json FROM checks").fetchall())
    print(f"compact_checks rewrote {n} rows: {full / 1e6:.1f} MB -> {after / 1e6:.1f} MB")
