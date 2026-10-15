"""Phase 17 plan 05: the question queue on the status pane (G3, G4, G10, G11, G12).

The pane is rendered as a subprocess (deploy/cockpit/steward-dash-jobs.py) over a throwaway store, the way
tests/test_dash_polish.py does. The pane shows a count and buttons, never cards.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from questions_helpers import make_record, running_job, seed, seed_queue, set_state
from steward import dashact, questions
from steward.models import JobState
from steward.store import Store

_REPO = Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\033\[[0-9;]*m")


def _render(steward_home, tmp_path, cols, rows=None, expanded=(), armed=None):
    m = tmp_path / f"map{cols}_{rows}"
    dashact.save(dashact.state_path(str(m)), {"expanded": {k: True for k in expanded}, "armed": armed or {}})
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_DASH_COLS": str(cols), "STEWARD_HEALTH": "0",
           "STEWARD_DASH_MAP": str(m), "PYTHONPATH": str(_REPO)}
    if rows:
        env["STEWARD_DASH_ROWS"] = str(rows)
    else:
        env.pop("STEWARD_DASH_ROWS", None)
    res = subprocess.run([sys.executable, str(_REPO / "deploy/cockpit/steward-dash-jobs.py")],
                         capture_output=True, text=True, env=env, cwd=_REPO)
    assert res.returncode == 0, res.stderr
    lines = [ANSI.sub("", ln).replace("{SPIN}", "x") for ln in res.stdout.splitlines()]
    return lines, m.read_text().splitlines()


def _btns(cmap, row=None):
    out = [ln.split() for ln in cmap if ln.startswith("btn ") and " fold " not in ln and " steer " not in ln and " explain " not in ln and " unblock " not in ln]  # the [n] fold cell and header [ steer ] are not question buttons
    return [b for b in out if row is None or int(b[1]) == row]


def _count_row(lines):
    return next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("? "))


# -- task 1: the count line and its buttons -----------------------------------------------------------------


def test_count_line_and_buttons_merge_when_cols_allow(steward_home, tmp_path):
    store = Store()
    jid = running_job(store, "Ship the widget")
    seed_queue(store, jid, 14, 1)
    lines, cmap = _render(steward_home, tmp_path, 88)
    row = _count_row(lines)
    assert lines[row] == "    ? 15 questions · 14 ★ · 1 needs you  [ accept all ★ ]  [ answer ]"
    assert any("Ship the widget" in ln for ln in lines[:row])  # below the goal lines
    assert any(ln.startswith("[3]") for ln in lines[:row])
    b = _btns(cmap, row)
    assert [x[4] for x in b] == ["accept_rec", "answer"] and all(x[5] == jid and x[6] == "-" for x in b)
    assert [lines[row][int(x[2]) - 1:int(x[3])] for x in b] == ["[ accept all ★ ]", "[ answer ]"]


def test_narrow_pane_puts_the_buttons_on_their_own_row(steward_home, tmp_path):
    store = Store()
    jid = running_job(store)
    seed_queue(store, jid, 14, 1)
    lines, cmap = _render(steward_home, tmp_path, 60)
    row = _count_row(lines)
    assert lines[row] == "    ? 15 questions · 14 ★ · 1 needs you"
    assert lines[row + 1] == "    [ accept all ★ ]  [ answer ]"
    b = _btns(cmap, row + 1)
    assert [lines[row + 1][int(x[2]) - 1:int(x[3])] for x in b] == ["[ accept all ★ ]", "[ answer ]"]
    assert not _btns(cmap, row)
    assert all(len(ln) <= 60 for ln in lines)


@pytest.mark.parametrize("starred,unstarred,text,has_all", [
    (1, 0, "? 1 question · 1 ★", True),
    (0, 3, "? 3 questions · 3 need you", False),
    (0, 1, "? 1 question · 1 needs you", False),
    (2, 2, "? 4 questions · 2 ★ · 2 need you", True),
])
def test_variants(steward_home, tmp_path, starred, unstarred, text, has_all):
    store = Store()
    jid = running_job(store)
    seed_queue(store, jid, starred, unstarred)
    lines, cmap = _render(steward_home, tmp_path, 100)
    row = _count_row(lines)
    assert lines[row].startswith(f"    {text}")
    assert ("[ accept all ★ ]" in lines[row]) is has_all and "[ answer ]" in lines[row]
    assert [x[4] for x in _btns(cmap, row)] == (["accept_rec", "answer"] if has_all else ["answer"])


def test_nothing_waiting_adds_no_line(steward_home, tmp_path):
    store = Store()
    jid = running_job(store)
    seed(store, jid, [make_record("gsd_context:Q-CONTEXT.md:G1", status="answered"),
                      make_record("gsd_context:Q-CONTEXT.md:G2", status="deferred"),
                      make_record("gsd_context:Q-CONTEXT.md:G3", status="withdrawn")])
    lines, cmap = _render(steward_home, tmp_path, 88)
    assert not any(ln.lstrip().startswith("? ") for ln in lines)
    assert not any("accept_rec" in c or " answer " in c for c in cmap)


def test_stale_counts_as_waiting_and_needing_sam(steward_home, tmp_path):
    store = Store()
    jid = running_job(store)
    seed(store, jid, [make_record("gsd_context:Q-CONTEXT.md:G1", starred=True),
                      make_record("gsd_context:Q-CONTEXT.md:G2", status="stale", starred=True)])
    lines, _ = _render(steward_home, tmp_path, 100)
    assert lines[_count_row(lines)].startswith("    ? 2 questions · 1 ★ · 1 needs you")


def test_worker_decided_line(steward_home, tmp_path):
    store = Store()
    jid = running_job(store)
    worker = make_record("gsd_context:Q-CONTEXT.md:G3", status="answered")
    worker["flags"]["answered_by_worker"] = True
    worker["answer"] = {"option": "as_written", "by": "worker"}
    seed(store, jid, [worker])
    lines, cmap = _render(steward_home, tmp_path, 88)
    assert "    ⚠ 1 question decided by the worker, not you (G3)" in lines
    assert not any(ln.lstrip().startswith("? ") for ln in lines) and not _btns(cmap)
    many = []
    for n in range(1, 6):
        r = make_record(f"gsd_context:Q-CONTEXT.md:G{n}", status="answered", line=n)
        r["flags"]["answered_by_worker"] = True
        r["answer"] = {"option": "as_written", "by": "worker"}
        many.append(r)
    seed(store, jid, many)
    lines, _ = _render(steward_home, tmp_path, 88)
    assert "    ⚠ 5 questions decided by the worker, not you (G1, G2, G3, +2)" in lines


def test_states_covered(steward_home, tmp_path):
    store = Store()
    ids = [running_job(store, f"job {s.name}", s) for s in (JobState.RUNNING, JobState.PAUSED, JobState.NEEDS_HUMAN)]
    for jid in ids:
        seed_queue(store, jid, 2)
    lines, _ = _render(steward_home, tmp_path, 100)
    assert sum(1 for ln in lines if ln.lstrip().startswith("? 2 questions")) == 3
    done = running_job(store, "finished", JobState.ACCEPTED)
    seed_queue(store, done, 2)
    store.kv_set(done, "incorporated_commit", "abc1234def")
    lines, _ = _render(steward_home, tmp_path, 100)
    assert sum(1 for ln in lines if ln.lstrip().startswith("? 2 questions")) == 3


def test_armed_accept_rec_renders_the_confirm_label(steward_home, tmp_path):
    import time

    store = Store()
    jid = running_job(store)
    seed_queue(store, jid, 14, 1)
    armed = {jid: {"action": "accept_rec", "until": time.time() + 30}}
    lines, cmap = _render(steward_home, tmp_path, 100, armed=armed)
    row = _count_row(lines)
    b = _btns(cmap, row)
    assert lines[row][int(b[0][2]) - 1:int(b[0][3])] == "[ confirm accept all ★ ]"
    assert lines[row][int(b[1][2]) - 1:int(b[1][3])] == "[ answer ]"


def test_pane_summary_reads_the_kv_list_only(steward_home):
    store = Store()
    jid = running_job(store)
    assert questions.pane_summary(store, jid)["waiting"] == 0
    seed_queue(store, jid, 2, 1)
    s = questions.pane_summary(store, jid)
    assert (s["waiting"], s["starred"], s["needs_you"], s["worker_decided"]) == (3, 2, 1, [])
    r = make_record("gsd_context:Q-CONTEXT.md:G9", status="answered")
    r["source"]["anchor"] = "G9\x1b[31m"
    r["flags"]["answered_by_worker"] = True
    seed(store, jid, [r])
    assert questions.pane_summary(store, jid)["worker_decided"] == ["G9[31m"]


def test_a_broken_queue_never_breaks_the_pane(steward_home, tmp_path):
    store = Store()
    jid = running_job(store, "Ship the widget")
    store.kv_set(jid, questions.KV_KEY, "not a list")
    lines, _ = _render(steward_home, tmp_path, 88)
    assert any("Ship the widget" in ln for ln in lines)


# -- task 2: pane actions ---------------------------------------------------------------------------------------

_CLI = ["python3", "-m", "steward.cli"]


def test_argv_for_new_actions():
    assert dashact.argv_for("answer", "J") == [*_CLI, "phone", "--questions", "J", "--open"]
    assert dashact.argv_for("accept_rec", "J") == [*_CLI, "answer", "J", "--accept-recommended", "--origin", "dash"]
    assert dashact.argv_for("accept_rec_accept", "J") == [*_CLI, "answer", "J", "--accept-recommended", "--then-accept",
                                                          "--origin", "dash"]
    assert dashact.argv_for("write_source", "J") == [*_CLI, "answer", "J", "--write-source", "--origin", "dash"]
    with pytest.raises(ValueError):
        dashact.argv_for("frobnicate", "J")


def test_confirm_tuple_and_one_tap_decisions(tmp_path):
    assert dashact.CONFIRM == ("accept_rec", "accept_rec_accept")
    path = dashact.state_path(str(tmp_path / "m"))
    for action in ("accept", "stop", "reject", "approve", "deny", "why", "answer", "write_source"):
        status, argv = dashact.press(path, action, "J", "A1", now=1.0)
        assert status == "run" and argv == dashact.argv_for(action, "J", "A1"), action


def test_accept_rec_arms_then_runs(tmp_path):
    path = dashact.state_path(str(tmp_path / "m"))
    review = "accept 14 recommended, 1 needs you (G9)"
    assert dashact.press(path, "accept_rec", "J", now=100.0, review=review) == ("armed", [review])
    assert dashact.load(path)["armed"]["J"]["review"] == review
    assert dashact.press(path, "accept_rec", "J", now=101.0, review=review) == ("run", dashact.argv_for("accept_rec", "J"))
    assert dashact.armed_action(dashact.load(path), "J", 102.0) is None
    stale = 200.0 + dashact.ARM_S + 1
    assert dashact.press(path, "accept_rec", "J", now=200.0, review=review)[0] == "armed"
    assert dashact.press(path, "accept_rec", "J", now=stale, review=review)[0] == "armed"  # expired: arms again
    assert dashact.press(path, "answer", "J", now=stale + 1)[0] == "run"  # another button never confirms it
    assert dashact.press(path, "accept_rec", "K", now=stale + 2, review=review)[0] == "armed"  # nor another job
    assert dashact.press(path, "accept_rec", "J", now=stale + 3, review=review)[0] == "run"
    assert dashact.press(path, "accept_rec", "J", now=400.0)[0:2] == ("armed", [])  # no review: armed, nothing to show


def test_accept_rec_accept_arms_keeps_panels_then_closes_them(tmp_path):
    path = dashact.state_path(str(tmp_path / "m"))
    dashact.save(path, {"expanded": {"J": True, "inc:J": True}, "armed": {}})
    assert dashact.press(path, "accept_rec_accept", "J", now=100.0, review="r") == ("armed", ["r"])
    assert dashact.is_expanded(dashact.load(path), "J")  # the confirm button must still be on screen
    status, argv = dashact.press(path, "accept_rec_accept", "J", now=101.0, review="r")
    assert status == "run" and argv[-3:] == ["--then-accept", "--origin", "dash"]
    data = dashact.load(path)
    assert not dashact.is_expanded(data, "J") and not dashact.is_expanded(data, "inc:J")


def _press_cli(steward_home, tmp_path, *args):
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_DASH_MAP": str(tmp_path / "m"),
           "PYTHONPATH": str(_REPO)}
    res = subprocess.run([sys.executable, "-m", "steward.dashact", *args], capture_output=True, text=True, env=env, cwd=_REPO)
    return res.stdout.split("\n")


def test_dashact_main_prints_the_review(steward_home, tmp_path):
    store = Store()
    jid = running_job(store)
    recs = [make_record(f"gsd_context:Q-CONTEXT.md:G{n}", starred=n != 9, line=n, file="Q-CONTEXT.md") for n in range(1, 16)]
    seed(store, jid, recs)
    first = _press_cli(steward_home, tmp_path, "press", "accept_rec", jid, "-")
    assert first[:2] == ["armed", "accept 14 recommended, 1 needs you (G9)"]
    second = _press_cli(steward_home, tmp_path, "press", "accept_rec", jid, "-")
    assert second[0] == "run" and second[1:7] == ["python3", "-m", "steward.cli", "answer", jid, "--accept-recommended"]
    third = _press_cli(steward_home, tmp_path, "press", "accept_rec_accept", jid, "-")
    assert third[:2] == ["armed", "accept 14 recommended, 1 needs you (G9)"]


def test_dashact_main_store_error_prints_just_armed(tmp_path):
    broken = tmp_path / "not-a-directory"
    broken.write_text("x")
    assert _press_cli(broken, tmp_path, "press", "accept_rec", "J", "-") == ["armed", ""]


def test_dash_script_parses():
    script = _REPO / "deploy/cockpit/steward-dash"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    assert "steward.dashapp" in script.read_text()  # the entry name stays; the loop is the resident Python process
    text = (_REPO / "steward/dashapp.py").read_text()
    assert "accept all ★" in text and "accept ★ + accept" in text
    armed = text[text.index('if status == "armed"'):text.index("# fold / incorporate panel")]
    assert "review" in armed and "tap again" in armed


# -- task 3: acceptance panel, buttons and the discussion-job warning ---------------------------------------------

from steward import approvals, evidence, phone  # noqa: E402
from steward.contract import Contract  # noqa: E402

DISCUSS = "Discuss phase 17 and record the gray areas as CONTEXT.md"


def _acceptance_job(store, goal="Fix the parser bug", approval=True):
    jid = running_job(store, goal, JobState.NEEDS_HUMAN)
    if approval:
        approvals.open_approval(store, jid, approvals.ACCEPTANCE_QUESTION, kind="acceptance")
    return jid


def _labels(dec):
    return [b[0] for b in evidence.buttons_for(dec)]


def _actions(dec):
    return [b[1] for b in evidence.buttons_for(dec)]


def test_acceptance_panel_shows_open_questions(steward_home):
    from steward import session

    store = Store()
    jid = _acceptance_job(store)
    assert not any("open question" in ln for ln in evidence.panel(store, jid, 100, cost_usd=1.0))
    seed_queue(store, jid, 14, 1)
    dec = evidence.decision(store, jid)
    lines = evidence.panel(store, jid, 100, cost_usd=1.0, dec=dec)
    assert lines[-2:] == ["cost $1.00", "15 open questions (14 ★)"]
    # session.build_result passes dec without `questions`: the panel reads the queue itself
    bare = evidence.panel(store, jid, 100, cost_usd=1.0, dec={"kind": "acceptance", "approval": None})
    assert bare[-1] == "15 open questions (14 ★)"
    assert any("15 open questions (14 ★)" in ln for ln in session.build_result(store, jid)["evidence"])
    seed(store, jid, [make_record("gsd_context:Q-CONTEXT.md:G1", starred=True), make_record("gsd_context:Q-CONTEXT.md:G2", status="deferred"),
                      make_record("gsd_context:Q-CONTEXT.md:G3", status="answered")])
    assert evidence.panel(store, jid, 100, cost_usd=1.0, dec=evidence.decision(store, jid))[-1] == "1 open question (1 ★)"
    seed(store, jid, [make_record("gsd_context:Q-CONTEXT.md:G2", status="deferred"),
                      make_record("gsd_context:Q-CONTEXT.md:G3", status="answered")])
    assert not any("open question" in ln for ln in evidence.panel(store, jid, 100, cost_usd=1.0, dec=evidence.decision(store, jid)))


def test_buttons_gain_answer_and_accept_star(steward_home):
    store = Store()
    jid = _acceptance_job(store)
    assert _actions(evidence.decision(store, jid)) == ["accept", "sendback", "stop", "chat"]  # nothing waiting: the old list
    seed_queue(store, jid, 14, 1)
    dec = evidence.decision(store, jid)
    assert _actions(dec) == ["accept", "sendback", "stop", "chat", "answer", "accept_rec_accept"]
    assert _labels(dec)[-2:] == ["answer", "accept ★ + accept"]
    seed_queue(store, jid, 0, 2)  # no starred waiting: only `answer` is added
    assert _actions(evidence.decision(store, jid)) == ["accept", "sendback", "stop", "chat", "answer"]
    # approval and escalation decisions are untouched
    seed_queue(store, jid, 3)
    assert _actions({"kind": "approval", "approval": {"id": "a"}, "questions": questions.summary(questions.load(store, jid))}) \
        == ["approve", "deny", "why"]


def test_mcp_decision_ignores_the_new_actions(steward_home):
    from steward import mcp_server  # noqa: F401  (import must not fail)

    store = Store()
    jid = _acceptance_job(store)
    seed_queue(store, jid, 2)
    dec = evidence.decision(store, jid)
    known = {"accept", "stop", "approve", "deny", "why"}
    assert [a for a in _actions(dec) if a in known] == ["accept", "stop"]


def test_decision_carries_the_summary(steward_home):
    store = Store()
    jid = _acceptance_job(store)
    dec = evidence.decision(store, jid)
    assert dec["questions"] is None and dec["deliverable"] is False
    seed_queue(store, jid, 2, 1)
    dec = evidence.decision(store, jid)
    assert dec["questions"]["waiting"] == 3 and dec["questions"]["starred"] == 2 and dec["deliverable"] is False
    run = running_job(store)
    seed_queue(store, run, 2)
    approvals.open_approval(store, run, "worker permission: Bash {'command': 'ls'}", kind="permission")
    perm = evidence.decision(store, run)
    assert perm["kind"] == "approval" and not perm.get("questions") and not perm.get("deliverable")


@pytest.mark.parametrize("goal,done_when,expect", [
    ("Discuss phase 17 and record the gray areas as CONTEXT.md", [], True),
    ("Start the DISCUSSION of the export format", [], True),
    ("Fix the parser; list the open questions for Sam", [], True),
    ("Leave the design questions in NOTES.md", [], True),
    ("Write up the grey area list", [], False),
    ("Fix the parser bug", [], False),
    ("Implement the question queue", [], False),
    ("Add a discount code field", [], False),
    ("Fix the parser bug", ["the gray areas are listed for Sam"], True),
    ("Fix the parser bug", ["tests pass", "docs updated"], False),
])
def test_is_deliverable_table(steward_home, goal, done_when, expect):
    from dataclasses import asdict

    store = Store()
    jid = running_job(store, goal)
    if done_when:
        store.kv_set(jid, "contract", asdict(Contract(goal=goal, done_when=done_when)))
    assert questions.is_deliverable(store, jid) is expect


def test_discussion_job_warns_not_locks(steward_home):
    store = Store()
    jid = _acceptance_job(store, DISCUSS)
    seed_queue(store, jid, 14, 1)
    dec = evidence.decision(store, jid)
    assert dec["deliverable"] is True
    assert _labels(dec)[0] == "accept anyway" and _actions(dec)[0] == "accept"
    lines = evidence.panel(store, jid, 100, cost_usd=1.0, dec=dec)
    assert lines[-2:] == ["⚠ discussion job: 15 questions are its deliverable and still wait for you",
                          "15 open questions (14 ★)"]
    assert dashact.argv_for("accept", jid)[3:5] == ["accept", jid]  # still the plain accept, one tap
    # answered or deferred: the label is plain `accept` again and the warning is gone
    done = [make_record(f"gsd_context:Q-CONTEXT.md:G{n}", status="answered" if n % 2 else "deferred", line=n) for n in range(1, 16)]
    seed(store, jid, done)
    dec = evidence.decision(store, jid)
    assert _labels(dec)[0] == "accept" and not dec["deliverable"]
    assert not any("discussion job" in ln for ln in evidence.panel(store, jid, 100, cost_usd=1.0, dec=dec))
    other = _acceptance_job(store, "Fix the parser bug")
    seed_queue(store, other, 14, 1)
    assert _labels(evidence.decision(store, other))[0] == "accept"


def test_phone_decision_hints_follow(steward_home):
    store = Store()
    jid = _acceptance_job(store, DISCUSS)
    base = phone._decision_hints({"kind": "acceptance", "approval": None})
    assert base == [("A", "accept"), ("S", "send back"), ("x", "stop"), ("C", "chat")]
    seed_queue(store, jid, 2)
    hints = phone._decision_hints(evidence.decision(store, jid))
    assert hints[0] == ("A", "accept anyway") and hints[1:] == base[1:]
    plain = _acceptance_job(store, "Fix the parser bug")
    seed_queue(store, plain, 2)
    assert phone._decision_hints(evidence.decision(store, plain)) == base


def test_button_rows_fill_greedily():
    buttons = [("accept", "accept"), ("send back", "sendback"), ("stop", "stop"), ("chat", "chat"), ("answer", "answer"),
               ("accept ★ + accept", "accept_rec_accept")]
    one = dashact.button_rows(buttons, None, 100)
    assert len(one) == 1 and one[0][1] == dashact.button_row(buttons)[1]
    two = dashact.button_rows(buttons, None, 80)
    assert len(two) == 2 and all(len(plain) <= 80 for _c, plain, _s in two)
    assert [act for _c, _p, spans in two for _a, _b, act in spans] == [a for _l, a in buttons]
    assert dashact.button_rows([], None, 80) == [] or dashact.button_rows([], None, 80)[0][2] == []
    tiny = dashact.button_rows(buttons[:2], None, 5)  # a button wider than COLS still gets its own row
    assert len(tiny) == 2


def test_button_row_wraps_when_wider_than_cols(steward_home, tmp_path):
    store = Store()
    jid = _acceptance_job(store)
    seed_queue(store, jid, 14, 1)
    for cols, nrows in ((80, 2), (100, 1)):
        lines, cmap = _render(steward_home, tmp_path, cols, expanded=[jid])
        ask = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("→ "))
        acc = [b for b in _btns(cmap) if int(b[1]) > ask]  # the panel's rows, not the count line's [ answer ]
        assert [b[4] for b in acc] == ["accept", "sendback", "stop", "chat", "answer", "accept_rec_accept"]
        assert len({b[1] for b in acc}) == nrows, cols
        assert all(len(ln) <= cols for ln in lines)
        for b in acc:
            text = lines[int(b[1])][int(b[2]) - 1:int(b[3])]
            assert text.startswith("[ ") and text.endswith(" ]") and b[5] == jid
        texts = [lines[int(b[1])][int(b[2]) - 1:int(b[3])] for b in acc]
        assert texts[-2:] == ["[ answer ]", "[ accept ★ + accept ]"]
    # a job without questions still has its four buttons on one row
    plain = _acceptance_job(store, "Another job")
    lines, _ = _render(steward_home, tmp_path, 80, expanded=[plain])
    assert any("[ accept ]  [ send back ]  [ stop ]  [ chat ]" in ln for ln in lines)


# -- task 4: height fit, the ALL JOBS count and the merged-answers row ----------------------------------------------


def _two_jobs_and_a_done(steward_home):
    store = Store()
    a = running_job(store, "First job goal that is long enough to wrap onto a second line of the pane for sure yes")
    b = running_job(store, "Second job goal that is long enough to wrap onto a second line of the pane for sure yes")
    seed_queue(store, a, 14, 1)
    seed_queue(store, b, 14, 1)
    done = running_job(store, "finished long ago", JobState.ACCEPTED)
    store.kv_set(done, "incorporated_commit", "abc1234def")
    return store, a, b, done


def _check_map(lines, cmap):
    for entry in cmap:
        kind, *rest = entry.split(" ")
        if kind in ("row", "toggle", "btn", "expand") and rest and rest[0].isdigit():
            assert int(rest[0]) < len(lines), entry


def test_height_fit_keeps_button_rows(steward_home, tmp_path):
    _store, a, b, _done = _two_jobs_and_a_done(steward_home)
    lines, cmap = _render(steward_home, tmp_path, 60, rows=16)
    assert len(lines) <= 16 - 9
    _check_map(lines, cmap)
    btns = _btns(cmap)
    assert sorted(x[4] for x in btns) == ["accept_rec", "accept_rec", "answer", "answer"]
    assert {x[5] for x in btns} == {a, b}
    for x in btns:  # the click map still points at the button text after lines were dropped
        assert lines[int(x[1])][int(x[2]) - 1:int(x[3])] in ("[ accept all ★ ]", "[ answer ]") or \
            lines[int(x[1])][int(x[2]) - 1:int(x[3])].startswith("[ ")
    assert not any("tokens" in ln and "per model" in ln for ln in lines)  # lower priority lines went first
    # at COLS 88 the merged line (count + buttons) survives at priority 9
    lines, cmap = _render(steward_home, tmp_path, 88, rows=16)
    merged = [ln for ln in lines if ln.lstrip().startswith("? 15 questions") and "[ accept all ★ ]" in ln]
    assert len(merged) == 2 and len(lines) <= 7
    _check_map(lines, cmap)


def test_priorities_are_9_for_buttons_5_for_the_count(steward_home, tmp_path):
    """Shrink the pane: the priority 3 header goes before any priority 5 count line, and a priority 9 button row is
    never dropped while a lower-priority line remains."""
    _two_jobs_and_a_done(steward_home)
    shape = []
    for rows in range(40, 9, -1):
        lines, cmap = _render(steward_home, tmp_path, 60, rows=rows)
        _check_map(lines, cmap)
        shape.append((any(ln.startswith("ALL JOBS") for ln in lines),
                      sum(1 for ln in lines if ln.lstrip().startswith("? 15 questions")),
                      sum(1 for ln in lines if ln.strip() == "[ accept all \u2605 ]  [ answer ]")))
    assert all(buttons == 2 for _h, _c, buttons in shape)  # never dropped
    header_gone = next(i for i, (h, _c, _b) in enumerate(shape) if not h)
    count_gone = next(i for i, (_h, c, _b) in enumerate(shape) if c < 2)
    assert header_gone <= count_gone  # header (3) before the count lines (5)
    assert shape[0] == (True, 2, 2) and shape[-1][1] < 2


def test_all_jobs_header_counts_questions(steward_home, tmp_path):
    store = Store()
    a = running_job(store, "one")
    b = running_job(store, "two", JobState.PAUSED)
    seed_queue(store, a, 10, 5)
    seed_queue(store, b, 2)
    for cols in (60, 80, 100):
        lines, _ = _render(steward_home, tmp_path, cols)
        head = next(ln for ln in lines if ln.startswith("ALL JOBS"))
        assert head.endswith("· ? 17") and len(head) <= cols, (cols, head)
    done = running_job(store, "merged", JobState.ACCEPTED)
    seed_queue(store, done, 5)
    store.kv_set(done, "incorporated_commit", "abc1234def")
    lines, _ = _render(steward_home, tmp_path, 80)
    assert next(ln for ln in lines if ln.startswith("ALL JOBS")).endswith("· ? 17")  # the done group does not count
    seed(store, a, [])
    seed(store, b, [])
    lines, _ = _render(steward_home, tmp_path, 80)
    assert "? " not in next(ln for ln in lines if ln.startswith("ALL JOBS"))


def test_total_waiting_and_source_unwritten(steward_home):
    store = Store()
    a, b = running_job(store), running_job(store)
    seed_queue(store, a, 3)
    seed_queue(store, b, 1, 1)
    assert questions.total_waiting(store, [a, b]) == 5 and questions.total_waiting(store, []) == 0
    assert questions.total_waiting(store, [a, "no-such-job"]) == 3
    assert questions.source_unwritten(store, a) == []
    store.kv_set(a, "questions_source_unwritten", ["q1", "q2"])
    assert questions.source_unwritten(store, a) == ["q1", "q2"]


def _merged_with_unwritten(store, n=3, branch="main"):
    jid = running_job(store, "Discuss and record", JobState.ACCEPTED)
    store.kv_set(jid, "incorporated_commit", "abc1234def")
    store.kv_set(jid, "questions_source_unwritten", [f"q{i}" for i in range(1, n + 1)])
    if branch:
        from steward.models import Judgment

        store.append_event(jid, what="incorporated", evidence={"merge_commit": "abc1234def", "source_branch": branch},
                           rule="r", judgment=Judgment.human)
    return jid


def test_merged_job_with_unwritten_answers_shows_the_write_row(steward_home, tmp_path):
    store = Store()
    jid = _merged_with_unwritten(store)
    quiet = running_job(store, "merged and clean", JobState.ACCEPTED)
    store.kv_set(quiet, "incorporated_commit", "fff1234def")
    lines, cmap = _render(steward_home, tmp_path, 88)
    row = next(i for i, ln in enumerate(lines) if ln.startswith("    ? 3 answers not on main"))
    assert lines[row] == "    ? 3 answers not on main  [ write answers to main ]"
    b = _btns(cmap, row)
    assert len(b) == 1 and b[0][4:] == ["write_source", jid, "-"]
    assert lines[row][int(b[0][2]) - 1:int(b[0][3])] == "[ write answers to main ]"
    assert jid[:8] in lines[row - 1]  # under its own done line, outside the collapsed group
    assert any("done (1)" in ln for ln in lines) and not any(quiet[:8] in ln for ln in lines)
    assert dashact.argv_for("write_source", jid)[3:] == ["answer", jid, "--write-source", "--origin", "dash"]


def test_write_row_singular_default_branch_and_clean_job(steward_home, tmp_path):
    store = Store()
    _merged_with_unwritten(store, n=1, branch="")
    lines, _ = _render(steward_home, tmp_path, 100)
    assert "    ? 1 answer not on the source branch  [ write answers to the source branch ]" in lines
    clean = running_job(store, "merged clean", JobState.ACCEPTED)
    store.kv_set(clean, "incorporated_commit", "fff1234def")
    lines, _ = _render(steward_home, tmp_path, 100)
    assert any("done (1)" in ln for ln in lines) and not any(clean[:8] in ln for ln in lines)


def test_write_row_fits_narrow_panes(steward_home, tmp_path):
    store = Store()
    _merged_with_unwritten(store, n=12, branch="steward/ed11ba3c-0cc9-4f07-aad9-47d7283fd89e")
    for cols in (40, 60, 80):
        lines, cmap = _render(steward_home, tmp_path, cols)
        assert all(len(ln) <= cols for ln in lines), cols
        row = next(i for i, ln in enumerate(lines) if ln.startswith("    ? 12 answers"))
        b = _btns(cmap, row) or _btns(cmap, row + 1)  # too narrow for one row: the button is on the next
        assert len(b) == 1 and b[0][4] == "write_source"
        text = lines[int(b[0][1])][int(b[0][2]) - 1:int(b[0][3])]
        assert text.startswith("[ write answers") and text.endswith(" ]")
        if cols == 80:  # room for one row, branch shortened but recognisable
            assert int(b[0][1]) == row and "steward/" in lines[row] and "…" in lines[row]


def test_frame_with_15_questions_fits_and_click_map_is_valid(steward_home, tmp_path):
    store = Store()
    jid = running_job(store, "Ship the widget " * 6)
    seed_queue(store, jid, 14, 1)
    for rows in (12, 14, 20, 43):
        for cols in (60, 80, 88):
            lines, cmap = _render(steward_home, tmp_path, cols, rows=rows)
            assert len(lines) <= max(rows - 9, 5), (rows, cols, len(lines))
            _check_map(lines, cmap)
            assert {x[4] for x in _btns(cmap)} == {"accept_rec", "answer"}
