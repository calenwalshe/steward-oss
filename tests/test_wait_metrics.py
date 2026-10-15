"""Phase 42 (SRD-15): scripts/wait_metrics.py counts pure-wait turns and steer->ack latency, read-only."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from steward import approvals as approvals_module
from steward.steering import Steering
from steward.store import Store

_SPEC = importlib.util.spec_from_file_location("wait_metrics", Path(__file__).resolve().parent.parent / "scripts"
                                               / "wait_metrics.py")
wm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wm)


def _ask(store, job_id, command, tool="Bash"):
    approvals_module.open_approval(store, job_id, f"worker permission: {tool} {{'command': {command!r}}}",
                                   kind="permission")


def _job(store):
    return store.create_job("g", "/repo").job_id


def test_counts_only_pure_wait_permission_asks(steward_home):
    store = Store(steward_home)
    jid = _job(store)
    for cmd in ("sleep 600", "end=$((SECONDS+585)); until [ -f X ] || [ $SECONDS -ge $end ]; do sleep 20; done",
                "steward check wait cr-0123456789ab", "git status", "rm -rf build", "sleep 5 && echo x > out"):
        _ask(store, jid, cmd)
    got = wm.pure_wait_turns(wm.connect_ro(steward_home), jid)
    assert got["turns"] == 3 and got["unparsed"] == 0
    assert got["forms"] == {"sleep": 1, "loop": 1, "check_wait": 1}


def test_a_truncated_question_is_counted_unparsed_not_guessed(steward_home):
    store = Store(steward_home)
    jid = _job(store)
    approvals_module.open_approval(store, jid, "worker permission: Bash {'command': 'sleep 6", kind="permission")
    got = wm.pure_wait_turns(wm.connect_ro(steward_home), jid)
    assert got["turns"] == 0 and got["unparsed"] == 1


def test_steer_ack_latency_excludes_contract_boundaries_and_unacked(steward_home):
    store = Store(steward_home)
    jid = _job(store)
    st = Steering(store)
    for n, (origin, ack) in enumerate([("cli", 30), ("check_run", 90), ("cli", None), ("contract", 1)]):
        st.record(jid, "nudge", f"x{n}", origin=origin)
    ins = store.kv_get(jid, "instructions")
    base = 1_700_000_000
    from datetime import datetime, timezone

    iso = lambda s: datetime.fromtimestamp(base + s, timezone.utc).isoformat()  # noqa: E731
    for i, (rec, ack) in zip(ins, [(0, 30), (0, 90), (0, None), (0, 1)]):
        i["received_at"], i["acknowledged_at"] = iso(rec), (iso(ack) if ack is not None else None)
    store.kv_set(jid, "instructions", ins)
    assert sorted(wm.steer_latencies(wm.connect_ro(steward_home), jid)) == [30.0, 90.0]


def test_percentile_is_nearest_rank():
    assert wm.percentile([], 0.95) is None
    assert wm.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5) == 5
    assert wm.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10


def test_verdict_needs_80_percent_fewer_turns_and_p95_under_ten_minutes():
    total = {"turns_per_hour": 60.0, "ack_p95_s": 300.0}
    assert wm.verdict(total, baseline_turns_per_hour=327.0)["pass"] is True          # -81.6 %
    assert wm.verdict({**total, "turns_per_hour": 90.0}, baseline_turns_per_hour=327.0)["turns_ok"] is False
    assert wm.verdict({**total, "ack_p95_s": 601.0}, baseline_turns_per_hour=327.0)["pass"] is False
    assert wm.verdict(total, baseline_turns_per_hour=None)["pass"] is None            # measures only


def test_the_store_is_opened_read_only(steward_home):
    Store(steward_home)
    conn = wm.connect_ro(steward_home)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE jobs SET state = 'STOPPED'")
    with pytest.raises(FileNotFoundError):
        wm.connect_ro(steward_home / "nope")


def test_main_prints_a_table_and_json(steward_home, capsys):
    store = Store(steward_home)
    jid = _job(store)
    _ask(store, jid, "sleep 300")
    assert wm.main(["--home", str(steward_home)]) == 0
    assert "TOTAL" in capsys.readouterr().out
    assert wm.main(["--home", str(steward_home), "--json", "--job", jid[:8], "--baseline-turns-per-hour", "100"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["total"]["turns"] == 1 and doc["jobs"][0]["job"] == jid and "verdict" in doc
    assert wm.main(["--home", str(steward_home / "missing")]) == 1
