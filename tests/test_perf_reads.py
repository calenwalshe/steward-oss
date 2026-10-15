"""Regression tests for the 2026-10-05 TUI speed pass (docs/perf/2026-10-05-tui-speed-plan.md): the filtered event
reads must answer exactly what the full-history scans they replaced answered (ordering, newest-wins, other jobs and
unrelated events interleaved), the tokens cache must stay correct across passes while skipping needless saves, and
`status --recent` ages out only accepted jobs with nothing left to do."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from steward import api as api_module
from steward import activation, autopilot, cli, delivery, herdr_tabs, hold, ownership, tokens
from steward.chat import project_slug, worktree_path
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import WAL_SIZE_LIMIT, Store

NOW = 1_800_000_000.0
NOISE = ("approval opened", "worker permission requested", "approval approved", "permission_auto_allowed",
         "permission decision relayed", "permission answer delivered", "heartbeat class changed", "inner_verdict")


def _ev(store, job_id, what, **evidence):
    return store.append_event(job_id, what=what, evidence=evidence, rule="r", judgment=Judgment.code)


def _noise(store, *job_ids, n=3):
    for jid in job_ids:
        for i in range(n):
            _ev(store, jid, NOISE[i % len(NOISE)], i=i)


def _state(store, job_id, state, updated_at=None):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ?, updated_at = COALESCE(?, updated_at) WHERE job_id = ?",
                            (state.value, updated_at, job_id))


class _FullScan:
    """The pre-index reads: `events_of` / `last_event` answered by filtering the full history in Python."""

    def __init__(self, store):
        self.store = store

    def events_of(self, job_id, whats, rules=()):
        whats, rules = set(whats), set(rules)
        return [e for e in self.store.events(job_id) if e.what in whats or e.rule in rules]

    def last_event(self, job_id, whats):
        got = self.events_of(job_id, whats)
        return got[-1] if got else None


def _as_full_scan(monkeypatch, store):
    ref = _FullScan(store)
    monkeypatch.setattr(store, "events_of", ref.events_of)
    monkeypatch.setattr(store, "last_event", ref.last_event)


# -- store: the indexed reads ---------------------------------------------------------------------------------------

def test_events_of_keeps_seq_order_and_skips_other_jobs_and_whats(steward_home):
    store = Store(steward_home)
    a, b = store.create_job("a", "/r"), store.create_job("b", "/r")
    _noise(store, a.job_id, b.job_id)
    want = []
    for i in range(4):
        want.append(_ev(store, a.job_id, "incorporated" if i % 2 else "verification recorded", i=i).seq)
        _ev(store, b.job_id, "incorporated", i=i)  # same what, other job
        _noise(store, a.job_id, n=2)
    got = store.events_of(a.job_id, ("verification recorded", "incorporated"))
    assert [e.seq for e in got] == want and want == sorted(want)
    assert [e.evidence["i"] for e in got] == [0, 1, 2, 3]
    assert [e.seq for e in store.events_of(a.job_id, ["incorporated"])] == want[1::2]  # any iterable of whats
    assert store.events_of(a.job_id, ("never",)) == []


def test_last_event_is_the_newest_match_else_none(steward_home):
    store = Store(steward_home)
    a, b = store.create_job("a", "/r"), store.create_job("b", "/r")
    assert store.last_event(a.job_id, ("verification recorded",)) is None
    _ev(store, a.job_id, "verification recorded", n=1)
    _noise(store, a.job_id)
    _ev(store, a.job_id, "verify_worktree_dirty", n=2)
    _noise(store, a.job_id)
    _ev(store, b.job_id, "verification recorded", n=3)  # newer, but another job's
    last = store.last_event(a.job_id, ("verification recorded", "verify_worktree_dirty"))
    assert (last.what, last.evidence["n"]) == ("verify_worktree_dirty", 2)
    assert store.last_event(a.job_id, ("verification recorded",)).evidence["n"] == 1


def test_events_of_matches_whats_or_rules(steward_home):
    store = Store(steward_home)
    a = store.create_job("a", "/r")
    _noise(store, a.job_id)
    marker = store.append_event(a.job_id, what="worker said done", evidence={}, rule="fabric_done_frame",
                                judgment=Judgment.code)
    _noise(store, a.job_id)
    passed = _ev(store, a.job_id, "checks passed at exact commit")
    got = store.events_of(a.job_id, ("checks passed at exact commit",), rules=("fabric_done_frame",))
    assert [e.seq for e in got] == [marker.seq, passed.seq]
    assert store.events_of(a.job_id, (), rules=("fabric_done_frame",))[0].seq == marker.seq


def test_store_has_the_read_indexes_and_a_wal_size_limit(steward_home):
    store = Store(steward_home)
    names = {r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"events_job_seq", "kv_key"} <= names
    plan = " ".join(r[3] for r in store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM events WHERE job_id = 'x' ORDER BY seq"))
    assert "events_job_seq" in plan and "SCAN events" not in plan
    assert store._conn.execute("PRAGMA journal_size_limit").fetchone()[0] == WAL_SIZE_LIMIT


# -- api: verification reads ----------------------------------------------------------------------------------------

def _verification_history(store, job_id):
    _noise(store, job_id)
    _ev(store, job_id, "verification recorded", commit_sha="old", new_failures=["t::old"])
    _noise(store, job_id)
    _ev(store, job_id, "checks unverified; acceptance unresolved", reasons=["test-weakening detected: old", "x"])
    _ev(store, job_id, "finish_line_not_met", command="pytest", first_failing_line="E boom")
    _noise(store, job_id)
    _ev(store, job_id, "verify_worktree_dirty", commit_sha="dirty")
    _ev(store, job_id, "verification recorded", commit_sha="new", new_failures=[], fixed=["t::f"],
        unverified=[{"command": "pytest", "reason": "timed out"}])
    _noise(store, job_id)
    _ev(store, job_id, "checks passed; acceptance unresolved", reasons=["test-weakening detected: new", "y"])
    _ev(store, job_id, "finish_line_met", command="pytest")
    _noise(store, job_id)


def test_get_verification_matches_the_full_history_scan(steward_home, monkeypatch):
    store = Store(steward_home)
    job = store.create_job("do it\nFINISH LINE:\n- `pytest` passes\n- docs updated", "/r")
    other = store.create_job("other", "/r")
    _verification_history(store, job.job_id)
    _ev(store, other.job_id, "verification recorded", commit_sha="theirs")  # interleaved, newer, other job
    api = api_module.StewardAPI(store)
    fast = api.get_verification(job.job_id)
    assert fast["commit_sha"] == "new" and fast["dirty"] is False and fast["verified"] is False
    assert fast["weakening"] == ["test-weakening detected: new"]
    assert fast["unverified_reason"] == "`pytest`: timed out"
    _as_full_scan(monkeypatch, store)
    assert api.get_verification(job.job_id) == fast


def test_get_verification_dirty_last_and_none(steward_home, monkeypatch):
    store = Store(steward_home)
    job, bare = store.create_job("g", "/r"), store.create_job("g", "/r")
    _ev(store, job.job_id, "verification recorded", commit_sha="a", new_failures=["t::x"])
    _noise(store, job.job_id)
    _ev(store, job.job_id, "verify_worktree_dirty", commit_sha="b")
    _noise(store, job.job_id, bare.job_id)
    api = api_module.StewardAPI(store)
    fast = (api.get_verification(job.job_id), api.get_verification(bare.job_id))
    assert fast[0]["dirty"] is True and fast[0]["new_failures"] == [] and fast[1]["verified"] is False
    _as_full_scan(monkeypatch, store)
    assert (api.get_verification(job.job_id), api.get_verification(bare.job_id)) == fast


# -- autopilot: the fix-up chain ------------------------------------------------------------------------------------

def _fixup_chain(store):
    """parent -> c1 (stopped duplicate), c2 (live, launched) -> grandchild g (failed), noise everywhere."""
    parent = store.create_job("parent", "/r")
    _state(store, parent.job_id, JobState.ACCEPTED)
    kids = {}
    for name, state in (("c1", JobState.STOPPED), ("c2", JobState.RUNNING), ("g", JobState.FAILED)):
        kids[name] = store.create_job(name, "/r").job_id
        _state(store, kids[name], state)
    store.kv_set(kids["c2"], "herdr_tab", "w1:t2")
    _noise(store, parent.job_id)
    _ev(store, parent.job_id, "auto_followup_started", follow_up=kids["c1"])
    _noise(store, parent.job_id, kids["c1"])
    _ev(store, parent.job_id, "auto_followup_started", follow_up=kids["c2"])
    _ev(store, parent.job_id, "auto_followup_started", follow_up=kids["c2"])  # a duplicate start counts once
    _noise(store, parent.job_id, kids["c2"])
    _ev(store, kids["c2"], "auto_followup_started", follow_up=kids["g"])
    _noise(store, kids["c2"])
    _ev(store, kids["c2"], "autopilot_escalated", launch_failed="")
    _noise(store, kids["c2"])
    return parent.job_id, kids


def test_fixup_children_and_followup_status_match_the_full_scan(steward_home, monkeypatch):
    store = Store(steward_home)
    parent, kids = _fixup_chain(store)
    children = [c for c, _, _ in autopilot._fixup_children(store, parent)]
    assert children == [kids["c1"], kids["c2"]]  # oldest first, each once
    fast = autopilot.followup_status(store, parent)
    assert fast is not None
    _as_full_scan(monkeypatch, store)
    assert [c for c, _, _ in autopilot._fixup_children(store, parent)] == children
    assert autopilot.followup_status(store, parent) == fast


# -- delivery sweep (every 30 s over every accepted job) -------------------------------------------------------------

def test_milestones_match_the_full_scan(steward_home, monkeypatch):
    store = Store(steward_home)
    jobs = []
    for marker_rule, state in (("steward_done_marker", JobState.RUNNING), ("idle_after_final_message", JobState.PAUSED),
                               (None, JobState.RUNNING), ("fabric_done_frame", JobState.RUNNING),
                               ("fabric_done_frame", JobState.ACCEPTED)):
        job = store.create_job("g", "/r")
        _state(store, job.job_id, state)
        _noise(store, job.job_id)
        if marker_rule:
            store.append_event(job.job_id, what="marker", evidence={}, rule=marker_rule, judgment=Judgment.code)
        _noise(store, job.job_id)
        if marker_rule == "steward_done_marker":
            _ev(store, job.job_id, "checks passed at exact commit")
            store.append_event(job.job_id, what="later", evidence={}, rule="idle_after_final_message",
                               judgment=Judgment.code)  # the newest marker wins
        jobs.append(store.get_job(job.job_id))
    fast = [ownership._milestones(store, j.job_id, j) for j in jobs]
    assert [m["worker_completion"]["status"] for m in fast] == ["pending", "pending", "pending", "done", "done"]
    assert hold.worker_claim(store.events_of(jobs[0].job_id, (), rules=tuple(hold._MARKER_RULES))) == "unfinished"
    assert fast[0]["verification"]["status"] == "done" and fast[1]["verification"]["status"] == "pending"
    _as_full_scan(monkeypatch, store)
    assert [ownership._milestones(store, j.job_id, j) for j in jobs] == fast


def test_activation_state_and_targets_match_the_full_scan(steward_home, monkeypatch):
    store = Store(steward_home)
    job = store.create_job("g", "/r")
    _noise(store, job.job_id)
    _ev(store, job.job_id, "auto_deployed", merge_commit="m1")
    for i in range(activation.VERIFY_MAX - 1):
        _ev(store, job.job_id, "activation_unproven", merge_commit="m1")
        _noise(store, job.job_id, n=1)
    _ev(store, job.job_id, "activation_verified", merge_commit="m2")  # another merge's probe: not counted for m1
    plain = store.create_job("g", "/r")
    _noise(store, plain.job_id)
    args = [(job.job_id, "m1"), (job.job_id, "m2"), (job.job_id, "m3"), (plain.job_id, "m1")]
    fast = [activation.state(store, j, m, now=NOW) for j, m in args]
    assert fast[0] == "verify" and fast[3] == "deploy"
    targets = [delivery.activation_targets(store, j) for j in (job.job_id, plain.job_id)]
    assert targets[0] == ("control", "runners") and targets[1] == ()
    _ev(store, job.job_id, "activation_unproven", merge_commit="m1")  # the VERIFY_MAX-th probe
    assert activation.state(store, job.job_id, "m1", now=NOW) == "exhausted"
    fast[0] = "exhausted"
    _as_full_scan(monkeypatch, store)
    assert [activation.state(store, j, m, now=NOW) for j, m in args] == fast
    assert [delivery.activation_targets(store, j) for j in (job.job_id, plain.job_id)] == targets


def test_publish_writes_the_summary_only_when_it_changed(steward_home, monkeypatch):
    store = Store(steward_home)
    job = store.create_job("g", "/r")
    _state(store, job.job_id, JobState.ACCEPTED)
    writes = []
    real = store.kv_set
    monkeypatch.setattr(store, "kv_set", lambda jid, key, value: (writes.append(key), real(jid, key, value)))
    first = delivery.publish(store, job.job_id)
    assert writes.count(delivery.SUMMARY_KEY) == 1
    assert store.kv_get(job.job_id, delivery.SUMMARY_KEY) == {k: first[k] for k in ("delivered", "next", "text")}
    delivery.publish(store, job.job_id)
    assert writes.count(delivery.SUMMARY_KEY) == 1  # unchanged: no write
    store.kv_set(job.job_id, delivery.SUMMARY_KEY, {"delivered": [], "next": "x", "text": "stale"})
    delivery.publish(store, job.job_id)
    assert store.kv_get(job.job_id, delivery.SUMMARY_KEY)["text"] == first["text"]


# -- herdr_tabs.sweep: closed jobs skip the settle read -------------------------------------------------------------

def _closable(store, state=JobState.RELEASED, tab="w3T:t2"):
    job = store.create_job("goal", "/repo")
    store.kv_set(job.job_id, "herdr_tab", tab)
    _state(store, job.job_id, state)
    return job


def test_sweep_skips_closed_and_drained_jobs_without_reading_their_events(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _closable(store)
    later = datetime.now(timezone.utc) + timedelta(seconds=120)
    panes = CommandResult(argv=[], cwd="", exit_code=0, stdout=json.dumps({"result": {"panes": []}}), stderr="",
                          duration_s=0.0)
    runner = FakeRunner(lambda argv, cwd: panes)  # Herdr answers `pane list`: the legacy drain completes, as live
    assert herdr_tabs.sweep(store, runner, now=later) == [job.job_id]
    assert store.kv_get(job.job_id, "herdr_tab_closed") and store.kv_get(job.job_id, "questions_tab_drained")
    reads = []
    monkeypatch.setattr(store, "events_of", lambda *a: reads.append(a) or [])
    assert herdr_tabs.sweep(store, runner, now=later) == []
    assert reads == []


def test_sweep_still_drains_a_closed_job_whose_legacy_tabs_were_never_drained(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _closable(store)
    store.kv_set(job.job_id, "herdr_tab_closed", "w3T:t2")  # closed before the drain existed
    drained = []
    monkeypatch.setattr(herdr_tabs, "_drain_legacy_questions", lambda s, r, j, t: drained.append(j.job_id))
    herdr_tabs.sweep(store, FakeRunner(), now=datetime.now(timezone.utc) + timedelta(seconds=120))
    assert drained == [job.job_id]


def test_settled_at_takes_the_newest_incorporation_among_noise(steward_home):
    store = Store(steward_home)
    job = _closable(store, JobState.ACCEPTED)
    _noise(store, job.job_id)
    _ev(store, job.job_id, "incorporated")
    _noise(store, job.job_id)
    last = _ev(store, job.job_id, "incorporated")
    _noise(store, job.job_id)
    job = store.get_job(job.job_id)
    assert herdr_tabs._settled_at(store, job) == max(datetime.fromisoformat(job.updated_at),
                                                     datetime.fromisoformat(last.ts))


# -- status --recent: settled accepted jobs age out -----------------------------------------------------------------

def _status(capsys, *args):
    capsys.readouterr()
    assert cli.main(["status", "--json", *args]) == 0
    return {s["job_id"] for s in json.loads(capsys.readouterr().out)}


def test_status_recent_ages_out_only_settled_accepted_jobs(steward_home, capsys):
    store = Store(steward_home)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    new = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    jobs = {k: store.create_job(k, "/r").job_id for k in ("merged_old", "merged_new", "unmerged_old",
                                                            "unwritten_old", "running")}
    for k in ("merged_old", "merged_new", "unmerged_old", "unwritten_old"):
        _state(store, jobs[k], JobState.ACCEPTED, new if k == "merged_new" else old)
        if k != "unmerged_old":
            store.kv_set(jobs[k], "incorporated_commit", "abc")
    store.kv_set(jobs["unwritten_old"], "questions_source_unwritten", ["q1"])
    _state(store, jobs["running"], JobState.RUNNING, old)
    assert _status(capsys, "--recent", "7") == set(jobs.values()) - {jobs["merged_old"]}
    assert _status(capsys) == set(jobs.values())  # the phone list (no --recent) is unchanged


# -- cli: one parser per process ------------------------------------------------------------------------------------

def test_parser_is_built_once_and_rebuilt_when_swapped(monkeypatch):
    built = []
    real = cli._build_parser

    def counting():
        built.append(1)
        return real()

    monkeypatch.setattr(cli, "_build_parser", counting)
    first = cli._parser()
    assert cli._parser() is first and len(built) == 1
    monkeypatch.setattr(cli, "_build_parser", lambda: real())
    assert cli._parser() is not first


def test_cached_parser_follows_the_owner_name(monkeypatch):
    """The help text names the owner: a parser cached under one name must not answer under another."""
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    assert "Sam labels a watcher verdict" in _sub_help("watcher")
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Robin")
    assert "Robin labels a watcher verdict" in _sub_help("watcher")


def _sub_help(*path):
    parser = cli._parser()
    for name in path:
        action = next(a for a in parser._actions if isinstance(a, __import__("argparse")._SubParsersAction))
        parser = action.choices[name]
    return parser.format_help()


# -- tokens: per-pass memos, dirty-only saves -----------------------------------------------------------------------

def _msg(mid, ts):
    return json.dumps({"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)),
                       "message": {"id": mid, "usage": {"input_tokens": 1, "output_tokens": 2,
                                                        "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}}})


def _review_request(store, job_id, rid, at):
    with store._conn:
        store._conn.execute(
            "INSERT INTO events (job_id, ts, what, evidence, rule, judgment) VALUES (?, ?, ?, ?, 'r', 'code')",
            (job_id, datetime.fromtimestamp(at, timezone.utc).isoformat(), "trajectory_review_requested",
             json.dumps({"review_id": rid})))


@pytest.fixture
def astra(steward_home, tmp_path):
    """Two jobs and the advisor folder; `review(name, start, mid)` writes one Astra review transcript."""
    store = Store(steward_home)
    a, b = store.create_job("a", "/r").job_id, store.create_job("b", "/r").job_id
    proj = tmp_path / "projects"
    adir = proj / project_slug(tokens._ASTRA_DIR)
    adir.mkdir(parents=True)

    def review(name, start, mid):
        user = json.dumps({"type": "user", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(start)),
                           "message": {"role": "user", "content": tokens._ASTRA_MARKER + " ..."}})
        (adir / f"{name}.jsonl").write_text("\n".join([user, _msg(mid, start + 5)]))

    return store, a, b, proj, review


def _astra_of(result, job_id):
    return result["jobs"][job_id]["by_source"]["astra"]


def test_all_tokens_sees_a_review_request_added_between_passes(astra):
    store, a, b, proj, review = astra
    _review_request(store, a, "r-a", NOW - 300)
    review("ra", NOW - 298, "ma")
    review("rb", NOW - 118, "mb")  # no request yet: nobody's
    first = tokens.all_tokens(store, [a, b], now=NOW, projects_dir=proj)
    assert (_astra_of(first, a), _astra_of(first, b)) == (10, 0)
    _review_request(store, b, "r-b", NOW - 120)
    second = tokens.all_tokens(store, [a, b], now=NOW, projects_dir=proj)
    assert (_astra_of(second, a), _astra_of(second, b)) == (10, 10)


def test_all_tokens_answers_each_job_selection_on_its_own(astra):
    store, a, b, proj, review = astra
    _review_request(store, a, "r-a", NOW - 300)
    _review_request(store, b, "r-b", NOW - 120)
    review("ra", NOW - 298, "ma")
    review("rb", NOW - 118, "mb")
    only_b = tokens.all_tokens(store, [b], now=NOW, projects_dir=proj)
    only_a = tokens.all_tokens(store, [a], now=NOW, projects_dir=proj)
    assert list(only_b["jobs"]) == [b] and _astra_of(only_b, b) == 10 and only_b["grand"]["total"]["all"] == 10
    assert list(only_a["jobs"]) == [a] and _astra_of(only_a, a) == 10


def _worker_transcript(store, job_id, proj):
    d = proj / project_slug(worktree_path(store, job_id))
    d.mkdir(parents=True, exist_ok=True)
    store.kv_set(job_id, "worker_session_id", "w")
    return d / "w.jsonl"


def test_all_tokens_counts_transcript_growth_and_saves_only_when_changed(astra, monkeypatch):
    store, a, b, proj, review = astra
    path = _worker_transcript(store, a, proj)
    path.write_text(_msg("m1", NOW - 60) + "\n")
    saves = []
    real_save = tokens._save_cache
    monkeypatch.setattr(tokens, "_save_cache", lambda home, cache: saves.append(dict(cache)) or real_save(home, cache))

    assert tokens.all_tokens(store, [a, b], now=NOW, projects_dir=proj)["jobs"][a]["by_source"]["worker"] == 10
    assert len(saves) == 1 and not any(k.startswith("__") for k in tokens.cache_entries(store.home))
    # warm and unchanged: same answer, no rewrite of the cache file
    assert tokens.all_tokens(store, [a, b], now=NOW, projects_dir=proj)["jobs"][a]["by_source"]["worker"] == 10
    assert len(saves) == 1
    # the transcript grows: counted, and the cache is saved again
    with path.open("a") as fh:
        fh.write(_msg("m2", NOW - 30) + "\n")
    assert tokens.all_tokens(store, [a, b], now=NOW, projects_dir=proj)["jobs"][a]["by_source"]["worker"] == 20
    assert len(saves) == 2
    # a newly parsed advisor transcript also marks the pass dirty
    _review_request(store, b, "r-b", NOW - 120)
    review("rb", NOW - 118, "mb")
    assert _astra_of(tokens.all_tokens(store, [a, b], now=NOW, projects_dir=proj), b) == 10
    assert len(saves) == 3


def test_new_pass_drops_only_pass_scoped_entries():
    cache = {"/p/x.jsonl": {"sig": [1, 2]}, "__astra_by_job__": {}, "__astra_requests__": [], "__dirty__": True,
             "__astra_listing__:/d": []}
    assert tokens.new_pass(cache) == {"/p/x.jsonl": {"sig": [1, 2]}}


def test_a_long_lived_cache_sees_new_requests_after_new_pass(astra):
    """efficiency_watch keeps one cache for the process: its per-pass memos must not outlive a pass."""
    store, a, b, proj, review = astra
    cache = {}
    review("rb", NOW - 118, "mb")
    assert tokens.job_tokens(store, b, now=NOW, projects_dir=proj, _cache=cache)["by_source"]["astra"] == 0
    _review_request(store, b, "r-b", NOW - 120)
    tokens.new_pass(cache)
    assert tokens.job_tokens(store, b, now=NOW, projects_dir=proj, _cache=cache)["by_source"]["astra"] == 10


def test_efficiency_watch_starts_a_new_tokens_pass_per_recompute():
    import inspect

    from steward import efficiency_watch

    src = inspect.getsource(efficiency_watch.EfficiencyWatcher._efficiency)
    assert src.index("tokens.new_pass(self._cache)") < src.index("efficiency.compute(")
