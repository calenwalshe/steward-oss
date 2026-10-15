"""`steward phone` Q mode (plan 17-04): one card per screen, rounds of 4, the relayed `answer` verb, accept-all
with review-then-confirm, the queue on HOME and JOB, and the `--questions` tab. No network, no real herdr."""

from __future__ import annotations

import pytest

from steward import phone, questions
from steward.models import JobState, Judgment
from questions_helpers import make_record, make_store_job, seed, set_state


def _recorder(rc=0, out="ok"):
    calls: list[list[str]] = []

    def run_cli(argv):
        calls.append(argv)
        return rc, out

    return run_cli, calls


def _records(n: int = 5, *, blocking_at: int | None = 2, unstarred: tuple[int, ...] = ()) -> list[dict]:
    return [make_record(f"gsd_context:17-CONTEXT.md:G{i}", line=i, blocking=(i == blocking_at),
                        starred=i not in unstarred, recommended=() if i in unstarred else ("a",))
            for i in range(1, n + 1)]


def _applier(store, job_id, calls, rc=0, out="ok"):
    """A run_cli that records every argv and applies `answer` writes to the store like the relayed verb would."""

    def run_cli(argv):
        calls.append(argv)
        if rc:
            return rc, out
        if argv[0] == "answer" and "--done" not in argv:
            item = {"qid": argv[2]}
            if "--defer" in argv:
                item["defer"] = True
            elif "--other" in argv:
                item["other"] = argv[argv.index("--other") + 1]
            elif "--option" in argv:
                item["option"] = argv[argv.index("--option") + 1]
            if "--note" in argv:
                item["note"] = argv[argv.index("--note") + 1]
            if "--accept-recommended" in argv:
                questions.apply(store, job_id, accept_recommended=True, origin="moshi")
            else:
                questions.apply(store, job_id, answers=[item], origin="moshi")
        elif argv[0] == "answer" and "--accept-recommended" in argv:
            questions.apply(store, job_id, accept_recommended=True, origin="moshi")
        return 0, out

    return run_cli


def _setup(steward_home, n: int = 5, *, state=JobState.RUNNING, run_cli=None, records=None, **kw):
    store, job_id = make_store_job()
    set_state(store, job_id, state)
    seed(store, job_id, records if records is not None else _records(n, **kw))
    calls: list[list[str]] = []
    if run_cli is None:
        run_cli = _applier(store, job_id, calls)
    p = phone.Phone(store, run_cli=run_cli, tokens_fn=lambda s: None)
    return p, store, job_id, calls


def _text(p, width=100, rows=40):
    return [phone.plain(l) for l in p.lines(width, rows)]


def _open_q(p):
    p.handle("1")
    p.handle("Q")


def _press(p, *keys):
    for k in keys:
        p.handle(k)


def _type(p, text):
    for ch in text:
        p.handle(ch)


def _waiting(store, job_id):
    return questions.summary(questions.load(store, job_id))["waiting"]


# -- task 1 --------------------------------------------------------------------------------------


def test_q_from_job_screen_opens_the_first_card(steward_home):
    p, store, job_id, _ = _setup(steward_home)
    _open_q(p)
    assert p.screen == "q"
    text = "\n".join(_text(p))
    assert f"QUESTIONS {job_id[:8]}" in text and "1/4" in text and "5 waiting" in text
    assert "G2" in text and "Question G2" in text  # blocking card first
    assert "blocks work" in text and "why: because" in text
    lines = _text(p)
    i1 = next(i for i, t in enumerate(lines) if "[1]★ Option a" in t)
    assert "[2]" in lines[i1 + 1] and "Option b" in lines[i1 + 1]
    for word in ("1-4 pick", "n note", "o other", "d defer", "enter save+next", "s skip", "e evidence",
                 "A accept all", "b back"):
        assert word in text


def test_enter_answers_the_star_and_advances(steward_home):
    p, store, job_id, calls = _setup(steward_home)
    _open_q(p)
    p.handle("enter")
    assert calls == [["answer", job_id, "gsd_context:17-CONTEXT.md:G2", "--option", "recommended", "--origin", "moshi"]]
    assert "saved" in _text(p)[-3:] or any(t == "saved" for t in _text(p))
    assert "Question G1" in "\n".join(_text(p)) and "2/4" in "\n".join(_text(p))
    assert len(calls) == 1


def test_failed_write_keeps_the_card(steward_home):
    run_cli, _ = _recorder(1, "boom")
    p, store, job_id, _ = _setup(steward_home, run_cli=run_cli)  # a bare recorder: nothing is applied
    _open_q(p)
    p.handle("enter")
    text = _text(p)
    assert "failed: boom" in text and "Question G2" in "\n".join(text) and "1/4" in "\n".join(text)


@pytest.mark.parametrize("width", [40, 60, 100])
@pytest.mark.parametrize("rows", [12, 30])
def test_render_fits_every_width_and_row_budget(steward_home, width, rows):
    p, *_ = _setup(steward_home)
    _open_q(p)
    p.handle("e")
    lines = p.lines(width, rows)
    assert len(lines) <= rows
    assert all(len(phone.plain(l)) <= width for l in lines)


def test_no_waiting_questions_gives_a_note(steward_home):
    p, store, job_id, _ = _setup(steward_home, records=[make_record(status="answered")])
    p.handle("1")
    p.handle("Q")
    assert p.screen == "job" and "no open questions" in _text(p)


def test_question_text_is_stripped_of_escape_sequences(steward_home):
    rec = make_record(title="evil \x1b[31mred\x07 title")
    p, *_ = _setup(steward_home, records=[rec])
    _open_q(p)
    assert all("\x1b" not in t and "\x07" not in t for t in _text(p))


# -- task 2: keys, rounds, defer, notes, stale cards, flush ------------------------------------------


def _qid(n: int) -> str:
    return f"gsd_context:17-CONTEXT.md:G{n}"


def test_option_keys_note_other_and_defer(steward_home):
    p, store, job_id, calls = _setup(steward_home, n=4, blocking_at=None)
    _open_q(p)
    _press(p, "2", "enter")  # the 2nd displayed option of G1
    assert calls[-1] == ["answer", job_id, _qid(1), "--option", "b", "--origin", "moshi"]
    p.handle("n")
    assert p.mode == "input" and any(t.startswith("note> ") for t in _text(p))
    _type(p, "watch out")
    _press(p, "enter")
    assert p.mode is None and len(calls) == 1
    _press(p, "enter")
    assert calls[-1] == ["answer", job_id, _qid(2), "--option", "recommended", "--note", "watch out", "--origin", "moshi"]
    p.handle("o")
    assert any(t.startswith("other> ") for t in _text(p))
    _type(p, "my own")
    _press(p, "enter")
    _press(p, "enter")
    assert calls[-1] == ["answer", job_id, _qid(3), "--other", "my own", "--origin", "moshi"]
    p.handle("o")
    _press(p, "enter")  # empty text cancels
    assert p.mode is None and len(calls) == 3 and "Question G4" in "\n".join(_text(p))
    p.handle("d")
    assert any(t.startswith("defer reason (Enter = none)> ") for t in _text(p))
    _press(p, "enter")  # empty Enter defers
    assert calls[-1] == ["answer", job_id, _qid(4), "--defer", "--origin", "moshi"]


def test_defer_with_a_reason_and_esc_cancels(steward_home):
    p, store, job_id, calls = _setup(steward_home, n=3, blocking_at=None)
    _open_q(p)
    p.handle("d")
    _type(p, "later")
    p.handle("enter")
    assert calls[-1] == ["answer", job_id, _qid(1), "--defer", "--note", "later", "--origin", "moshi"]
    for key in ("n", "o", "d"):
        p.handle(key)
        _type(p, "x")
        p.handle("esc")
        assert p.mode is None
    assert len(calls) == 1 and p.screen == "q"


def test_skip_and_navigation(steward_home):
    p, store, job_id, calls = _setup(steward_home, n=4, blocking_at=None)
    _open_q(p)
    p.handle("3")
    p.handle("s")
    assert "2/4" in "\n".join(_text(p)) and "Question G2" in "\n".join(_text(p))
    p.handle("j")
    p.handle("down")
    assert "4/4" in "\n".join(_text(p))
    p.handle("k")
    p.handle("up")
    p.handle("up")
    lines = _text(p)
    assert "1/4" in "\n".join(lines)
    assert any(t.startswith("▶[3]") for t in lines)  # the selection survived moving away and back
    assert calls == []
    # a tap on an option row presses its digit key
    shown = p.lines(100, 40)
    row = next(i for i, l in enumerate(shown, 1) if phone.plain(l).startswith(" [2]"))
    p.tap(3, row, shown)
    assert any(t.startswith("▶[2]") for t in _text(p))


def test_rounds_of_four_and_summary(steward_home):
    p, store, job_id, calls = _setup(steward_home, n=10, blocking_at=None)
    _open_q(p)
    assert "round 1/3" in "\n".join(_text(p))
    p.handle("s")  # skipped: stays waiting
    _press(p, "enter", "enter", "enter")
    text = "\n".join(_text(p))
    assert "ROUND 1 DONE · 3 answered · 1 skipped · 7 left" in text
    p.handle("enter")  # round 2: the skipped card comes back first, then the next three waiting
    text = "\n".join(_text(p))
    assert "round 2/3" in text and "1/4" in text and "Question G1" in text
    _press(p, "enter", "enter", "enter", "enter")
    assert "ROUND 2 DONE · 4 answered · 3 left" in "\n".join(_text(p))
    p.handle("enter")
    text = "\n".join(_text(p))
    assert "round 3/3" in text and "1/3" in text
    _press(p, "enter", "enter", "enter")
    assert "ROUND 3 DONE · 3 answered · 0 left" in "\n".join(_text(p)) and "all answered" in "\n".join(_text(p))
    p.handle("enter")
    assert "all answered" in _text(p) and p.q_stage == "summary"


def test_deferred_cards_leave_the_waiting_list(steward_home):
    p, store, job_id, calls = _setup(steward_home, n=5, blocking_at=None)
    _open_q(p)
    _press(p, "d", "enter")  # G1 deferred
    assert _waiting(store, job_id) == 4
    _press(p, "enter", "enter", "enter")
    text = "\n".join(_text(p))
    assert "ROUND 1 DONE · 4 answered · 1 left" in text  # deferred cards count as saved, then leave the waiting list
    p.handle("enter")
    assert "1/1" in "\n".join(_text(p)) and "Question G5" in "\n".join(_text(p))
    recs = {r["qid"]: r["status"] for r in questions.load(store, job_id)}
    assert recs[_qid(1)] == "deferred"


def test_a_round_can_stop_anywhere(steward_home):
    p, store, job_id, calls = _setup(steward_home, n=6, blocking_at=None)
    _open_q(p)
    _press(p, "enter", "enter")
    p.handle("b")
    assert p.screen == "job" and len(calls) == 3  # two answers and the one --done
    assert calls[-1] == ["answer", job_id, "--done", "--origin", "moshi"]


def test_leaving_after_answers_sends_done_once(steward_home):
    p, store, job_id, calls = _setup(steward_home, n=6, blocking_at=None)
    _open_q(p)
    p.handle("b")  # nothing saved: no --done
    assert calls == [] and p.screen == "job"
    p.handle("Q")
    _press(p, "enter", "enter", "esc")
    assert [c for c in calls if "--done" in c] == [["answer", job_id, "--done", "--origin", "moshi"]]
    p.handle("Q")
    _press(p, "enter", "enter", "enter", "enter")  # a whole round: the summary screen
    assert p.q_stage == "summary"
    p.handle("f")
    assert "written" in "\n".join(_text(p))
    n = len([c for c in calls if "--done" in c])
    p.handle("b")
    assert len([c for c in calls if "--done" in c]) == n  # already flushed


def test_c_on_summary_is_write_and_continue_only_for_needs_human(steward_home):
    p, store, job_id, calls = _setup(steward_home, n=2, blocking_at=None, state=JobState.NEEDS_HUMAN)
    _open_q(p)
    _press(p, "enter", "enter")
    assert p.q_stage == "summary"
    p.handle("c")
    assert calls[-1] == ["answer", job_id, "--done", "--and-continue", "--origin", "moshi"]


def test_c_on_a_running_job_only_notes(steward_home):
    p, store, job_id, calls = _setup(steward_home, n=2, blocking_at=None)
    _open_q(p)
    _press(p, "enter", "enter", "c")
    assert "only a NEEDS_HUMAN job can continue" in _text(p)
    assert not any("--done" in c for c in calls)


def test_stale_card_preselects_the_old_answer(steward_home):
    stale = make_record(_qid(1), status="stale")
    stale["answer"] = {"option": "b", "label": "Option b", "other": "", "note": "", "by": "sam"}
    stale["stale_from"] = {"hash": "h0", "answer": stale["answer"], "to_hash": "h1"}
    p, store, job_id, calls = _setup(steward_home, records=[stale])
    _open_q(p)
    text = _text(p)
    assert "stale: was Option b" in text
    p.handle("enter")
    assert calls == [["answer", job_id, _qid(1), "--option", "b", "--origin", "moshi"]]


def test_unstarred_card_needs_a_pick(steward_home):
    unstarred = make_record(_qid(2), starred=False, recommended=())
    p, store, job_id, calls = _setup(steward_home, records=[unstarred])
    _open_q(p)
    p.handle("enter")
    assert "pick an option, or o for other text" in _text(p) and calls == []


def test_updated_and_blocking_badges(steward_home):
    rec = make_record(_qid(1), blocking=True)
    rec["flags"]["updated"] = True
    p, *_ = _setup(steward_home, records=[rec])
    _open_q(p)
    text = "\n".join(_text(p))
    assert "blocks work" in text and "(updated)" in text
    lines = p.lines(40, 12)
    plain = [phone.plain(l) for l in lines]
    assert len(lines) <= 12 and any("[1]★" in t for t in plain) and any("b back" in t for t in plain)


def test_evidence_toggle(steward_home):
    p, *_ = _setup(steward_home)
    _open_q(p)
    assert "steward/x.py:1" not in "\n".join(_text(p))
    p.handle("e")
    assert "steward/x.py:1" in "\n".join(_text(p))
    p.handle("e")
    assert "steward/x.py:1" not in "\n".join(_text(p))


def test_input_purposes_never_route_to_steer(steward_home):
    p, store, job_id, calls = _setup(steward_home, n=2, blocking_at=None)
    _open_q(p)
    p.handle("n")
    _type(p, "x")
    p.handle("enter")
    assert not any(c[0] in ("steer", "send-back") for c in calls)


# -- task 3: accept-all review, and the queue on HOME and JOB ----------------------------------------


def _fifteen(store, job_id):
    recs = [make_record(_qid(i), line=i, starred=(i != 9), recommended=() if i == 9 else ("a",)) for i in range(1, 16)]
    seed(store, job_id, recs)


def test_accept_all_shows_the_review_and_needs_a_second_key(steward_home):
    p, store, job_id, calls = _setup(steward_home)
    _fifteen(store, job_id)
    p.refresh()
    _open_q(p)
    p.handle("A")
    assert calls == []
    assert "accept 14 recommended, 1 needs you (G9)? y/n" in _text(p, 100)
    p.handle("n")
    assert calls == [] and p.mode is None and "cancelled" in _text(p)
    p.handle("A")
    p.handle("esc")
    assert calls == [] and p.mode is None
    p.handle("A")
    p.handle("y")
    assert calls == [["answer", job_id, "--accept-recommended", "--origin", "moshi"]]
    assert _waiting(store, job_id) == 1
    p.handle("A")
    assert p.mode is None and "nothing to accept" in _text(p) and len(calls) == 1


def test_accept_all_counts_toward_the_flush_on_leaving(steward_home):
    p, store, job_id, calls = _setup(steward_home)
    _open_q(p)
    _press(p, "A", "y", "b")
    assert calls[-1] == ["answer", job_id, "--done", "--origin", "moshi"] and p.screen == "job"


def test_review_line():
    def recs(n_star, n_no=0, *, extra=()):
        out = [make_record(f"gsd_context:x.md:G{i}", line=i) for i in range(1, n_star + 1)]
        out += [make_record(f"gsd_context:x.md:N{i}", line=100 + i, starred=False, recommended=()) for i in range(1, n_no + 1)]
        return out + list(extra)

    assert questions.review_line(recs(14, 1)) == "accept 14 recommended, 1 needs you (N1)"
    assert questions.review_line(recs(15)) == "accept 15 recommended"
    assert questions.review_line(recs(0, 2)) == "nothing to accept"
    assert questions.review_line([]) == "nothing to accept"
    assert questions.review_line(recs(2, 5)) == "accept 2 recommended, 5 needs you (N1, N2, N3, +2)"
    not_open = [make_record(f"gsd_context:x.md:S{i}", status=st, line=200 + i)
                for i, st in enumerate(("stale", "answered", "deferred", "withdrawn"), 1)]
    line = questions.review_line(recs(3, extra=not_open))
    assert line == "accept 3 recommended, 1 needs you (S1)"  # only the stale card is waiting; it is not accepted


def test_reason_precedence():
    from steward import jobview

    ask = [{"status": "pending", "kind": "permission", "question": "worker permission: Bash: rm -rf x"}]
    summ = {"waiting": 15, "starred": 14}
    events = [{"what": "checks passed; acceptance unresolved", "evidence": {"reasons": ["Sam accepts"]}}]
    assert jobview.reason(ask, "RUNNING", [], questions=summ).startswith("approve? ")
    assert jobview.reason([], "NEEDS_HUMAN", events, questions=summ).startswith("accept or reject? ")
    assert jobview.reason([], "RUNNING", [], questions=summ) == "answer 15 questions (14 ★)"
    assert jobview.reason([], "RUNNING", [], questions={"waiting": 1, "starred": 1}) == "answer 1 question (1 ★)"
    assert jobview.reason([], "RUNNING", [], questions={"waiting": 0, "starred": 0}) is None
    assert jobview.reason([], "RUNNING", []) is None
    assert jobview.reason([], "NEEDS_HUMAN", events).startswith("accept or reject? ")


def test_home_shows_the_queue(steward_home):
    p, store, job_id, _ = _setup(steward_home)
    _fifteen(store, job_id)
    p.refresh()
    text = _text(p, 100)
    assert any(t.startswith("    ◆ answer 15 questions (14 ★)") for t in text)
    assert "Q questions" in "\n".join(text)
    p.handle("Q")
    assert p.screen == "q" and p.q_job == job_id


def test_home_q_with_two_jobs_asks_which_and_with_none_notes(steward_home):
    p, store, job_a, calls = _setup(steward_home)
    store2 = store
    job_b = store2.create_job("another", "/nonexistent-repo").job_id
    set_state(store2, job_b, JobState.RUNNING)
    seed(store2, job_b, [make_record(_qid(1))])
    p.refresh()
    p.handle("Q")
    assert p.mode == "pick" and any("questions which job?" in t for t in _text(p))
    p.handle("2")
    assert p.screen == "q" and p.q_job in (job_a, job_b)
    # no queue anywhere: a note, no Q hint
    store3, job_c = make_store_job()
    set_state(store3, job_c, JobState.RUNNING)
    questions.save(store3, job_a, [])
    questions.save(store3, job_b, [])
    q = phone.Phone(store3, run_cli=_recorder()[0], tokens_fn=lambda s: None)
    assert "Q questions" not in "\n".join(_text(q))
    q.handle("Q")
    assert "no open questions" in _text(q)


def test_job_screen_hint_and_acceptance(steward_home):
    p, store, job_id, _ = _setup(steward_home)
    p.handle("1")
    assert "Q answer" in "\n".join(_text(p))
    p2_store, p2_job = make_store_job()
    set_state(p2_store, p2_job, JobState.NEEDS_HUMAN)
    seed(p2_store, p2_job, _records(3))
    p2_store.append_event(p2_job, what="checks passed; acceptance unresolved", evidence={"reasons": ["Sam accepts"]},
                          rule="r", judgment=Judgment.code)
    jobs = [j for j in phone.gather(p2_store).jobs if j.job_id == p2_job]
    assert jobs[0].ask.startswith("accept or reject?")  # the acceptance ask keeps the ask line
    assert jobs[0].questions["waiting"] == 3


def test_job_without_a_queue_has_the_old_hints(steward_home):
    p, store, job_id, _ = _setup(steward_home, records=[])
    p.handle("1")
    text = "\n".join(_text(p))
    assert "Q answer" not in text and "s steer" in text
    p.handle("Q")
    assert "no open questions" in _text(p)


def test_gather_reads_the_summary_cheaply(steward_home):
    p, store, job_id, _ = _setup(steward_home)
    before = len(store.events(job_id))
    snap = phone.gather(store)
    info = next(j for j in snap.jobs if j.job_id == job_id)
    assert info.questions["waiting"] == 5 and info.ask == "answer 5 questions (5 ★)"
    assert len(store.events(job_id)) == before


@pytest.mark.parametrize("screen", ["home", "job"])
def test_width_40_home_and_job(steward_home, screen):
    p, store, job_id, _ = _setup(steward_home)
    _fifteen(store, job_id)
    p.refresh()
    if screen == "job":
        p.handle("1")
    lines = p.lines(40, 30)
    assert all(len(phone.plain(l)) <= 40 for l in lines)


# -- task 4: steward phone --questions JOB and its Herdr tab -------------------------------------------


def _res(argv, out="", code=0):
    from steward.runner_exec import CommandResult

    return CommandResult(argv=argv, cwd="", exit_code=code, stdout=out, stderr="", duration_s=0)


def _herdr(label=None):
    import json

    from steward.runner_exec import FakeRunner

    tabs = [{"label": "other", "tab_id": "w1:t1", "workspace_id": "w1"}]
    panes = [{"pane_id": "w1:p1", "tab_id": "w1:t1"}]
    if label:
        tabs.append({"label": label, "tab_id": "w1:t9", "workspace_id": "w1"})
        panes.append({"pane_id": "w1:p9", "tab_id": "w1:t9", "label": label})

    def respond(argv, cwd):
        key = " ".join(argv[:3])
        if key == "herdr tab list":
            return _res(argv, json.dumps({"result": {"tabs": tabs}}))
        if key == "herdr pane list":
            return _res(argv, json.dumps({"result": {"panes": panes}}))
        if key == "herdr tab create":
            return _res(argv, json.dumps({"result": {"tab": {"tab_id": "w1:t7"}, "root_pane": {"pane_id": "w1:p7"}}}))
        return _res(argv)

    return FakeRunner(respond)


def test_phone_command_carries_the_job():
    plain_cmd = phone.phone_command()
    cmd = phone.phone_command(questions_job="abc")
    assert cmd.endswith("steward.cli phone --questions abc") and "cd " in cmd
    assert plain_cmd.endswith("-m steward.cli phone")
    assert cmd.split("steward.cli")[0] == plain_cmd.split("steward.cli")[0]


def test_open_questions_tab_creates_and_runs():
    r = _herdr()
    rc, msg = phone.open_questions_tab(r, "w1", "abcdef1234567890")
    assert rc == 0 and msg.splitlines()[-1] == "link: moshi://herdr?workspace=w1&tab=w1%3At7&pane=w1%3Ap7"
    argvs = [c["argv"] for c in r.calls]
    create = next(a for a in argvs if a[:3] == ["herdr", "tab", "create"])
    assert create[create.index("--label") + 1] == "? questions abcdef12" and "--no-focus" in create
    assert ["herdr", "pane", "rename", "w1:p7", "? questions abcdef12"] in argvs
    run = next(a for a in argvs if a[:3] == ["herdr", "pane", "run"])
    assert run[3] == "w1:p7" and run[4].endswith("steward.cli phone --questions abcdef1234567890")


def test_open_questions_tab_focuses_the_existing_one():
    r = _herdr("? questions abcdef12")
    rc, msg = phone.open_questions_tab(r, "w1", "abcdef1234567890")
    argvs = [c["argv"] for c in r.calls]
    assert rc == 0 and ["herdr", "tab", "focus", "w1:t9"] in argvs
    assert not any(a[:3] == ["herdr", "tab", "create"] for a in argvs)


def test_open_questions_tab_dry_run_and_no_workspace():
    r = _herdr()
    rc, msg = phone.open_questions_tab(r, "w1", "abcdef1234567890", dry_run=True)
    assert rc == 0 and r.calls == [] and "herdr tab create" in msg and "? questions abcdef12" in msg
    assert phone.open_questions_tab(_herdr(), None, "abcdef12")[0] == 2
    assert phone.open_questions_tab(_herdr(), None, "abcdef12")[1] == phone.open_phone_tab(_herdr(), None)[1]


def test_run_tui_starts_in_q_mode(steward_home):
    p, store, job_id, _ = _setup(steward_home)
    q = phone.start_phone(job_id, store)
    assert q.screen == "q" and q.q_job == job_id
    store2, empty = make_store_job()
    set_state(store2, empty, JobState.RUNNING)
    h = phone.start_phone(empty, store2)
    assert h.screen == "home" and "no open questions" in [phone.plain(l) for l in h.lines(80, 30)]
    assert phone.start_phone(None, store2).screen == "home"


# -- questions as a pane inside the job's own tab (tab design 2026-09-30) ------------------------------------------


def _job_tab_herdr(extra_panes=()):
    import json

    from steward.runner_exec import FakeRunner

    tabs = [{"label": "● P18 ETA", "tab_id": "w1:t5", "workspace_id": "w1"}]
    panes = [{"pane_id": "w1:p50", "tab_id": "w1:t5", "label": "runner abcdef12"},
             {"pane_id": "w1:p51", "tab_id": "w1:t5", "label": "watchers abcdef12"}, *extra_panes]

    def respond(argv, cwd):
        key = " ".join(argv[:3])
        if key == "herdr tab list":
            return _res(argv, json.dumps({"result": {"tabs": tabs}}))
        if key == "herdr pane list":
            return _res(argv, json.dumps({"result": {"panes": panes}}))
        if key == "herdr pane split":
            return _res(argv, json.dumps({"result": {"pane": {"pane_id": "w1:p77"}}}))
        return _res(argv)

    return FakeRunner(respond)


def _store_with_tab(steward_home):
    from steward.store import Store

    store = Store(steward_home)
    job = store.create_job("goal", "/repo", job_id="abcdef12-0000-0000-0000-000000000000")
    store.kv_set(job.job_id, "herdr_tab", "w1:t5")
    return store, job


def test_questions_open_as_a_pane_split_down_off_watchers_in_the_job_tab(steward_home):
    store, job = _store_with_tab(steward_home)
    r = _job_tab_herdr()
    rc, msg = phone.open_questions_pane(r, "w1", job.job_id, store=store)
    argvs = [c["argv"] for c in r.calls]
    split = next(a for a in argvs if a[:3] == ["herdr", "pane", "split"])
    assert rc == 0 and split[split.index("--pane") + 1] == "w1:p51" and split[split.index("--direction") + 1] == "down"
    assert ["herdr", "pane", "rename", "w1:p77", "questions abcdef12"] in argvs
    run = next(a for a in argvs if a[:3] == ["herdr", "pane", "run"])
    assert run[3] == "w1:p77" and run[4].endswith("steward.cli phone --questions " + job.job_id)
    assert not any(a[:3] == ["herdr", "tab", "create"] for a in argvs)
    assert msg.splitlines()[-1] == "link: moshi://herdr?workspace=w1&tab=w1%3At5&pane=w1%3Ap77"


def test_questions_pane_focuses_the_existing_one(steward_home):
    store, job = _store_with_tab(steward_home)
    r = _job_tab_herdr([{"pane_id": "w1:p77", "tab_id": "w1:t5", "label": "questions abcdef12"}])
    rc, msg = phone.open_questions_pane(r, "w1", job.job_id, store=store)
    argvs = [c["argv"] for c in r.calls]
    assert rc == 0 and ["herdr", "tab", "focus", "w1:t5"] in argvs
    assert not any(a[:3] == ["herdr", "pane", "split"] for a in argvs)


def test_questions_pane_falls_back_to_a_tab_when_the_job_has_none(steward_home):
    from steward.store import Store

    store = Store(steward_home)
    job = store.create_job("goal", "/repo", job_id="abcdef12-0000-0000-0000-000000000000")
    r = _herdr()
    rc, msg = phone.open_questions_pane(r, "w1", job.job_id, store=store)
    assert rc == 0 and any(c["argv"][:3] == ["herdr", "tab", "create"] for c in r.calls)


def test_questions_pane_dry_run_touches_no_herdr(steward_home):
    store, job = _store_with_tab(steward_home)
    r = _job_tab_herdr()
    rc, msg = phone.open_questions_pane(r, "w1", job.job_id, store=store, dry_run=True)
    assert rc == 0 and r.calls == [] and "tab w1:t5" in msg
