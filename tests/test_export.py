"""Export to workspace: eligibility, the dashboard button, bundle contents + redaction, workspace/cwd/name, resume, event."""

from __future__ import annotations

import json
import re

import pytest
from test_dash_polish import _render

from steward import chat as chat_module
from steward import cli, dashact
from steward import export as export_module
from steward.models import JobState
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

SECRET = "sk-abcdefghijklmnop1234567890"
FINAL = "All done: widget shipped, 12 tests green."


def _set_state(store, job, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))


def _res(argv, out="", code=0):
    return CommandResult(argv=argv, cwd="", exit_code=code, stdout=out, stderr="", duration_s=0.0)


def fake_runner(*, resume_help=True, ws="w9", fail_create=False):
    def respond(argv, cwd):
        if argv[:3] == ["herdr", "workspace", "create"]:
            if fail_create:
                return _res(argv, code=1)
            return _res(argv, json.dumps({"result": {"workspace": {"workspace_id": ws}, "root_pane": {"pane_id": f"{ws}:p1"}}}))
        if argv[:2] == ["claude", "--help"]:
            return _res(argv, "  --resume <session-id>\n" if resume_help else "  --model\n")
        if argv[:2] == ["git", "diff"]:
            return _res(argv, " a.py | 2 +-\n 1 file changed\n")
        if argv[:2] == ["git", "log"]:
            return _res(argv, "abc1234 feat: the widget\n")
        if argv[:2] == ["git", "merge-base"]:
            return _res(argv, "base000\n")
        if argv[:2] == ["git", "rev-parse"]:
            return _res(argv, "head000\n")
        return _res(argv)
    return FakeRunner(respond)


@pytest.fixture(autouse=True)
def _no_real_claude_config(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_module, "CLAUDE_CONFIG", tmp_path / "claude.json")  # ensure_trusted must not touch ~/.claude.json


@pytest.fixture
def world(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("Ship the widget. FINISH LINE: tests green.", str(tmp_path / "repo"))
    (tmp_path / "repo").mkdir()
    wt = chat_module.worktree_path(store, job.job_id)
    wt.mkdir(parents=True)
    _set_state(store, job, JobState.ACCEPTED)
    store.kv_set(job.job_id, "worker_session_id", "sess-1")
    proj = tmp_path / "projects"
    d = proj / chat_module.project_slug(wt)
    d.mkdir(parents=True)
    rows = [
        {"type": "user", "timestamp": "2026-09-30T10:00:00Z", "message": {"content": "Ship the widget"}},
        {"type": "assistant", "timestamp": "2026-09-30T10:00:05Z", "message": {"id": "m1", "content": [
            {"type": "text", "text": "Starting."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": f"curl -H 'Authorization: Bearer {SECRET}' x"}}]}},
        {"type": "user", "timestamp": "2026-09-30T10:00:09Z", "message": {"content": "[rev=2] use the small widget"}},
        {"type": "assistant", "timestamp": "2026-09-30T10:01:00Z", "message": {"id": "m2", "content": [
            {"type": "text", "text": f"{FINAL} api_key={SECRET}"}]}},
    ]
    (d / "sess-1.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return store, job, wt, proj


# -- eligibility / button ---------------------------------------------------------------------------------


@pytest.mark.parametrize("state,ok", [(JobState.ACCEPTED, True), (JobState.RELEASED, True), (JobState.STOPPED, True),
                                      (JobState.RUNNING, False), (JobState.PAUSED, False), (JobState.FAILED, True),
                                      (JobState.NEEDS_HUMAN, False)])
def test_eligible_states(world, state, ok):
    store, job, _, _ = world
    _set_state(store, job, state)
    assert export_module.eligible(store, store.get_job(job.job_id)) is ok


def test_needs_human_at_acceptance_is_eligible(world):
    store, job, _, _ = world
    _set_state(store, job, JobState.NEEDS_HUMAN)
    store.append_event(job.job_id, what="checks passed; acceptance unresolved", evidence={}, rule="t", judgment=export_module.Judgment.code)
    assert export_module.eligible(store, store.get_job(job.job_id))


def test_export_refuses_unfinished_job(world):
    store, job, _, proj = world
    _set_state(store, job, JobState.RUNNING)
    runner = fake_runner()
    with pytest.raises(export_module.ExportError, match="finished"):
        export_module.export_job(store, job.job_id, runner, projects_dir=proj)
    assert runner.calls == []


def test_dashact_argv():
    assert dashact.argv_for("export", "J")[-2:] == ["export", "J"]


def _btns(cmap, action):
    return [c.split() for c in cmap if re.match(r"btn \d+ \d+ \d+ " + action + " ", c)]


def test_button_only_on_finished_jobs(steward_home, tmp_path):
    from test_dash_polish import _running

    store = Store(steward_home)
    run = _running(store)
    done = _running(store, goal="Finished one")
    _set_state(store, done, JobState.ACCEPTED)
    store.kv_set(done.job_id, "incorporated_commit", "abc1234def")
    stopped = _running(store, goal="At acceptance")  # NEEDS_HUMAN waiting at acceptance (STOPPED jobs are not in the pane)
    _set_state(store, stopped, JobState.NEEDS_HUMAN)
    store.append_event(stopped.job_id, what="checks passed; acceptance unresolved", evidence={}, rule="t",
                       judgment=export_module.Judgment.code)
    blocked = _running(store, goal="Waiting on a permission")
    _set_state(store, blocked, JobState.NEEDS_HUMAN)
    lines, cmap = _render(steward_home, tmp_path, 100, expanded=("done:",))
    on = {b[5] for b in _btns(cmap, "export")}
    assert on == {done.job_id, stopped.job_id} and not {run.job_id, blocked.job_id} & on
    b = next(b for b in _btns(cmap, "export") if b[5] == done.job_id)
    row = lines[int(b[1])]
    assert row[int(b[2]) - 1:int(b[3])] == "[ export ]"


def test_button_shows_exported_workspace(steward_home, tmp_path):
    from test_dash_polish import _running

    store = Store(steward_home)
    done = _running(store, goal="Finished one")
    _set_state(store, done, JobState.ACCEPTED)
    store.kv_set(done.job_id, "incorporated_commit", "abc1234def")
    store.kv_set(done.job_id, "exported_workspace", {"workspace": "w9", "name": "finished-one"})
    lines, cmap = _render(steward_home, tmp_path, 120, expanded=("done:",))
    assert not _btns(cmap, "export")
    assert any("exported → finished-one" in ln and done.job_id[:8] in ln for ln in lines)


# -- the export -------------------------------------------------------------------------------------------


def test_workspace_cwd_name_and_event(world):
    store, job, wt, proj = world
    runner = fake_runner()
    res = export_module.export_job(store, job.job_id, runner, name="my widget", projects_dir=proj)
    create = next(c["argv"] for c in runner.calls if c["argv"][:3] == ["herdr", "workspace", "create"])
    assert create[create.index("--cwd") + 1] == str(wt) and create[create.index("--label") + 1] == "my widget"
    assert [c["argv"][:3] for c in runner.calls if c["argv"][0] == "herdr"].count(["herdr", "workspace", "create"]) == 1
    assert not any(a[:2] in (["herdr", "workspace"],) and a[2] != "create" for a in (c["argv"] for c in runner.calls))
    run = next(c["argv"] for c in runner.calls if c["argv"][:3] == ["herdr", "pane", "run"])
    assert run[3] == "w9:p1" and "claude --model sonnet" in run[4] and f"cd {wt}" in run[4]
    ev = [e for e in store.events(job.job_id) if e.what == "exported"]
    assert len(ev) == 1 and ev[0].evidence["workspace"] == "w9" and ev[0].evidence["cwd"] == str(wt)
    assert store.get_job(job.job_id).state == JobState.ACCEPTED  # no state change
    assert export_module.exported(store, job.job_id) == "my widget"
    assert res.workspace == "w9"
    assert not any(a[0] == "git" and a[1] == "push" for a in (c["argv"] for c in runner.calls))


def test_default_name_is_short_name(world):
    store, job, _, proj = world
    runner = fake_runner()
    res = export_module.export_job(store, job.job_id, runner, projects_dir=proj)
    assert res.name and res.name != job.job_id[:8]


def test_bundle_contents_and_redaction(world):
    store, job, wt, proj = world
    export_module.export_job(store, job.job_id, fake_runner(), projects_dir=proj)
    d = wt / ".steward" / "export"
    handoff = (d / "HANDOFF.md").read_text()
    for needle in ("Ship the widget", "Finish line", "tests green", FINAL, "a.py | 2", "abc1234 feat: the widget",
                   "Open questions", "Efficiency / cost", "sess-1.jsonl", "steward.cli transcript"):
        assert needle in handoff, needle
    trans = (d / "TRANSCRIPT.md").read_text()
    assert "TOOL: Bash" in trans and "STEER" in trans and "use the small widget" in trans
    for f in d.iterdir():
        assert SECRET not in f.read_text(), f.name
    assert (d / "CONTRACT.json").is_file()


def test_open_questions_and_answers_in_bundle(world):
    store, job, wt, proj = world
    from steward import questions

    questions.save(store, job.job_id, [{"qid": "q1", "title": "Which widget?", "question": "big or small", "status": "answered",
                                        "answer": {"text": "small"}, "source": {}}])
    export_module.export_job(store, job.job_id, fake_runner(), projects_dir=proj)
    handoff = (wt / ".steward" / "export" / "HANDOFF.md").read_text()
    assert "Which widget?" in handoff and "answer: small" in handoff


def test_resume_when_session_and_claude_supports_it(world):
    store, job, _, proj = world
    runner = fake_runner(resume_help=True)
    res = export_module.export_job(store, job.job_id, runner, model="opus", projects_dir=proj)
    assert res.resumed and "--resume sess-1" in res.command and "--model opus" in res.command
    assert "read .steward/export/HANDOFF.md first" in res.command or ".steward/export/HANDOFF.md first" in res.command
    ev = next(e for e in store.events(job.job_id) if e.what == "exported")
    assert ev.evidence["resumed"] is True and ev.evidence["session_id"] == "sess-1"


@pytest.mark.parametrize("case", ["old-claude", "no-session", "no-resume-flag"])
def test_no_resume_paths(world, case):
    store, job, _, proj = world
    kwargs = {}
    if case == "old-claude":
        runner = fake_runner(resume_help=False)
    else:
        runner = fake_runner()
    if case == "no-session":
        store.kv_delete(job.job_id, "worker_session_id")
        for f in (proj / chat_module.project_slug(chat_module.worktree_path(store, job.job_id))).glob("*.jsonl"):
            f.unlink()
    if case == "no-resume-flag":
        kwargs["resume"] = False
    res = export_module.export_job(store, job.job_id, runner, projects_dir=proj, **kwargs)
    assert not res.resumed and "--resume" not in res.command
    assert "--append-system-prompt" in res.command and "Steward job" in res.command


def test_incorporated_job_uses_source_repo_unless_configured(world, monkeypatch):
    store, job, wt, proj = world
    store.kv_set(job.job_id, "incorporated_commit", "mergecommit1234")
    repo = export_module.Path(store.get_job(job.job_id).repo)
    res = export_module.export_job(store, job.job_id, fake_runner(), projects_dir=proj, dry_run=True)
    assert res.cwd == str(repo)
    monkeypatch.setenv(export_module.EXPORT_CWD_ENV, "worktree")
    assert export_module.export_job(store, job.job_id, fake_runner(), projects_dir=proj, dry_run=True).cwd == str(wt)


def test_dry_run_touches_nothing(world):
    store, job, wt, proj = world
    runner = fake_runner()
    export_module.export_job(store, job.job_id, runner, projects_dir=proj, dry_run=True)
    assert not (wt / ".steward").exists()
    assert not any(c["argv"][0] == "herdr" for c in runner.calls)
    assert not [e for e in store.events(job.job_id) if e.what == "exported"]


def test_herdr_failure_records_no_event(world):
    store, job, _, proj = world
    with pytest.raises(export_module.ExportError):
        export_module.export_job(store, job.job_id, fake_runner(fail_create=True), projects_dir=proj)
    assert not [e for e in store.events(job.job_id) if e.what == "exported"]
    assert export_module.exported(store, job.job_id) is None


def test_cli_export_parser():
    ns = cli._build_parser().parse_args(["export", "J", "--name", "n", "--model", "opus"])
    assert (ns.command, ns.job, ns.name, ns.model) == ("export", "J", "n", "opus")


# -- finished-job history: the pane's done group and `steward jobs --finished` ----------------------------------


def _finished(store, goal, state, days_ago):
    from datetime import datetime, timedelta, timezone

    from test_dash_polish import _running

    j = _running(store, goal=goal)
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?", (state.value, at, j.job_id))
    return j


@pytest.fixture
def history(steward_home):
    store = Store(steward_home)
    jobs = {
        "released": _finished(store, "Released recently", JobState.RELEASED, 1),
        "stopped": _finished(store, "Stopped a while ago", JobState.STOPPED, 3),
        "failed": _finished(store, "Failed lately", JobState.FAILED, 0.1),
        "old": _finished(store, "Released ages ago", JobState.RELEASED, 30),
        "running": _finished(store, "Still running", JobState.RUNNING, 0),
    }
    return store, jobs


def test_pane_done_group_lists_recent_history_newest_first(history, steward_home, tmp_path):
    store, jobs = history
    lines, cmap = _render(steward_home, tmp_path, 110)
    head = next(ln for ln in lines if "done (" in ln)
    assert "done (3)" in head and "tap to show" in head  # collapsed by default; the old one is outside the 7-day window
    assert not any(jobs["released"].job_id[:8] in ln for ln in lines)
    lines, cmap = _render(steward_home, tmp_path, 110, ("done:",))
    rows = [ln for ln in lines if any(jobs[k].job_id[:8] in ln for k in ("released", "stopped", "failed", "old"))]
    ids = [next(k for k in ("released", "stopped", "failed", "old") if jobs[k].job_id[:8] in r) for r in rows]
    assert ids == ["failed", "released", "stopped"]  # newest first; `old` is outside the window
    assert all("ago" in r and "[ export ]" in r for r in rows)
    assert {b[5] for b in _btns(cmap, "export")} == {jobs[k].job_id for k in ("released", "stopped", "failed")}
    assert not any(b[5] == jobs["running"].job_id for b in _btns(cmap, "export"))


def test_pane_window_is_configurable(history, steward_home, tmp_path, monkeypatch):
    store, jobs = history
    monkeypatch.setenv("STEWARD_DASH_RECENT_DAYS", "60")
    lines, _ = _render(steward_home, tmp_path, 110, ("done:",))
    assert any(jobs["old"].job_id[:8] in ln for ln in lines)
    monkeypatch.setenv("STEWARD_DASH_RECENT_DAYS", "0.5")
    lines, _ = _render(steward_home, tmp_path, 110, ("done:",))
    assert [k for k in ("released", "stopped", "failed", "old") if any(jobs[k].job_id[:8] in ln for ln in lines)] == ["failed"]


def test_pane_row_shows_exported_workspace(history, steward_home, tmp_path):
    store, jobs = history
    store.kv_set(jobs["stopped"].job_id, "exported_workspace", {"workspace": "w3", "name": "stopped-one"})
    lines, cmap = _render(steward_home, tmp_path, 120, ("done:",))
    row = next(ln for ln in lines if jobs["stopped"].job_id[:8] in ln)
    assert "exported → stopped-one" in row and "[ export ]" not in row
    assert jobs["stopped"].job_id not in {b[5] for b in _btns(cmap, "export")}


def test_phone_status_output_is_unchanged(history, capsys):
    store, jobs = history
    assert cli.main(["status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [s["job_id"] for s in out] == [jobs["running"].job_id]  # finished jobs stay off the phone list
    assert all("finished_at" not in s for s in out)
    assert cli.main(["status", "--json", "--recent", "7"]) == 0
    rec = {s["job_id"]: s for s in json.loads(capsys.readouterr().out)}
    assert set(rec) == {jobs[k].job_id for k in ("released", "stopped", "failed", "running")}
    assert "finished_at" in rec[jobs["failed"].job_id] and "finished_at" not in rec[jobs["running"].job_id]


def test_cli_jobs_finished_history(history, capsys):
    store, jobs = history
    store.kv_set(jobs["released"].job_id, "exported_workspace", {"workspace": "w3", "name": "rel"})
    assert cli.main(["jobs", "--finished", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["job_id"] for r in rows] == [jobs[k].job_id for k in ("failed", "released", "stopped", "old")]
    assert {r["job_id"]: r["exported"] for r in rows}[jobs["released"].job_id] == "rel"
    assert all(r["state"] and r["name"] and r["finished_at"] for r in rows)
    assert cli.main(["jobs", "--since", "7"]) == 0  # --since implies --finished
    text = capsys.readouterr().out
    assert jobs["old"].job_id[:8] not in text and jobs["failed"].job_id[:8] in text
    assert "exported → rel" in text and "not exported" in text and "RELEASED" in text and "STOPPED" in text
    assert cli.main(["jobs", "--json"]) == 0  # the default list is untouched: active jobs only
    assert [r["job_id"] for r in json.loads(capsys.readouterr().out)] == [jobs["running"].job_id]


def test_export_can_target_an_old_job_found_in_history(history):
    store, jobs = history
    old = jobs["old"]
    wt = chat_module.worktree_path(store, old.job_id)
    wt.mkdir(parents=True)
    runner = fake_runner()
    res = export_module.export_job(store, old.job_id, runner, projects_dir=wt / "none")
    assert res.cwd == str(wt) and store.get_job(old.job_id).state == JobState.RELEASED


@pytest.mark.parametrize("cols", [40, 60, 80])
def test_narrow_panes_keep_every_line_in_bounds_and_the_button_reachable(history, steward_home, tmp_path, cols):
    store, jobs = history
    lines, cmap = _render(steward_home, tmp_path, cols, ("done:",))
    start = next(i for i, ln in enumerate(lines) if "done (" in ln)  # (the HEALTH header's own width is not this feature's)
    assert all(len(ln) <= cols for ln in lines[start:])
    b = next(b for b in _btns(cmap, "export") if b[5] == jobs["failed"].job_id)
    assert lines[int(b[1])][int(b[2]) - 1:int(b[3])] == "[ export ]"
