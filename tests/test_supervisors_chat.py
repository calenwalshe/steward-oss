"""Supervisor rows, `steward chat` command building, shim session recording,
and the dashboard renderer's click map."""

from __future__ import annotations

import json
import pytest
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from steward.roles import role_label
from steward import cli
from steward import chat as chat_module
from steward.chat import ChatError, build_chat, ensure_trusted, open_chat, project_slug, worker_session_id
from steward.models import JobState, Judgment
from steward.shim import Shim
from steward.store import Store, resolve_home
from steward.supervisors import model_from_args, supervisor_rows

_REPO = Path(__file__).resolve().parents[1]
JOB_STATES = JobState


def _job(steward_home, state=JobState.RUNNING):
    store = Store()
    job = store.create_job("do the thing", "/tmp/somerepo")
    with store._conn:  # tests only: skip the transition graph
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))
    store.kv_set(job.job_id, "herdr_tab", "w1:t2")
    store.kv_set(job.job_id, "worker_model", "sonnet")
    return store, job


def test_model_from_args():
    assert model_from_args(["claude", "-p", "--model", "opus"]) == "opus"
    assert model_from_args(["--model=haiku"]) == "haiku"
    assert model_from_args(["claude"]) is None


def test_supervisor_rows_shape_and_counts(steward_home):
    store, job = _job(steward_home)
    for i in range(3):
        store.append_event(job.job_id, what="worker permission requested", evidence={"tool": "Bash"}, rule="r", judgment=Judgment.code)
    store.append_event(job.job_id, what="stall diagnosis produced", evidence={"verdict": "needs_human"}, rule="r", judgment=Judgment.code)
    store.append_event(job.job_id, what="jev_unavailable", evidence={"code": "disabled"}, rule="r", judgment=Judgment.code)
    rows = supervisor_rows(store, job.job_id)
    assert [r["role"] for r in rows] == ["worker", "astra", "sol", "efficiency", "jev"]
    worker, astra, sol, efficiency, jev = rows  # noqa: F841
    assert worker["model"] == "sonnet" and worker["stats"]["actions"] == 3
    assert worker["display"].startswith("{SPIN} ") and "3 approvals" in worker["display"]
    assert "$" not in worker["display"]  # spend hidden when zero
    assert astra["model"] == "opus" and astra["stats"]["reviews"] == 0 and "~45m" in astra["display"]
    assert sol["stats"]["diagnoses"] == 1 and sol["display"] == "[on] standby · 1 diagnoses"
    assert jev["status"] == "shadow" and jev["display"].startswith("[shadow▸] 10s · 0 checks")


def test_worker_model_unknown(steward_home):
    store = Store()
    job = store.create_job("g", "/tmp/r")
    assert supervisor_rows(store, job.job_id)[0]["model"] in ("?", "sonnet", "opus", "haiku")


def test_supervisors_cli_json(steward_home, capsys):
    store, job = _job(steward_home)
    assert cli.main(["supervisors", job.job_id, "--json"]) == 0
    assert [r["role"] for r in json.loads(capsys.readouterr().out)] == ["worker", "astra", "sol", "efficiency", "jev"]
    assert cli.main(["supervisors", "nope"]) == 1


def test_project_slug():
    p = "/home/user/.local/state/steward/worktrees/abc"
    assert project_slug(p) == "-home-user--local-state-steward-worktrees-abc"


def test_session_id_kv_wins_then_jsonl_fallback(steward_home, tmp_path):
    store, job = _job(steward_home)
    projects = tmp_path / "projects"
    d = projects / project_slug(store.home / "worktrees" / job.job_id)
    d.mkdir(parents=True)
    old, new = d / "aaaa.jsonl", d / "bbbb.jsonl"
    old.write_text("{}")
    new.write_text("{}")
    os.utime(old, (time.time() - 100, time.time() - 100))
    assert worker_session_id(store, job.job_id, projects_dir=projects) == "bbbb"
    store.kv_set(job.job_id, "worker_session_id", "recorded-id")
    assert worker_session_id(store, job.job_id, projects_dir=projects) == "recorded-id"


def test_worker_chat_forks_session(steward_home):
    store, job = _job(steward_home)
    store.kv_set(job.job_id, "worker_session_id", "sess-1")
    plan = build_chat(store, job.job_id, "worker")
    wt = store.home / "worktrees" / job.job_id
    assert plan.command.startswith(
        f"cd {wt} && env -u ANTHROPIC_API_KEY claude --resume sess-1 --fork-session --model sonnet '"
    )
    assert "COPY of your session" in plan.command  # opens with a status question, not a blank prompt
    assert plan.cwd == str(wt)


def test_worker_chat_without_session_errors(steward_home, monkeypatch, tmp_path):
    store, job = _job(steward_home)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    try:
        build_chat(store, job.job_id, "worker")
        raise AssertionError("expected ChatError")
    except ChatError:
        pass


def test_astra_and_sol_chat_write_primer(steward_home):
    store, job = _job(steward_home)
    for role, who in (("advisor", "the Advisor"), ("doctor", "the Doctor")):
        plan = build_chat(store, job.job_id, role)
        assert plan.cwd == "/tmp/somerepo"
        assert f"claude --model {'opus' if role == 'advisor' else 'sonnet'} --append-system-prompt" in plan.command
        text = Path(plan.primer_path).read_text()
        assert f"You are {who}" in text and "do the thing" in text and "advisory only" in text
        assert len(text) < 4000
        assert plan.primer_path in plan.command


def test_jev_has_no_chat(steward_home, capsys):
    store, job = _job(steward_home)
    try:
        build_chat(store, job.job_id, "jev")
        raise AssertionError("expected ChatError")
    except ChatError:
        pass
    assert cli.main(["chat", job.job_id, "jev", "--dry-run"]) == 1


class _FakeExec:
    def __init__(self, panes=None):
        self.calls = []
        self.panes = panes or []

    def run(self, argv, cwd, **kw):
        self.calls.append(argv)
        if argv[:3] == ["herdr", "pane", "list"]:
            out = json.dumps({"result": {"panes": self.panes}})
        elif argv[:3] == ["herdr", "pane", "split"]:
            out = json.dumps({"result": {"pane": {"pane_id": "w1:pNEW"}}})
        else:
            out = "{}"
        return SimpleNamespace(exit_code=0, stdout=out, stderr="")


def _panes(job, *, watchers=True, runner=True, chat=None):
    id8 = job.job_id[:8]
    out = []
    if runner:
        out.append({"pane_id": "w1:p1", "tab_id": "w1:t5", "label": f"runner {id8}"})
    if watchers:
        out.append({"pane_id": "w1:p2", "tab_id": "w1:t5", "label": f"watchers {id8}"})
    if chat:
        out.append({"pane_id": "w1:p3", "tab_id": "w1:t5", "label": chat})
    return out


def test_open_chat_splits_down_of_watchers_in_job_tab(steward_home, tmp_path, monkeypatch):
    monkeypatch.setattr(chat_module, "CLAUDE_CONFIG", tmp_path / "claude.json")
    (tmp_path / "claude.json").write_text("{}")
    store, job = _job(steward_home)
    store.kv_set(job.job_id, "worker_session_id", "s")
    plan = build_chat(store, job.job_id, "worker")
    ex = _FakeExec(_panes(job))
    label, pane = open_chat(store, job.job_id, plan, ex)
    assert (label, pane) == (f"worker chat {job.job_id[:8]}", "w1:pNEW")
    split = next(c for c in ex.calls if c[:3] == ["herdr", "pane", "split"])
    assert split[split.index("--pane") + 1] == "w1:p2"
    assert split[split.index("--direction") + 1] == "down"
    assert "--no-focus" not in split
    assert ["herdr", "pane", "rename", "w1:pNEW", label] in ex.calls
    assert ["herdr", "pane", "run", "w1:pNEW", plan.command] in ex.calls
    assert ex.calls[-1] == ["herdr", "tab", "focus", "w1:t5"]
    # the chat's folder is pre-trusted so it opens on the conversation, not a prompt
    assert json.loads((tmp_path / "claude.json").read_text())["projects"][plan.cwd]["hasTrustDialogAccepted"] is True
    # dry run touches nothing
    ex2 = _FakeExec()
    assert open_chat(store, job.job_id, plan, ex2, dry_run=True) == (label, None)
    assert ex2.calls == []


def test_open_chat_reuses_existing_labelled_pane(steward_home):
    store, job = _job(steward_home)
    store.kv_set(job.job_id, "worker_session_id", "s")
    plan = build_chat(store, job.job_id, "worker")
    ex = _FakeExec(_panes(job, chat=f"worker chat {job.job_id[:8]}"))
    assert open_chat(store, job.job_id, plan, ex) == (f"worker chat {job.job_id[:8]}", None)
    assert ex.calls[-1] == ["herdr", "tab", "focus", "w1:t5"]
    assert not any(c[:3] == ["herdr", "pane", "split"] for c in ex.calls)


def test_open_chat_falls_back_to_runner_pane_split_right(steward_home, tmp_path, monkeypatch):
    monkeypatch.setattr(chat_module, "CLAUDE_CONFIG", tmp_path / "claude.json")
    store, job = _job(steward_home)
    store.kv_set(job.job_id, "worker_session_id", "s")
    plan = build_chat(store, job.job_id, "worker")
    ex = _FakeExec(_panes(job, watchers=False))
    open_chat(store, job.job_id, plan, ex)
    split = next(c for c in ex.calls if c[:3] == ["herdr", "pane", "split"])
    assert split[split.index("--pane") + 1] == "w1:p1"
    assert split[split.index("--direction") + 1] == "right"
    assert ex.calls[-1] == ["herdr", "tab", "focus", "w1:t5"]


def test_open_chat_errors_without_any_job_pane(steward_home):
    store, job = _job(steward_home)
    store.kv_set(job.job_id, "worker_session_id", "s")
    plan = build_chat(store, job.job_id, "worker")
    with pytest.raises(ChatError):
        open_chat(store, job.job_id, plan, _FakeExec([]))


def test_ensure_trusted_leaves_unparseable_config_alone(tmp_path):
    cfg = tmp_path / "claude.json"
    cfg.write_text("not json")
    ensure_trusted("/x", cfg)
    assert cfg.read_text() == "not json"


def test_shim_records_session_id_from_init_event():
    seen = []
    shim = Shim("j", "p", api=None, mailbox=None, spawn=None, agent_argv=[], on_session=seen.append)
    shim._handle_agent_event({"type": "system", "subtype": "init", "session_id": "abc-123"})
    shim._handle_agent_event({"type": "system", "subtype": "other", "session_id": "zzz"})
    assert seen == ["abc-123"]


def test_renderer_map_has_chat_action_per_supervisor(steward_home, tmp_path):
    store, job = _job(steward_home)
    store.kv_set(job.job_id, "herdr_workspace", "w1")
    store.kv_set(job.job_id, "herdr_pane", "w1:p7")
    map_path = tmp_path / "map"
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_DASH_COLS": "60",
           "STEWARD_DASH_MAP": str(map_path), "PYTHONPATH": str(_REPO)}
    res = subprocess.run([sys.executable, str(_REPO / "deploy/cockpit/steward-dash-jobs.py")],
                         capture_output=True, text=True, env=env, cwd=_REPO)
    import re
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln).replace("{SPIN}", "x") for ln in res.stdout.splitlines()]
    assert all(len(ln) <= 60 for ln in lines), lines
    assert any("├ worker" in ln for ln in lines) and any("└ observer" in ln for ln in lines)
    actions = map_path.read_text().splitlines()
    chats = [a for a in actions if a.startswith("chatbtn ")]  # the [ chat ] buttons (no row taps any more)
    assert [a.split()[-1] for a in chats] == ["worker", "astra", "sol"]
    # each chat row index points at the matching supervisor line
    for a in chats:
        idx = int(a.split()[1])
        assert role_label(a.split()[-1]) in lines[idx]
    assert "key 3 w1:t2" in actions
