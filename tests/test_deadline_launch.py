"""v2.2 Phase 20: `steward new --deadline` composes creation/contract and launch in one relayed command (DL-01).
Fake launcher/runner and temp stores only; no Herdr, no network."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone

import pytest

from steward import cli, deadline, launch
from steward.control_pane import ControlPane
from steward.models import JobState
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)


class FakeLaunch:
    def __init__(self, fail_times=0):
        self.calls, self.fail_times = [], fail_times

    def __call__(self, runner_exec, job, **kw):
        self.calls.append((job.job_id, kw))
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("herdr tab create failed: socket gone")
        return f"t{len(self.calls)}", f"w1:p{len(self.calls)}"


@pytest.fixture
def env(steward_home, tmp_path, monkeypatch):
    monkeypatch.setattr(deadline, "utcnow", lambda: NOW)
    fake = FakeLaunch()
    monkeypatch.setattr(launch, "launch_tab", fake)
    repo = tmp_path / "repo"
    repo.mkdir()
    return {"store": Store(steward_home), "launch": fake, "repo": str(repo), "monkeypatch": monkeypatch}


def _new(env, *extra, goal="Add CSV import. Optional: CSV export."):
    return cli.main(["new", goal, "--repo", env["repo"], *extra])


def _jobs(store):
    return store.list_jobs()


def _handler_new(store, rid, args, origin="cli"):
    pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _l: None)
    return pane.execute({"request_id": rid, "op": "new", "args": args, "origin": origin})


def test_deadline_new_creates_commits_and_launches_in_one_command(env, capsys):
    rc = _new(env, "--deadline", "+4h", "--workspace", "w1")
    out = capsys.readouterr().out
    assert rc == 0, out
    (job,) = _jobs(env["store"])
    jid = job.job_id
    assert len(env["launch"].calls) == 1 and env["launch"].calls[0][0] == jid
    kw = env["launch"].calls[0][1]
    assert kw["workspace"] == "w1" and kw["worker"] == "fabric" and kw["kind"] == "claude"
    assert kw["agent_args"] == ["--model", cli.DEFAULT_PLAY_MODEL]
    rec = deadline.current(env["store"], jid)
    assert rec["rev"] == 1 and rec["due_at_utc"] == "2026-10-03T00:00:00Z" and rec["judgment"] == "human"
    assert [i["class"] for i in rec["items"]] == ["required", "optional"]
    assert "DEADLINE: 2026-10-02 17:00 -07:00 (rev 1)" in out and "At risk · estimating" in out
    prov = env["store"].kv_get(jid, "deadline_provenance")
    assert prov["workspace"] == {"value": "w1", "source": "explicit"}
    assert prov["worker"]["source"] == "default" and prov["agent_args"]["source"] == "default play model"
    contract = env["store"].kv_get(jid, "contract")
    assert any("baseline deferred" in a for a in contract["assumptions"])  # no synchronous baseline at start
    assert not env["store"].get_checks(jid, label="baseline")


def test_workspace_defaults_from_env_and_explicit_overrides_are_recorded(env, monkeypatch):
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "wEnv")
    rc = _new(env, "--deadline", "+2h", "--worker", "herdr-tui", "--kind", "codex", "--agent-arg=--model",
              "--agent-arg=o3")
    assert rc == 0
    jid = _jobs(env["store"])[0].job_id
    prov = env["store"].kv_get(jid, "deadline_provenance")
    assert prov["workspace"] == {"value": "wEnv", "source": "HERDR_WORKSPACE_ID"}
    assert prov["worker"] == {"value": "herdr-tui", "source": "explicit"}
    assert prov["kind"] == {"value": "codex", "source": "explicit"}
    assert env["launch"].calls[0][1]["agent_args"] == ["--model", "o3"]


@pytest.mark.parametrize("extra,why", [
    (("--deadline", "+4h"), "workspace"),  # no workspace anywhere
    (("--deadline", "12:00", "--workspace", "w1"), "not in the future"),
    (("--deadline", "2026-11-01 01:30", "--workspace", "w1"), "ambiguous"),
    (("--deadline", "+4h", "--workspace", "w1", "--worker", "fabric", "--kind", "codex"), "unsupported worker"),
])
def test_prevalidation_fails_before_any_job_exists(env, capsys, extra, why):
    rc = _new(env, *extra)
    assert rc != 0 and why in capsys.readouterr().err
    assert _jobs(env["store"]) == [] and env["launch"].calls == []


def test_deadline_with_after_is_refused_but_plain_after_still_queues(env, capsys):
    store = env["store"]
    pred = store.create_job("pred", env["repo"])
    rc = _new(env, "--deadline", "+4h", "--workspace", "w1", "--after", pred.job_id)
    assert rc != 0 and "cannot be queued" in capsys.readouterr().err
    assert [j.job_id for j in _jobs(store)] == [pred.job_id]
    assert _new(env, "--after", pred.job_id, "--no-baseline") == 0  # the ordinary queue path is unchanged
    queued = [j for j in _jobs(store) if j.job_id != pred.job_id][0]
    assert deadline.commitment(store, queued.job_id) is None and env["launch"].calls == []


def test_second_deadline_refused_naming_the_active_job(env, capsys):
    assert _new(env, "--deadline", "+4h", "--workspace", "w1") == 0
    first = _jobs(env["store"])[0].job_id
    capsys.readouterr()
    assert _new(env, "--deadline", "+3h", "--workspace", "w1", goal="other") != 0
    assert first[:8] in capsys.readouterr().err and len(_jobs(env["store"])) == 1


def test_persistent_launch_failure_stops_job_releases_slot_and_keeps_history(env, capsys):
    env["launch"].fail_times = 2
    rc = _new(env, "--deadline", "+4h", "--workspace", "w1")
    assert rc != 0 and "slot released" in capsys.readouterr().err
    (job,) = _jobs(env["store"])
    store = env["store"]
    assert job.state == JobState.STOPPED and len(env["launch"].calls) == cli.DEADLINE_LAUNCH_TRIES
    whats = [e.what for e in store.events(job.job_id)]
    assert "deadline_launch_retry" in whats and "deadline_launch_failed" in whats
    assert deadline.ever(store, job.job_id) and deadline.commitment(store, job.job_id)["current"] == 1
    assert deadline.active_job(store) is None
    env["launch"].fail_times = 0
    assert _new(env, "--deadline", "+4h", "--workspace", "w1", goal="retry as a new request") == 0


def test_one_launch_failure_is_retried_on_the_same_job(env):
    env["launch"].fail_times = 1
    assert _new(env, "--deadline", "+4h", "--workspace", "w1") == 0
    (job,) = _jobs(env["store"])
    assert [c[0] for c in env["launch"].calls] == [job.job_id, job.job_id]
    assert env["store"].kv_get(job.job_id, deadline.K_LAUNCH)["status"] == "launched"


def test_tab_created_but_runner_failed_closes_the_partial_tab(env, monkeypatch):
    monkeypatch.setattr(launch, "launch_tab", _REAL_LAUNCH_TAB)

    def respond(argv, cwd):
        if argv[:3] == ["herdr", "tab", "create"]:
            out = json.dumps({"result": {"tab": {"tab_id": "w1:t9"}, "root_pane": {"pane_id": "w1:p9"}}})
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)
        if argv[:3] == ["herdr", "pane", "run"]:
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=1, stdout="", stderr="runner died", duration_s=0.0)
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    runner = FakeRunner(respond)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner)
    rc = _new(env, "--deadline", "+4h", "--workspace", "w1", "--no-baseline")
    assert rc != 0
    closes = [c["argv"] for c in runner.calls if c["argv"][:3] == ["herdr", "tab", "close"]]
    assert closes == [["herdr", "tab", "close", "w1:t9"]] * cli.DEADLINE_LAUNCH_TRIES
    (job,) = _jobs(env["store"])
    assert job.state == JobState.STOPPED and deadline.active_job(env["store"]) is None


_REAL_LAUNCH_TAB = launch.launch_tab


def _deadline_args(env, **kw):
    return {"goal": "Add CSV import.", "repo": env["repo"], "deadline": "+4h", "workspace": "w1", **kw}


def test_same_request_id_twice_never_creates_a_second_job(env):
    store = env["store"]
    first = _handler_new(store, "same-rid", _deadline_args(env))
    second = _handler_new(store, "same-rid", _deadline_args(env))
    assert first["accepted"] and second["duplicate"] and second["job_id"] == first["job_id"]
    assert len(_jobs(store)) == 1 and len(env["launch"].calls) == 1


def test_retry_after_receipt_loss_resumes_its_own_job_without_a_second_one(env):
    """A crash after the job was created and contracted but before commit/launch: the receipt never landed, so the
    same request id arrives again. It resumes that job (commit + one launch), never a new job."""
    store = env["store"]
    handlers = cli._build_control_handlers(store)
    real_launch = env["launch"]
    env["monkeypatch"].setattr(launch, "launch_tab", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        handlers["new"]({"request_id": "crashy", "op": "new", "args": _deadline_args(env), "origin": "cli"})
    (job,) = _jobs(store)
    assert job.state == JobState.CONTRACTED and deadline.current(store, job.job_id)["rev"] == 1
    env["monkeypatch"].setattr(launch, "launch_tab", real_launch)
    store.kv_set(job.job_id, deadline.K_LAUNCH, {"status": "launching", "owner": "dead", "at": 0})  # stale claim
    again = _handler_new(store, "crashy", _deadline_args(env))
    assert again["accepted"] and again["job_id"] == job.job_id and again.get("resumed")
    assert len(_jobs(store)) == 1 and [c[0] for c in real_launch.calls] == [job.job_id]
    assert len(deadline.commitment(store, job.job_id)["revisions"]) == 1


def test_concurrent_same_request_id_on_separate_connections_makes_one_job(env, steward_home):
    rid = f"race-{uuid.uuid4()}"
    n = 4
    barrier = threading.Barrier(n)
    results: list[dict] = []
    lock = threading.Lock()

    def go():
        own = Store(steward_home)
        handlers = cli._build_control_handlers(own)
        barrier.wait()
        res = handlers["new"]({"request_id": rid, "op": "new", "args": _deadline_args(env), "origin": "cli"})
        with lock:
            results.append(res)

    threads = [threading.Thread(target=go) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    store = env["store"]
    assert len(results) == n and len(_jobs(store)) == 1
    jid = _jobs(store)[0].job_id
    assert {r.get("job_id") for r in results} == {jid}
    assert len(env["launch"].calls) == 1 and len(deadline.commitment(store, jid)["revisions"]) == 1


def test_one_minute_window_is_admitted_and_starts(env):
    assert _new(env, "--deadline", "+1m", "--workspace", "w1") == 0
    (job,) = _jobs(env["store"])
    assert deadline.current(env["store"], job.job_id)["due_at_utc"] == "2026-10-02T20:01:00Z"


def test_non_human_origin_cannot_create_a_deadline_job(env):
    res = _handler_new(env["store"], "agent-rid", _deadline_args(env), origin="unblock-agent")
    assert not res["accepted"] and _jobs(env["store"]) == []


def test_disabled_feature_refuses_creation(env, monkeypatch, capsys):
    monkeypatch.setenv("STEWARD_DEADLINE", "0")
    assert _new(env, "--deadline", "+4h", "--workspace", "w1") != 0
    assert "disabled" in capsys.readouterr().err and _jobs(env["store"]) == []


def test_ordinary_new_is_unchanged(env):
    assert _new(env, "--no-baseline") == 0
    (job,) = _jobs(env["store"])
    store = env["store"]
    assert job.state == JobState.CONTRACTED and env["launch"].calls == []
    assert deadline.commitment(store, job.job_id) is None and not deadline.ever(store, job.job_id)
    assert store.kv_get(job.job_id, "deadline_provenance") is None
