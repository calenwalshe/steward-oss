"""Zero-touch Task 9: `steward health` reports `liveness` once the control tick's sweep has a heartbeat (spec §5.1).
Before the first sweep the row is absent, so the base component list (and its tests) are unchanged."""
from __future__ import annotations

from steward import health, liveness
from steward.store import Store
from test_health import FakeHerdr, _by, _check


def test_liveness_absent_until_the_sweep_runs_then_reported_last(steward_home):
    store = Store(steward_home)
    assert "liveness" not in _by(_check(store, FakeHerdr(store)))
    store.kv_set(liveness.PSEUDO, "tick_at", 0.5)  # the Time() clock in _check starts at 0: a fresh sweep
    comps = _by(_check(store, FakeHerdr(store)))
    assert [c.name for c in comps.values()] == ["control", "advisor", "herdr", "runners", "mcp", "budget", "liveness"]
    assert all(c.ok is True for n, c in comps.items() if n != "budget"), comps
    assert comps["budget"].ok is None


def test_liveness_red_when_the_sweep_is_stale(steward_home):
    store = Store(steward_home)
    store.kv_set(liveness.PSEUDO, "tick_at", -1000.0)  # > 300 s before the fake clock
    assert _by(_check(store, FakeHerdr(store)))["liveness"].ok is False


def test_liveness_is_alerted_not_watchdog_owned(tmp_path):
    store = Store(tmp_path)
    assert "liveness" in health.ALL_COMPONENTS and "liveness" not in health.WATCHDOG_OWNED
    comps = [health.Component(n, False, "down", [f"{n}: down"]) for n in health.ALL_COMPONENTS]
    sent = []
    health.tick(store, now=1000.0, ttl_s=0, check_fn=lambda: comps, heal_fn=None, alert_fn=sent.append)
    health.tick(store, now=2000.0, ttl_s=0, check_fn=lambda: comps, heal_fn=None, alert_fn=lambda t: sent.append(t) or True,
                alert_skip=health.WATCHDOG_OWNED)
    names = {t.split(" red for")[0].split()[-1] for t in sent}
    assert names == {"advisor", "mcp", "budget", "liveness"}


def test_header_renders_liveness(tmp_path):
    store = Store(tmp_path)
    comps = [health.Component(n, True, "ok") for n in health.ALL_COMPONENTS]
    state = health.update_state(health.load_state(store), comps, 1000.0)
    assert "liveness" in "\n".join(health.render_header(state, color=False))
