"""Cost of `herdr_tabs.refresh` (part of every control tick) with a Herdr that answers: the sandbox has no socket, so
bench.py's tick measures refresh's early bail-out only. Tabs = every job tab recorded and not yet closed, plus the
pinned ones. Run through scripts/perf/sandbox.sh."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from herdr_fake import FakeHerdr  # noqa: E402

from steward import herdr_tabs  # noqa: E402
from steward.store import Store  # noqa: E402

assert "/.perf/" in os.environ["STEWARD_HOME"]
store = Store()
open_tabs = [(j.job_id, store.kv_get(j.job_id, "herdr_tab")) for j in store.list_jobs()
             if store.kv_get(j.job_id, "herdr_tab") and not store.kv_get(j.job_id, "herdr_tab_closed")]
tabs = [{"tab_id": "w4Q:t1", "label": "ops"}, {"tab_id": "w4Q:t2", "label": "steward"}]
tabs += [{"tab_id": t, "label": f"job {jid[:8]}"} for jid, t in open_tabs]
panes = [{"pane_id": "w4Q:p1", "tab_id": "w4Q:t1", "label": "operator"},
         {"pane_id": "w4Q:p2", "tab_id": "w4Q:t2", "label": "steward control"}]
fake = FakeHerdr(tabs, panes)
for i in range(4):
    w, c = time.perf_counter(), time.process_time()
    herdr_tabs.refresh(store, fake, fake)
    print(f"refresh with {len(open_tabs)} open job tabs: wall {1000 * (time.perf_counter() - w):.0f} ms, "
          f"cpu {1000 * (time.process_time() - c):.0f} ms")
