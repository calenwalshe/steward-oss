"""Token accounting: dedupe across forks, last-hour window, attribution, caching,
dashboard line + map alignment, reviewer usage parsing."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from steward.roles import role_label
from steward import review, tokens
from steward.chat import project_slug, worktree_path
from steward.models import JobState
from steward.store import Store

_REPO = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def _msg(mid, ts, i=1, o=2, cr=3, cc=4):
    return json.dumps({"type": "assistant", "timestamp": _iso(ts), "message": {"id": mid, "usage": {
        "input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc}}})


def _user(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


def _setup(steward_home, tmp_path):
    store = Store()
    job = store.create_job("g", "/tmp/repo")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    proj = tmp_path / "projects"
    d = proj / project_slug(worktree_path(store, job.job_id))
    d.mkdir(parents=True)
    return store, job, proj, d


def test_dedupe_attribution_window_and_astra(steward_home, tmp_path):
    store, job, proj, d = _setup(steward_home, tmp_path)
    store.kv_set(job.job_id, "worker_session_id", "w")
    old, new = NOW - 7200, NOW - 60
    # worker: m1 old, m2 twice (streamed lines), m3 recent
    (d / "w.jsonl").write_text("\n".join([_user("task"), _msg("m1", old), _msg("m2", old), _msg("m2", old), _msg("m3", new)]))
    # fork copies m1..m3 and adds m4
    (d / "fork.jsonl").write_text("\n".join([_user("task"), _msg("m1", old), _msg("m2", old), _msg("m3", new), _msg("m4", new)]))
    (d / "sol.jsonl").write_text("\n".join([_user("A coding worker under a durable job runner appears stalled. x"), _msg("s1", new)]))
    store.kv_set(job.job_id, "trajectory_advice:r1", {"answered_at": new, "usage": {"input": 10, "output": 0, "cache_read": 0, "cache_creation": 0, "ts": new}})
    store.kv_set(job.job_id, "trajectory_advice:r0", {"answered_at": old})  # no usage: not counted
    r = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert r["by_source"] == {"worker": 30, "chats": 10, "sol": 10, "astra": 10}
    assert r["total"]["all"] == 60 and r["total"]["output"] == 10
    assert r["last_hour"]["all"] == 10 + 10 + 10 + 10  # m3, m4, s1, astra
    # per-model column: total / last hour / output per source
    assert {k: v["all"] for k, v in r["sources"].items()} == r["by_source"]
    assert r["sources"]["worker"]["hour"] == 10 and r["sources"]["chats"]["hour"] == 10
    assert sum(v["out"] for v in r["sources"].values()) == r["total"]["output"]


def test_worker_fallback_newest_file(steward_home, tmp_path):
    store, job, proj, d = _setup(steward_home, tmp_path)
    (d / "a.jsonl").write_text(_msg("x", NOW))
    r = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert r["by_source"]["worker"] == 10


def test_cache_reused_until_file_changes(steward_home, tmp_path, monkeypatch):
    store, job, proj, d = _setup(steward_home, tmp_path)
    f = d / "a.jsonl"
    f.write_text(_msg("x", NOW))
    calls = []
    real = tokens._parse_file
    monkeypatch.setattr(tokens, "_parse_file", lambda p, **kw: calls.append(p) or real(p, **kw))
    tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert len(calls) == 1  # second call served from the on-disk cache
    f.write_text(_msg("x", NOW) + "\n" + _msg("y", NOW))
    r = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert len(calls) == 2 and r["total"]["all"] == 20


def test_humanize():
    assert [tokens.humanize(n) for n in (5, 1500, 33000, 6_400_000)] == ["5", "1.5k", "33k", "6.4M"]


def test_reviewer_parses_usage():
    verdict = {"trajectory": "sound", "confidence": 0.9, "urgency": "low", "concern": "none", "observation": "ok",
               "recommendation": {"action": "continue", "instruction": "", "current_approach": "a", "recommended_approach": "a"}}
    out = json.dumps({"result": json.dumps(verdict), "total_cost_usd": 0.1, "duration_ms": 5,
                      "usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}})
    res = review._parse_review(out)
    assert res.usage == {"input": 1, "output": 2, "cache_read": 3, "cache_creation": 4}
    assert review._parse_review(json.dumps({"result": "nope", "usage": {"output_tokens": 7}})).usage["output"] == 7
    assert review._parse_review(json.dumps({"result": json.dumps(verdict)})).usage is None


def test_renderer_token_lines_and_map_alignment(steward_home, tmp_path):
    store = Store()
    job = store.create_job("do the thing", "/tmp/somerepo")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    store.kv_set(job.job_id, "herdr_tab", "w1:t2")
    store.kv_set(job.job_id, "trajectory_advice:r", {"answered_at": time.time(), "usage": {"input": 1200000, "output": 5000, "cache_read": 0, "cache_creation": 0}})
    map_path = tmp_path / "map"
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_DASH_COLS": "60",
           "STEWARD_DASH_MAP": str(map_path), "PYTHONPATH": str(_REPO), "HOME": str(tmp_path)}
    res = subprocess.run([sys.executable, str(_REPO / "deploy/cockpit/steward-dash-jobs.py")],
                         capture_output=True, text=True, env=env, cwd=_REPO)
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln).replace("{SPIN}", "x") for ln in res.stdout.splitlines()]
    assert all(len(ln) <= 60 for ln in lines), lines
    assert any(ln.strip().startswith("tokens 1.2M") for ln in lines), lines
    assert any(ln.strip().startswith("cached 0 · fresh 1.2M · out 5.0k") for ln in lines[3:]), lines  # per job
    assert lines[0].startswith("ALL JOBS  tokens 1.2M") and "out 5.0k" in lines[2]  # total up top
    assert lines[1].strip().startswith("effective ") and "($" in lines[1]  # cost-weighted, with dollars
    for a in map_path.read_text().splitlines():
        if " chat " in a:
            assert role_label(a.split()[-1]) in lines[int(a.split()[1])]


def test_astra_transcripts_go_to_the_nearest_review_request(steward_home, tmp_path):
    store, job, proj, d = _setup(steward_home, tmp_path)
    other = store.create_job("other", "/repo")
    for jid, rid, at in ((job.job_id, "r-a", NOW - 120), (other.job_id, "r-b", NOW - 60)):
        store._conn.execute(
            "INSERT INTO events (job_id, ts, what, evidence, rule, judgment) VALUES (?, ?, ?, ?, 'r', 'code')",
            (jid, _iso(at).replace("Z", "+00:00"), "trajectory_review_requested", json.dumps({"review_id": rid})),
        )
    astra_dir = proj / project_slug(tokens._ASTRA_DIR)
    astra_dir.mkdir(parents=True)

    def review(name, start, mid):
        user = json.dumps({"type": "user", "timestamp": _iso(start),
                           "message": {"role": "user", "content": tokens._ASTRA_MARKER + " ..."}})
        (astra_dir / f"{name}.jsonl").write_text("\n".join([user, _msg(mid, start + 5)]))

    review("a", NOW - 118, "am")  # 2s after job's request, 58s before other's
    review("b", NOW - 55, "bm")   # 65s after job's request but only 5s after other's
    (astra_dir / "chat.jsonl").write_text("\n".join([_user("hello"), _msg("cm", NOW)]))  # not a review
    # the review with a transcript doesn't also count its advice usage
    store.kv_set(job.job_id, "trajectory_advice:r-a", {"usage": {"input": 99, "ts": NOW}})

    mine = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    theirs = tokens.job_tokens(store, other.job_id, now=NOW, projects_dir=proj)
    assert mine["by_source"]["astra"] == 10 and theirs["by_source"]["astra"] == 10


def test_price_and_effective_tokens_use_published_rates():
    # Sonnet 5.5: $2 in, $10 out, $0.20 cache read, cache write 1.25x input
    assert tokens.price_usd("claude-sonnet-5-5", [1_000_000, 0, 0, 0]) == 2.0
    assert tokens.price_usd("claude-sonnet-5-5", [0, 1_000_000, 0, 0]) == 10.0
    assert tokens.price_usd("claude-sonnet-5-5", [0, 0, 1_000_000, 0]) == 0.2
    assert tokens.price_usd("claude-sonnet-5-5", [0, 0, 0, 1_000_000]) == 2.5
    assert tokens.price_usd("claude-opus-5-5", [0, 1_000_000, 0, 0]) == 20.0
    assert tokens.price_usd("claude-fable-5-1", [0, 1_000_000, 0, 0]) == 50.0
    assert tokens.price_usd("opus", [1_000_000, 0, 0, 0]) == 4.0  # role alias
    # one Sonnet output token is worth five effective (Sonnet-input) tokens
    assert int(tokens.price_usd("claude-sonnet-5-5", [0, 1000, 0, 0]) / tokens.EFFECTIVE_UNIT_USD) == 5000


def test_all_tokens_with_no_jobs_has_every_field(steward_home):
    g = tokens.all_tokens(Store(steward_home), [])["grand"]
    assert g["total"]["effective"] == 0 and g["last_hour"]["usd"] == 0.0 and g["total"]["all"] == 0


# -- phase 16 (G1): subagent transcripts counted under sub:<agentType> ---------------------------------


def _sub(d, session, agent, msgs, agent_type="gsd-executor", meta=True):
    sd = d / session / "subagents"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / f"agent-{agent}.jsonl").write_text("\n".join(msgs))
    if meta is True:
        (sd / f"agent-{agent}.meta.json").write_text(json.dumps({"agentType": agent_type, "description": "x"}))
    elif isinstance(meta, str):
        (sd / f"agent-{agent}.meta.json").write_text(meta)
    return sd / f"agent-{agent}.jsonl"


def test_subagents_counted_under_sub_agent_type(steward_home, tmp_path):
    store, job, proj, d = _setup(steward_home, tmp_path)
    store.kv_set(job.job_id, "worker_session_id", "w")
    new = NOW - 60
    (d / "w.jsonl").write_text("\n".join([_user("task"), _msg("m1", new)]))
    _sub(d, "w", "a1", [_msg("e1", new), _msg("e2", new)], "gsd-executor")
    _sub(d, "w", "a2", [_msg("p1", new)], "gsd-planner")
    r = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert r["by_source"] == {"worker": 10, "chats": 0, "sol": 0, "astra": 0, "sub:gsd-executor": 20, "sub:gsd-planner": 10}
    assert r["sources"]["sub:gsd-executor"]["usd"] == 2 * tokens.price_usd(None, [1, 2, 3, 4])
    assert r["total"]["all"] == 40
    assert abs(r["total"]["usd"] - sum(s["usd"] for s in r["sources"].values())) < 1e-12


def test_subagent_without_or_with_bad_meta_is_sub_unknown(steward_home, tmp_path):
    store, job, proj, d = _setup(steward_home, tmp_path)
    store.kv_set(job.job_id, "worker_session_id", "w")
    (d / "w.jsonl").write_text(_msg("m1", NOW))
    _sub(d, "w", "a1", [_msg("u1", NOW)], meta=False)
    _sub(d, "w", "a2", [_msg("u2", NOW)], meta="{not json")
    _sub(d, "w", "a3", [_msg("u3", NOW)], meta=json.dumps({"description": "no type"}))
    _sub(d, "w", "a4", [_msg("u4", NOW)], meta=json.dumps({"agentType": ""}))
    r = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert r["by_source"]["sub:unknown"] == 40
    assert not [k for k in r["by_source"] if k.startswith("sub:") and k != "sub:unknown"]


def test_subagent_meta_appearing_later_is_picked_up(steward_home, tmp_path):
    store, job, proj, d = _setup(steward_home, tmp_path)
    store.kv_set(job.job_id, "worker_session_id", "w")
    (d / "w.jsonl").write_text(_msg("m1", NOW))
    f = _sub(d, "w", "a1", [_msg("u1", NOW)], meta=False)
    assert tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)["by_source"]["sub:unknown"] == 10
    f.with_suffix(".meta.json").write_text(json.dumps({"agentType": "gsd-verifier"}))
    r = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert r["by_source"]["sub:gsd-verifier"] == 10 and "sub:unknown" not in r["by_source"]


def test_subagent_ids_dedupe_against_worker_and_streamed_repeats(steward_home, tmp_path):
    store, job, proj, d = _setup(steward_home, tmp_path)
    store.kv_set(job.job_id, "worker_session_id", "w")
    (d / "w.jsonl").write_text("\n".join([_msg("m1", NOW)]))
    _sub(d, "w", "a1", [_msg("m1", NOW), _msg("s1", NOW, o=2), _msg("s1", NOW, o=9)])
    r = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert r["by_source"]["worker"] == 10  # shared id stays with the worker
    assert r["by_source"]["sub:gsd-executor"] == 1 + 9 + 3 + 4  # streamed repeat keeps the larger output
    assert r["total"]["output"] == 2 + 9


def test_subagents_of_every_session_dir_counted_other_dirs_ignored(steward_home, tmp_path):
    store, job, proj, d = _setup(steward_home, tmp_path)
    store.kv_set(job.job_id, "worker_session_id", "w")
    (d / "w.jsonl").write_text(_msg("m1", NOW))
    (d / "other-session.jsonl").write_text(_msg("c1", NOW))
    _sub(d, "other-session", "b", [_msg("b1", NOW)], "gsd-planner")
    (d / "w" / "tool-results").mkdir(parents=True)
    (d / "w" / "tool-results" / "x.jsonl").write_text(_msg("t1", NOW))
    (d / "w" / "subagents" / "nested").mkdir(parents=True)
    (d / "w" / "subagents" / "nested" / "y.jsonl").write_text(_msg("n1", NOW))
    r = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert r["by_source"] == {"worker": 10, "chats": 10, "sol": 0, "astra": 0, "sub:gsd-planner": 10}


def test_subagent_parse_uses_the_shared_cache(steward_home, tmp_path, monkeypatch):
    store, job, proj, d = _setup(steward_home, tmp_path)
    store.kv_set(job.job_id, "worker_session_id", "w")
    (d / "w.jsonl").write_text(_msg("m1", NOW))
    f1 = _sub(d, "w", "a1", [_msg("e1", NOW)])
    f2 = _sub(d, "w", "a2", [_msg("e2", NOW)], "gsd-planner")
    calls = []
    real = tokens._parse_file
    monkeypatch.setattr(tokens, "_parse_file", lambda p, **kw: calls.append(p) or real(p, **kw))
    tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert len(calls) == 3
    calls.clear()
    tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert calls == []  # unchanged files are never reparsed
    f1.write_text(f1.read_text() + "\n" + _msg("e3", NOW))
    r = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert calls == [f1] and r["by_source"]["sub:gsd-executor"] == 20
    keys = tokens.cache_entries(Path(store.home))  # one sqlite row per transcript (TUI speed plan 11)
    assert str(f1) in keys and str(f2) in keys and keys[str(f1)]["agent_type"] == "gsd-executor"


def test_subagents_in_last_hour_and_all_tokens(steward_home, tmp_path):
    store, job, proj, d = _setup(steward_home, tmp_path)
    store.kv_set(job.job_id, "worker_session_id", "w")
    (d / "w.jsonl").write_text(_msg("m1", NOW - 7200))
    _sub(d, "w", "a1", [_msg("old", NOW - 7200), _msg("new", NOW - 60)])
    r = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert r["last_hour"]["all"] == 10 and r["sources"]["sub:gsd-executor"]["hour"] == 10
    assert r["last_hour"]["usd"] == r["sources"]["sub:gsd-executor"]["usd_hour"] > 0
    g = tokens.all_tokens(store, [job.job_id], now=NOW, projects_dir=proj)["grand"]
    assert g["total"]["all"] == 30 and g["last_hour"]["all"] == 10


# -- phase 16 (G1/G11): corrected worker spend feeds every budget ---------------------------------------


def _costly(mid, ts, usd):
    """One sonnet-5.5 message costing exactly `usd` (output tokens at $10/M)."""
    return _msg(mid, ts, i=0, o=int(usd / 10 * 1_000_000), cr=0, cc=0)


def _spend_setup(steward_home, tmp_path, worker=0.10, sub=5.00, watcher=0.60):
    store, job, proj, d = _setup(steward_home, tmp_path)
    store.kv_set(job.job_id, "worker_session_id", "w")
    ts = NOW - 60
    (d / "w.jsonl").write_text("\n".join([_user("task"), _costly("w1", ts, worker)]))
    _sub(d, "w", "a1", [_costly("e1", ts, sub)], "gsd-executor")
    (d / "wd.jsonl").write_text("\n".join([_user("You are a Steward custom watcher named drift\nx"), _costly("d1", ts, watcher)]))
    return store, job, proj


def test_worker_usd_sums_worker_and_subagents():
    t = {"sources": {"worker": {"usd": 1, "usd_hour": 0.5}, "sub:x": {"usd": 9, "usd_hour": 2}, "watcher:w": {"usd": 3}}}
    assert tokens.worker_usd(t) == 10.0 and tokens.worker_usd(t, hour=True) == 2.5
    assert tokens.worker_usd({}) == 0.0 and tokens.worker_usd({"sources": {"worker": {}}}) == 0.0
    assert tokens.worker_usd(None) == 0.0


def test_supervision_cap_uses_worker_plus_subagents(steward_home, tmp_path):
    from steward import custom_watchers as cw

    store, job, proj = _spend_setup(steward_home, tmp_path)
    watchers = cw.CustomWatchers(store, clock=lambda: NOW, projects_dir=proj)
    worker, sup = watchers._spend(job.job_id)
    assert (round(worker, 2), round(sup, 2)) == (5.10, 0.60)
    assert watchers._observe_only(job.job_id, NOW) is False  # 0.60 < 25% of 5.10; worker-only 0.10 would trip


def test_spend_hour_predicate_includes_subagents(steward_home, tmp_path):
    from steward import custom_watchers as cw

    store, job, proj = _spend_setup(steward_home, tmp_path)
    watchers = cw.CustomWatchers(store, clock=lambda: NOW, projects_dir=proj)
    assert round(watchers._spend_hour(job.job_id), 2) == 5.10


def test_overspend_ratio_uses_corrected_worker_spend(steward_home, tmp_path):
    from steward import stability

    store, job, proj = _spend_setup(steward_home, tmp_path, watcher=0.90)
    t = tokens.job_tokens(store, job.job_id, now=NOW, projects_dir=proj)
    assert round(t["sources"]["watcher:drift"]["usd"], 2) == 0.90
    assert stability.spend_findings(t)["overspenders"] == []
    assert stability.spend_warnings({**t, "last_hour": {"usd": 0.0}}) == []
    # without the subagent spend the same watcher IS an overspender
    no_sub = {"sources": {k: v for k, v in t["sources"].items() if not k.startswith("sub:")}, "last_hour": {"usd": 0.0}}
    assert [o[0] for o in stability.spend_findings(no_sub)["overspenders"]] == ["drift"]


# ---- audit rec 10: the cost ledger bills what Anthropic bills ---------------------------------


def test_one_hour_cache_writes_bill_at_2x_input_and_five_minute_at_1_25x():
    px = tokens.price_usd
    assert px("claude-sonnet-5-5", [0, 0, 0, 1_000_000], 1_000_000) == 4.0  # all 1h: 2 x $2
    assert px("claude-sonnet-5-5", [0, 0, 0, 1_000_000], 400_000) == 0.4 * 4.0 + 0.6 * 2.5
    assert px("claude-sonnet-5-5", [0, 0, 0, 1_000_000]) == 2.5  # no TTL split: the 5-minute rate
    assert px("claude-sonnet-5-5", [0, 0, 0, 100], 10**9) == px("claude-sonnet-5-5", [0, 0, 0, 100], 100)  # clamped


def test_fable_cache_reads_bill_at_a_quarter_dollar():
    assert tokens.price_usd("claude-fable-5-1", [0, 0, 1_000_000, 0]) == 0.25
    assert tokens.price_usd("claude-fable-5-1", [0, 0, 0, 1_000_000], 1_000_000) == 20.0


def test_transcript_cost_uses_the_ttl_split_from_usage(steward_home, tmp_path):
    store, job, proj, d = _setup(steward_home, tmp_path)
    store.kv_set(job.job_id, "worker_session_id", "w")
    line = json.dumps({"type": "assistant", "timestamp": _iso(NOW - 60), "message": {
        "id": "m1", "model": "claude-sonnet-5-5", "usage": {
            "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 1_000_000,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1_000_000}}}})
    (d / "w.jsonl").write_text("\n".join([_user("task"), line]))
    listing = tokens.job_transcripts(store, job.job_id, proj)
    assert listing[0]["entry"]["cost"]["m1"] == 4.0
    assert listing[0]["entry"]["cc1h"]["m1"] == 1_000_000


def test_cached_entries_priced_under_old_rates_are_reparsed(steward_home, tmp_path):
    store, job, proj, d = _setup(steward_home, tmp_path)
    store.kv_set(job.job_id, "worker_session_id", "w")
    (d / "w.jsonl").write_text("\n".join([_user("task"), _msg("m1", NOW - 60, i=0, o=0, cr=0, cc=1_000_000)]))
    tokens.job_transcripts(store, job.job_id, proj)
    cache = tokens.cache_entries(Path(store.home))  # one sqlite row per transcript (TUI speed plan 11)
    (key,) = [k for k in cache if k.endswith("w.jsonl")]
    cache[key]["rates"] = 1
    cache[key]["cost"] = {"m1": 999.0}
    tokens._save_cache(Path(store.home), cache)
    listing = tokens.job_transcripts(store, job.job_id, proj)
    assert listing[0]["entry"]["cost"]["m1"] != 999.0
