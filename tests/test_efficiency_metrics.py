"""Deterministic efficiency metrics (phase 16-02): per-agent, stage/unit/time, incremental parse."""
from __future__ import annotations

import json

from efficiency_synth import T0, agent_lines, setup_job, write_subagent, write_worker
from steward import efficiency, tokens

NOW = T0 + 100_000
SONNET = "claude-sonnet-5-5"


def _usage_cost(model, ctx, out, cc, n):
    """Σ tokens.price_usd over n identical messages (input 10, cc, cache_read rest)."""
    cci = min(cc, max(ctx - 10, 0))
    return n * tokens.price_usd(model, [10, out, max(ctx - 10 - cci, 0), cci])


def _tracer_job(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 5, ctx0=2000, ctx_step=100, out=50, gen_s=4, tool_s=1, prefix="w",
                                model=SONNET, effort="medium"))
    write_subagent(d, "w", "p1", "gsd-planner", "Plan phase 1",
                   agent_lines(T0 + 100, 10, ctx0=3000, ctx_step=1000, out=200, gen_s=10, tool_s=5, prefix="p",
                               model="claude-opus-5-5", effort="xhigh"))
    write_subagent(d, "w", "e1", "gsd-executor", "Execute phase 1 plans",
                   agent_lines(T0 + 400, 12, ctx0=5000, ctx_step=0, out=100, gen_s=8, tool_s=2, prefix="e",
                               model=SONNET, effort="high"))
    return store, job, proj, d


def _by_key(m):
    return {a["key"]: a for a in m["agents"]}


def test_tracer_worker_and_two_subagents(steward_home, tmp_path):
    store, job, proj, _ = _tracer_job(steward_home, tmp_path)
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)
    assert len(m["agents"]) == 3
    a = _by_key(m)
    w, p, e = a["w"], a["agent-p1"], a["agent-e1"]
    assert (w["stage"], w["kind"], w["unit"]) == ("worker", "worker", None)
    assert (p["stage"], p["kind"], p["unit"], p["unit_src"]) == ("gsd-planner", "plan", (1,), "description")
    assert (e["stage"], e["kind"], e["unit"]) == ("gsd-executor", "build", (1,))
    assert (w["calls"], p["calls"], e["calls"]) == (5, 10, 12)
    assert w["ctx_sum"] == sum(2000 + 100 * k for k in range(5)) and w["ctx_peak"] == 2400
    assert w["ctx_mean"] == w["ctx_sum"] / 5
    assert p["ctx_peak"] == 12000 and p["ctx_sum"] == sum(3000 + 1000 * k for k in range(10))
    assert e["ctx_mean"] == 5000 and e["ctx_sum"] == 60000
    exp_p = sum(tokens.price_usd("claude-opus-5-5", [10, 200, 3000 + 1000 * k - 110, 100]) for k in range(10))
    assert abs(p["usd"] - exp_p) < 1e-9
    assert abs(e["usd"] - _usage_cost(SONNET, 5000, 100, 100, 12)) < 1e-9
    assert abs(e["usd_input_side"] - 12 * tokens.price_usd(SONNET, [10, 0, 4890, 100])) < 1e-9
    assert (p["model"], p["effort"], e["effort"], w["effort"]) == ("claude-opus-5-5", "xhigh", "high", "medium")
    assert p["finished"] and e["finished"] and w["finished"]
    assert p["start"] == T0 + 100 and e["start"] == T0 + 400
    assert m["finished"] == ["agent-e1", "agent-p1", "w"]
    assert [x["key"] for x in m["agents"]] == ["w", "agent-p1", "agent-e1"]  # sorted by (start, key)
    assert m["job_id"] == job.job_id and m["now"] == NOW


def test_visible_chars_and_hidden_thinking(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, out=1000, vis_ratio=0.4, bash=["c" * 186], finished=False, prefix="w"))
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)
    w = m["agents"][0]
    assert w["vis_chars"] == 600
    assert w["think_est"] == 850 and w["think_share"] == 0.85
    assert tokens.visible_chars([{"type": "thinking", "thinking": "secret" * 99}, {"type": "text", "text": "ab"}]) == 2
    assert tokens.visible_chars([{"type": "tool_use", "input": {"a": 1}}]) == len('{"a":1}')


def test_job_tokens_unchanged_by_detail(steward_home, tmp_path):
    store, job, proj, _ = _tracer_job(steward_home, tmp_path)
    r = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert r["by_source"] == {"worker": sum(2000 + 100 * k + 50 for k in range(5)),
                              "chats": 0, "sol": 0, "astra": 0,
                              "sub:gsd-planner": sum(3000 + 1000 * k + 200 for k in range(10)),
                              "sub:gsd-executor": 12 * (5000 + 100)}
    assert tokens.worker_usd(r) == r["total"]["usd"] > 0
    listing = tokens.job_transcripts(store, job.job_id, projects_dir=proj)
    assert [t["source"] for t in listing] == ["worker", "sub:gsd-planner", "sub:gsd-executor"]
    assert listing[1]["description"] == "Plan phase 1" and listing[1]["session"] == "w"
    assert all("detail" in t["entry"] for t in listing)
    cache = json.dumps(tokens.cache_entries(tokens.Path(store.home)))  # one sqlite row per transcript (plan 11)
    assert "Plan phase 1" not in cache and "z" * 10 not in cache  # T-16-04: no text is cached


# -- task 2: stage / unit / time / critical path / projection -------------------------------------------
from dataclasses import asdict  # noqa: E402

from steward.contract import Contract  # noqa: E402


def _dur(calls, gen, tool):
    return calls * gen + (calls - 1) * tool


def _serial_job(steward_home, tmp_path):
    """worker [0,10]; plan1 [10,65]; build1 [75,155]; plan2 [175,215]."""
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, gen_s=10, prefix="w"))
    write_subagent(d, "w", "p1", "gsd-planner", "Plan phase 1", agent_lines(T0 + 10, 4, gen_s=10, tool_s=5, prefix="p1"))
    write_subagent(d, "w", "b1", "gsd-executor", "Execute phase 1", agent_lines(T0 + 75, 3, gen_s=20, tool_s=10, prefix="b1"))
    write_subagent(d, "w", "p2", "gsd-planner", "Plan phase 2", agent_lines(T0 + 175, 3, gen_s=10, tool_s=5, prefix="p2"))
    return store, job, proj, d


def test_parse_unit_from_description_and_occurrence(steward_home, tmp_path):
    assert efficiency.parse_unit("Plan phase 4") == (4,)
    assert efficiency.parse_unit("Execute phase 7 plans") == (7,)
    assert efficiency.parse_unit("Verify phase 2.1") == (2.1,)
    assert efficiency.parse_unit("Classify plan") is None
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, prefix="w"))
    write_subagent(d, "w", "a", "helper", "do a thing", agent_lines(T0 + 20, 2, prefix="a"))
    write_subagent(d, "w", "b", "helper", "do another thing", agent_lines(T0 + 90, 2, prefix="b"))
    a = _by_key(efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj))
    assert (a["agent-a"]["unit"], a["agent-a"]["unit_src"]) == ((1,), "occurrence")
    assert (a["agent-b"]["unit"], a["agent-b"]["unit_src"]) == ((2,), "occurrence")


def test_stage_kind_patterns_and_override():
    k = efficiency.stage_kind
    assert [k(t, "", None) for t in ("gsd-planner", "gsd-executor", "gsd-verifier", "gsd-plan-checker",
                                     "gsd-doc-classifier")] == ["plan", "build", "verify", "verify", "other"]
    assert k("unknown", "Review the diff", None) == "verify" and k("", "Implement it", None) == "build"
    assert k("gsd-executor", "review", None) == "build"  # description only when the type is unknown
    assert k("gsd-doc-classifier", "", {"plan": ("classif",)}) == "plan"


def test_cache_miss_share(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, prefix="w"))
    write_subagent(d, "w", "a", "gsd-executor", "Execute phase 1", agent_lines(T0 + 20, 2, ctx0=1000, cc=100, prefix="a"))
    write_subagent(d, "w", "b", "gsd-executor", "Execute phase 2", agent_lines(T0 + 90, 2, ctx0=1000, cc=500, prefix="b"))
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)
    a = _by_key(m)
    assert abs(a["agent-a"]["cache_miss"] - 100 / 990) < 1e-9 and abs(a["agent-b"]["cache_miss"] - 500 / 990) < 1e-9
    assert abs(m["stages"]["gsd-executor"]["cache_miss"] - 600 / 1980) < 1e-9


def test_generation_tool_time_and_tok_s(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, prefix="w"))
    write_subagent(d, "w", "a", "gsd-executor", "Execute phase 1", agent_lines(T0 + 20, 3, out=100, gen_s=10, tool_s=5, prefix="a"))
    write_subagent(d, "w", "b", "gsd-planner", "Plan phase 1", agent_lines(T0 + 20, 2, out=100, gen_s=700, tool_s=5, prefix="b"))
    a = _by_key(efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj))
    assert (a["agent-a"]["gen_s"], a["agent-a"]["tool_s"], a["agent-a"]["tok_s"]) == (30, 10, 10)
    assert a["agent-b"]["gen_s"] == 1200 and abs(a["agent-b"]["tok_s"] - 200 / 1200) < 1e-9  # clamped at 600 per call
    assert a["w"]["tok_s"] == 100 / 10


def test_max_call_gap(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 4, gen_s=1, tool_s=[5, 40, 5, 0], prefix="w"))
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)
    assert m["agents"][0]["max_call_gap_s"] == 40


def test_idle_wait_handoff_concurrency_and_critical_path(steward_home, tmp_path):
    store, job, proj, _ = _serial_job(steward_home, tmp_path)
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)
    t = m["time"]
    assert m["window"] == {"start": T0, "end": T0 + 215, "wall_s": 215}
    assert t["idle_s"] == 30 and t["idle_gaps"] == [[T0 + 65, T0 + 75], [T0 + 155, T0 + 175]]
    assert t["last_busy"] == T0 + 215 and t["open_tool"] is False
    assert [h["to"] for h in t["handoffs"]] == ["agent-b1", "agent-p2"] and [h["latency_s"] for h in t["handoffs"]] == [10, 20]
    assert t["handoff_s"] == 30
    assert abs(t["concurrency"] - (55 + 80 + 40) / 215) < 1e-9 < 1
    assert t["critical_path"] == {"build": 80, "idle": 30, "plan": 95, "worker": 10} and t["crit"] == "plan"


def test_concurrency_above_one_and_no_handoff_when_overlapping(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, gen_s=10, prefix="w"))
    for n in ("a", "b"):
        write_subagent(d, "w", n, "gsd-executor", f"Execute phase {1 if n == 'a' else 2}", agent_lines(T0, 6, gen_s=10, tool_s=8, prefix=n))
    t = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)["time"]
    assert t["concurrency"] == 2 * _dur(6, 10, 8) / _dur(6, 10, 8) == 2.0
    assert t["handoffs"] == []
    assert t["crit"] == "build"


def test_open_tool_and_unfinished_agent(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, prefix="w"))
    write_subagent(d, "w", "a", "gsd-executor", "Execute phase 1", agent_lines(T0 + 20, 3, prefix="a", finished=False))
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)
    assert m["time"]["open_tool"] is True and m["finished"] == ["w"]


def test_stage_and_unit_rollups(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, prefix="w"))
    write_subagent(d, "w", "p1", "gsd-planner", "Plan phase 1", agent_lines(T0 + 20, 2, gen_s=10, tool_s=5, prefix="p1", effort="xhigh"))
    write_subagent(d, "w", "b1", "gsd-executor", "Execute phase 1", agent_lines(T0 + 100, 3, gen_s=10, tool_s=5, prefix="b1"))
    write_subagent(d, "w", "b2", "gsd-executor", "Execute phase 2", agent_lines(T0 + 200, 3, gen_s=10, tool_s=5, prefix="b2", finished=False))
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)
    a = _by_key(m)
    ex = m["stages"]["gsd-executor"]
    assert ex["kind"] == "build" and ex["agents"] == 2 and ex["calls"] == 6
    assert abs(ex["usd"] - (a["agent-b1"]["usd"] + a["agent-b2"]["usd"])) < 1e-12
    assert ex["out"] == 600 and ex["wall_s"] == a["agent-b1"]["end"] - a["agent-b1"]["start"] + a["agent-b2"]["end"] - a["agent-b2"]["start"]
    assert ex["effort"] == "high" and m["stages"]["gsd-planner"]["effort"] == "xhigh" and m["stages"]["worker"]["kind"] == "worker"
    assert ex["gen_s"] == a["agent-b1"]["gen_s"] + a["agent-b2"]["gen_s"]
    u = {x["unit"]: x for x in m["units"]}
    assert set(u) == {(1,), (2,)}
    assert abs(u[(1,)]["usd"] - (a["agent-p1"]["usd"] + a["agent-b1"]["usd"])) < 1e-12
    assert u[(1,)]["complete"] is True and u[(2,)]["complete"] is False


def test_unit_complete_needs_a_finished_build_agent_when_the_job_has_builders(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, prefix="w"))
    write_subagent(d, "w", "p1", "gsd-planner", "Plan phase 1", agent_lines(T0 + 20, 2, prefix="p1"))
    write_subagent(d, "w", "p2", "gsd-planner", "Plan phase 2", agent_lines(T0 + 120, 2, prefix="p2"))
    write_subagent(d, "w", "b2", "gsd-executor", "Execute phase 2", agent_lines(T0 + 220, 2, prefix="b2"))
    u = {x["unit"]: x["complete"] for x in efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)["units"]}
    assert u == {(1,): False, (2,): True}  # unit 1 has no finished builder
    # a job with no build kind at all: every agent finished is enough
    s2, j2, p2, d2 = setup_job(steward_home, tmp_path / "two")
    write_worker(d2, agent_lines(T0, 1, prefix="w"))
    write_subagent(d2, "w", "p1", "gsd-planner", "Plan phase 1", agent_lines(T0 + 20, 2, prefix="p1"))
    assert efficiency.compute(s2, j2.job_id, now=NOW, projects_dir=p2)["units"][0]["complete"] is True


def test_projection_only_with_units_total(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, prefix="w"))
    for n, (t0, done) in enumerate([(20, True), (140, True), (260, False)], start=1):
        write_subagent(d, "w", f"b{n}", "gsd-executor", f"Execute phase {n}",
                       agent_lines(T0 + t0, 3, gen_s=10, tool_s=5, prefix=f"b{n}", finished=done))
    assert efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)["projection"] is None
    store.kv_set(job.job_id, "contract", asdict(Contract(goal="g", units_total=5)))
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)
    a, pj = _by_key(m), m["projection"]
    assert pj["units_total"] == 5 and pj["units_complete"] == 2 and pj["remaining"] == 3
    assert abs(pj["usd_per_unit"] - (a["agent-b1"]["usd"] + a["agent-b2"]["usd"]) / 2) < 1e-12
    assert abs(pj["min_per_unit"] - 40 / 60) < 1e-9  # each complete unit spans 40 s
    assert abs(pj["projected_usd"] - 3 * pj["usd_per_unit"]) < 1e-12


def test_supervision_by_source(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 2, prefix="w"))
    write_subagent(d, "w", "b1", "gsd-executor", "Execute phase 1", agent_lines(T0 + 20, 2, prefix="b1"))
    write_worker(d, agent_lines(T0, 1, prefix="s", user_text="A coding worker under a durable job runner appears stalled. x"), "solx")
    write_worker(d, agent_lines(T0, 2, prefix="c"), "fork")
    write_worker(d, agent_lines(T0, 1, prefix="x", user_text="You are a Steward custom watcher named pace. go"), "wat")
    store.kv_set(job.job_id, "trajectory_advice:r1", {"answered_at": T0, "usage": {"input": 1000, "output": 0, "cache_read": 0, "cache_creation": 0, "ts": T0}})
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)
    one = tokens.price_usd(SONNET, [10, 100, 890, 100])
    sup = m["supervision"]
    assert set(sup["by_source"]) == {"astra", "chats", "sol", "watcher:pace"}
    assert abs(sup["by_source"]["sol"] - one) < 1e-12 and abs(sup["by_source"]["chats"] - 2 * one) < 1e-12
    worker_usd = tokens.worker_usd(tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj))
    assert abs(sup["usd"] - sum(sup["by_source"].values())) < 1e-12
    assert abs(sup["share_of_worker"] - sup["usd"] / worker_usd) < 1e-12
    assert abs(m["totals"]["usd"] - worker_usd) < 1e-12 and abs(m["totals"]["sub_share"] - 2 / 4) < 1e-9


def test_artifacts_and_big_results(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 1, prefix="w"))
    write_subagent(d, "w", "p1", "gsd-planner", "Plan phase 1",
                   agent_lines(T0 + 20, 2, prefix="p1", writes=[("/w/PLAN.md", 5000)], bash=[]))
    write_subagent(d, "w", "b1", "gsd-executor", "Execute phase 1",
                   agent_lines(T0 + 200, 4, prefix="b1", reads=[("/w/PLAN.md", True), ("/w/PLAN.md", False)],
                               bash=["pytest -q"], tool_results=[0, 0, 9000]))
    write_subagent(d, "w", "b2", "gsd-executor", "Execute phase 2",
                   agent_lines(T0 + 400, 2, prefix="b2", bash=["pytest -q"], tool_results=[3000]))
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj)
    (art,) = m["artifacts"]
    assert (art["writer"], art["stage"], art["chars"]) == ("agent-p1", "gsd-planner", 5000)
    assert art["path_key"] == tokens._key10("/w/PLAN.md") and art["whole_readers"] == ["agent-b1"]
    assert m["big_results"][tokens._key10("pytest -q")] == [3000, 9000]


def test_contract_units_total_defaults_and_round_trips():
    old = {"goal": "g", "boundaries": [], "done_when": [], "next_step": "", "authority": [], "assumptions": [],
           "revision": 1, "research": False, "keys": [], "follows": ""}
    assert Contract(**old).units_total == 0
    c = Contract(goal="g", units_total=7)
    assert Contract(**asdict(c)) == c and asdict(c)["units_total"] == 7


def test_to_json_is_byte_stable(steward_home, tmp_path):
    store, job, proj, _ = _serial_job(steward_home, tmp_path)
    s1 = efficiency.to_json(efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj))
    s2 = efficiency.to_json(efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj, _cache={}))
    assert s1 == s2 and json.loads(s1)["window"]["wall_s"] == 215


# -- task 3: incremental parse and the tick budget -----------------------------------------------------------
import time  # noqa: E402
from pathlib import Path  # noqa: E402


def _entry(store, job, proj, cache=None):
    (tr,) = tokens.job_transcripts(store, job.job_id, projects_dir=proj, _cache=cache)
    return tr["entry"]


def _same(a, b):
    strip = lambda e: {k: v for k, v in e.items() if k not in ("sig", "agent_type")}  # noqa: E731
    return json.dumps(strip(a), sort_keys=True) == json.dumps(strip(b), sort_keys=True)


def test_incremental_parse_equals_full_parse(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    lines = agent_lines(T0, 6, out=80, vis_ratio=0.5, gen_s=3, tool_s=1, prefix="w", writes=[("/a", 10)])
    f, ref, cache = d / "w.jsonl", tmp_path / "ref.jsonl", {}

    def step(text, complete):
        with open(f, "a") as fh:
            fh.write(text)
        ref.write_text("".join(ln + "\n" for ln in lines[:complete]))
        ent = _entry(store, job, proj, cache)
        assert _same(ent, tokens._parse_file(ref)), complete
        assert ent["offset"] == ref.stat().st_size  # a partial trailing line is not consumed

    step("".join(ln + "\n" for ln in lines[:7]), 7)
    step("".join(ln + "\n" for ln in lines[7:15]) + lines[15][:25], 15)  # the chunk ends mid-line
    step(lines[15][25:] + "\n" + "".join(ln + "\n" for ln in lines[16:]), len(lines))


def test_incremental_parse_resumes_instead_of_reparsing(steward_home, tmp_path, monkeypatch):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    lines = agent_lines(T0, 4, prefix="w")
    f, cache = write_worker(d, lines[:5]), {}
    _entry(store, job, proj, cache)
    seen = []
    real = tokens._parse_file
    monkeypatch.setattr(tokens, "_parse_file", lambda p, **kw: seen.append(kw.get("start", 0)) or real(p, **kw))
    with open(f, "a") as fh:
        fh.write("".join(ln + "\n" for ln in lines[5:]))
    _entry(store, job, proj, cache)
    assert len(seen) == 1 and seen[0] > 0  # resumed from the stored byte offset, not from 0


def test_incremental_parse_keeps_dedupe_and_markers(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    first = agent_lines(T0, 1, out=10, prefix="x", finished=False, user_text="You are a Steward custom watcher named pace. go")
    rep = [ln.replace('"output_tokens": 10', '"output_tokens": 50') for ln in first[1:]]
    f, cache = write_worker(d, first), {}
    ent = _entry(store, job, proj, cache)
    assert ent["watcher"] == "pace" and ent["msgs"][0][3] == 10
    with open(f, "a") as fh:
        fh.write("".join(ln + "\n" for ln in rep))  # a streamed repeat of the same id, larger output
    ent = _entry(store, job, proj, cache)
    assert ent["watcher"] == "pace" and len(ent["msgs"]) == 1 and ent["msgs"][0][3] == 50
    assert ent["cost"]["x-m0"] == tokens.price_usd(SONNET, [10, 50, 890, 100])
    assert _same(ent, tokens._parse_file(f))


def test_shrunk_file_is_fully_reparsed(steward_home, tmp_path, monkeypatch):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    f, cache = write_worker(d, agent_lines(T0, 6, prefix="w")), {}
    assert len(_entry(store, job, proj, cache)["msgs"]) == 6
    seen = []
    real = tokens._parse_file
    monkeypatch.setattr(tokens, "_parse_file", lambda p, **kw: seen.append(kw.get("start", 0)) or real(p, **kw))
    write_worker(d, agent_lines(T0, 2, prefix="n"))
    ent = _entry(store, job, proj, cache)
    assert seen == [0] and [m[0] for m in ent["msgs"]] == ["n-m0", "n-m1"] and _same(ent, real(f))


def test_tick_recompute_under_one_second_cpu(steward_home, tmp_path):
    store, job, proj, d = setup_job(steward_home, tmp_path)
    write_worker(d, agent_lines(T0, 10, prefix="w"))
    files = []
    for n in range(30):
        files.append(write_subagent(d, "w", f"a{n}", "gsd-executor", f"Execute phase {n + 1}",
                                    agent_lines(T0 + n, 100, ctx0=4000, ctx_step=10, vis_ratio=0.3, prefix=f"a{n}")))
    cache = {}
    efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj, _cache=cache)  # warm
    extra = agent_lines(T0 + 5000, 3, prefix="zz")[1:]
    with open(files[7], "a") as fh:
        fh.write("".join(ln + "\n" for ln in extra))
    c0 = time.process_time()
    listing = tokens.job_transcripts(store, job.job_id, projects_dir=proj, _cache=cache)
    m = efficiency.compute(store, job.job_id, now=NOW, projects_dir=proj, transcripts=listing, _cache=cache)
    assert time.process_time() - c0 < 1.0
    assert m["totals"]["calls"] == 10 + 30 * 100 + 3 and len(m["agents"]) == 31
