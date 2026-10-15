"""Spec 2026-10-13 §4.6: `steward metrics pause-review` prints the trial view (text or JSON)."""
from __future__ import annotations

import json

from steward import cli
from steward.models import Judgment
from steward.store import Store


def test_cli_prints_the_pause_review_view(steward_home, capsys):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    store.kv_set(job.job_id, "contract", {"goal": "g", "pause": "unit"})
    store.append_event(job.job_id, what="pause_verdict", evidence={"outcome": "done", "wait_s": 12.0,
                                                                  "provisional": False},
                       rule="pause_review", judgment=Judgment.model_informed_code_authorized)
    assert cli.main(["metrics", "pause-review", "--since", "2026-10-01", "--no-spend", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["arms"]["pause"]["reviews"] == 1 and out["arms"]["pause"]["usd_per_job"] is None
    assert cli.main(["metrics", "pause-review", "--no-spend"]) == 0
    assert "pause arm: 1 jobs" in capsys.readouterr().out
