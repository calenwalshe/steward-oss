"""Delivery and measurement of the efficiency watcher's advice (16-06): policy in the brief and at relaunch, effect
measurement (G13) and the optional, cost-capped model review (G5, G12). Fakes only; no real claude, herdr or network."""
from __future__ import annotations


import pytest

from steward import cli
from steward import efficiency_policy as ep
from steward.brief import render_brief
from steward.contract import Contract, store_contract
from steward.diagnosis import Diagnosis, FakeDiagnoser
from steward.runner import Runner
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from steward.worker import FakeWorker, WorkerObservation


POLICY_LINES = [
    "respawn: end each helper when its unit is done ...",
    "effort gsd-planner: high (advisory: request it when you spawn this helper type)",
    "plan ahead: ...",
    "output hygiene: ...",
]


def _contract(goal="do the thing"):
    return Contract(goal=goal, boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[])


def _job(store):
    job = store.create_job("do the thing", "/repo")
    store_contract(store, job.job_id, _contract())
    return job


# -- Task 1: policy in the brief and relaunch effort ---------------------------------------------------------


def test_brief_unchanged_without_policy():
    base = render_brief(_contract(), [], None, None)
    assert render_brief(_contract(), [], None, None, efficiency_policy=None) == base
    assert render_brief(_contract(), [], None, None, efficiency_policy=[]) == base
    assert "EFFICIENCY POLICY" not in base


def test_brief_has_efficiency_policy_section():
    text = render_brief(_contract(), [{"enforcement": "hard", "text": "no net"}], {"hypothesis": "h"}, None,
                        efficiency_policy=POLICY_LINES)
    lines = text.split("\n")
    i = lines.index("EFFICIENCY POLICY")
    assert lines[i + 1:i + 5] == [f"- {x}" for x in POLICY_LINES]
    assert lines.index("CONSTRAINTS") < i < lines.index("HANDOFF")


class _Clock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        self.t += 1.0
        return self.t


class _Factory:
    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.created: list[FakeWorker] = []

    def __call__(self, existing_ref):
        w = FakeWorker(script=self._scripts.pop(0) if self._scripts else [])
        self.created.append(w)
        return w


def _git():
    def respond(argv, cwd):
        out = "sha-1\n" if argv[:2] == ["git", "rev-parse"] else ""
        return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=out, stderr="", duration_s=0.0)
    return FakeRunner(respond)


def _diag():
    return FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="g", hypothesis="h"))


def test_runner_start_brief_carries_policy(steward_home):
    store = Store(steward_home)
    job = _job(store)
    ep.change(store, job.job_id, {"respawn": "per_unit"}, actor="cli")
    factory = _Factory([[]])
    Runner(store, factory, _git(), _diag(), clock=_Clock()).start(job.job_id)
    brief = factory.created[0].started_brief
    assert "EFFICIENCY POLICY" in brief and "end each helper agent when its unit is done" in brief


def test_runner_replacement_brief_carries_policy(steward_home):
    store = Store(steward_home)
    job = _job(store)
    ep.change(store, job.job_id, {"respawn": "per_unit"}, actor="cli")
    dead = WorkerObservation(lifecycle="exited", output_tail="boom", pid_alive=False, ts="t")
    factory = _Factory([[dead], []])
    runner = Runner(store, factory, _git(), _diag(), clock=_Clock())
    runner.start(job.job_id)
    assert runner.tick(job.job_id).action == "replace"
    assert "EFFICIENCY POLICY" in factory.created[1].started_brief


def test_runner_brief_unchanged_without_policy(steward_home):
    store = Store(steward_home)
    job = _job(store)
    factory = _Factory([[]])
    Runner(store, factory, _git(), _diag(), clock=_Clock()).start(job.job_id)
    assert "EFFICIENCY POLICY" not in factory.created[0].started_brief


def _claude_argv(store, job_id, **kw):
    return cli._build_shim_agent_argv(store, job_id, agent="claude", agent_script=None, model=None,
                                      extra_args=[], **kw)


def test_shim_argv_adds_effort_from_worker_policy(steward_home):
    store = Store(steward_home)
    job = _job(store)
    ep.change(store, job.job_id, {"effort": {"worker": "medium"}}, actor="cli")
    argv = _claude_argv(store, job.job_id)
    assert argv.count("--effort") == 1 and argv[argv.index("--effort") + 1] == "medium"
    fake = cli._build_shim_agent_argv(store, job.job_id, agent="fake", agent_script=None, model=None, extra_args=[])
    assert "--effort" not in fake


def test_shim_argv_unchanged_without_worker_effort(steward_home):
    store = Store(steward_home)
    job = _job(store)
    base = _claude_argv(store, job.job_id)
    assert "--effort" not in base
    ep.change(store, job.job_id, {"effort": {"gsd-executor": "medium"}}, actor="cli")
    assert _claude_argv(store, job.job_id) == base


def test_worker_effort_only_from_closed_list(steward_home):
    store = Store(steward_home)
    job = _job(store)
    store.kv_set(job.job_id, ep.KV_KEY, {"effort": {"worker": "medium; rm -rf /"}})
    assert ep.worker_effort(store, job.job_id) is None
    assert _claude_argv(store, job.job_id).count("--effort") == 0


def test_subagent_effort_is_advisory_only(steward_home):
    store = Store(steward_home)
    job = _job(store)
    ep.change(store, job.job_id, {"effort": {"gsd-executor": "medium"}}, actor="cli")
    lines = ep.brief_lines(store, job.job_id)
    assert lines == ["effort gsd-executor: medium (advisory: request it when you spawn this helper type)"]
    assert "--effort" not in _claude_argv(store, job.job_id)


def test_brief_lines_all_keys(steward_home):
    store = Store(steward_home)
    job = _job(store)
    assert ep.brief_lines(store, job.job_id) == []
    ep.change(store, job.job_id, {"respawn": "max_calls=60", "plan_ahead": "on", "output_hygiene": "on",
                                  "effort": {"worker": "medium"}}, actor="cli")
    lines = ep.brief_lines(store, job.job_id)
    assert any("about 60 model calls" in x for x in lines)
    assert any("this session runs at effort medium (applied at relaunch)" in x for x in lines)
    assert any(x.startswith("plan ahead:") for x in lines) and any(x.startswith("output hygiene:") for x in lines)


# -- Task 2: effect measurement (G13) ----------------------------------------------------------------------

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from efficiency_synth import T0, agent_lines, write_subagent  # noqa: E402
from test_efficiency_authority import _finding, _t2_active, _watcher, _eff_instructions  # noqa: E402
from test_efficiency_watch import NOW, _job as _watch_job, _long  # noqa: E402
from test_stability import _events  # noqa: E402

from steward import efficiency_detect as detect  # noqa: E402
from steward import efficiency_watch as ew  # noqa: E402
from steward import watcher_authority as wa  # noqa: E402

FID = "long_lived_agent:agent-e1"


def _e1_state(store, job, proj, d):
    """A watcher stepped once over an unfinished 81-call executor: (watcher, its stored finding)."""
    _long(d)
    w = _watcher(store, proj)
    w.step(job.job_id)
    f = next(f for f in store.kv_get(job.job_id, ew.STATE_KEY)["findings"] if f["id"] == FID)
    return w, f


def test_baseline_recorded_on_action(steward_home, tmp_path):
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    w, f = _e1_state(store, job, proj, d)
    w._on_action(job.job_id, f, "nudge", 7)
    bl = store.kv_get(job.job_id, ew.BASELINES_KEY)[FID]
    assert bl["finding"] == FID and bl["kind"] == "long_lived_agent" and bl["stage"] == "gsd-executor"
    assert bl["metric"] == "ctx_sum" and bl["before"] == f["metric"]["value"]  # no finished predecessor
    assert bl["before_usd"] > 0 and bl["before_min"] > 0
    assert bl["action"] == "nudge" and bl["ref"] == 7 and bl["unit"] == f["unit"] and "at" in bl
    assert bl["closed"] is False
    w._on_action(job.job_id, f, "escalate", 8)  # an open baseline is not replaced
    assert store.kv_get(job.job_id, ew.BASELINES_KEY)[FID]["ref"] == 7


def test_baseline_without_metrics_falls_back_to_finding_value(steward_home, tmp_path):
    store, job, proj, d = _t2_active(steward_home, tmp_path)
    w = _watcher(store)
    f = _finding(store, job.job_id)
    w._act(job.job_id, [f], None, {"agent-e1"}, NOW)
    bl = store.kv_get(job.job_id, ew.BASELINES_KEY)[FID]
    # reconfigure runs first and owns the baseline; the nudge that follows does not replace it
    assert bl["before"] == 21_000_000.0 and bl["action"] == "reconfigure" and bl["ref"] == "pc-1"
    assert len(_eff_instructions(store, job.job_id)) == 1


def _next_executor(d, agent_id="e2", calls=20):
    write_subagent(d, "w", agent_id, "gsd-executor", "Execute phase 2",
                   agent_lines(T0 + 400, calls=calls, prefix=agent_id, finished=True, ctx0=100_000, gen_s=5, tool_s=1))


def test_effect_reported_once_after_next_comparable_unit(steward_home, tmp_path):
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    w, f = _e1_state(store, job, proj, d)
    w._on_action(job.job_id, f, "nudge", 7)
    assert _events(store, job, "efficiency_effect") == []
    _long(d, agent_id="e1", calls=90, finished=True)  # the firing agent finishing is not the next unit
    w.spend_clock.t += 100
    w.step(job.job_id)
    assert _events(store, job, "efficiency_effect") == []
    _next_executor(d)
    for _ in range(3):
        w.spend_clock.t += 100
        w.step(job.job_id)
    [ev] = _events(store, job, "efficiency_effect")
    e = ev.evidence
    assert e["finding"] == FID and e["metric"] == "ctx_sum" and e["action"] == "nudge" and e["ref"] == 7
    assert e["before"] > e["after"] > 0 and e["agent"] == "agent-e2"
    assert e["saved_usd"] == pytest.approx(e["before_usd"] - e["after_usd"], abs=1e-3)
    assert e["saved_min"] > 0
    assert ev.judgment.value == "code"
    assert store.kv_get(job.job_id, ew.BASELINES_KEY)[FID]["closed"] is True


def _ag(key, stage, kind, unit, start, end, finished=True, **kw):
    base = {"key": key, "stage": stage, "kind": kind, "unit": unit, "start": start, "end": end, "finished": finished,
            "usd": 1.0, "ctx_sum": 0, "ctx_mean": 0, "think_share": 0.0, "cache_miss": 0.0, "max_call_gap_s": 0.0}
    return {**base, **kw}


def _finding_for(kind, subject, stage, metric, value):
    return {"id": f"{kind}:{subject}", "kind": kind, "subject": subject, "stage": stage, "unit": 1,
            "metric": {"name": metric, "value": value}}


CFG = detect.DEFAULTS


@pytest.mark.parametrize("kind,metric,field,b_val,a_val", [
    ("long_lived_agent", "ctx_sum", "ctx_sum", 21e6, 4e6),
    ("context_growth", "ctx_mean", "ctx_mean", 300e3, 120e3),
    ("hidden_thinking", "think_share", "think_share", 0.7, 0.4),
    ("cache_misses", "cache_miss_share", "cache_miss", 0.5, 0.1),
])
def test_effect_metric_per_agent_kind(kind, metric, field, b_val, a_val):
    before_m = {"agents": [_ag("a1", "ex", "build", 1, 0, 3600, **{field: b_val}, usd=5.0)]}
    f = _finding_for(kind, "a1", "ex", metric, b_val)
    bl = ew.make_baseline(f, "nudge", 3, 10.0, before_m, CFG)
    assert (bl["metric"], bl["before"], bl["before_usd"], bl["before_min"]) == (metric, b_val, 5.0, 60.0)
    after_m = {"agents": [*before_m["agents"], _ag("a2", "ex", "build", 2, 4000, 4600, **{field: a_val}, usd=1.5)]}
    e = ew.measure_effect(bl, after_m, CFG)
    assert (e["metric"], e["before"], e["after"], e["saved_usd"], e["saved_min"]) == (metric, b_val, a_val, 3.5, 50.0)
    assert ew.measure_effect({**bl, "closed": True}, after_m, CFG) is None


def test_effect_metric_planning_serial():
    b1 = _ag("b1", "exec", "build", 1, 0, 100)
    p2 = _ag("p2", "plan", "plan", 2, 100, 400)
    f = _finding_for("planning_serial", "plan", "plan", "serial_plan_s", 300.0)
    bl = ew.make_baseline(f, "nudge", 1, 1.0, {"agents": [b1, p2]}, CFG)
    assert bl["before"] == 300.0 and bl["before_min"] == 5.0
    b2 = _ag("b2", "exec", "build", 2, 100, 900)
    p3 = _ag("p3", "plan", "plan", 3, 500, 700)  # overlaps build 2: no serial time
    e = ew.measure_effect(bl, {"agents": [b1, p2, b2, p3]}, CFG)
    assert (e["metric"], e["after"], e["saved_min"]) == ("serial_plan_s", 0.0, 5.0)


def test_effect_metric_artifact_chars():
    p1 = _ag("p1", "plan", "plan", 1, 0, 100)
    arts = [{"writer": "p1", "stage": "plan", "path_key": "P1.md", "chars": 50_000, "ts": 50}]
    f = _finding_for("artifact_bloat", "P1.md", "plan", "artifact_chars", 50_000)
    bl = ew.make_baseline(f, "nudge", 1, 1.0, {"agents": [p1], "artifacts": arts}, CFG)
    assert bl["before"] == 50_000.0
    p2 = _ag("p2", "plan", "plan", 2, 200, 300)
    arts2 = [*arts, {"writer": "p2", "stage": "plan", "path_key": "P2.md", "chars": 20_000, "ts": 250}]
    e = ew.measure_effect(bl, {"agents": [p1, p2], "artifacts": arts2}, CFG)
    assert (e["metric"], e["before"], e["after"]) == ("artifact_chars", 50_000.0, 20_000.0)


def test_effect_metric_build_output_bloat_counts_new_big_results():
    a1 = _ag("a1", "ex", "build", 1, 0, 100)
    big = {"make test": [30_000, 30_000, 30_000]}
    f = _finding_for("build_output_bloat", "make test", "", "big_results", 3)
    bl = ew.make_baseline(f, "nudge", 1, 1.0, {"agents": [a1], "big_results": big}, CFG)
    assert bl["before"] == 3 and bl["stage"] == ""
    a2 = _ag("a2", "ex", "build", 2, 200, 300)
    e = ew.measure_effect(bl, {"agents": [a1, a2], "big_results": {"make test": [30_000] * 4 + [100]}}, CFG)
    assert (e["metric"], e["after"]) == ("big_results", 1.0)


def test_effect_can_be_negative_and_labels_stay_human(steward_home, tmp_path, monkeypatch):
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    w, f = _e1_state(store, job, proj, d)
    calls = []
    monkeypatch.setattr(wa, "record_case", lambda *a, **k: calls.append(a), raising=False)
    spec = ew.builtin_spec(detect.load_config(store, job.job_id))
    ew.ensure_record(store, job.job_id, spec)
    tier = wa.tier_of(store, job.job_id, spec)
    w._on_action(job.job_id, f, "nudge", 7)
    _next_executor(d, calls=200)  # a worse unit: more expensive than the firing agent so far
    w.spend_clock.t += 100
    w.step(job.job_id)
    [ev] = _events(store, job, "efficiency_effect")
    assert ev.evidence["saved_usd"] < 0
    assert calls == [] and wa.tier_of(store, job.job_id, spec) == tier
    assert not [e for e in store.events(job.job_id) if "case" in e.what or "promot" in e.what]


def test_efficiency_cli_shows_effects(steward_home, tmp_path, monkeypatch, capsys):
    from test_efficiency_watch import _home_with_projects

    store, job, proj, d = _watch_job(steward_home, tmp_path)
    w, f = _e1_state(store, job, proj, d)
    w._on_action(job.job_id, f, "nudge", 7)
    _home_with_projects(tmp_path, proj, monkeypatch)
    assert cli.main(["efficiency", job.job_id, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["effects"] == [] and out["baselines"][FID]["ref"] == 7
    assert cli.main(["efficiency", job.job_id]) == 0
    assert "baselines awaiting the next unit (1):" in capsys.readouterr().out
    _next_executor(d)
    w.spend_clock.t += 100
    w.step(job.job_id)
    assert cli.main(["efficiency", job.job_id, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [e["finding"] for e in out["effects"]] == [FID]
    assert cli.main(["efficiency", job.job_id]) == 0
    text = capsys.readouterr().out
    assert "effects (1):" in text and f"{FID} ctx_sum:" in text


# -- Task 3: the optional Sonnet review with its own cost cap --------------------------------------------------

from steward import custom_watchers as cw  # noqa: E402
from steward import efficiency_review as er  # noqa: E402
from steward import tokens  # noqa: E402
from steward.steering import Steering  # noqa: E402
from test_stability import Clock  # noqa: E402


class FakePopen:
    """Records argv/kwargs and writes `output` to the stdout file at creation; `finish_now` False = still running."""
    instances: list = []
    output = ""
    finish_now = True

    def __init__(self, argv, **kw):
        self.argv, self.kw, self.killed, self.pid = argv, kw, False, 4242
        self.out_path = kw["stdout"].name
        self.rc = 0 if FakePopen.finish_now else None
        if FakePopen.finish_now:
            kw["stdout"].write(FakePopen.output)
            kw["stdout"].flush()
        FakePopen.instances.append(self)

    def finish(self, output):
        Path(self.out_path).write_text(output)
        self.rc = 0

    def poll(self):
        return self.rc

    def kill(self):
        self.killed, self.rc = True, -9


def _review_out(cost=0.04, patterns=None):
    patterns = [{"observation": "re-reading the same big file", "evidence": "12 reads", "estimated_saving_usd": 2}] \
        if patterns is None else patterns
    return json.dumps({"type": "result", "result": "Looked.\n" + json.dumps({"patterns": patterns}),
                       "total_cost_usd": cost})


@pytest.fixture
def fake_popen():
    FakePopen.instances = []
    FakePopen.finish_now = True
    FakePopen.output = _review_out()
    return FakePopen


def _cfg_dir(tmp_path, on=True):
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    (d / "steward.toml").write_text(f"[efficiency]\nmodel_review = {'true' if on else 'false'}\n")
    return d


def _exec(d, n, *, finished, t0=None, **kw):
    write_subagent(d, "w", f"e{n}", "gsd-executor", f"Execute phase {n}",
                   agent_lines(t0 if t0 is not None else T0 + 1000 * n, calls=3, prefix=f"e{n}", finished=finished,
                               gen_s=5, tool_s=1, **kw))


def _review_watcher(store, proj, tmp_path, popen, *, on=True, notes=None, clock=None):
    from test_stability import Clock as C

    clock = clock or C(NOW)
    notify = (lambda *a: notes.append(a)) if notes is not None else None
    cfgd = _cfg_dir(tmp_path, on)
    rv = er.EfficiencyReview(store, popen=popen, clock=clock, notify=notify, config_dir=cfgd)
    w = ew.EfficiencyWatcher(store, clock=clock, spend_clock=C(1000.0), projects_dir=proj, config_dir=cfgd,
                             review=rv, notify=notify,
                             tokens_fn=lambda st, jid, now=None: {"sources": {}, "last_hour": {"usd": 0.0}})
    return w, rv, clock


def _tick(w, clock, job_id, dt=100):
    clock.t += dt
    w.spend_clock.t += dt
    w.step(job_id)


def test_model_review_off_by_default_no_subprocess(steward_home, tmp_path, fake_popen):
    assert detect.DEFAULTS["model_review"] is False
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    w, rv, clock = _review_watcher(store, proj, tmp_path, fake_popen, on=False)
    _exec(d, 1, finished=False)
    _tick(w, clock, job.job_id)
    _exec(d, 1, finished=True)
    _tick(w, clock, job.job_id)
    _exec(d, 2, finished=True)
    _tick(w, clock, job.job_id)
    assert store.kv_get(job.job_id, ew.UNITS_COMPLETE_KEY) == ["1", "2"]  # boundaries happened (kv holds unit labels)
    assert fake_popen.instances == [] and _events(store, job, "efficiency_review") == []


def test_review_once_per_unit_boundary_with_read_only_sonnet_low(steward_home, tmp_path, fake_popen):
    fake_popen.finish_now = False
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    w, rv, clock = _review_watcher(store, proj, tmp_path, fake_popen)
    _exec(d, 1, finished=False, ctx0=20_000_000)
    _tick(w, clock, job.job_id)
    assert fake_popen.instances == []  # no boundary yet
    _exec(d, 1, finished=True, ctx0=20_000_000)
    _tick(w, clock, job.job_id)
    [p] = fake_popen.instances
    assert p.argv[:10] == ["env", "-u", "ANTHROPIC_API_KEY", "claude", "-p", "--model", "sonnet", "--effort", "low",
                           "--output-format"] and p.argv[10] == "json"
    assert p.argv[p.argv.index("--tools") + 1] == "Read,Glob,Grep"
    allowed = p.argv[p.argv.index("--allowedTools") + 1:p.argv.index("--disallowedTools")]
    assert allowed == ["Read", "Glob", "Grep"]
    assert p.argv[p.argv.index("--disallowedTools") + 1:] == ["Bash", "Edit", "Write", "NotebookEdit"]
    assert p.kw["cwd"] == store.get_job(job.job_id).repo
    for _ in range(3):  # later steps for the same boundary start nothing and poll without blocking
        _tick(w, clock, job.job_id)
    assert len(fake_popen.instances) == 1 and _events(store, job, "efficiency_review") == []
    p.finish(_review_out())
    _tick(w, clock, job.job_id)
    [ev] = _events(store, job, "efficiency_review")
    assert ev.evidence["unit"] == "1" and ev.evidence["observations"] == 1 and ev.evidence["cost_usd"] == 0.04
    assert ev.judgment.value == "model"
    _tick(w, clock, job.job_id)
    assert len(fake_popen.instances) == 1
    _exec(d, 2, finished=True, ctx0=20_000_000)  # the next boundary gets its own review
    _tick(w, clock, job.job_id)
    assert len(fake_popen.instances) == 2


def test_review_prompt_capped_and_untrusted(steward_home, tmp_path, fake_popen):
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    metrics = {"totals": {"usd": 3.0}, "stages": {f"stage-{i}-" + "x" * 30: {"kind": "build", "agents": 1, "calls": 5,
               "usd": 1.0, "think_share": 0.5, "cache_miss": 0.1, "effort": "high"} for i in range(2000)},
               "units": [{"unit": 1, "usd": 1.0, "wall_s": 60.0, "complete": True}]}
    findings = [{"kind": "long_lived_agent", "stage": "gsd-executor", "metric": {"name": "ctx_sum", "value": 1}}]
    text = er.build_prompt(store, job.job_id, metrics, findings, 1)
    assert text.startswith(er.REVIEW_MARKER) and er.REVIEW_MARKER == "You are the Steward efficiency reviewer"
    assert len(text) <= 32_000 and "untrusted" in text
    small = er.build_prompt(store, job.job_id, {**metrics, "stages": {}}, findings, 1)
    assert "none of those" in small.lower() or "NONE of those" in small
    assert "long_lived_agent" in small and "reply with only json" in small.lower()
    assert not any(m in small for m in ("tool_result", "assistant"))  # numbers and ids only, no transcript text


def test_review_output_recorded_never_actuates(steward_home, tmp_path, fake_popen):
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    _tier_t2(store, job.job_id)
    fake_popen.output = _review_out(cost=0.04)
    w, rv, clock = _review_watcher(store, proj, tmp_path, fake_popen)
    _exec(d, 1, finished=False)
    _tick(w, clock, job.job_id)
    _exec(d, 1, finished=True)
    _tick(w, clock, job.job_id)
    _tick(w, clock, job.job_id)
    [ev] = _events(store, job, "efficiency_review")
    assert ev.judgment.value == "model" and ev.evidence["observations"] == 1
    state = store.kv_get(job.job_id, ew.STATE_KEY)
    [f] = [f for f in state["findings"] if f["kind"] == "review"]
    assert tuple(f["actions"]) == ("escalate",) and f["policy"] is None
    assert not _eff_instructions(store, job.job_id)  # no Steering instruction
    assert store.kv_get(job.job_id, ep.KV_KEY) is None and store.kv_get(job.job_id, ep.LOG_KEY) is None
    assert not [e for e in store.events(job.job_id) if e.what in ("efficiency_nudged", "efficiency_policy_changed")]


def _tier_t2(store, job_id):
    spec = ew.builtin_spec(detect.load_config(store, job_id))
    ew.ensure_record(store, job_id, spec)
    wa.promote(store, job_id, spec, actor="cli", tier=2)
    store.kv_set(job_id, ew.SWITCH_KEY, "active")


def _rv(store, tmp_path, notes=None, clock=None, popen=None):
    clock = clock or Clock(NOW)
    return er.EfficiencyReview(store, popen=popen or FakePopen, clock=clock, config_dir=_cfg_dir(tmp_path),
                               notify=(lambda *a: notes.append(a)) if notes is not None else None), clock


METRICS = {"totals": {"usd": 100.0}, "supervision": {"usd": 0.0, "by_source": {}}, "stages": {}, "units": []}


def test_review_caps_trip_observe_only_and_notify_once(steward_home, tmp_path, fake_popen):
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    notes: list = []
    fake_popen.output = _review_out(cost=0.30)
    rv, clock = _rv(store, tmp_path, notes)
    assert rv.maybe_start(job.job_id, METRICS, [], 1, clock(), mode="active") == "started"
    assert rv.poll(job.job_id, clock()) == "ok"
    assert rv.maybe_start(job.job_id, METRICS, [], 2, clock(), mode="active") == "tripped"  # 0.30 + 0.30 > 0.50/h
    [ev] = _events(store, job, "efficiency_cap_tripped")
    assert ev.evidence["reason"] == "usd_per_hour" and ev.evidence["tripped"] is True
    assert store.kv_get(job.job_id, "efficiency_observe_only") is True
    assert len(fake_popen.instances) == 1
    assert [n[1] for n in notes] == ["BUDGET"] and notes[0][0] == job.job_id
    assert rv.maybe_start(job.job_id, METRICS, [], 3, clock(), mode="active") == "tripped"
    assert len(_events(store, job, "efficiency_cap_tripped")) == 1 and len(notes) == 1  # once per trip
    # 16-05's nudge gate now reports observe_only
    _tier_t2(store, job.job_id)
    w = _watcher(store)
    w._act(job.job_id, [_finding(store, job.job_id)], None, {"agent-e1"}, NOW)
    [wn] = _events(store, job, "efficiency_would_nudge")
    assert "observe_only" in wn.evidence["blocked"] and _eff_instructions(store, job.job_id) == []
    # the hour rolls: the flag clears and the release is recorded
    clock.t += 3700
    assert rv.poll(job.job_id, clock()) is None
    assert store.kv_get(job.job_id, "efficiency_observe_only") is False
    assert [e.evidence["tripped"] for e in _events(store, job, "efficiency_cap_tripped")] == [True, False]
    assert rv.maybe_start(job.job_id, METRICS, [], 3, clock(), mode="active") == "started"


def test_review_spend_over_hour_cap_seeded(steward_home, tmp_path, fake_popen):
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    rv, clock = _rv(store, tmp_path)
    store.kv_set(job.job_id, er.LOG_KEY, [{"at": clock() - 60, "usd": 0.30}, {"at": clock() - 30, "usd": 0.25}])
    assert rv._violation(job.job_id, clock(), None) == "usd_per_hour"


def test_review_share_of_job_cap(steward_home, tmp_path, fake_popen):
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    notes: list = []
    rv, clock = _rv(store, tmp_path, notes)
    store.kv_set(job.job_id, er.LOG_KEY, [{"at": clock() - 7200, "usd": 0.04}])  # old: the hour cap is quiet
    assert rv.maybe_start(job.job_id, {**METRICS, "totals": {"usd": 1.0}}, [], 1, clock(), mode="active") == "tripped"
    [ev] = _events(store, job, "efficiency_cap_tripped")
    assert ev.evidence["reason"] == "share_of_job" and len(notes) == 1
    # the job grew: 0.08 <= 2% of $10 -> released on the next poll
    assert rv.poll(job.job_id, clock(), {**METRICS, "totals": {"usd": 10.0}}) is None
    assert store.kv_get(job.job_id, "efficiency_observe_only") is False


def test_review_transcripts_are_supervision_source_efficiency(steward_home, tmp_path):
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    (d / "rev.jsonl").write_text("\n".join(agent_lines(
        T0, calls=3, prefix="rv", finished=True, user_text=er.REVIEW_MARKER + " for Steward job x.")) + "\n")
    data = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert data["sources"]["efficiency"]["usd"] > 0
    assert tokens.worker_usd(data) == 0.0
    worker, sup = cw.CustomWatchers(store, clock=lambda: NOW, projects_dir=proj)._spend(job.job_id)
    assert worker == 0.0 and sup == pytest.approx(data["sources"]["efficiency"]["usd"])


def test_review_timeout_kills_and_records(steward_home, tmp_path, fake_popen):
    fake_popen.finish_now = False
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    rv, clock = _rv(store, tmp_path)
    assert rv.maybe_start(job.job_id, METRICS, [], 1, clock(), mode="active") == "started"
    clock.t += 100
    assert rv.poll(job.job_id, clock()) is None
    clock.t += 250
    assert rv.poll(job.job_id, clock()) == "timeout"
    assert fake_popen.instances[0].killed is True
    [ev] = _events(store, job, "efficiency_review")
    assert ev.evidence["status"] == "timeout" and store.kv_get(job.job_id, er.INFLIGHT_KEY) is None


def test_review_bad_output_is_recorded_as_error(steward_home, tmp_path, fake_popen):
    fake_popen.output = "not json"
    store, job, proj, d = _watch_job(steward_home, tmp_path)
    rv, clock = _rv(store, tmp_path)
    rv.maybe_start(job.job_id, METRICS, [], 1, clock(), mode="active")
    assert rv.poll(job.job_id, clock()) == "error"
    [ev] = _events(store, job, "efficiency_review")
    assert ev.evidence["status"] == "error" and ev.evidence["observations"] == 0
    assert store.kv_get(job.job_id, er.FINDINGS_KEY) is None


def test_feed_says_when_the_cap_cleared(steward_home):
    from steward import watchers
    from steward.models import Judgment

    store = Store(steward_home)
    job = _job(store)
    for ev in ({"tripped": True, "reason": "usd_per_hour"}, {"tripped": False, "reason": "usd_per_hour"}):
        store.append_event(job.job_id, what="efficiency_cap_tripped", evidence=ev, rule="efficiency_review",
                           judgment=Judgment.code)
    texts = [watchers.format_event(e)[1] for e in store.events(job.job_id) if e.what == "efficiency_cap_tripped"]
    assert texts[0].startswith("cap tripped:") and texts[1].startswith("cap cleared:")
