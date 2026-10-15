"""Custom watchers: CLI + relay + events, `new --watcher`, scheduling, the code gate,
approvals, dashboard rows/map, feed lines, token attribution, chat plan."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from steward import cli, tokens
from steward import custom_watchers as cw
from steward import watchers as feed
from steward.approvals import list_approvals
from steward.chat import build_chat, project_slug, worktree_path
from steward.models import JobState
from steward.steering import Steering
from steward.store import Store
from steward.supervisors import supervisor_rows

_REPO = Path(__file__).resolve().parents[1]


def _running(store):
    job = store.create_job("ship the widget", "/tmp/somerepo")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    return job


def _spec(name="tests", **kw):
    return cw.make_spec(name, kw.pop("ask", "are tests green?"), **kw)


class FakePopen:
    """Records argv/kwargs; writes `output` to the stdout file when created; `rc` None = still running."""
    instances: list = []

    def __init__(self, argv, **kw):
        self.argv, self.kw = argv, kw
        self.rc = None
        self.killed = False
        self.pid = 4242
        self.output = FakePopen.next_output
        kw["stdout"].write(self.output if FakePopen.finish_now else "")
        kw["stdout"].flush()
        if FakePopen.finish_now:
            self.rc = 0
        FakePopen.instances.append(self)

    next_output = ""
    finish_now = True

    def poll(self):
        return self.rc

    def kill(self):
        self.killed = True
        self.rc = -9


def _out(status="concern", note="behind", **extra):
    verdict = {"status": status, "note": note, **extra}
    return json.dumps({"type": "result", "result": "Looked around.\n" + json.dumps(verdict),
                       "total_cost_usd": 0.01, "usage": {"input_tokens": 5, "output_tokens": 7}})


@pytest.fixture
def fake_popen():
    FakePopen.instances = []
    FakePopen.finish_now = True
    FakePopen.next_output = _out()
    return FakePopen


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _watchers(store, popen, clock=None, notes=None):
    return cw.CustomWatchers(store, clock=clock or Clock(), popen=popen,
                             notify=(lambda *a: notes.append(a)) if notes is not None else None)


# -- CLI / relay / events -----------------------------------------------------


def test_parse_duration_and_spec_defaults():
    assert cw.parse_duration("10m") == 600 and cw.parse_duration("1h") == 3600 and cw.parse_duration("60s") == 60
    for bad in ("30s", "5", "abc", "0m"):
        with pytest.raises(cw.WatcherError):
            cw.parse_duration(bad)
    s = _spec()
    assert s == {"name": "tests", "model": "haiku", "every_s": 600, "ask": "are tests green?", "may": ["escalate"],
                 "enabled": True}  # 15-01: default grant is escalate (record item 8)
    with pytest.raises(cw.WatcherError):
        _spec(may="nudge,urgent")
    with pytest.raises(cw.WatcherError):
        _spec("astra")
    assert _spec(model="claude-opus-5-5", may="escalate,nudge")["may"] == ["nudge", "escalate"]


def test_add_rm_toggle_list_relay_and_events(steward_home, capsys):
    store = Store(steward_home)
    job = _running(store)
    j = job.job_id
    assert cli.main(["watcher", "add", j, "tests", "--ask", "green?", "--every", "10m", "--may", "nudge"]) == 0
    assert cw.list_watchers(store, j)[0]["may"] == ["nudge"]
    assert cli.main(["watcher", "add", j, "tests", "--ask", "dup"]) == 1  # duplicate rejected
    assert cli.main(["watcher", "add", j, "fast", "--ask", "q", "--every", "30s"]) == 1  # below min
    assert cli.main(["watcher", "toggle", j, "tests"]) == 0
    assert cw.list_watchers(store, j)[0]["enabled"] is False
    assert cli.main(["watcher", "run", j, "tests"]) == 1  # off: cannot wake
    assert cli.main(["supervisor", j, "tests", "toggle"]) == 0  # switch routes to watcher toggle
    assert cw.list_watchers(store, j)[0]["enabled"] is True
    assert cli.main(["supervisor", j, "tests", "shadow"]) == 1
    assert cli.main(["watcher", "run", j, "tests"]) == 0
    assert store.kv_get(j, "watcher_run_now:tests") is True
    capsys.readouterr()
    assert cli.main(["watcher", "list", j, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["name"] == "tests" and rows[0]["runs"] == 0 and rows[0]["enabled"] is True
    assert cli.main(["watcher", "list", j]) == 0
    assert cli.main(["watcher", "rm", j, "tests"]) == 0
    assert cli.main(["watcher", "rm", j, "tests"]) == 1
    assert cw.list_watchers(store, j) == []
    events = [e for e in store.events(j) if e.what.startswith("watcher_")]
    assert [e.what for e in events] == ["watcher_added", "watcher_toggled", "watcher_toggled", "watcher_removed"]
    assert all(e.judgment.value == "human" for e in events)


def test_relay_mapping_and_supervisor_rejects_worker():
    p = cli._build_parser()
    ns = p.parse_args(["watcher", "add", "abc", "n", "--ask", "q", "--model", "sonnet", "--every", "1h", "--may", "nudge,escalate"])
    assert cli._op_and_args_for(ns) == ("watcher", {"action": "add", "job_id": "abc", "name": "n", "ask": "q",
                                                    "model": "sonnet", "every": "1h", "may": "nudge,escalate"})
    ns = p.parse_args(["supervisor", "abc", "n", "toggle"])
    assert cli._op_and_args_for(ns)[0] == "watcher"
    ns = p.parse_args(["supervisor", "abc", "sol", "toggle"])
    assert cli._op_and_args_for(ns)[0] == "supervisor"
    with pytest.raises(SystemExit):
        p.parse_args(["supervisor", "abc", "worker", "off"])


def test_new_watcher_flag_parsing():
    p = cli._build_parser()
    ns = p.parse_args(["new", "goal", "--watcher", "tests|10m|haiku|nudge|Are tests passing? a|b", "--watcher", "cost||opus||spend ok?"])
    assert [w["name"] for w in ns.watcher] == ["tests", "cost"]
    assert ns.watcher[0]["ask"] == "Are tests passing? a|b" and ns.watcher[0]["may"] == ["nudge"]
    assert ns.watcher[1]["every_s"] == 600 and ns.watcher[1]["model"] == "opus" and ns.watcher[1]["may"] == ["escalate"]
    op, args = cli._op_and_args_for(ns)
    assert op == "new" and len(args["watchers"]) == 2
    with pytest.raises(SystemExit):
        p.parse_args(["new", "goal", "--watcher", "bad|5s|haiku||q"])
    with pytest.raises(SystemExit):
        p.parse_args(["new", "goal", "--watcher", "only|two"])


def test_new_records_watchers_right_after_creation(steward_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-q",
                    "--allow-empty", "-m", "i"], check=True)
    assert cli.main(["new", "goal", "--repo", str(repo), "--no-baseline", "--watcher", "tests|10m|haiku|nudge|green?"]) == 0
    store = Store(steward_home)
    jid = store.list_jobs()[0].job_id if hasattr(store, "list_jobs") else None
    if jid is None:
        jid = store._conn.execute("SELECT job_id FROM jobs").fetchone()["job_id"]
    assert [w["name"] for w in cw.list_watchers(store, jid)] == ["tests"]


# -- scheduling ---------------------------------------------------------------


def test_scheduling_due_not_due_inflight_timeout(steward_home, fake_popen):
    store = Store(steward_home)
    j = _running(store).job_id
    cw.add_watcher(store, j, _spec(may="nudge"))
    clock = Clock()
    w = _watchers(store, fake_popen, clock)
    fake_popen.finish_now = False
    w.step(j)  # never ran: due now, started, not blocking
    assert len(fake_popen.instances) == 1
    argv = fake_popen.instances[0].argv
    assert argv[:5] == ["env", "-u", "ANTHROPIC_API_KEY", "claude", "-p"]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
    assert allowed == ["Read", "Glob", "Grep"]
    assert set(argv[argv.index("--disallowedTools") + 1:]) == {"Bash", "Edit", "Write", "NotebookEdit"}
    prompt = Path(fake_popen.instances[0].kw["stdin"].name).read_text()
    assert prompt.startswith("You are a Steward custom watcher named tests")
    assert "ship the widget" in prompt and "are tests green?" in prompt and "Powers granted: nudge" in prompt
    clock.t += 30
    w.step(j)  # in flight, still running: no second process
    assert len(fake_popen.instances) == 1
    clock.t += 400  # past the 300s timeout
    w.step(j)
    assert fake_popen.instances[0].killed
    assert [e.what for e in store.events(j) if e.what == "watcher_timed_out"] == ["watcher_timed_out"]
    assert store.kv_get(j, "watcher_inflight:tests") is None
    clock.t += 100  # 530s since start < 600: not due
    w.step(j)
    assert len(fake_popen.instances) == 1
    clock.t += 100  # 630s: due again
    fake_popen.finish_now = True
    w.step(j)
    assert len(fake_popen.instances) == 2
    w.step(j)  # completes: verdict recorded
    assert [e.evidence["status"] for e in store.events(j) if e.what == "watcher_verdict"] == ["concern"]
    assert cw.run_count(store, j, "tests") == 2


def test_disabled_not_run_and_run_now_forces(steward_home, fake_popen):
    store = Store(steward_home)
    j = _running(store).job_id
    cw.add_watcher(store, j, _spec())
    w = _watchers(store, fake_popen)
    w.step(j)
    w.step(j)
    assert len(fake_popen.instances) == 1
    w.step(j)  # not due, nothing new
    assert len(fake_popen.instances) == 1
    cw.request_run(store, j, "tests")
    w.step(j)
    assert len(fake_popen.instances) == 2
    cw.toggle_watcher(store, j, "tests")
    cw.CustomWatchers.step(w, j)
    w.clock.t += 10_000
    w.step(j)
    assert len(fake_popen.instances) == 2


def test_bad_output_records_error_not_verdict(steward_home, fake_popen):
    store = Store(steward_home)
    j = _running(store).job_id
    cw.add_watcher(store, j, _spec())
    fake_popen.next_output = json.dumps({"result": "no json here"})
    w = _watchers(store, fake_popen)
    w.step(j)
    w.step(j)
    kinds = [e.what for e in store.events(j) if e.what.startswith("watcher_") and e.what != "watcher_added"]
    assert kinds == ["watcher_error"]


# -- the gate -----------------------------------------------------------------


def _apply(store, j, w, verdict, clock):
    return cw.CustomWatchers(store, clock=clock).apply_verdict(j, w, verdict, cost_usd=0.02, usage={"input_tokens": 1})


def test_gate_granted_nudge_is_recorded_non_urgent(steward_home):
    store = Store(steward_home)
    j = _running(store).job_id
    w = _spec(may="nudge")
    clock = Clock()
    ev = _apply(store, j, w, {"status": "concern", "note": "n", "nudge": "run the tests"}, clock)
    inst = Steering(store).all_instructions(j)
    assert len(inst) == 1 and inst[0]["verb"] == "nudge" and inst[0]["origin"] == "watcher:tests"
    assert inst[0]["text"] == "[watcher tests] run the tests" and not inst[0].get("urgent")
    assert ev["nudge_rev"] == 1 and ev["suppressed"] == [] and ev["cost_usd"] == 0.02
    with pytest.raises(ValueError):
        Steering(store).record(j, "nudge", "x", origin="watcher:tests", urgent=True)
    with pytest.raises(ValueError):
        Steering(store).record(j, "focus", "x", origin="watcher:tests")


def test_gate_suppressions(steward_home):
    store = Store(steward_home)
    j = _running(store).job_id
    clock = Clock()
    # not granted
    ev = _apply(store, j, _spec(may=""), {"status": "alarm", "note": "n", "nudge": "x", "escalate": "y"}, clock)
    assert ev["suppressed"] == ["nudge:not_granted", "escalate:not_granted"]
    assert Steering(store).all_instructions(j) == [] and list_approvals(store, j) == []
    # ok never nudges
    w = _spec(may="nudge")
    assert _apply(store, j, w, {"status": "ok", "note": "n", "nudge": "x"}, clock)["suppressed"] == ["nudge:status_ok"]
    # first nudge goes through (rev 1, still unacked), so the next is blocked by the pending rev
    _apply(store, j, w, {"status": "concern", "note": "n", "nudge": "a"}, clock)
    clock.t += 5000
    assert _apply(store, j, w, {"status": "concern", "note": "n", "nudge": "b"}, clock)["suppressed"] == ["nudge:unacked_pending"]
    Steering(store).mark_acknowledged  # ack it, then cooldown applies
    inst = Steering(store)._load(j)
    inst[0]["status"] = "acknowledged"
    Steering(store)._save(j, inst)
    clock.t = 1_800_000_000.0 + 600  # < 2 x every (1200) since the nudge
    assert _apply(store, j, w, {"status": "concern", "note": "n", "nudge": "c"}, clock)["suppressed"] == ["nudge:cooldown"]
    clock.t = 1_800_000_000.0 + 1300
    assert "nudge_rev" in _apply(store, j, w, {"status": "alarm", "note": "n", "nudge": "d"}, clock)
    assert len(Steering(store).all_instructions(j)) == 2


def test_gate_escalate_opens_approval_and_notifies(steward_home):
    store = Store(steward_home)
    j = _running(store).job_id
    notes = []
    w = _spec(may="escalate")
    ev = cw.CustomWatchers(store, clock=Clock(), notify=lambda *a: notes.append(a)).apply_verdict(
        j, w, {"status": "alarm", "note": "n", "escalate": "worker is looping"})
    pend = list_approvals(store, j, status="pending")
    assert len(pend) == 1 and pend[0]["question"] == "watcher tests: worker is looping"
    assert notes and notes[0][1] == "NEEDS_YOU" and "worker is looping" in notes[0][3]
    assert ev["escalated"] == pend[0]["id"]


# -- dashboard rows, map, feed, tokens, chat ----------------------------------


def test_supervisor_rows_and_feed(steward_home, fake_popen):
    store = Store(steward_home)
    j = _running(store).job_id
    cw.add_watcher(store, j, _spec("verylongname", every=None, may="nudge"))
    rows = supervisor_rows(store, j)
    assert [r["role"] for r in rows] == ["worker", "astra", "sol", "verylongname", "efficiency", "jev"]
    r = rows[3]
    assert r["switch"] == "on" and r["next"] == "off" and r["model"] == "haiku" and r["source"] == "watcher:verylongname"
    assert r["display"].startswith("[on] every 10m · 0 runs · next ")
    w = _watchers(store, fake_popen)
    w.step(j)
    w.step(j)
    r = supervisor_rows(store, j)[3]
    assert " · 1 runs · last " in r["display"] and " behind" in r["display"]
    cw.toggle_watcher(store, j, "verylongname")
    r = supervisor_rows(store, j)[3]
    assert r["switch"] == "off" and r["display"].startswith("[off] ") and " next " not in r["display"]
    lines = [item[1:] for item in feed.collect(store.events(j))]
    texts = [t for _, t in lines]
    assert "watcher verylongname concern: behind" in texts
    assert any(t.startswith("watcher verylongname added") for t in texts)
    assert "watcher verylongname switched on → off" in texts


def test_feed_nudge_escalate_suppressed_timeout(steward_home):
    store = Store(steward_home)
    j = _running(store).job_id
    w = cw.CustomWatchers(store, clock=Clock(), notify=lambda *a: None)
    spec = _spec(may="nudge,escalate")
    w.apply_verdict(j, spec, {"status": "alarm", "note": "bad", "nudge": "fix", "escalate": "help"})
    w.apply_verdict(j, _spec(may=""), {"status": "alarm", "note": "bad", "nudge": "fix"})
    store.append_event(j, what="watcher_timed_out", evidence={"name": "tests"}, rule="r",
                       judgment=__import__("steward.models", fromlist=["Judgment"]).Judgment.code)
    texts = [t for _, _, t in feed.collect(store.events(j))]
    assert any(t.startswith("watcher tests → nudge rev 1") for t in texts)
    assert "watcher tests → escalated" in texts
    assert any("(suppressed: nudge:not_granted)" in t for t in texts)
    assert "watcher tests timed out" in texts


def test_renderer_map_watcher_toggle_alignment(steward_home, tmp_path):
    store = Store(steward_home)
    job = _running(store)
    store.kv_set(job.job_id, "herdr_tab", "w1:t2")
    store.kv_set(job.job_id, "worker_model", "sonnet")
    cw.add_watcher(store, job.job_id, _spec("verylongname"))
    cw.add_watcher(store, job.job_id, _spec("quiet"))
    cw.toggle_watcher(store, job.job_id, "quiet")
    map_path = tmp_path / "map"
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_DASH_COLS": "70",
           "STEWARD_DASH_MAP": str(map_path), "PYTHONPATH": str(_REPO)}
    res = subprocess.run([sys.executable, str(_REPO / "deploy/cockpit/steward-dash-jobs.py")],
                         capture_output=True, text=True, env=env, cwd=_REPO)
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln).replace("{SPIN}", "x") for ln in res.stdout.splitlines()]
    assert all(len(ln) <= 70 for ln in lines), lines
    cmap = map_path.read_text().splitlines()
    toggles = [a.split() for a in cmap if a.startswith("toggle ")]
    assert [t[5] for t in toggles] == ["astra", "sol", "verylongname", "quiet", "efficiency", "jev"]
    assert [t[6] for t in toggles] == ["active", "off", "off", "on", "active", "active"]
    for _, idx, c0, c1, jid, role, nxt in toggles:
        line = lines[int(idx)]
        assert line[int(c0) - 1] == "[" and line[int(c1) - 1] == "]", (line, c0, c1)
    wline = lines[int(toggles[2][1])]
    assert "verylon… haiku" in wline and "verylongname" not in wline  # label cut to 7 chars + an ellipsis (keeps a gap before the model)
    assert any(c.startswith(f"chatbtn {toggles[2][1]} ") and c.endswith(f" {job.job_id} verylongname") for c in cmap)  # the [ chat ] button


def test_token_attribution_for_watcher_transcripts(steward_home, tmp_path):
    import time as _t
    store = Store(steward_home)
    job = _running(store)
    proj = tmp_path / "projects"
    d = proj / project_slug(worktree_path(store, job.job_id))
    d.mkdir(parents=True)
    store.kv_set(job.job_id, "worker_session_id", "w")

    def user(text):
        return json.dumps({"type": "user", "message": {"role": "user", "content": text}})

    def msg(mid, model):
        return json.dumps({"type": "assistant", "timestamp": "2027-01-15T08:00:00.000Z", "message": {
            "id": mid, "model": model, "usage": {"input_tokens": 1000, "output_tokens": 1000,
                                                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})

    (d / "w.jsonl").write_text("\n".join([user("task"), msg("m1", "claude-sonnet-5-5")]))
    (d / "wa.jsonl").write_text("\n".join([user(cw.MARKER + "tests, attached to Steward job x.\nRole: ..."), msg("a1", "claude-haiku-4-5")]))
    (d / "wb.jsonl").write_text("\n".join([user(cw.MARKER + "cost, attached"), msg("b1", "claude-opus-5-5")]))
    r = tokens.job_tokens(store, job.job_id, now=1_800_000_000.0, projects_dir=proj)
    assert r["by_source"]["watcher:tests"] == 2000 and r["by_source"]["watcher:cost"] == 2000
    assert r["by_source"]["chats"] == 0 and r["total"]["all"] == 6000
    assert r["sources"]["watcher:tests"]["usd"] == pytest.approx((1000 * 1 + 1000 * 5) / 1e6)
    assert r["sources"]["watcher:cost"]["usd"] == pytest.approx((1000 * 4 + 1000 * 20) / 1e6)
    assert r["total"]["usd"] == pytest.approx(sum(v["usd"] for v in r["sources"].values()))


def test_chat_plan_for_watcher(steward_home):
    store = Store(steward_home)
    j = _running(store).job_id
    cw.add_watcher(store, j, _spec(model="sonnet", ask="is the diff small?"))
    cw.CustomWatchers(store, clock=Clock()).apply_verdict(j, _spec(model="sonnet"), {"status": "ok", "note": "tiny diff"})
    plan = build_chat(store, j, "tests")
    assert plan.role == "tests" and plan.model == "sonnet"
    assert "claude --model sonnet" in plan.command and "--append-system-prompt" in plan.command
    primer = Path(plan.primer_path).read_text()
    assert "You are tests, a custom watcher" in primer and "is the diff small?" in primer and "tiny diff" in primer
    with pytest.raises(Exception):
        build_chat(store, j, "nosuch")
