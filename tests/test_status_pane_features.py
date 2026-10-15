"""Quick task 20261001: one clickable style derived from the click map, `[ explain ]` + `steward explain`, and
`[ unblock now ]` + `steward unblock` (the unblock agent). Fakes only: no model, herdr or network."""

from __future__ import annotations

import json
import time

import pytest
from test_dash_polish import _running, world  # noqa: F401  (world is a fixture)
from test_incorporate import scratch  # noqa: F401  (fixture)
from test_mcp_tools import live_tools_server  # noqa: F401  (fixture)

from steward import approvals as approvals_module
from steward import chat as chat_module
from steward import cli, dashact, dashrender, explain, jobview, owner, transcript, unblock_agent, watchers
from steward import efficiency_watch
from steward.control_pane import ControlPane
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store


def _set_state(store, job, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))


def _pane(steward_home, tmp_path, cols, expanded=()):
    path = tmp_path / f"state{cols}"
    dashact.save(dashact.state_path(str(path)), {"expanded": {k: True for k in expanded}, "armed": {}})
    return dashrender.Pane(cols, 0, dashact.state_path(str(path)), store=Store(steward_home)).render(refresh=True)


@pytest.fixture
def job(steward_home):
    store = Store(steward_home)
    return store, _running(store, goal="Ship the widget. FINISH LINE: tests green.")


# =====================================================================================================
# (1) one clickable style, derived from the click map
# =====================================================================================================


def _assert_style_matches_cmap(out, cmap):
    """Every cmap button/switch is styled; no non-button cell uses the button style; no underline anywhere."""
    spans, tappable = dashrender.click_regions(cmap)
    for i, line in enumerate(out):
        cells = dashrender.styled_cells(line)
        plain = dashrender.ANSI.sub("", line)
        buttons = {c for a, b in spans.get(i, []) for c in range(a, b + 1)}
        for c in buttons:
            assert cells.get(c) == "button", (i, c, plain)
        for c, kind in cells.items():
            assert kind == "button" and c in buttons, (i, c, kind, plain)
    joined = "".join(out)
    assert "\033[36m" not in joined and "\033[4m" not in joined and "\033[24m" not in joined
    assert not [m for m in dashrender.ANSI.findall(joined) if m.startswith("\033[4;") or ";4;" in m or m.endswith(";4m")]


@pytest.mark.parametrize("cols", [60, 88, 120])
def test_every_button_is_styled_and_nothing_else(world, steward_home, tmp_path, cols):
    store, j, done = world
    from steward.supervisors import worker_model  # noqa: F401
    out, cmap = _pane(steward_home, tmp_path, cols)
    assert cmap and any(c.startswith("btn") for c in cmap) and any(c.startswith("num") for c in cmap)
    _assert_style_matches_cmap(out, cmap)


@pytest.mark.parametrize("expanded", [(), ("done:",), ("fold:",), ("explain:",)])
def test_style_matches_cmap_in_every_mode(world, steward_home, tmp_path, expanded):
    store, j, done = world
    keys = tuple(k + j.job_id if k.endswith(":") and k != "done:" else k for k in expanded)
    out, cmap = _pane(steward_home, tmp_path, 88, keys)
    _assert_style_matches_cmap(out, cmap)


def test_buttons_are_bold_bright_cyan_and_rows_unstyled(job, steward_home, tmp_path):
    out, cmap = _pane(steward_home, tmp_path, 100)
    head = next(ln for ln in out if "[ steer ]" in ln)
    assert f"{dashrender.CLICK_BUTTON}[ steer ]" in head and f"{dashrender.CLICK_BUTTON}[ explain ]" in head
    assert dashrender.CLICK_BUTTON == "\033[1;96m"
    assert not hasattr(dashrender, "CLICK_LINE")


def test_explain_on_header_when_wide_else_own_row(job, steward_home, tmp_path):
    wide, _ = _pane(steward_home, tmp_path, 100)
    assert "[ steer ]  [ explain ]" in dashrender.ANSI.sub("", next(ln for ln in wide if "[ steer ]" in ln))
    assert sum("[ explain ]" in ln for ln in wide) == 1
    narrow, _ = _pane(steward_home, tmp_path, 40)
    head = next(ln for ln in narrow if "[ steer ]" in ln)
    assert "[ explain ]" not in head
    row = next(ln for ln in narrow if "[ explain ]" in ln)
    assert dashrender.ANSI.sub("", row).startswith("    [ explain ]")


def test_style_pass_keeps_surrounding_styles():
    out = ["\033[2m  dim text [ go ] more\033[0m", "plain", "  \033[33mwarn\033[0m line"]
    cmap = ["btn 0 9 14 go J -", "row 2 focus t"]
    got = dashrender.style_clickable(out, cmap)
    assert dashrender.ANSI.sub("", got[0]) == dashrender.ANSI.sub("", out[0]) and got[1] == "plain"
    assert dashrender.styled_cells(got[0]) == {c: "button" for c in range(9, 15)}
    assert "\033[2m" in got[0].split(dashrender.CLICK_BUTTON_OFF, 1)[1]  # dim resumes after the button
    assert got[2] == out[2]  # tappable rows get no styling
    assert dashrender.style_clickable(out, []) == out


def test_unclicked_lines_get_no_style(job, steward_home, tmp_path):
    out, cmap = _pane(steward_home, tmp_path, 88)
    spans, tappable = dashrender.click_regions(cmap)
    for i, line in enumerate(out):
        if i not in tappable and i not in spans:
            assert dashrender.styled_cells(line) == {}


# =====================================================================================================
# (2) explain
# =====================================================================================================


def _eta(now):
    return {"source": "units", "units_done": 3, "units_total": 7, "units_exact": True, "unit_label": "plan 04",
            "at": now, "elapsed_min": 30.0,
            "shown": {"p10_end": now + 3000, "p50_end": now + 3600, "p90_end": now + 5400, "source": "units",
                      "p50_min_at_set": 60.0}}


def _seed(store, j, now, monkeypatch):
    monkeypatch.setattr(transcript, "live_line", lambda *a, **k: {
        "now": "running Bash: pytest -q", "running_tool": "Bash: pytest -q",
        "since": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 190)), "last_said": None, "last_activity_ts": None})
    store.kv_set(j.job_id, efficiency_watch.STATE_KEY, {"mode": "shadow", "eta": _eta(now), "findings": []})
    store.append_event(j.job_id, what="verification recorded", evidence={}, rule="t", judgment=Judgment.code)
    (store.home / "worktrees" / j.job_id).mkdir(parents=True)


def _git_log(now):
    return FakeRunner(lambda argv, cwd: CommandResult(
        argv, str(cwd), 0, f"{int(now - 600)}\tabc1234\tfeat(04-01): add widget\n{int(now - 7200)}\tdef5678\ttest: red\n", "", 0.0))


def test_explain_text_has_the_standard_sections(job, monkeypatch):
    store, j = job
    now = time.time()
    _seed(store, j, now, monkeypatch)
    text = explain.text(store, j.job_id, now=now, runner=_git_log(now))
    lines = text.splitlines()
    assert lines[0].startswith(f"Explain {j.job_id[:8]}") and lines[0].endswith("RUNNING")
    assert "Now: The worker is running Bash: pytest -q for 3m." in text
    assert "Progress: units 3/7 · current unit plan 04 · eta" in text
    assert "Last milestones:" in text and "commit abc1234 feat(04-01): add widget" in text
    assert "verification recorded" in text
    assert "Blocking / waiting on you: nothing" in text
    assert "Spend: $0.00 so far" in text and "Health: ok" in text
    assert [ln.split(":")[0] for ln in lines if ln and not ln.startswith(" ")][1:] == [
        "Now", "Progress", "Last milestones", "Blocking / waiting on you", "Spend", "Health"]


def test_explain_is_deterministic_and_last_three_milestones_only(job, monkeypatch):
    store, j = job
    now = time.time()
    _seed(store, j, now, monkeypatch)
    runner = _git_log(now)
    a, b = explain.text(store, j.job_id, now=now, runner=runner), explain.text(store, j.job_id, now=now, runner=runner)
    assert a == b
    assert len(explain.data(store, j.job_id, now=now, runner=runner)["milestones"]) == explain.MILESTONES == 3


def test_explain_names_what_blocks_and_flags_health(job, monkeypatch):
    store, j = job
    now = time.time()
    _seed(store, j, now, monkeypatch)
    approvals_module.open_approval(store, j.job_id, "worker permission: Bash: rm -rf build", kind="permission")
    store.kv_set(j.job_id, efficiency_watch.STATE_KEY, {"mode": "shadow", "eta": _eta(now), "findings": [
        {"id": "f1", "kind": "idle_helper", "detail": "a helper idles"}]})
    d = explain.data(store, j.job_id, now=now, runner=_git_log(now))
    assert any(b.startswith("approval ") and "rm -rf build" in b for b in d["blocking"]) and d["blocking"] != ["nothing"]
    assert "Blocking / waiting on you:" in explain.render(d) and "  - approval" in explain.render(d)
    _set_state(store, j, JobState.PAUSED)
    assert "the job is paused and needs a resume" in explain.data(store, j.job_id, now=now)["blocking"]


def test_explain_without_data_is_still_standard(job):
    store, j = job
    d = explain.data(store, j.job_id, runner=FakeRunner())
    assert d["progress"].startswith("no progress data yet") and d["milestones"] == [] and "none yet" in explain.render(d)
    with pytest.raises(KeyError):
        explain.data(store, "nope")


def test_cli_explain_prints_same_text_and_json(job, capsys, monkeypatch):
    store, j = job
    monkeypatch.setattr(explain, "_milestones", lambda *a, **k: [])
    assert cli.main(["explain", j.job_id[:8]]) == 0
    text = capsys.readouterr().out
    assert text.startswith(f"Explain {j.job_id[:8]}") and "Health:" in text
    assert cli.main(["explain", j.job_id, "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["job_id"] == j.job_id and set(d) >= {"now", "progress", "milestones", "blocking", "spend", "health"}
    assert cli.main(["explain", "nope"]) == 1


def test_explain_button_on_live_waiting_rows_folded_or_not(world, steward_home, tmp_path):
    store, j, done = world
    for keys in ((), (dashact.FOLD_PREFIX + j.job_id,)):
        out, cmap = _pane(steward_home, tmp_path, 88, keys)
        row = next(i for i, ln in enumerate(out) if "[ explain ]" in dashrender.ANSI.sub("", ln))
        assert any(c.startswith(f"btn {row} ") and c.endswith(f" explain {j.job_id} -") for c in cmap)
    for d in done:  # finished jobs have no explain button
        assert not any(c.endswith(f" explain {d.job_id} -") for c in cmap)
    for state in (JobState.PAUSED, JobState.NEEDS_HUMAN):
        _set_state(store, j, state)
        out, cmap = _pane(steward_home, tmp_path, 88)
        assert any(c.endswith(f" explain {j.job_id} -") for c in cmap)


def test_explain_panel_opens_under_the_job_with_more_button(job, steward_home, tmp_path, monkeypatch):
    store, j = job
    monkeypatch.setattr(explain, "_milestones", lambda *a, **k: ["2m ago: commit abc1234 feat: x"])
    out, cmap = _pane(steward_home, tmp_path, 88, (dashact.EXPLAIN_PREFIX + j.job_id,))
    plain = [dashrender.ANSI.sub("", ln) for ln in out]
    first = next(i for i, ln in enumerate(plain) if "Explain " in ln)
    assert any("[ explain ]" in ln for ln in plain[:first])  # on the header (or its own row) above the panel
    assert any("Last milestones" in ln for ln in plain) and any("commit abc1234" in ln for ln in plain)
    more = next(i for i, ln in enumerate(plain) if "[ more ]" in ln)
    assert any(c.startswith(f"btn {more} ") and c.endswith(f" explain_more {j.job_id} -") for c in cmap)
    closed, _ = _pane(steward_home, tmp_path, 88)
    assert not any("Last milestones" in ln for ln in closed)


def test_explain_actions_argv():
    assert dashact.argv_for("explain", "J") == ["python3", "-m", "steward.dashact", "expand", "explain:J"]
    assert dashact.argv_for("explain_more", "J")[-3:] == ["explain", "J", "--chat"]


def test_more_opens_the_job_chat_primed_with_the_explanation(job, monkeypatch):
    store, j = job
    monkeypatch.setattr(explain, "_milestones", lambda *a, **k: [])
    plan = chat_module.build_chat(store, j.job_id, "job", explain=True, runner=FakeRunner())
    primer = open(plan.primer_path).read()
    assert primer.startswith("== EXPLAIN (what the owner just read) ==") and f"Explain {j.job_id[:8]}" in primer
    plain = chat_module.build_chat(store, j.job_id, "job")
    assert "== EXPLAIN" not in open(plain.primer_path).read()
    ns = cli._build_parser().parse_args(["explain", "J", "--chat"])
    assert ns.chat and cli._build_parser().parse_args(["chat", "J", "job", "--explain"]).explain


def test_mcp_explain_job_tool_is_a_read_tool_returning_the_same_text(steward_home, live_tools_server):
    from test_mcp_tools import _call, _contracted_job, _tools_list

    from steward import mcp_auth

    base_url, store, _ = live_tools_server
    job_id = _contracted_job(store)
    token, _ = mcp_auth.create_token(list(mcp_auth.SCOPES), home=steward_home)
    tool = next(t for t in _tools_list(base_url, token) if t["name"] == "explain_job")
    assert tool["annotations"]["readOnlyHint"] is True and tool["required_scope"] == "runner:read" if "required_scope" in tool else True
    body = _call(base_url, token, "explain_job", {"job_id": job_id})["result"]["structuredContent"]
    assert body["text"].startswith(f"Explain {job_id[:8]}") and body["explanation"]["job_id"] == job_id
    assert set(body["explanation"]) >= {"now", "progress", "milestones", "blocking", "spend", "health"}
    missing = _call(base_url, token, "explain_job", {"job_id": "nope"})["result"]["structuredContent"]
    assert missing["explanation"] is None


# =====================================================================================================
# (3) unblock
# =====================================================================================================


def _argv(job_id, *rest):
    return [rest[0], job_id, *rest[1:]]


@pytest.mark.parametrize("argv,ok", [
    (["pause", "J"], True), (["resume", "J"], True), (["runner-restart", "J"], True), (["fix-merge", "J"], True),
    (["steer", "J", "do x", "--urgent", "--verb", "nudge"], True), (["steer", "J", "do x"], True),
    (["approve", "J", "a1"], True), (["deny", "J", "a1"], True), (["send-back", "J", "fix it"], True),
    (["answer", "J", "Q1", "--option", "a"], True), (["answer", "J", "--accept-recommended"], True),
    (["launch", "J"], True), (["launch", "J", "--workspace", "w1"], True), (["status", "J"], True), (["events", "J", "--json"], True),
    (["stop", "J"], False), (["release", "J"], False), (["accept", "J"], False), (["incorporate", "J"], False),
    (["new", "goal"], False), (["watcher", "add", "J"], False), (["pause", "OTHER"], False), (["pause"], False),
    (["steer", "OTHER", "x"], False), (["steer", "J", "x", "--origin", "dash"], False), (["steer", "J", "x", "--bogus"], False),
    (["steer", "J", "x", "--verb", "stop"], False), (["pause", "J", "extra"], False), (["approve", "J"], False),
    (["answer", "J", "Q1", "--other", "free text"], False), (["answer", "J", "--then-accept"], False),
    (["answer", "J", "--write-source"], False), (["launch", "J", "--keep-pane"], False), ([], False),
])
def test_allowed_command_rules(argv, ok):
    argv = [("J" if a == "J" else a) for a in argv]
    assert (unblock_agent.check_command("J", argv) is None) is ok, argv


def _cmd(op, job_id, n, origin=unblock_agent.UNBLOCK_ORIGIN, **args):
    return {"request_id": f"r{n}", "op": op, "args": {"job_id": job_id, **args}, "origin": origin}


def _control(store, ran):
    def ok(cmd):
        ran.append(cmd["op"])
        return {"accepted": True, "detail": "done"}

    ops = ("pause", "resume", "runner_restart", "approve", "deny", "accept", "stop", "incorporate", "release", "launch", "steer", "answer")
    return ControlPane(store, {op: ok for op in ops}, print_fn=lambda s: None)


def test_control_pane_guard_needs_an_active_run_for_this_job(job):
    store, j = job
    other = _running(store, goal="another job")
    ran = []
    pane = _control(store, ran)
    assert "no unblock run is active" in pane.execute(_cmd("pause", j.job_id, 1))["detail"]
    unblock_agent.acquire(store, j.job_id, "gpt-6-astra", state="running")
    assert pane.execute(_cmd("pause", j.job_id, 2))["accepted"] and ran == ["pause"]
    assert not pane.execute(_cmd("pause", other.job_id, 3))["accepted"]  # another job: refused
    assert pane.execute({**_cmd("pause", other.job_id, 4), "origin": "dash"})["accepted"]  # humans unaffected


@pytest.mark.parametrize("op", ["accept", "stop", "incorporate", "release"])
def test_control_pane_refuses_irreversible_ops_from_unblock_agent(job, op):
    store, j = job
    unblock_agent.acquire(store, j.job_id, "gpt-6-astra", state="running")
    ran = []
    res = _control(store, ran).execute(_cmd(op, j.job_id, 5))
    assert not res["accepted"] and ran == [] and owner.poss() in res["detail"]


def test_floor_and_acceptance_approvals_are_refused(job):
    store, j = job
    unblock_agent.acquire(store, j.job_id, "gpt-6-astra", state="running")
    floor = approvals_module.open_approval(store, j.job_id, "Bash: git push origin main", kind="permission")
    store.kv_set(j.job_id, f"hold:{floor['id']}", {"tool": "Bash", "input": {"command": "git push origin main"}, "reason": "git push"})
    easy = approvals_module.open_approval(store, j.job_id, "Bash: ls", kind="permission")
    store.kv_set(j.job_id, f"hold:{easy['id']}", {"tool": "Bash", "input": {"command": "ls"}, "reason": ""})
    acc = approvals_module.open_approval(store, j.job_id, approvals_module.ACCEPTANCE_QUESTION, kind="acceptance")
    ran = []
    pane = _control(store, ran)
    assert not pane.execute(_cmd("approve", j.job_id, 6, approval_id=floor["id"]))["accepted"]
    assert pane.execute(_cmd("approve", j.job_id, 7, approval_id=easy["id"]))["accepted"]
    assert not pane.execute(_cmd("approve", j.job_id, 8, approval_id=acc["id"]))["accepted"]
    assert not pane.execute(_cmd("deny", j.job_id, 9, approval_id=acc["id"]))["accepted"]
    assert ran == ["approve"]


def test_answers_only_on_the_recorded_default(job, monkeypatch):
    from steward import questions

    store, j = job
    unblock_agent.acquire(store, j.job_id, "gpt-6-astra", state="running")
    monkeypatch.setattr(questions, "load", lambda s, jid: [{"qid": "Q1", "recommended": ["a"], "status": "open"}])
    ran = []
    pane = _control(store, ran)

    def ans(n, **item):
        return pane.execute(_cmd("answer", j.job_id, n, answers=[{"qid": "Q1", **item}]))["accepted"]

    assert ans(10, option="a") and ans(11, option="recommended")
    assert not ans(12, option="b") and not ans(13, other="my own idea")
    assert pane.execute(_cmd("answer", j.job_id, 14, accept_recommended=True, answers=[]))["accepted"]
    assert not pane.execute(_cmd("answer", j.job_id, 15, accept_recommended=True, then_accept=True, answers=[]))["accepted"]
    assert len(ran) == 3


def test_one_run_per_job_and_stale_runs_expire(job):
    store, j = job
    unblock_agent.acquire(store, j.job_id, "gpt-6-astra")
    with pytest.raises(unblock_agent.Busy):
        unblock_agent.acquire(store, j.job_id, "fable")
    unblock_agent.acquire(store, j.job_id, "gpt-6-astra", state="running")  # the pane's run takes over the launcher's slot
    with pytest.raises(unblock_agent.Busy):
        unblock_agent.acquire(store, j.job_id, "gpt-6-astra", state="running")
    unblock_agent.release(store, j.job_id)
    unblock_agent.acquire(store, j.job_id, "gpt-6-astra")  # free again
    unblock_agent.release(store, j.job_id)
    store.kv_set(j.job_id, unblock_agent.RUN_KEY, {"state": "running", "model": "x", "started_at": time.time() - 3 * 3600})
    assert unblock_agent.active_run(store, j.job_id) is None


class FakeModel:
    """Stands in for `codex exec` / `claude -p`: records argv, answers per binary."""

    def __init__(self, plan_by_bin, fail=()):
        self.calls, self.plans, self.fail = [], plan_by_bin, set(fail)

    def __call__(self, argv, stdin, cwd, timeout):
        binary = "codex" if "codex" in argv else "claude"
        self.calls.append({"bin": binary, "argv": argv, "stdin": stdin, "cwd": cwd})
        if binary in self.fail:
            return 1, "", f"{binary} not available"
        text = self.plans[binary] if isinstance(self.plans[binary], str) else json.dumps(self.plans[binary])
        if binary == "codex":
            open(argv[argv.index("-o") + 1], "w").write(text)
            return 0, "", ""
        return 0, json.dumps({"result": text}), ""


class FakeCmds:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd, env):
        self.calls.append({"argv": argv, "env": env})
        return 0, "ok", ""


def _plan(job_id, extra=()):
    return {"report": "The worker looped on a flaky test.\nI interrupted it and told it to skip that test.",
            "actions": [["steer", job_id, "skip the flaky test", "--urgent", "--verb", "nudge"], ["runner-restart", job_id], *extra]}


def test_default_run_uses_codex_astra_with_full_context_and_executes_the_plan(job, monkeypatch, tmp_path):
    store, j = job
    monkeypatch.setattr(explain, "_milestones", lambda *a, **k: [])
    model, cmds = FakeModel({"codex": _plan(j.job_id, [["stop", j.job_id]])}), FakeCmds()
    res = unblock_agent.run(store, j.job_id, model_run=model, cmd_run=cmds, runner=FakeRunner())
    assert [c["bin"] for c in model.calls] == ["codex"]
    argv = model.calls[0]["argv"]
    assert argv[argv.index("-m") + 1] == "gpt-6-astra" and argv[argv.index("-s") + 1] == "read-only" and "exec" in argv
    prompt = model.calls[0]["stdin"]
    for needle in ("UNBLOCK AGENT", "== EXPLAIN", "SITUATION BRIEFING", "Worker's last turns", "Pending approvals", "Recent events",
                   "Diff stat", "Merge state:", "Efficiency / ETA:", "Questions waiting for"):
        assert needle in prompt, needle
    # the two allowed actions ran with the forced origin; the third (stop) never reached the CLI
    assert cmds.calls[0]["argv"][-6:] == ["steer", j.job_id, "skip the flaky test", "--urgent", "--verb", "nudge"]
    assert len(cmds.calls) == 2 and all(c["env"]["STEWARD_FORCED_ORIGIN"] == "unblock-agent" for c in cmds.calls)
    assert [a["ok"] for a in res["actions"]] == [True, True, False] and "refused" in res["actions"][2]["detail"]
    lines = res["report"].splitlines()
    assert 2 <= len(lines) <= 5 and lines[-1].startswith("Did: steer ok; runner-restart ok; stop FAILED")
    assert res["model"] == "gpt-6-astra" and res["ok"]


def test_report_is_the_note_and_in_the_feed_and_lock_is_released(job, monkeypatch):
    store, j = job
    monkeypatch.setattr(explain, "_milestones", lambda *a, **k: [])
    unblock_agent.run(store, j.job_id, model_run=FakeModel({"codex": _plan(j.job_id)}), cmd_run=FakeCmds(), runner=FakeRunner())
    note = unblock_agent.note(store, j.job_id)
    assert note and "looped on a flaky test" in note["text"] and note["model"] == "gpt-6-astra"
    assert unblock_agent.note(store, j.job_id, now=time.time() + 7200) is None  # stale notes drop off
    feed = [t for _, _, t in watchers.collect(store.events(j.job_id))]
    assert any(t.startswith("unblock-agent (gpt-6-astra):") and "flaky test" in t for t in feed)
    assert any(t.startswith("unblock-agent steer --urgent") for t in feed)
    assert unblock_agent.active_run(store, j.job_id) is None
    unblock_agent.acquire(store, j.job_id, "gpt-6-astra")  # the slot is free again


def test_report_is_capped_at_five_lines(job, monkeypatch):
    store, j = job
    monkeypatch.setattr(explain, "_milestones", lambda *a, **k: [])
    long = {"report": "\n".join(f"line {i}" for i in range(12)), "actions": []}
    res = unblock_agent.run(store, j.job_id, model_run=FakeModel({"codex": long}), cmd_run=FakeCmds(), runner=FakeRunner())
    assert len(res["report"].splitlines()) == 5 and res["report"].splitlines()[-1] == "Did: no action taken"


def test_falls_back_to_fable_when_codex_is_unavailable(job, monkeypatch):
    store, j = job
    monkeypatch.setattr(explain, "_milestones", lambda *a, **k: [])
    model = FakeModel({"codex": "", "claude": f"here you go: {json.dumps(_plan(j.job_id))}"}, fail=("codex",))
    res = unblock_agent.run(store, j.job_id, model_run=model, cmd_run=FakeCmds(), runner=FakeRunner())
    assert [c["bin"] for c in model.calls] == ["codex", "claude"]
    argv = model.calls[1]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1" and argv[:3] == ["env", "-u", "ANTHROPIC_API_KEY"] and "-p" in argv
    assert res["model"] == "claude-fable-5-1" and "fell back to claude-fable-5-1" in res["report"]


def test_explicit_model_does_not_fall_back_and_a_dead_model_changes_nothing(job, monkeypatch):
    store, j = job
    monkeypatch.setattr(explain, "_milestones", lambda *a, **k: [])
    model, cmds = FakeModel({"codex": ""}, fail=("codex",)), FakeCmds()
    res = unblock_agent.run(store, j.job_id, model="gpt-6-astra", fallback=False, model_run=model, cmd_run=cmds, runner=FakeRunner())
    assert [c["bin"] for c in model.calls] == ["codex"] and cmds.calls == [] and not res["ok"]
    assert "Nothing was changed." in res["report"]
    both = FakeModel({"codex": "not json", "claude": "also not json"})
    res = unblock_agent.run(store, j.job_id, model_run=both, cmd_run=cmds, runner=FakeRunner())
    assert [c["bin"] for c in both.calls] == ["codex", "claude"] and cmds.calls == [] and not res["ok"]


def test_fable_alias_picks_the_claude_lane(job, monkeypatch):
    store, j = job
    monkeypatch.setattr(explain, "_milestones", lambda *a, **k: [])
    assert unblock_agent.resolve_model("fable") == ("claude-fable-5-1", True)
    assert unblock_agent.resolve_model(None) == ("gpt-6-astra", False)
    model = FakeModel({"claude": {"report": "all fine", "actions": []}})
    unblock_agent.run(store, j.job_id, model="fable", model_run=model, cmd_run=FakeCmds(), runner=FakeRunner())
    assert [c["bin"] for c in model.calls] == ["claude"]


def test_run_refuses_a_second_concurrent_run(job):
    store, j = job
    unblock_agent.acquire(store, j.job_id, "gpt-6-astra", state="running")
    with pytest.raises(unblock_agent.Busy):
        unblock_agent.run(store, j.job_id, model_run=FakeModel({}), cmd_run=FakeCmds())
    assert unblock_agent.active_run(store, j.job_id)  # the first run's slot is untouched


def test_plan_command_pane_label_and_origin(job):
    store, j = job
    plan = chat_module.build_chat(store, j.job_id, "unblock", model="fable")
    assert chat_module.chat_label(plan.role, j.job_id) == f"unblock chat {j.job_id[:8]}"
    assert "STEWARD_FORCED_ORIGIN=unblock-agent" in plan.command and f"unblock-run {j.job_id} --model claude-fable-5-1" in plan.command
    assert "--fallback" not in plan.command and "--fallback" in chat_module.build_chat(store, j.job_id, "unblock").command
    assert cli._build_parser().parse_args(["pause", "J", "--origin", "unblock-agent"]).origin == "unblock-agent"


def test_cli_unblock_dry_run_and_state_guard(job, capsys, monkeypatch):
    store, j = job
    assert cli.main(["unblock", j.job_id[:8], "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "unblock chat" in out and "model: gpt-6-astra" in out and "unblock-run" in out
    assert unblock_agent.active_run(store, j.job_id) is None  # a dry run claims nothing
    _set_state(store, j, JobState.ACCEPTED)
    assert cli.main(["unblock", j.job_id]) == 2
    assert "unblock works on running" in capsys.readouterr().err
    assert cli.main(["unblock", "nope"]) == 1


def test_cli_unblock_claims_the_slot_and_a_second_tap_is_busy(job, capsys, monkeypatch):
    store, j = job
    opened = []
    monkeypatch.setattr(chat_module, "open_chat", lambda s, jid, plan, runner, dry_run=False: (opened.append(plan.role) or ("unblock chat x", "p1")))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/herdr")
    assert cli.main(["unblock", j.job_id]) == 0 and opened == ["unblock"]
    assert unblock_agent.active_run(store, j.job_id)["state"] == "starting"
    assert cli.main(["unblock", j.job_id]) == 3
    assert "unblock already running" in capsys.readouterr().err


def test_cli_unblock_releases_the_slot_if_the_pane_cannot_open(job, monkeypatch):
    store, j = job

    def boom(*a, **k):
        raise chat_module.ChatError("no panes")

    monkeypatch.setattr(chat_module, "open_chat", boom)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/herdr")
    assert cli.main(["unblock", j.job_id]) == 1
    assert unblock_agent.active_run(store, j.job_id) is None


def test_cli_unblock_run_prints_the_report(job, capsys, monkeypatch):
    store, j = job
    monkeypatch.setattr(unblock_agent, "run", lambda s, jid, **kw: {"model": "gpt-6-astra", "report": "was stuck\nDid: no action taken", "ok": True})
    assert cli.main(["unblock-run", j.job_id, "--fallback"]) == 0
    assert "unblock report (gpt-6-astra):\nwas stuck" in capsys.readouterr().out


def test_unblock_button_states_and_note_in_pane(world, steward_home, tmp_path):
    store, j, done = world
    for state, shown in ((JobState.RUNNING, True), (JobState.PAUSED, True), (JobState.NEEDS_HUMAN, True), (JobState.CONTRACTED, False)):
        _set_state(store, j, state)
        out, cmap = _pane(steward_home, tmp_path, 88)
        has = any(c.endswith(f" unblock {j.job_id} -") for c in cmap)
        assert has is shown, state
        if shown:
            row = next(i for i, ln in enumerate(out) if "[ unblock now ]" in dashrender.ANSI.sub("", ln))
            assert any(c.startswith(f"btn {row} ") and c.endswith(f" unblock {j.job_id} -") for c in cmap)
    assert not any(c.endswith(f" unblock {d.job_id} -") for d in done for c in cmap)
    _set_state(store, j, JobState.RUNNING)
    store.kv_set(j.job_id, unblock_agent.NOTE_KEY, {"text": "was stuck on a flaky test\nDid: steer ok", "model": "gpt-6-astra", "at": time.time()})
    out, cmap = _pane(steward_home, tmp_path, 88)
    plain = [dashrender.ANSI.sub("", ln) for ln in out]
    assert any("unblock (gpt-6-astra): was stuck on a flaky test" in ln for ln in plain) and any("Did: steer ok" in ln for ln in plain)
    _assert_style_matches_cmap(out, cmap)
    unblock_agent.acquire(store, j.job_id, "gpt-6-astra", state="running")
    out, _ = _pane(steward_home, tmp_path, 88)
    assert any("unblock agent (gpt-6-astra) is working on it" in dashrender.ANSI.sub("", ln) for ln in out)


def test_unblock_action_argv():
    assert dashact.argv_for("unblock", "J")[-2:] == ["unblock", "J"]


def test_snapshot_100_cols_running_job_with_supervisors_has_no_underline(world, steward_home, tmp_path):
    out, cmap = _pane(steward_home, tmp_path, 100)
    plain = "\n".join(dashrender.ANSI.sub("", ln) for ln in out)
    assert "RUNNING" in plain and "[ steer ]  [ explain ]" in plain
    assert any(c.startswith("toggle ") or " toggle " in c for c in cmap) or "[on]" in plain or "shadow" in plain
    for ln in out:
        for seq in dashrender.ANSI.findall(ln):
            params = seq[2:-1].split(";")
            assert "4" not in params and "24" not in params, repr(ln)
        assert "\033[4m" not in ln
