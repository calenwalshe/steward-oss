"""Clean-install finding 2026-10-14: `steward init` wrote no roster.toml, so a fresh install had no tech lead
(`tech_lead_unwired`) and a pause-review job waited for ever. init now ships a Claude-only roster."""
from __future__ import annotations

from steward import templates_io
from steward.fleet_registry import load_roster


def test_init_templates_include_a_roster():
    assert "roster.toml" in templates_io.NAMES


def test_the_default_roster_is_claude_only_with_a_worker_and_a_tech_lead():
    roster = load_roster(templates_io.TEMPLATE_DIR / "roster.toml")
    enabled = [e for e in roster.entries if e.enabled]
    assert {e.provider for e in enabled} == {"anthropic"}
    assert any("worker" in e.roles for e in enabled)
    assert any("tech_lead" in e.roles for e in enabled)
