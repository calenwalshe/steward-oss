"""Decision buttons: the evidence panel, expand/arm state, click map columns, the why chat, phone keys."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from steward import approvals, chat as chat_module, cli, dashact, evidence, phone
from steward.models import JobState, Judgment
from steward.store import Store

_REPO = Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
BASE = "b" * 40


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def _mkjob(store, state):
    job = store.create_job("Ship the ticker; finish line: ticks.csv has 30 rows", "/tmp/somerepo")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))
    store.kv_set(job.job_id, "worker_model", "sonnet")
    return job


def _ev(store, job, what, **evidence_):
    store.append_event(job.job_id, what=what, evidence=evidence_, rule="r", judgment=Judgment.code)


@pytest.fixture
def accept_job(steward_home, tmp_path):
    """NEEDS_HUMAN job: scratch worktree repo with 2 commits past baseline, checks, transcript, watcher."""
    store = Store()
    job = _mkjob(store, JobState.NEEDS_HUMAN)
    wt = store.home / "worktrees" / job.job_id
    wt.mkdir(parents=True)
    _git(wt, "init", "-q")
    (wt / "a.txt").write_text("0")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "baseline commit")
    base = _git(wt, "rev-parse", "HEAD")
    for i in (1, 2):
        (wt / "a.txt").write_text(str(i))
        _git(wt, "commit", "-qam", f"tick {i}")
    head = _git(wt, "rev-parse", "HEAD")
    store.add_check(job.job_id, commit_sha=base, command="pytest", exit_code=1, results={"t::a": "passed", "t::b": "failed"}, label="baseline")
    store.add_check(job.job_id, commit_sha=head, command="pytest", exit_code=0, results={"t::a": "passed", "t::b": "passed", "t::c": "passed"}, label="verify")
    _ev(store, job, "verification recorded", commit_sha=head, new_failures=[], fixed=[], preexisting_failures=[])
    _ev(store, job, "checks passed; acceptance unresolved", reasons=["Sam accepts"])
    rec = approvals.open_approval(store, job.job_id, approvals.ACCEPTANCE_QUESTION, kind="acceptance")
    from steward import custom_watchers

    custom_watchers.add_watcher(store, job.job_id, custom_watchers.make_spec("pace", "is it on pace?"))
    store.kv_set(job.job_id, "watcher_runs:pace", 30)
    _ev(store, job, "watcher_verdict", name="pace", status="ok", note="fine")
    projects = tmp_path / "projects"
    d = projects / chat_module.project_slug(wt)
    d.mkdir(parents=True)
    store.kv_set(job.job_id, "worker_session_id", "sess1")
    rows = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "early note"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "All done: ticker works.\nSecond line here.\nThird line."}]}},
            {"type": "user", "message": {"content": "thanks"}}]
    (d / "sess1.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return store, job, rec, head, projects


def test_acceptance_panel(accept_job):
    store, job, rec, head, projects = accept_job
    dec = evidence.decision(store, job.job_id)
    assert dec["kind"] == "acceptance" and dec["approval"]["id"] == rec["id"]
    lines = evidence.panel(store, job.job_id, 80, projects_dir=projects, cost_usd=1.5)
    assert lines[0] == "goal: Ship the ticker"
    assert f"verified yes · commit {head[:7]} · clean · tests 3/0 (baseline 1)" in lines
    assert "commits since baseline (2):" in lines
    assert any(ln.endswith("tick 2") for ln in lines) and any(ln.endswith("tick 1") for ln in lines)
    assert "worker said: All done: ticker works." in lines and "  Second line here." in lines
    assert "  Third line." in lines and not any("early note" in ln for ln in lines)
    assert "watchers: pace 30 runs, ok" in lines
    assert lines[-1] == "cost $1.50"
    assert all(len(ln) <= 80 for ln in evidence.panel(store, job.job_id, 40, projects_dir=projects, cost_usd=1.5))


def test_panel_marks_dirty_verification(accept_job):
    store, job, *_ = accept_job
    _ev(store, job, "verify_worktree_dirty", commit_sha="c" * 40)
    assert any(" · dirty · " in ln for ln in evidence.panel(store, job.job_id, 80, cost_usd=0))


def test_approval_panel(steward_home):
    store = Store()
    job = _mkjob(store, JobState.RUNNING)
    cmd = "rm -rf /home/user/precious && " + "echo x " * 30
    rec = approvals.open_approval(store, job.job_id, f"worker permission: Bash {{'command': {cmd!r}}}", kind="permission")
    dec = evidence.decision(store, job.job_id)
    assert dec["kind"] == "approval"
    lines = evidence.panel(store, job.job_id, 60, dec=dec)
    assert lines[0] == "Bash:"
    body = [ln for ln in lines if ln.startswith("  ")]
    assert 1 < len(body) <= 3 and body[0].startswith("  rm -rf /home/user/precious")
    assert any(ln.startswith("held: rm targets a path outside the worktree") for ln in lines)
    assert lines[-1].startswith("asked ") and lines[-1].endswith(" ago")
    assert [b[1] for b in evidence.buttons_for(dec)] == ["approve", "deny", "why"]
    assert rec["id"] == dec["approval"]["id"]


def test_escalation_panel(steward_home):
    store = Store()
    job = _mkjob(store, JobState.RUNNING)
    rec = approvals.open_approval(store, job.job_id, "watcher pace: ticks look stuck, please check")
    _ev(store, job, "watcher_escalated", name="pace", approval_id=rec["id"], text="ticks look stuck, please check")
    _ev(store, job, "watcher_verdict", name="pace", status="alarm", note="no tick in 20m")
    dec = evidence.decision(store, job.job_id)
    assert dec["kind"] == "escalation"
    lines = evidence.panel(store, job.job_id, 80, dec=dec)
    assert lines[:3] == ["watcher pace (alarm):", "note: no tick in 20m", "text: ticks look stuck, please check"]
    assert [b[1] for b in evidence.buttons_for(dec)] == ["approve", "deny", "why"]


def test_no_decision_no_panel(steward_home):
    store = Store()
    job = _mkjob(store, JobState.RUNNING)
    assert evidence.decision(store, job.job_id) is None and evidence.panel(store, job.job_id) == []


# -- expand state + confirm flow -----------------------------------------------------


def test_expand_toggle_state(tmp_path):
    path = dashact.state_path(str(tmp_path / "m"))
    assert path.name == "m.expanded"
    assert dashact.toggle_expand(path, "j1") is True
    assert dashact.is_expanded(dashact.load(path), "j1") and not dashact.is_expanded(dashact.load(path), "j2")
    assert dashact.toggle_expand(path, "j1") is False
    assert not dashact.is_expanded(dashact.load(path), "j1")


def test_one_tap_decides(tmp_path):
    path = dashact.state_path(str(tmp_path / "m"))
    status, argv = dashact.press(path, "accept", "J", now=100.0)
    assert status == "run" and argv == ["python3", "-m", "steward.cli", "accept", "J", "--origin", "dash"]
    assert dashact.press(path, "deny", "J", "A1", now=101.0)[1][3:] == ["deny", "J", "A1", "--origin", "dash"]


def test_second_tap_guard_still_works_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(dashact, "CONFIRM", ("approve", "deny"))
    path = dashact.state_path(str(tmp_path / "m"))
    stale = 100.0 + dashact.ARM_S + 1
    assert dashact.press(path, "approve", "J", "A1", now=100.0)[0] == "armed"
    assert dashact.armed_action(dashact.load(path), "J", stale) is None
    assert dashact.press(path, "approve", "J", "A1", now=stale)[0] == "armed"  # stale: arms again, no run
    assert dashact.press(path, "deny", "J", "A1", now=stale + 1)[0] == "armed"  # different button: arms, no run
    status, argv = dashact.press(path, "deny", "J", "A1", now=stale + 2)
    assert (status, argv[3:]) == ("run", ["deny", "J", "A1", "--origin", "dash"])


def test_reject_denies_acceptance_approval_and_why_needs_no_confirm(tmp_path):
    path = dashact.state_path(str(tmp_path / "m"))
    dashact.press(path, "reject", "J", "A9", now=1.0)
    assert dashact.press(path, "reject", "J", "A9", now=2.0)[1][3:] == ["deny", "J", "A9", "--origin", "dash"]
    assert dashact.press(path, "why", "J", now=3.0) == ("run", ["python3", "-m", "steward.cli", "chat", "J", "why"])


def test_dashact_cli_prints_argv(tmp_path):
    env = {**os.environ, "STEWARD_DASH_MAP": str(tmp_path / "m"), "PYTHONPATH": str(_REPO)}
    run = lambda *a: subprocess.run([sys.executable, "-m", "steward.dashact", *a], capture_output=True, text=True, env=env, cwd=_REPO).stdout.split("\n")
    assert run("press", "approve", "J", "A1")[:2] == ["run", "python3"]  # one tap
    assert run("expand", "J")[0] == "open"


# -- click map vs rendered columns -------------------------------------------------------


def _render(steward_home, tmp_path, expanded_jobs, cols=80):
    map_path = tmp_path / "map"
    dashact.save(dashact.state_path(str(map_path)), {"expanded": {j: True for j in expanded_jobs}, "armed": {}})
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_DASH_COLS": str(cols),
           "STEWARD_DASH_MAP": str(map_path), "PYTHONPATH": str(_REPO)}
    res = subprocess.run([sys.executable, str(_REPO / "deploy/cockpit/steward-dash-jobs.py")],
                         capture_output=True, text=True, env=env, cwd=_REPO)
    lines = [ANSI.sub("", ln).replace("{SPIN}", "x") for ln in res.stdout.splitlines()]
    return lines, map_path.read_text().splitlines()


@pytest.mark.parametrize("kind", ["acceptance", "approval"])
def test_button_map_columns_match_rendered_brackets(steward_home, tmp_path, kind):
    store = Store()
    if kind == "acceptance":
        job = _mkjob(store, JobState.NEEDS_HUMAN)
        _ev(store, job, "checks passed; acceptance unresolved", reasons=["Sam accepts"])
        approvals.open_approval(store, job.job_id, approvals.ACCEPTANCE_QUESTION, kind="acceptance")
        expect = ["accept", "sendback", "stop", "chat"]
    else:
        job = _mkjob(store, JobState.RUNNING)
        approvals.open_approval(store, job.job_id, "worker permission: Bash {'command': 'rm -rf /home/user/x'}", kind="permission")
        expect = ["approve", "deny", "why"]
    collapsed, cmap0 = _render(steward_home, tmp_path, [])
    assert not any(a.startswith("btn ") and " steer " not in a and " export " not in a and " explain " not in a and " unblock " not in a and " fold " not in a for a in cmap0)
    assert any(ln.lstrip().startswith("→ ") for ln in collapsed)
    lines, cmap = _render(steward_home, tmp_path, [job.job_id])
    assert all(len(ln) <= 80 for ln in lines)
    exp = [a.split() for a in cmap if a.startswith("expand ")]
    assert exp == [["expand", str(next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("→ "))), job.job_id]]
    btns = [a.split() for a in cmap if a.startswith("btn ") and " steer " not in a and " export " not in a and " explain " not in a and " unblock " not in a and " fold " not in a]
    assert [b[4] for b in btns] == expect
    row = int(btns[0][1])
    assert all(int(b[1]) == row for b in btns)
    for b in btns:
        text = lines[row][int(b[2]) - 1:int(b[3])]
        assert text.startswith("[ ") and text.endswith(" ]"), text
        assert b[5] == job.job_id
    assert lines[row].count("[ ") == len(expect)
    if kind == "acceptance":
        assert btns[2][6] != "-"  # stop knows the approval to deny
        assert "[ accept ]  [ send back ]  [ stop ]  [ chat ]" in lines[row]


def test_armed_button_renders_confirm_label(steward_home, tmp_path):
    store = Store()
    job = _mkjob(store, JobState.NEEDS_HUMAN)
    _ev(store, job, "checks passed; acceptance unresolved", reasons=["x"])
    approvals.open_approval(store, job.job_id, approvals.ACCEPTANCE_QUESTION, kind="acceptance")
    map_path = tmp_path / "map"
    import time
    dashact.save(dashact.state_path(str(map_path)), {"expanded": {job.job_id: True},
                                                     "armed": {job.job_id: {"action": "accept", "until": time.time() + 30}}})
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_DASH_COLS": "80",
           "STEWARD_DASH_MAP": str(map_path), "PYTHONPATH": str(_REPO)}
    out = subprocess.run([sys.executable, str(_REPO / "deploy/cockpit/steward-dash-jobs.py")], capture_output=True, text=True, env=env, cwd=_REPO).stdout
    lines = [ANSI.sub("", ln) for ln in out.splitlines()]
    row = next(ln for ln in lines if "[ confirm accept ]" in ln)
    btn = next(a.split() for a in map_path.read_text().splitlines() if a.startswith("btn ") and " accept " in a)
    assert row[int(btn[2]) - 1:int(btn[3])] == "[ confirm accept ]"


# -- why chat --------------------------------------------------------------------------


def test_why_chat_plan(accept_job):
    store, job, rec, head, projects = accept_job
    plan = chat_module.build_chat(store, job.job_id, "why")
    assert plan.role == "why" and plan.model == "sonnet"
    assert plan.cwd == str(store.home / "worktrees" / job.job_id)
    assert "claude --model sonnet" in plan.command and "--resume" not in plan.command
    primer = Path(plan.primer_path).read_text()
    assert "Sam is deciding whether to accept; answer his questions about the evidence; you have no authority to act" in primer
    assert "finish line: ticks.csv has 30 rows" in primer
    assert head in primer  # the verification dict
    assert approvals.ACCEPTANCE_QUESTION in primer
    assert "verified yes" in primer and "commits since baseline" in primer
    assert chat_module.chat_label("why", job.job_id) == f"why chat {job.job_id[:8]}"


def test_why_chat_needs_a_pending_decision(steward_home):
    store = Store()
    job = _mkjob(store, JobState.RUNNING)
    with pytest.raises(chat_module.ChatError):
        chat_module.build_chat(store, job.job_id, "why")


# -- origin dash -----------------------------------------------------------------------


def test_origin_dash_accepted_by_parser():
    p = cli._build_parser()
    assert p.parse_args(["accept", "J", "--origin", "dash"]).origin == "dash"
    assert p.parse_args(["approve", "J", "A", "--origin", "dash"]).origin == "dash"
    assert p.parse_args(["deny", "J", "A", "--origin", "dash"]).origin == "dash"
    assert p.parse_args(["chat", "J", "why"]).role == "why"


def test_accept_relays_origin_dash(steward_home, monkeypatch):
    store = Store()
    job = _mkjob(store, JobState.NEEDS_HUMAN)
    seen = {}

    def fake_relay(store_, op, args, **kw):
        seen.update(op=op, args=args, **kw)
        return {"accepted": True, "detail": f"{job.job_id}\tACCEPTED"}

    monkeypatch.setattr(cli, "_relay", fake_relay)
    assert cli.main(["accept", job.job_id, "--origin", "dash"]) == 0
    assert seen["op"] == "accept" and seen["origin"] == "dash"


# -- phone -----------------------------------------------------------------------------


def _phone_for(store, job):
    calls = []
    p = phone.Phone(store, run_cli=lambda argv: (calls.append(argv) or (0, "ok")), tokens_fn=lambda s: None)
    p.job_id, p.screen = job.job_id, "job"
    return p, calls


def test_phone_job_screen_shows_panel_and_keys_accept(accept_job):
    store, job, rec, head, projects = accept_job
    p, calls = _phone_for(store, job)
    text = [phone.plain(ln) for ln in p.lines(80, 60)]
    assert any(t.startswith("verified yes · commit") for t in text)
    assert any("A accept" in t and "S send back" in t and "x stop" in t and "C chat" in t for t in text)
    p.handle("A")  # one key decides
    assert calls == [["accept", job.job_id, "--origin", "moshi"]]


def test_phone_reject_denies_acceptance_approval_in_one_key(accept_job):
    store, job, rec, *_ = accept_job
    p, calls = _phone_for(store, job)
    p.handle("R")
    assert calls == [["deny", job.job_id, rec["id"], "--origin", "moshi"]]


def test_phone_approval_keys_and_why(steward_home):
    store = Store()
    job = _mkjob(store, JobState.RUNNING)
    rec = approvals.open_approval(store, job.job_id, "worker permission: Bash {'command': 'rm -rf /home/user/x'}", kind="permission")
    p, calls = _phone_for(store, job)
    p.handle("A")
    p.handle("y")
    p.handle("R")
    p.handle("y")
    p.handle("?")
    assert calls == [["approve", job.job_id, rec["id"], "--origin", "moshi"],
                     ["deny", job.job_id, rec["id"], "--origin", "moshi"],
                     ["chat", job.job_id, "why"]]


def test_phone_keys_with_nothing_pending_and_existing_keys(two_jobs=None, steward_home=None):
    store = Store()
    job = _mkjob(store, JobState.RUNNING)
    p, calls = _phone_for(store, job)
    p.handle("A")
    assert p.mode is None and calls == [] and p.note == "nothing waiting on you"
    p.handle("x")  # existing stop-confirm still works
    assert p.mode == "confirm"
    p.handle("y")
    assert calls == [["stop", job.job_id, "--origin", "moshi"]]


def test_release_button_is_one_tap_with_dash_origin(tmp_path):
    path = dashact.state_path(str(tmp_path / "m"))
    assert dashact.press(path, "release", "J", now=1.0) == (
        "run", ["python3", "-m", "steward.cli", "release", "J", "--origin", "dash"])


def test_accept_closes_panels_and_merge_panel_needs_its_own_tap(tmp_path):
    # Regression: accepting left the shared "expanded" flag on, so the merge panel opened by
    # itself and [ merge into master ] appeared under Sam's finger.
    path = dashact.state_path(str(tmp_path / "m"))
    dashact.toggle_expand(path, "J")  # evidence panel open
    assert dashact.press(path, "accept", "J")[0] == "run"
    data = dashact.load(path)
    assert not dashact.is_expanded(data, "J") and not dashact.is_expanded(data, dashact.INC_PREFIX + "J")
    assert dashact.argv_for("incorporate", "J")[-1] == dashact.INC_PREFIX + "J"


# -- richer panel ------------------------------------------------------------------------


def test_finish_line_checklist_parsing():
    goal, fin = evidence.split_goal("Build the ticker; FINISH LINE: ticks.csv has 30 rows (sorted, no dups); README documents it. "
                                    "Tests pass, and docs/notes.md exists")
    assert goal == "Build the ticker"
    assert evidence.finish_items(fin) == ["ticks.csv has 30 rows (sorted, no dups)", "README documents it",
                                          "Tests pass", "docs/notes.md exists"]
    assert evidence.split_goal("no marker here") == ("no marker here", "")
    assert evidence.finish_items("(1) a thing (2) another thing") == ["a thing", "another thing"]


def test_panel_new_sections(accept_job):
    store, job, rec, head, projects = accept_job
    wt = store.home / "worktrees" / job.job_id
    (wt / "docs").mkdir()
    (wt / "docs" / "report.md").write_text("x\ny\n")
    (wt / "new.py").write_text("print(1)\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "docs")
    lines = evidence.panel(store, job.job_id, 80, projects_dir=projects, cost_usd=0)
    assert lines[0] == "goal: Ship the ticker"
    i = lines.index("finish line:")
    assert lines[i + 1] == "  ☐ ticks.csv has 30 rows"
    ch = next(k for k, ln in enumerate(lines) if ln.startswith("changed: "))
    assert lines[ch] == "changed: 3 files, +4/-1"
    assert lines[ch].startswith("changed: 3 files, +") and "/-" in lines[ch]
    assert lines[ch + 1] == "  docs/report.md" and lines[ch + 2] == "  new.py" and lines[ch + 3] == "  a.txt"
    assert lines.index("worker said: All done: ticker works.") > ch
    assert all(len(ln) <= 80 for ln in lines)


def test_worker_said_up_to_six_lines_redacted(accept_job):
    store, job, rec, head, projects = accept_job
    wt = store.home / "worktrees" / job.job_id
    d = projects / chat_module.project_slug(wt)
    text = "\n".join([f"line {i}" for i in range(1, 9)] + ["api_key=sk-secretvalue123456"])
    (d / "sess1.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
    lines = evidence.panel(store, job.job_id, 80, projects_dir=projects, cost_usd=0)
    i = lines.index("worker said: line 1")
    assert lines[i + 1:i + 6] == [f"  line {n}" for n in range(2, 7)] and lines[i + 6] != "  line 7"


def test_advisor_line(accept_job, monkeypatch):
    store, job, rec, head, projects = accept_job
    from steward import trajectory

    monkeypatch.setattr(trajectory, "review_history", lambda s, j: [
        {"verdict": {"trajectory": "on_track", "observation": "tests green,  scope fine"}}])
    lines = evidence.panel(store, job.job_id, 80, projects_dir=projects, cost_usd=0)
    assert "advisor: on_track — tests green, scope fine" in lines


# -- stop / send back / chat buttons ------------------------------------------------------


def test_acceptance_buttons_and_stop_is_old_reject(tmp_path):
    dec = {"kind": "acceptance", "approval": {"id": "A9"}}
    assert [b[0] for b in evidence.buttons_for(dec)] == ["accept", "send back", "stop", "chat"]
    assert [b[1] for b in evidence.buttons_for({"kind": "acceptance", "approval": None})] == ["accept", "sendback", "chat"]
    path = dashact.state_path(str(tmp_path / "m"))
    stop = dashact.press(path, "stop", "J", "A9", now=1.0)
    assert stop == dashact.press(path, "reject", "J", "A9", now=2.0)  # reject stays an alias
    assert stop[1][3:] == ["deny", "J", "A9", "--origin", "dash"]


def test_send_back_and_chat_open_the_job_chat(tmp_path):
    path = dashact.state_path(str(tmp_path / "m"))
    assert dashact.press(path, "sendback", "J")[1][3:] == ["chat", "J", "job", "--send-back"]
    assert dashact.press(path, "chat", "J")[1][3:] == ["chat", "J", "job"]
    p = cli._build_parser()
    ns = p.parse_args(["chat", "J", "job", "--send-back"])
    assert ns.role == "job" and ns.send_back is True


# -- job chat -----------------------------------------------------------------------------


def test_job_chat_plan(accept_job):
    import shlex

    store, job, rec, head, projects = accept_job
    other = "ffffffff-0000-0000-0000-000000000000"
    plan = chat_module.build_chat(store, job.job_id, "job", projects_dir=projects)
    assert plan.role == "job" and plan.model == "sonnet"
    assert plan.cwd == str(store.home / "worktrees" / job.job_id)
    assert chat_module.chat_label("job", job.job_id) == f"job chat {job.job_id[:8]}"
    argv = shlex.split(plan.command.split("&&", 1)[1])
    assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    i = argv.index("--allowedTools")
    tools = argv[i + 1:argv.index("--permission-mode")]
    assert tools[:3] == ["Read", "Glob", "Grep"] and tools == chat_module.job_allowed_tools(job.job_id)
    assert f"Bash(python3 -m steward.cli steer {job.job_id} *)" in tools
    assert f"Bash(python3 -m steward.cli send-back {job.job_id} *)" in tools
    assert f"Bash(python3 -m steward.cli watcher add {job.job_id}*)" in tools
    assert all(t.startswith("Bash(python3 -m steward.cli ") for t in tools[3:])
    assert all(job.job_id in t for t in tools[3:]) and not any(other in t for t in tools)
    assert argv[-1] == chat_module._JOB_OPENER
    primer = Path(plan.primer_path).read_text()
    for needle in ("ONE Steward job", "explicit words", "confirmation in chat", "--origin job-chat",
                   "Ship the ticker", "verified yes", "Watchers feed (last 20)",
                   "Worker transcript (last 10 turns)", "Pending decision (acceptance)", "All done: ticker works."):
        assert needle in primer, needle


def test_job_chat_send_back_opener(accept_job):
    store, job, rec, head, projects = accept_job
    plan = chat_module.build_chat(store, job.job_id, "job", projects_dir=projects, send_back=True)
    assert plan.command.rstrip().endswith(
        "'Sam wants to send this job back. Ask him in one short question what should change, "
        "then run the send-back command with his words.'")


def test_origin_job_chat_accepted_by_relay_parser():
    p = cli._build_parser()
    assert p.parse_args(["steer", "J", "hi", "--origin", "job-chat"]).origin == "job-chat"
    assert p.parse_args(["send-back", "J", "do x", "--origin", "job-chat"]).origin == "job-chat"
    assert p.parse_args(["accept", "J", "--origin", "job-chat"]).origin == "job-chat"


def test_send_back_model_flag(steward_home, monkeypatch):
    p = cli._build_parser()
    ns = p.parse_args(["send-back", "J", "again", "--model", "opus"])
    assert cli._op_and_args_for(ns)[1]["model"] == "opus"
    assert "model" not in cli._op_and_args_for(p.parse_args(["send-back", "J", "again"]))[1]


def test_phone_chat_key_opens_job_chat(accept_job):
    store, job, rec, head, projects = accept_job
    p, calls = _phone_for(store, job)
    p.handle("C")
    assert calls == [["chat", job.job_id, "job"]]
    p.handle("S")
    assert p.mode == "input" and p.purpose == "sendback"
