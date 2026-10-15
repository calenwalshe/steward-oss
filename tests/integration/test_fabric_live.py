"""FAB-05 gate: the fabric against a real Herdr session.

Marked `integration`; skipped by default (`addopts = -m "not integration"`).
Only ever runs against `steward-test-<pid>` sessions via the
`herdr_test_session` fixture (tests/conftest.py, tests/herdr_session.py) —
never the default/live session.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from steward.herdr_transport import LifecycleWatcher

pytestmark = pytest.mark.integration

REPO_ROOT = str(Path(__file__).resolve().parents[2])
PYTHON = sys.executable
FABRIC_CMD = f"{PYTHON} -m steward.fabric"


def _wait_for_json(path: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                return json.loads(Path(path).read_text())
            except (json.JSONDecodeError, OSError) as exc:
                last_error = exc
        time.sleep(0.2)
    raise TimeoutError(f"timed out waiting for {path}: {last_error}")


def _launch_echo(session, pane: str, peer: str, job: str) -> None:
    session.run_in_pane(pane, f"{FABRIC_CMD} --sock {session.socket_path} --peer {peer} --job {job} --echo")


def _launch_ping(session, pane: str, peer: str, job: str, n: int, sizes: str, out_file: str, timeout_s: float = 3.0) -> None:
    session.run_in_pane(
        pane,
        f"{FABRIC_CMD} --sock {session.socket_path} --peer {peer} --job {job} --ping "
        f"--n {n} --sizes {sizes} --timeout {timeout_s} --out {out_file}",
    )


# ---------------------------------------------------------------- (a) 200 round trips


def test_200_round_trips_incl_8kb_zero_loss_dup_with_noise(herdr_test_session, tmp_path):
    session = herdr_test_session
    echo_pane = session.create_pane(cwd=REPO_ROOT)
    ping_pane = session.create_pane(cwd=REPO_ROOT)
    out_file = str(tmp_path / "gate_result.json")

    _launch_echo(session, echo_pane, ping_pane, job="gate")
    time.sleep(0.5)  # let the echo process finish importing/starting before the first frame arrives
    _launch_ping(session, ping_pane, echo_pane, job="gate", n=200, sizes="64,8192,1024,3", out_file=out_file)

    for i in range(15):  # operator noise injected into the echo pane mid-run (09-SPIKE Q3)
        time.sleep(0.02)
        session.send_text(echo_pane, f"operator noise line {i}")

    result = _wait_for_json(out_file, timeout=60.0)
    assert result["n"] == 200
    assert result["ok"] == 200
    assert result["lost"] == []
    assert result["dup"] == 0
    assert result["corrupt"] == 0


# ---------------------------------------------------------------- (b) pane.exited


def test_lifecycle_watcher_sees_pane_exited(herdr_test_session):
    session = herdr_test_session
    pane = session.create_pane(cwd=REPO_ROOT)
    watcher = LifecycleWatcher(session.api)
    try:
        session.run_in_pane(pane, "exit")
        events = []
        deadline = time.time() + 10.0
        while time.time() < deadline:
            events.extend(watcher.poll(timeout=1.0))
            if any(e.kind == "pane_exited" and e.pane_id == pane for e in events):
                break
        assert any(e.kind == "pane_exited" and e.pane_id == pane for e in events), events
    finally:
        watcher.close()


# ---------------------------------------------------------------- (c) restart survival


def test_session_restart_preserves_panes_and_endpoints_relaunch(herdr_test_session, tmp_path):
    session = herdr_test_session
    echo_pane = session.create_pane(cwd=REPO_ROOT)
    ping_pane = session.create_pane(cwd=REPO_ROOT)
    watcher = LifecycleWatcher(session.api)
    try:
        panes_before = {p["pane_id"] for p in session.api.call("pane.list", {})["panes"]}

        session.stop()
        session.start()

        events = watcher.poll(timeout=5.0)
        assert any(e.kind == "reconnected" for e in events), events

        panes_after = {p["pane_id"] for p in session.api.call("pane.list", {})["panes"]}
        assert panes_after == panes_before
        assert {echo_pane, ping_pane} <= panes_after

        # each pane holds a fresh shell after restart (09-SPIKE Q2) — relaunch both endpoints
        out_file = str(tmp_path / "restart_result.json")
        _launch_echo(session, echo_pane, ping_pane, job="gate2")
        time.sleep(0.5)
        _launch_ping(session, ping_pane, echo_pane, job="gate2", n=30, sizes="64,8192", out_file=out_file)

        result = _wait_for_json(out_file, timeout=30.0)
        assert result["ok"] == 30
        assert result["lost"] == []
        assert result["dup"] == 0
    finally:
        watcher.close()
