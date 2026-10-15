"""Steward's own read-only stew.* tools are routine; every write/outbound one stays gated."""
from __future__ import annotations

from steward import permissions
from steward.mcp_server import build_tools


def test_readonly_list_is_a_subset_of_registered_readonly_tools():
    tools = {t.name: t for t in build_tools(None)}
    for name in permissions.STEW_READONLY_TOOLS:
        assert tools[name].annotations.get("readOnlyHint") is True, name


def test_every_registered_readonly_tool_is_routine_and_no_write_tool_is():
    for t in build_tools(None):
        got = permissions.stew_readonly_tool(f"stew.{t.name}")
        assert (got is not None) == bool(t.annotations.get("readOnlyHint")), t.name


def test_both_spellings_and_garbage():
    assert permissions.stew_readonly_tool("stew.get_recent_events") == "get_recent_events"
    assert permissions.stew_readonly_tool("mcp__stew__explain_blocker") == "explain_blocker"
    for bad in ("stew.steer_job", "stew.approve_action", "stew.launch_job", "other.get_job", "stew.get_job; rm", "", "Bash"):
        assert permissions.stew_readonly_tool(bad) is None, bad
