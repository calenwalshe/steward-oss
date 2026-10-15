"""WAL frames written by one auto-allowed worker permission (open -> decide -> relayed -> acked), per job size.
Run through scripts/perf/sandbox.sh against a scratch copy: STEWARD_HOME=.perf/walhome."""
import os
import sqlite3
import sys

from steward import approvals
from steward.store import Store

home = os.environ["STEWARD_HOME"]
assert "/.perf/" in home, home
store = Store()
store._conn.execute("PRAGMA wal_autocheckpoint=0")
for prefix in sys.argv[1:]:
    jid = store._conn.execute("SELECT job_id FROM jobs WHERE job_id LIKE ?", (prefix + "%",)).fetchone()[0]
    size = len(store._conn.execute("SELECT value_json FROM kv WHERE job_id=? AND key='approvals'", (jid,)).fetchone()[0])
    store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    rec = approvals.open_approval(store, jid, "worker permission: Bash {'command': 'ls'}")
    approvals.decide(store, jid, rec["id"], "approve", decided_by="code")
    approvals.mark_relayed(store, jid, rec["id"])
    approvals.mark_delivery_acked(store, jid, rec["id"])
    frames = (os.path.getsize(os.path.join(home, "steward.db-wal")) - 32) // (4096 + 24)
    print(f"{jid[:8]}: approvals blob {size // 1024} KB -> {frames} WAL frames ({frames * 4 // 1024} MB) per tool call")
