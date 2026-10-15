"""Quick task 20261001b: [ unblock now ] re-run, human-only blocks, and acceptance=auto vs review constraints.
Fakes only: no model, herdr or network."""

from __future__ import annotations

import time

import pytest
from test_dash_polish import _running  # noqa: F401
from test_status_pane_features import FakeCmds, FakeModel, _pane, _set_state

from steward import chat as chat_module
from steward import cli, closeout, explain, unblock_agent
from steward import questions
from steward.models import JobState, Judgment
from steward.runner_exec import FakeRunner
from steward.steering import Steering
from steward.store import Store


@pytest.fixture
def job(steward_home):
    store = Store(steward_home)
    return store, _running(store, goal="Ship the widget. FINISH LINE: tests green.")


def _report_plan(text="Nothing wrong."):
    return {"report": text, "actions": []}


# -- (1) re-run ----------------------------------------------------------------------------------------------------


def test_finished_run_does_not_block_a_new_one(job, monkeypatch):
    store, j = job
    monkeypatch.setattr(explain, "_milestones", lambda *a, **k: [])
    unblock_agent.acquire(store, j.job_id, "gpt-6-astra")
    assert unblock_agent.active_run(store, j.job_id)  # the launcher's claim, as `steward unblock` leaves it
    unblock_agent.run(store, j.job_id, model_run=FakeModel({"codex": _report_plan()}), cmd_run=FakeCmds(), runner=FakeRunner())
    # a crash left the record "running" even though the report was recorded: still finished
    store.kv_set(j.job_id, unblock_agent.RUN_KEY, {"state": "running", "model": "x", "started_at": time.time() - 60})
    assert unblock_agent.active_run(store, j.job_id) is None
    unblock_agent.acquire(store, j.job_id, "gpt-6-astra")  # no Busy


def test_in_progress_run_is_busy_with_since_text(job):
    store, j = job
    store.kv_set(j.job_id, unblock_agent.RUN_KEY, {"state": "running", "model": "x", "started_at": time.time() - 5 * 60})
    with pytest.raises(unblock_agent.Busy, match=r"unblock already running \(since 5m\)"):
        unblock_agent.acquire(store, j.job_id, "x")


class PaneRunner:
    def __init__(self, panes):
        self.panes, self.calls = panes, []

    def run(self, argv, cwd=None, **kw):
        from steward.runner_exec import CommandResult

        self.calls.append(argv)
        out = ""
        if argv[:3] == ["herdr", "pane", "list"]:
            import json

            out = json.dumps({"result": {"panes": self.panes}})
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)


def test_cli_rerun_closes_the_finished_pane_and_opens_a_fresh_one(job, monkeypatch):
    store, j = job
    label = chat_module.chat_label("unblock", j.job_id)
    runner = PaneRunner([{"label": label, "pane_id": "p9", "tab_id": "t1"}])
    closed = []
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: runner)
    monkeypatch.setattr(chat_module, "close_chat", lambda s, jid, role, r: closed.append(role) or True)
    opened = []
    monkeypatch.setattr(chat_module, "open_chat", lambda s, jid, plan, r, dry_run=False: (opened.append(1) or (label, "p10")))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/herdr")
    # first run finished (report recorded)
    store.kv_set(j.job_id, unblock_agent.RUN_KEY, {"state": "done", "model": "x", "started_at": time.time() - 600})
    assert cli.main(["unblock", j.job_id]) == 0
    assert closed == ["unblock"] and opened == [1]
    # a run in progress is only focused, never replaced, and says so in the note
    assert cli.main(["unblock", j.job_id]) == 3
    assert closed == ["unblock"] and len(opened) == 2
    assert unblock_agent.note(store, j.job_id)["text"].startswith("unblock already running (since ")


def test_close_chat_closes_only_the_labelled_pane(job):
    store, j = job
    label = chat_module.chat_label("unblock", j.job_id)
    r = PaneRunner([{"label": "other", "pane_id": "p1", "tab_id": "t"}, {"label": label, "pane_id": "p9", "tab_id": "t"}])
    assert chat_module.close_chat(store, j.job_id, "unblock", r) is True
    assert ["herdr", "pane", "close", "p9"] in r.calls
    assert chat_module.close_chat(store, j.job_id, "unblock", PaneRunner([])) is False


# -- (2) human-only blocks -----------------------------------------------------------------------------------------


def _needs_accept(store, j):
    _set_state(store, j, JobState.NEEDS_HUMAN)
    store.append_event(j.job_id, what=closeout.UNRESOLVED_WHATS[0], evidence={}, rule="acceptance_unresolved", judgment=Judgment.code)


def test_human_only_accept_is_named_in_report_and_note(job, monkeypatch):
    store, j = job
    monkeypatch.setattr(explain, "_milestones", lambda *a, **k: [])
    _needs_accept(store, j)
    assert unblock_agent.human_blockers(store, j.job_id) == [{"kind": "accept", "text": "needs your accept"}]
    res = unblock_agent.run(store, j.job_id, model_run=FakeModel({"codex": _report_plan("Looks fine.")}), cmd_run=FakeCmds(), runner=FakeRunner())
    assert "needs your accept" in res["report"] and "no action taken" not in res["report"]
    assert unblock_agent.note(store, j.job_id)["human"][0]["kind"] == "accept"


def test_human_only_question_is_named(job, monkeypatch):
    store, j = job
    monkeypatch.setattr(explain, "_milestones", lambda *a, **k: [])
    questions.save(store, j.job_id, [{"qid": "Q1", "title": "Which db?", "status": "open", "starred": False}])
    assert unblock_agent.human_blockers(store, j.job_id) == [{"kind": "answer", "text": "needs your answer on Which db?"}]
    res = unblock_agent.run(store, j.job_id, model_run=FakeModel({"codex": _report_plan("Waiting.")}), cmd_run=FakeCmds(), runner=FakeRunner())
    assert "needs your answer on Which db?" in res["report"]
    # a question with a recorded default is the agent's to answer, not human-only
    questions.save(store, j.job_id, [{"qid": "Q1", "title": "Which db?", "status": "open", "starred": True}])
    assert unblock_agent.human_blockers(store, j.job_id) == []


def test_job_row_surfaces_accept_and_answer_buttons(job, monkeypatch, steward_home, tmp_path):
    store, j = job
    _needs_accept(store, j)
    questions.save(store, j.job_id, [{"qid": "Q1", "title": "Which db?", "status": "open", "starred": False}])
    store.kv_set(j.job_id, unblock_agent.NOTE_KEY, {"text": "ok\nDid: no action taken", "model": "m", "at": time.time(),
                                                     "human": unblock_agent.human_blockers(store, j.job_id)})
    out, cmap = _pane(steward_home, tmp_path, 100)
    assert any(c.endswith(f" accept {j.job_id} -") for c in cmap)
    assert any(c.endswith(f" answer {j.job_id} -") for c in cmap)


# -- (3) acceptance policy -----------------------------------------------------------------------------------------


class _Contract:
    done_when = ["authorized checks pass"]


def _checked_job(store, goal):
    j = store.create_job(goal, "/tmp").job_id
    for st in (JobState.CONTRACTED, JobState.RUNNING, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED):
        store.set_state(j, st, what="t", evidence={}, rule="t", judgment=Judgment.code)
    return j


def test_goal_clause_about_api_makes_a_review_constraint(steward_home):
    from steward.contract import Contract, store_contract

    store = Store(steward_home)
    j = store.create_job("g", "/tmp").job_id
    store_contract(store, j, Contract(goal="g", boundaries=["Do not edit app code; keep the public API stable."], done_when=["x"]))
    assert [i["enforcement"] for i in Steering(store).active_constraints(j)] == ["review"]


def test_auto_accepts_docs_only_with_review_constraint_and_records_it(steward_home):
    store = Store(steward_home)
    j = _checked_job(store, "g")
    Steering(store).record(j, "focus", "do not edit app code; keep the public API stable")
    assert closeout._decide_acceptance(store, j, [], _Contract()) is None  # nothing blocks: it asks for the ruling
    ruling = {"status": "ruled", "ruling": "finished", "ruling_sha": "sha1", "sha": "sha1", "id": "rul-1"}
    assert closeout._decide_acceptance(store, j, [], _Contract(), commit_sha="sha1", ruling=ruling) == JobState.ACCEPTED
    ev = [e for e in store.events(j) if e.rule == "ruling_accepted"][-1]
    assert ev.evidence["review_constraints_violated"] == []
    assert "public API" in ev.evidence["review_constraints_waived"][0]


def test_auto_still_blocks_when_a_constraint_was_violated(steward_home):
    store = Store(steward_home)
    j = _checked_job(store, "g")
    Steering(store).record(j, "focus", "keep the public API stable")
    store.kv_set(j, "closeout_blocked", True)
    assert closeout._decide_acceptance(store, j, [], _Contract()) == JobState.NEEDS_HUMAN


def test_human_mode_still_blocks_on_review_constraints(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "human")
    store = Store(steward_home)
    j = _checked_job(store, "g")
    Steering(store).record(j, "focus", "keep the public API stable")
    assert closeout._decide_acceptance(store, j, [], _Contract()) == JobState.NEEDS_HUMAN
    ev = [e for e in store.events(j) if e.rule == "acceptance_unresolved"][-1]
    assert any("review-enforcement" in r for r in ev.evidence["reasons"])
