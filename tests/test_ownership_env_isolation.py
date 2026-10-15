"""The suite runs inside Steward workers and Claude Code sessions. Their identity variables must never leak into a
test: STEWARD_FORCED_ORIGIN relabels every CLI write (2 baseline failures in tests/test_send_back.py, verified
2026-10-05) and CLAUDECODE/CODEX_* would mark CLI writes as operator-agent actions (Plan 1, Task 3)."""
from __future__ import annotations

import os

import pytest

AMBIENT = ("STEWARD_FORCED_ORIGIN", "STEWARD_ACTOR", "STEWARD_OPERATOR", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
           "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_THREAD_ID", "STEWARD_OWNERSHIP")


def test_ambient_agent_identity_is_cleared():
    leaked = [k for k in AMBIENT if k in os.environ]
    assert leaked == []


@pytest.mark.legacy_ownership_off
def test_legacy_marker_switches_ownership_off():
    assert os.environ.get("STEWARD_OWNERSHIP") == "0"
