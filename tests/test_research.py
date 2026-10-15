"""Sanctioned research access: job capabilities, per-job mcp.json, granted keys,
masking, permissions, spend cap, cap-raise verb, feed and dashboard row."""

from __future__ import annotations

import json
import os
import stat

import pytest
from test_runner_fabric import (_FabricFactory, _contracted_job, _fake_diagnoser, _frame, _git_runner, _obs,
                                _Clock)

from steward import approvals as approvals_module
from steward import cli, exceptions, fabric, masking, permissions, research, supervisors, transcript, watchers
from steward.diagnosis import redact
from steward.contract import load_contract, render
from steward.runner import Runner
from steward.store import Store

FAKE_SECRET = "sk-test-FAKESECRETVALUE123456"
FAKE_BEARER = "tok-FAKEBEARERVALUE987654"


@pytest.fixture
def claude_json(tmp_path, monkeypatch):
    p = tmp_path / "fake.claude.json"
    p.write_text(json.dumps({
        "mcpServers": {
            "research": {"type": "http", "url": "https://research.example/mcp",
                         "headers": {"Authorization": f"Bearer {FAKE_BEARER}"}},
            "other": {"type": "http", "url": "https://other.example/mcp"},
        },
        "unrelated": {"x": 1},
    }))
    monkeypatch.setenv("STEWARD_CLAUDE_JSON", str(p))
    return p


@pytest.fixture
def keys_file(tmp_path, monkeypatch):
    p = tmp_path / "fake-api-keys"
    p.write_text(
        "# comment\n"
        f"export SCRAPECREATORS_API_KEY={FAKE_SECRET}\n"
        "FIRECRAWL_API_KEY='fc-quoted-value-0001'\n"
        'export TAVILY_API_KEY="tv-dquoted-value-0002"  \n'
        "SERPAPI_KEY=serp-bare-value-0003 # trailing comment\n"
        "\n"
        "not a line\n"
    )
    monkeypatch.setenv("STEWARD_API_KEYS_FILE", str(p))
    return p


# -- arg parsing + contract text ------------------------------------------------

def test_new_flags_parse_and_render_contract(steward_home, keys_file, capsys):
    rc = cli.main(["new", "look things up", "--repo", "/repo", "--research",
                   "--grant-keys", "ScrapeCreators,FIRECRAWL_API_KEY", "--research-cap", "2.5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "RESEARCH: on" in out
    assert "KEYS: SCRAPECREATORS_API_KEY, FIRECRAWL_API_KEY" in out
    job_id = out.split()[0]
    store = Store(steward_home)
    assert research.get_caps(store, job_id) == {
        "research": True, "keys": ["SCRAPECREATORS_API_KEY", "FIRECRAWL_API_KEY"], "cap_usd": 2.5}
    granted = [e.evidence for e in store.events(job_id) if e.what == "capability_granted"]
    assert {"capability": "research", "cap_usd": 2.5, "cap_source": "owner_explicit"} in granted
    assert {"capability": "key", "name": "SCRAPECREATORS_API_KEY"} in granted
    assert FAKE_SECRET not in json.dumps(granted)
    assert "RESEARCH: on" in render(load_contract(store, job_id))


def test_new_without_capabilities_is_unchanged(steward_home, capsys):
    cli.main(["new", "plain", "--repo", "/repo"])
    out = capsys.readouterr().out
    assert "RESEARCH" not in out and "KEYS" not in out


def test_unknown_key_is_reported_not_granted(steward_home, keys_file, capsys):
    cli.main(["new", "g", "--repo", "/repo", "--grant-keys", "NOPE_KEY"])
    cap = capsys.readouterr()
    assert "key not granted" in cap.err and "KEYS:" not in cap.out


def test_op_and_args_for_new_and_cap():
    ns = cli._build_parser().parse_args(["new", "g", "--research", "--grant-keys", "A_KEY,B_KEY", "--research-cap", "3"])
    op, args = cli._op_and_args_for(ns)
    assert op == "new" and args["research"] is True and args["grant_keys"] == ["A_KEY", "B_KEY"] and args["research_cap"] == 3.0
    ns = cli._build_parser().parse_args(["research-cap", "job1", "12"])
    assert cli._op_and_args_for(ns) == ("research_cap", {"job_id": "job1", "usd": 12.0})


# -- mcp.json -------------------------------------------------------------------

def test_mcp_json_has_only_research_server_and_is_0600(steward_home, claude_json):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    research.set_caps(store, job.job_id, research=True, keys=[])
    path = research.write_mcp_config(job.job_id)
    assert path == steward_home / "jobs" / job.job_id / "mcp.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(path.read_text())
    assert list(data["mcpServers"]) == ["research"] and "unrelated" not in data
    assert data["mcpServers"]["research"]["url"] == "https://research.example/mcp"


def test_worker_argv_has_mcp_flags_only_when_research_on(steward_home, claude_json):
    store = Store(steward_home)
    on = store.create_job("g", "/repo")
    off = store.create_job("g", "/repo")
    research.set_caps(store, on.job_id, research=True, keys=[])
    kw = dict(agent="claude", agent_script=None, model="haiku", extra_args=[])
    argv_on = cli._build_shim_agent_argv(store, on.job_id, **kw)
    argv_off = cli._build_shim_agent_argv(store, off.job_id, **kw)
    assert "--mcp-config" in argv_on and "--strict-mcp-config" in argv_on
    assert argv_on[argv_on.index("--mcp-config") + 1].endswith(f"{on.job_id}/mcp.json")
    assert "--mcp-config" not in argv_off and "--strict-mcp-config" not in argv_off


# -- key loader -------------------------------------------------------------------

def test_key_loader_parses_both_styles_and_returns_only_granted(keys_file):
    got = research.load_keys(["SCRAPECREATORS_API_KEY", "FIRECRAWL_API_KEY", "TAVILY_API_KEY", "SERPAPI_KEY"])
    assert got == {"SCRAPECREATORS_API_KEY": FAKE_SECRET, "FIRECRAWL_API_KEY": "fc-quoted-value-0001",
                   "TAVILY_API_KEY": "tv-dquoted-value-0002", "SERPAPI_KEY": "serp-bare-value-0003"}
    assert research.load_keys(["FIRECRAWL_API_KEY"]) == {"FIRECRAWL_API_KEY": "fc-quoted-value-0001"}
    assert research.load_keys(["MISSING"]) == {} and research.load_keys([]) == {}


def test_shim_env_carries_only_granted_keys(steward_home, keys_file, tmp_path):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    research.set_caps(store, job.job_id, research=False, keys=["SCRAPECREATORS_API_KEY"])
    env = cli._worker_spawn_env(store, job.job_id, tmp_path)
    assert env["SCRAPECREATORS_API_KEY"] == FAKE_SECRET
    assert "fc-quoted-value-0001" not in env.values()
    assert masking.mask(FAKE_SECRET) == masking.MASK


# -- masking ----------------------------------------------------------------------

def test_granted_secret_masked_in_events_frames_transcript_and_feed(steward_home, keys_file, tmp_path):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    research.set_caps(store, job.job_id, research=True, keys=["SCRAPECREATORS_API_KEY"])
    masking.clear()  # a fresh process: nothing armed until the store arms it lazily

    store.append_event(job.job_id, what=f"leak {FAKE_SECRET}", evidence={"cmd": f"curl -H {FAKE_SECRET}", "n": [FAKE_SECRET]},
                       rule="t", judgment=cli.Judgment.code)
    dumped = json.dumps([(e.what, e.evidence) for e in store.events(job.job_id)])
    assert FAKE_SECRET not in dumped and masking.MASK in dumped

    line = fabric.encode(fabric.Frame(v=1, seq=1, job=job.job_id, type="event", body={"text": f"x {FAKE_SECRET}"}))
    assert FAKE_SECRET not in line
    assert fabric.decode(line).body["text"] == f"x {masking.MASK}"  # still a valid, crc-checked frame

    assert FAKE_SECRET not in redact(f"key={FAKE_SECRET} and {FAKE_SECRET}")

    # transcript render
    proj = tmp_path / "projects"
    sid = "sess-1"
    store.kv_set(job.job_id, "worker_session_id", sid)
    from steward.chat import project_slug, worktree_path
    d = proj / project_slug(worktree_path(store, job.job_id))
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
        "message": {"id": "m1", "model": "haiku", "content": [{"type": "text", "text": f"my key is {FAKE_SECRET}"}]}}) + "\n")
    masking.clear()
    act = transcript.worker_activity(store, job.job_id, projects_dir=proj)
    assert act["turns"] and FAKE_SECRET not in json.dumps(act)

    # feed render
    from io import StringIO
    store.append_event(job.job_id, what="research_call", evidence={"tool": "research_quick", "query": f"q {FAKE_SECRET}",
                       "spent_usd": 0.02, "cap_usd": 5}, rule="r", judgment=cli.Judgment.code)
    out = StringIO()
    watchers.run_feed(store, job.job_id, out=out, width_fn=lambda: 200)
    assert FAKE_SECRET not in out.getvalue() and "research research_quick" in out.getvalue()


# -- permissions ------------------------------------------------------------------

def test_permissions_allow_research_tools():
    for tool in ("mcp__research__research_quick", "mcp__research__research_deep"):
        assert permissions.decide(tool, {"query": "x"}, "/wt").allow
    assert exceptions.load()["research-mcp"].audit == "event"


# -- cap accounting, NEEDS_YOU, denial, raise --------------------------------------

def _research_runner(steward_home, cap, requests):
    store = Store(steward_home)
    job = _contracted_job(store)
    research.set_caps(store, job.job_id, research=True, keys=[], cap_usd=cap)
    frames = tuple(_frame(3 + i, "permission_request", {"request_id": f"r{i}", "tool": t, "input": {"query": q}})
                   for i, (t, q) in enumerate(requests))
    factory = _FabricFactory(scripts=[[_obs(lifecycle="blocked", events=frames)]])
    sent = []

    class _N:
        def send(self, event):
            sent.append(event)
            return True

    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock(), notifier=_N())
    runner.start(job.job_id)
    return store, job, runner, factory, sent


def test_research_calls_metered_allowed_then_capped(steward_home):
    reqs = [("mcp__research__research_deep", "first deep dive"),
            ("mcp__research__research_deep", "second deep dive"),
            ("mcp__research__research_quick", "third after cap")]
    store, job, runner, factory, sent = _research_runner(steward_home, 1.0, reqs)
    runner.tick(job.job_id)

    ap = approvals_module.list_approvals(store, job.job_id)
    assert [a["status"] for a in ap] == ["approved", "approved", "denied"]
    calls = [e.evidence for e in store.events(job.job_id) if e.what == "research_call"]
    assert [c["est_usd"] for c in calls] == [0.5, 0.5] and calls[1]["spent_usd"] == 1.0
    assert calls[0]["query"] == "first deep dive"
    used = [e for e in store.events(job.job_id) if e.what == "exception_used" and e.evidence["name"] == "research-mcp"]
    assert len(used) == 2
    trips = [e for e in store.events(job.job_id) if e.what == "research_cap_tripped"]
    assert len(trips) == 1
    assert any(ev.kind == "NEEDS_YOU" and "research spend cap" in ev.body for ev in sent)
    assert any(e.what == "research_call_denied" for e in store.events(job.job_id))
    assert research.summary(store, job.job_id)["tripped"] is True


def test_research_calls_at_default_quick_cost_and_cap_raise_reopens(steward_home):
    store, job, runner, factory, sent = _research_runner(steward_home, 0.04, [
        ("mcp__research__research_quick", "a"), ("mcp__research__research_quick", "b"),
        ("mcp__research__research_quick", "c")])
    runner.tick(job.job_id)
    assert [a["status"] for a in approvals_module.list_approvals(store, job.job_id)] == ["approved", "approved", "denied"]
    # raise via the relayed verb
    handlers = cli._build_control_handlers(store)
    receipt = handlers["research_cap"]({"args": {"job_id": job.job_id, "usd": 1.0}})
    assert receipt["accepted"] and "$1.00" in receipt["detail"]
    s = research.summary(store, job.job_id)
    assert s["cap_usd"] == 1.0 and s["tripped"] is False
    ok, info = research.charge(store, job.job_id, "mcp__research__research_quick", {})
    assert ok and info["calls"] == 3


def test_research_cap_verb_end_to_end(steward_home, capsys):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    research.set_caps(store, job.job_id, research=True, keys=[])
    assert cli.main(["research-cap", job.job_id, "9"]) == 0
    assert "$9.00" in capsys.readouterr().out
    assert research.get_caps(store, job.job_id)["cap_usd"] == 9.0
    plain = store.create_job("g", "/repo")
    assert cli.main(["research-cap", plain.job_id, "9"]) == 2


def test_unregistered_research_path_is_denied(steward_home, monkeypatch, tmp_path):
    reg = tmp_path / "reg.toml"
    reg.write_text('[git]\ndirection = "out"\npurpose = "x"\naudit = "event"\n')
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(reg))
    store, job, runner, factory, sent = _research_runner(steward_home, 5.0, [("mcp__research__research_quick", "q")])
    runner.tick(job.job_id)
    assert approvals_module.list_approvals(store, job.job_id)[0]["status"] == "denied"


# -- feed + dashboard row ------------------------------------------------------------

@pytest.mark.legacy_ownership_off
def test_feed_lines_and_status_row(steward_home):
    store, job, runner, factory, sent = _research_runner(steward_home, 0.03, [
        ("mcp__research__research_quick", "what is firecrawl"), ("mcp__research__research_quick", "again")])
    runner.tick(job.job_id)
    texts = [t for _e, _r, t in watchers.collect(store.events(job.job_id))]
    assert any(t.startswith("granted: research MCP") for t in texts)
    assert any("research research_quick: what is firecrawl" in t for t in texts)
    assert any("research cap $0.03 reached" in t for t in texts)
    rows = [r for r in supervisors.supervisor_rows(store, job.job_id) if r["role"] == "research"]
    assert len(rows) == 1 and rows[0]["status"] == "capped"
    assert "2 calls" in rows[0]["display"] and "$0.04/$0.03" in rows[0]["display"]
    # no row for a job without research
    other = store.create_job("g", "/repo")
    assert not [r for r in supervisors.supervisor_rows(store, other.job_id) if r["role"] == "research"]
