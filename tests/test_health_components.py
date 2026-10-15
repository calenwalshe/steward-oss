"""Phase 19-06 (G10): the `mcp` health component follows the enabled components; no install.json keeps the legacy check."""

from __future__ import annotations

import pytest

from steward import components, health, installstate
from steward.store import Store
from test_health import FakeHerdr, Time, _by, _pane
from test_health import _check as legacy_check


def _state_with(home, **comps):
    state = installstate.new_state("0.1.0", "/pkg", "3.12.3")
    for seq, (name, info) in enumerate(comps.items(), start=1):
        installstate.record_component(state, name, enabled=True, seq=seq, **info)
    installstate.save(state, home)


def _check(store, panes=None, units=lambda u: True):
    tm = Time()
    api = FakeHerdr(store, panes=panes) if panes is not None else FakeHerdr(store)
    return _by(health.check_all(store, api=api, ping=False, unit_active=units, pid_alive=lambda p: True,
                                sleep=tm.sleep, clock=tm.clock, trajectory_on=False))["mcp"]


def test_legacy_units_without_install_json(steward_home):
    store = Store(steward_home)
    asked = []
    c = _check(store, units=lambda u: asked.append(u) or True)
    assert asked == list(health.UNITS) == ["steward-mcp", "steward-tunnel", "steward-mcp-tunnel-local"]
    assert c.ok is True and c.detail == "steward-mcp and tunnels active"
    c = _check(store, units=lambda u: u != "steward-tunnel")
    assert c.ok is False and c.problems == ["mcp: steward-tunnel is not active"]


def test_nothing_enabled_is_off_and_never_alerts(steward_home):
    store = Store(steward_home)
    _state_with(steward_home)
    asked = []
    c = _check(store, units=lambda u: asked.append(u) or False)
    assert (c.ok, c.detail, c.problems) == (None, "off (not enabled)", [])
    assert asked == []  # nothing is expected, so nothing is asked
    state = health.update_state({}, [c], now=1000.0)
    assert health.red_for(state, now=10_000.0) == []


def test_units_follow_the_components(steward_home):
    store = Store(steward_home)
    _state_with(steward_home, mcp={"mode": "systemd", "units": ["steward-mcp"], "targets": ["mcp"]},
                moshi={"tab": "x"})
    asked = []
    c = _check(store, units=lambda u: asked.append(u) or True)
    assert asked == ["steward-mcp"] and c.ok is True and "steward-mcp" in c.detail
    c = _check(store, units=lambda u: False)
    assert c.ok is False and c.problems == ["mcp: steward-mcp is not active"]


def test_several_components_expect_all_their_units(steward_home):
    store = Store(steward_home)
    _state_with(steward_home, mcp={"mode": "systemd", "units": ["steward-mcp"]},
                tunnel={"mode": "systemd", "units": ["steward-tunnel"]},
                **{"openai-tunnel": {"mode": "systemd", "units": ["steward-mcp-tunnel-local", "steward-openai-tunnel"]}})
    asked = []
    _check(store, units=lambda u: asked.append(u) or True)
    assert asked == ["steward-mcp", "steward-tunnel", "steward-mcp-tunnel-local", "steward-openai-tunnel"]


def test_a_disabled_component_is_not_expected(steward_home):
    store = Store(steward_home)
    state = installstate.new_state("0.1.0", "/pkg", "3.12.3")
    installstate.record_component(state, "mcp", enabled=False, mode="systemd", units=[])
    installstate.save(state, steward_home)
    assert _check(store).ok is None


def test_a_herdr_mode_component_is_checked_through_its_pane(steward_home):
    store = Store(steward_home)
    _state_with(steward_home, mcp={"mode": "herdr", "targets": ["mcp"], "units": []})
    asked = []
    up = [_pane("w1:p1", "control"), _pane("w1:p3", label="svc mcp")]
    c = _check(store, panes=up, units=lambda u: asked.append(u) or False)
    assert c.ok is True and asked == []
    c = _check(store, panes=[_pane("w1:p1", "control")])
    assert c.ok is False and c.problems == ["mcp: pane svc mcp is not running"]


def test_unreadable_install_json_falls_back_to_the_legacy_check(steward_home):
    store = Store(steward_home)
    (steward_home / "install.json").write_text("{not json")
    assert _check(store).detail == "steward-mcp and tunnels active"


def test_components_module_and_health_share_the_pane_label():
    assert components.pane_label("mcp") == "svc mcp"
