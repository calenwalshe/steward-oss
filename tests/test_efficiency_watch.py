"""The efficiency watcher (16-04): the runner step, state, findings, spend guard and the `steward efficiency` CLI.
Synthetic transcripts and fakes only: no network, no real herdr or claude."""
from __future__ import annotations

import json
import pytest

from efficiency_synth import T0, agent_lines, write_subagent
from test_stability import Clock, ScriptedRunner, _add_watcher, _events, _runner, _tokens
from test_runner import _contracted_job

from steward import cli, efficiency_watch as ew, stability
from steward import custom_watchers as cw
from steward.chat import project_slug, worktree_path
from steward.store import Store
from steward.worker import WorkerObservation

NOW = T0 + 1000.0


def _job(steward_home, tmp_path):
    """(store, job, projects_dir, slug_dir): a contracted job whose worker session id is "w"."""
    store = Store(steward_home)
    job = _contracted_job(store)
    proj = tmp_path / "projects"
    d = proj / project_slug(worktree_path(store, job.job_id))
    d.mkdir(parents=True)
    store.kv_set(job.job_id, "worker_session_id", "w")
    return store, job, proj, d


def _long(d, agent_id="e1", stage="gsd-executor", desc="Execute phase 1", t0=T0, calls=81, **kw):
    kw.setdefault("finished", False)
    kw.setdefault("ctx0", 100_000)
    kw.setdefault("gen_s", 5)
    kw.setdefault("tool_s", 1)
    write_subagent(d, "w", agent_id, stage, desc, agent_lines(t0, calls=calls, prefix=agent_id, **kw))


def _watcher(store, proj, clock, spend_clock, **kw):
    kw.setdefault("tokens_fn", lambda st, jid, now=None: {"sources": {}, "last_hour": {"usd": 0.0}})
    return ew.EfficiencyWatcher(store, clock=clock, spend_clock=spend_clock, projects_dir=proj, **kw)


def _running_runner(store, job, watcher):
    runner = _runner(store, ScriptedRunner(), [[WorkerObservation("working", "x", True, "t")] * 8],
                     clock=Clock(), efficiency=watcher)
    runner.start(job.job_id)
    return runner


# -- the tracer: one tick, state and finding ---------------------------------------------------------------


def test_tracer_tick_records_state_and_finding(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    store.bump_revision(job.job_id)
    store.bump_revision(job.job_id)
    _long(d)
    spend = Clock(1000.0)
    runner = _running_runner(store, job, _watcher(store, proj, lambda: NOW, spend))
    runner.tick(job.job_id)
    state = store.kv_get(job.job_id, ew.STATE_KEY)
    assert state["mode"] == "shadow" and state["computed_at"] == NOW and state["codex"] is False
    assert state["active"] == ["long_lived_agent:agent-e1"]
    assert state["summary"]["calls"] == 81 and state["summary"]["peak_ctx"] == 100_000
    [ev] = _events(store, job, "efficiency_finding")
    assert ev.evidence["at"] == NOW and ev.evidence["calls"] == 81 and ev.evidence["epoch"] == 2
    assert ev.evidence["id"] == "long_lived_agent:agent-e1" and ev.judgment.value == "code"
    runner.tick(job.job_id)  # same minute: nothing recomputed, no duplicate
    assert len(_events(store, job, "efficiency_finding")) == 1
    assert store.kv_get(job.job_id, ew.STATE_KEY)["computed_at"] == NOW


def _finish_e1(d, later_calls=82):
    """Append the tool_result and the final end_turn call to e1 (the unfinished file is a byte prefix)."""
    write_subagent(d, "w", "e1", "gsd-executor", "Execute phase 1",
                   agent_lines(T0, calls=later_calls, prefix="e1", finished=True, ctx0=100_000, gen_s=5, tool_s=1))


def test_finding_cleared_when_it_leaves_the_active_set(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    _long(d)
    spend, clock = Clock(1000.0), Clock(NOW)
    runner = _running_runner(store, job, _watcher(store, proj, clock, spend))
    runner.tick(job.job_id)
    assert len(_events(store, job, "efficiency_finding")) == 1
    _finish_e1(d)
    write_subagent(d, "w", "e2", "gsd-executor", "Execute phase 2",
                   agent_lines(T0 + 2000, calls=3, prefix="e2", finished=False))
    clock.t += 5000
    spend.t += 61
    runner.tick(job.job_id)
    [cleared] = _events(store, job, "efficiency_finding_cleared")
    assert cleared.evidence["id"] == "long_lived_agent:agent-e1" and cleared.evidence["at"] == clock.t
    assert store.kv_get(job.job_id, ew.STATE_KEY)["active"] == []
    spend.t += 61
    runner.tick(job.job_id)
    assert len(_events(store, job, "efficiency_finding_cleared")) == 1


def test_recompute_on_subagent_finished_inside_the_minute(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    _long(d)
    spend, clock = Clock(1000.0), Clock(NOW)
    runner = _running_runner(store, job, _watcher(store, proj, clock, spend))
    runner.tick(job.job_id)
    clock.t += 10
    spend.t += 10  # well inside the minute
    runner.tick(job.job_id)
    assert store.kv_get(job.job_id, ew.STATE_KEY)["computed_at"] == NOW  # nothing new: no recompute
    _finish_e1(d)
    clock.t += 10
    spend.t += 10
    runner.tick(job.job_id)
    state = store.kv_get(job.job_id, ew.STATE_KEY)
    assert state["computed_at"] == clock.t and "agent-e1" in state["finished"]


# -- the absorbed spend guard ----------------------------------------------------------------------------------


@pytest.mark.legacy_ownership_off
def test_spend_guard_absorbed_same_events(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    _add_watcher(store, job)
    asked = []
    spend = Clock(1000.0)
    calls = []
    w = _watcher(store, proj, lambda: NOW, spend, notify=lambda jid, kind, title, body: asked.append((kind, body)),
                 tokens_fn=lambda st, jid, now=None: calls.append(now) or _tokens(hour=6.0))
    w.step(job.job_id)
    w.step(job.job_id)  # same minute: the token data is read once
    assert calls == [NOW]
    assert cw.list_watchers(store, job.job_id)[0]["every_s"] == 1200
    [over] = _events(store, job, "watcher_overspend")
    assert over.evidence["ratio"] == 3.0 and over.rule == "spend_shape"
    assert len(_events(store, job, "watcher_cadence_halved")) == 1
    assert len(_events(store, job, "spend_rate_high")) == 1 and asked[0][0] == "NEEDS_YOU"
    kinds = {f["kind"]: f["evidence"] for f in store.kv_get(job.job_id, ew.STATE_KEY)["findings"]}
    assert kinds["watcher_overspend"] == "drift costs 3.0× worker" and kinds["spend_rate"] == "spend $6.00/h"
    # thin wrappers in stability give the same answers
    assert stability.spend_findings(_tokens()) == ew.spend_shape_findings(_tokens())
    assert stability.spend_warnings(_tokens(hour=6.2)) == ["⚠ drift costs 3.0× worker", "⚠ spend $6.20/h"]


def test_spend_check_error_keeps_the_event_and_job(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("no transcripts")

    w = _watcher(store, proj, lambda: NOW, Clock(1000.0), tokens_fn=boom)
    w.step(job.job_id)
    [ev] = _events(store, job, "spend_check_error")
    assert ev.evidence == {"error": "no transcripts"} and ev.rule == "spend_shape"


# -- cost cap and failure isolation ------------------------------------------------------------------------------


def test_slow_compute_records_efficiency_slow_once_per_10_min(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    _long(d)
    spend = Clock(1000.0)
    ticks = iter([0.0, 1.5] * 20)
    w = _watcher(store, proj, lambda: NOW, spend, cpu_fn=lambda: next(ticks))
    # cpu_fn is read at the start and end of every non-off step: pairs (0.0, 1.5)
    w.step(job.job_id)
    [ev] = _events(store, job, "efficiency_slow")
    assert ev.evidence["cpu_s"] == 1.5
    spend.t += 61
    w.step(job.job_id)  # recomputes, slow again but inside 10 minutes: not recorded again
    assert len(_events(store, job, "efficiency_slow")) == 1
    spend.t += 700
    w.step(job.job_id)
    assert len(_events(store, job, "efficiency_slow")) == 2


def test_efficiency_error_never_breaks_the_tick(steward_home, tmp_path, monkeypatch):
    store, job, proj, d = _job(steward_home, tmp_path)
    _long(d)

    def boom(*a, **k):
        raise ValueError("bad metrics")

    monkeypatch.setattr(ew.efficiency, "compute", boom)
    runner = _running_runner(store, job, _watcher(store, proj, lambda: NOW, Clock(1000.0)))
    assert runner.tick(job.job_id).state == "RUNNING"
    [ev] = _events(store, job, "efficiency_error")
    assert ev.evidence == {"error": "bad metrics"}


# -- the CLI -----------------------------------------------------------------------------------------------------


def _home_with_projects(tmp_path, proj, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "projects").symlink_to(proj)
    monkeypatch.setenv("HOME", str(home))


def test_cli_efficiency_json_is_read_only(steward_home, tmp_path, monkeypatch, capsys):
    store, job, proj, d = _job(steward_home, tmp_path)
    _long(d)
    _home_with_projects(tmp_path, proj, monkeypatch)
    before = (len(store.events(job.job_id)), store.kv_get(job.job_id, ew.STATE_KEY))
    assert cli.main(["efficiency", job.job_id, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"mode", "metrics", "findings", "state", "baselines", "effects"} and out["mode"] == "shadow"
    assert out["metrics"]["totals"]["calls"] == 81
    assert [f["id"] for f in out["findings"]] == ["long_lived_agent:agent-e1"]
    assert (len(store.events(job.job_id)), store.kv_get(job.job_id, ew.STATE_KEY)) == before
    assert cli.main(["efficiency", "nope"]) == 1


def test_cli_efficiency_text_table(steward_home, tmp_path, monkeypatch, capsys):
    store, job, proj, d = _job(steward_home, tmp_path)
    _long(d)
    _long(d, agent_id="p1", stage="gsd-planner", desc="Plan phase 1", t0=T0 + 1, calls=4, finished=True)
    _home_with_projects(tmp_path, proj, monkeypatch)
    assert cli.main(["efficiency", job.job_id]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("efficiency [shadow]")
    assert "agents:" in out and "stages:" in out
    assert any(ln.strip().startswith("agent-e1") and "running" in ln for ln in out)
    assert any(ln.strip().startswith("gsd-executor") and "1 agents" in ln for ln in out)
    assert any(ln.startswith("units:") for ln in out) and any(ln.startswith("supervision:") for ln in out)
    assert any(ln.startswith("findings (1):") for ln in out)
    [line] = [ln for ln in out if ln.lstrip().startswith("⚠")]
    assert "81 calls × 100k ctx → respawn a fresh helper per unit" in line


# -- the built-in role: switch, row, reserved name, codex n/a ------------------------------------------------------


def _sup(job, state):
    return cli.main(["supervisor", job.job_id, "efficiency", state])


def test_role_states_switch_and_cycle():
    from steward import supervisors as sup

    assert sup.ROLE_STATES["efficiency"] == ("off", "shadow", "active", "on")
    assert sup.SWITCH_KEYS["efficiency"] == "supervisor_efficiency"
    assert [sup.next_state("efficiency", m) for m in ("off", "shadow", "active", "on")] == [
        "shadow", "active", "off", "active"]


def test_supervisor_cli_switches_efficiency(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    assert _sup(job, "off") == 0 and store.kv_get(job.job_id, "supervisor_efficiency") == "off"
    assert _sup(job, "on") == 0 and store.kv_get(job.job_id, "supervisor_efficiency") == "shadow"
    assert _sup(job, "toggle") == 0 and store.kv_get(job.job_id, "supervisor_efficiency") == "active"
    ev = _events(store, job, "supervisor_toggled")
    assert [(e.evidence["role"], e.evidence["state"], e.evidence["previous"]) for e in ev] == [
        ("efficiency", "off", "shadow"), ("efficiency", "shadow", "off"), ("efficiency", "active", "shadow")]
    assert not _events(store, job, "watcher_toggled")
    assert cw.list_watchers(store, job.job_id) == []  # never routed to the custom-watcher toggle


def test_env_forces_off_and_toml_default(steward_home, tmp_path, monkeypatch):
    store = Store(steward_home)
    job = _contracted_job(store)
    assert ew.mode(store, job.job_id) == "shadow"
    store.kv_set(job.job_id, "supervisor_efficiency", "active")
    assert ew.mode(store, job.job_id) == "active"
    monkeypatch.setenv("STEWARD_EFFICIENCY", "0")
    assert ew.mode(store, job.job_id) == "off"
    monkeypatch.delenv("STEWARD_EFFICIENCY")
    store.kv_delete(job.job_id, "supervisor_efficiency")
    cfgdir = tmp_path / "cfg"
    cfgdir.mkdir()
    (cfgdir / "steward.toml").write_text('[efficiency]\nmode = "off"\n')
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfgdir))
    assert ew.mode(store, job.job_id) == "off"
    store.kv_set(job.job_id, "supervisor_efficiency", "on")  # the per-job switch wins; "on" is shadow
    assert ew.mode(store, job.job_id) == "shadow"


def test_off_skips_metrics_keeps_spend_guard(steward_home, tmp_path):
    store, job, proj, d = _job(steward_home, tmp_path)
    _long(d)
    store.kv_set(job.job_id, "supervisor_efficiency", "off")
    w = _watcher(store, proj, lambda: NOW, Clock(1000.0), tokens_fn=lambda st, jid, now=None: _tokens(hour=6.0))
    w.step(job.job_id)
    assert not _events(store, job, "efficiency_finding") and _events(store, job, "spend_rate_high")
    assert store.kv_get(job.job_id, ew.STATE_KEY) == {"mode": "off", "codex": False}
    assert w._cache == {}  # nothing was parsed


def test_efficiency_row_from_state(steward_home):
    from steward.supervisors import supervisor_rows

    store = Store(steward_home)
    job = _contracted_job(store)
    cw.add_watcher(store, job.job_id, cw.make_spec("pace", "on track?"))
    [row] = [r for r in supervisor_rows(store, job.job_id) if r["role"] == "efficiency"]
    assert row["parts"] == ["no data yet"] and row["label"] == "efficiency" and row["model"] == "code"
    store.kv_set(job.job_id, ew.STATE_KEY, {"mode": "shadow", "codex": False, "active": ["x"], "summary": {
        "usd": 86.4, "sub_share": 0.96, "calls": 5200, "peak_ctx": 602_000, "think_share": 0.7, "tok_s": 140.0,
        "crit": "build"}})
    rows = supervisor_rows(store, job.job_id)
    assert [r["role"] for r in rows] == ["worker", "astra", "sol", "pace", "efficiency", "jev"]
    row = rows[4]
    assert row["parts"] == ["$86", "sub 96%", "5.2k calls", "peak ctx 602k", "think≈70%", "140 tok/s", "crit: build"]
    assert row["status"] == "shadow" and row["switch"] == "shadow▸" and row["next"] == "active"
    assert row["display"] == "[shadow▸] " + " · ".join(row["parts"])
    store.kv_set(job.job_id, "supervisor_efficiency", "off")
    off = [r for r in supervisor_rows(store, job.job_id) if r["role"] == "efficiency"][0]
    assert off["status"] == "off" and off["display"] == "[off]" and off["next"] == "shadow"


def test_efficiency_row_codex_na(steward_home, tmp_path):
    from steward.supervisors import supervisor_rows

    store, job, proj, d = _job(steward_home, tmp_path)
    _long(d)
    store.kv_set(job.job_id, "worker_kind", "codex")
    w = _watcher(store, proj, lambda: NOW, Clock(1000.0))
    w.step(job.job_id)
    assert not _events(store, job, "efficiency_finding")
    [row] = [r for r in supervisor_rows(store, job.job_id) if r["role"] == "efficiency"]
    assert row["parts"] == ["n/a (codex transcript)"]


def test_efficiency_is_reserved_and_not_counted(steward_home):
    import pytest

    store = Store(steward_home)
    job = _contracted_job(store)
    with pytest.raises(cw.WatcherError):
        cw.make_spec("efficiency", "anything")
    for name in ("a", "b", "c", "d"):
        cw.add_watcher(store, job.job_id, cw.make_spec(name, "ok?"))
    with pytest.raises(cw.WatcherError):
        cw.add_watcher(store, job.job_id, cw.make_spec("e", "ok?"))
    assert len(cw.list_watchers(store, job.job_id)) == 4


def test_cli_efficiency_units_sets_contract_hint(steward_home, capsys):
    from steward.contract import load_contract

    store = Store(steward_home)
    job = _contracted_job(store)
    assert cli.main(["efficiency", job.job_id, "units", "8"]) == 0
    assert load_contract(store, job.job_id).units_total == 8
    [ev] = _events(store, job, "efficiency_units_set")
    assert ev.evidence == {"units_total": 8, "previous": 0} and ev.judgment.value == "human"
    for bad in ("0", "-2", "x"):
        assert cli.main(["efficiency", job.job_id, "units", bad]) == 1
    assert cli.main(["efficiency", job.job_id, "units"]) == 1
    assert load_contract(store, job.job_id).units_total == 8 and len(_events(store, job, "efficiency_units_set")) == 1


# -- pane lines, dash label and feed lines ---------------------------------------------------------------------------


def _finding(kind="long_lived_agent", subject="agent-e4", stage="gsd-executor", unit=4, evidence="131 calls × 356k ctx",
             short="respawn per unit", usd=6.12, minutes=0.0):
    return {"id": f"{kind}:{subject}", "kind": kind, "subject": subject, "stage": stage, "unit": unit,
            "evidence": evidence, "remedy_short": short, "saving_usd": usd, "saving_min": minutes, "actions": []}


def test_pane_warnings_one_line_per_active_finding(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    assert ew.pane_warnings(store, job.job_id) == []
    findings = [_finding(), _finding("hidden_thinking", "gsd-planner", "gsd-planner", None, "think≈75% of 200k out",
                                     "step effort down", 1.5, 3.0),
                _finding("spend_rate", "job", "", None, "spend $6.20/h", "", 0.0)]
    store.kv_set(job.job_id, ew.STATE_KEY, {"mode": "shadow", "codex": False, "findings": findings})
    assert ew.pane_warnings(store, job.job_id) == [
        "⚠ gsd-executor#4 131 calls × 356k ctx → respawn per unit (≈-$6.12)",
        "⚠ gsd-planner think≈75% of 200k out → step effort down (≈-$1.50 / ≈3 min)"]
    store.kv_set(job.job_id, ew.STATE_KEY, {"mode": "off", "codex": False, "findings": findings})
    assert ew.pane_warnings(store, job.job_id) == []


def test_dash_renders_efficiency_row_and_warnings(steward_home, tmp_path):
    import os
    import re
    import subprocess
    import sys
    from pathlib import Path

    from steward.models import JobState, Judgment

    repo = Path(__file__).resolve().parent.parent
    store = Store(steward_home)
    job = _contracted_job(store)
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    store.kv_set(job.job_id, ew.STATE_KEY, {"mode": "shadow", "codex": False, "active": ["a", "b"], "summary": {
        "usd": 86.4, "sub_share": 0.96, "calls": 5200, "peak_ctx": 602_000, "think_share": 0.7, "tok_s": 140.0,
        "crit": "build"}, "findings": [_finding(), _finding("spend_rate", "job", "", None, "spend $6.20/h", "", 0.0)]})
    env = {**os.environ, "STEWARD_HOME": str(store.home), "STEWARD_DASH_COLS": "88", "PYTHONPATH": str(repo),
           "HOME": str(tmp_path / "h"), "STEWARD_HEALTH": "0"}
    res = subprocess.run([sys.executable, str(repo / "deploy/cockpit/steward-dash-jobs.py")], capture_output=True,
                         text=True, env=env, cwd=repo)
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln).replace("{SPIN}", "x") for ln in res.stdout.splitlines()]
    text = "\n".join(lines)
    assert "⚠ gsd-executor#4 131 calls × 356k ctx → respawn per unit (≈-$6.12)" in text, text
    assert text.count("⚠") == 1  # the spend_rate finding is printed from the token data, not from the state
    [row] = [ln for ln in lines if "efficie…" in ln]
    assert "[shadow▸] $86 · sub 96%" in row
    assert all(len(ln) <= 88 for ln in lines), lines


def test_dash_long_label_is_ellipsized():
    from steward.dashrender import _label

    assert _label("verylongname") == "verylon…" and _label("efficiency") == "efficie…"
    assert _label("advisor") == "advisor" and _label("exactly8") == "exactly8"


def test_feed_formats_every_efficiency_event(steward_home):
    from steward import watchers
    from steward.models import Judgment

    store = Store(steward_home)
    job = _contracted_job(store)
    samples = {
        "efficiency_finding": _finding(),
        "efficiency_finding_cleared": {"id": "long_lived_agent:agent-e4", "kind": "long_lived_agent", "at": 1.0},
        "efficiency_error": {"error": "boom"},
        "efficiency_slow": {"cpu_s": 1.5},
        "efficiency_units_set": {"units_total": 8, "previous": 0},
        "efficiency_would_nudge": {"id": "long_lived_agent:agent-e4", "blocked": ["shadow", "cooldown"]},
        "efficiency_nudged": {"rev": 5, "remedy_short": "respawn per unit"},
        "efficiency_escalated": {"id": "idle_wait:job", "approval_id": "abcdef1234"},
        "efficiency_policy_changed": {"key": "respawn", "before": "off", "after": "per_unit", "actor": "sam"},
        "efficiency_refused": {"reason": "effort below the floor"},
        "efficiency_effect": {"metric": "ctx_sum", "before": 9.0, "after": 3.0, "saved_usd": 6.1, "saved_min": 4},
        "efficiency_review": {"observations": 3, "cost_usd": 0.12},
        "efficiency_cap_tripped": {"reason": "model spend over $0.50/h"},
        "efficiency_overrides_changed": {"keys": ["long_lived_calls", "envelope.effort_floor"]},
        "efficiency_future_kind": {"x": 1},
    }
    assert set(samples) - {"efficiency_future_kind"} == set(ew.EVENT_NAMES)
    for what, evidence in samples.items():
        ev = store.append_event(job.job_id, what=what, evidence=evidence, rule="t", judgment=Judgment.code)
        got = watchers.format_event(ev, {})
        if what == "efficiency_future_kind":
            assert got is None  # unknown kinds never raise
            continue
        assert got is not None and got[0] == "efficiency" and got[1] and "\n" not in got[1], (what, got)
    out = {e.what: watchers.format_event(e, {})[1] for e in store.events(job.job_id)
           if e.what in samples and e.what != "efficiency_future_kind"}
    assert out["efficiency_finding"] == "gsd-executor#4 131 calls × 356k ctx → respawn per unit (≈-$6.12)"
    assert out["efficiency_policy_changed"] == "policy respawn: off → per_unit (sam)"
    assert out["efficiency_effect"] == "effect ctx_sum: 9.0 → 3.0 (saved $6.10 / 4 min)"
    assert "abcdef12" in out["efficiency_escalated"] and "blocked: shadow, cooldown" in out["efficiency_would_nudge"]
    assert watchers.format_event(store.append_event(job.job_id, what="efficiency_slow", evidence=None, rule="t",
                                                    judgment=Judgment.code), {})[0] == "efficiency"
    line = watchers.render_line(store.events(job.job_id)[-1], "efficiency", "x", width=80, color=False)
    assert line.split(" ", 1)[1].startswith("efficie… x")
