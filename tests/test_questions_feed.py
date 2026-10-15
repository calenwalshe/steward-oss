"""The feed lines for the question queue and the operator's result line (Phase 17-06: G3, G4, G9, G13)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from questions_helpers import make_record, make_store_job, phase16_qid, scan_fixture, seed, set_state

from steward import cli, phone, questions, session, watchers
from steward.models import JobState

QID = "gsd_context:16-CONTEXT.md:G4"


def _ev(what, evidence, seq=1):
    return SimpleNamespace(what=what, evidence=evidence, seq=seq, ts="2026-09-30T12:00:00+00:00")


def _fmt(what, evidence):
    item = watchers.format_event(_ev(what, evidence))
    assert item is not None, what
    assert item[0] == "question"
    assert "\n" not in item[1]
    return item[1]


def test_feed_formats_every_question_event():
    assert _fmt("question_answered", {"qid": QID, "option": "recommended", "origin": "moshi"}) == \
        "G4 answered: recommended (moshi)"
    assert _fmt("question_answered", {"qid": "gsd_context:16-CONTEXT.md:G15", "option": "other",
                                      "other": "replay + live trial", "origin": "moshi"}) == \
        'G15 answered: other "replay + live trial" (moshi)'
    line = _fmt("question_answered", {
        "qid": QID, "option": "b", "origin": "dash", "note": "n" * 300,
        "changed": {"before": {"option": "a", "label": "Option a", "other": "", "note": ""},
                    "after": {"option": "b", "label": "Option b", "other": "", "note": ""}}})
    assert line.startswith("G4 answered: b (dash)") and "was a" in line and "note:" in line and len(line) < 200
    changed_other = _fmt("question_answered", {
        "qid": QID, "option": "b", "origin": "chatgpt",
        "changed": {"before": {"option": "other", "other": "my way"}, "after": {"option": "b"}}})
    assert 'was other "my way"' in changed_other and "(chatgpt)" in changed_other
    assert _fmt("question_deferred", {"qid": QID, "origin": "dash"}) == "G4 deferred (dash)"
    assert _fmt("question_stale", {"qid": QID}) == "G4 stale: the area changed after your answer"
    assert _fmt("question_updated", {"qid": QID}).startswith("G4 updated")
    assert "BLOCKS WORK" in _fmt("question_updated", {"qid": QID, "blocking": True})
    assert _fmt("question_withdrawn", {"qid": QID, "file": "16-CONTEXT.md"}).startswith("G4 withdrawn")
    assert _fmt("question_worker_decided", {"qid": QID, "marker_who": "worker"}) == \
        "G4 decided in the file by the worker, not by you"
    found = _fmt("questions_found", {"file": "16-CONTEXT.md", "qids": [QID, "gsd_context:16-CONTEXT.md:G5"]})
    assert "2 question(s)" in found and "G4, G5" in found
    assert "BLOCKS WORK: G5" in _fmt("questions_found", {
        "file": "x", "qids": [QID, "gsd_context:x:G5"], "blocking": ["gsd_context:x:G5"]})
    assert "abc1234" in _fmt("questions_written", {"commit": "abc1234def", "qids": [QID], "by": "dash"})
    assert "main" in _fmt("questions_written", {"commit": "abc1234def", "qids": [QID], "branch": "main", "by": "dash"})
    assert "steer" in _fmt("questions_delivered", {"steering_rev": 7, "qids": [QID], "mode": "steer", "by": "dash"})
    assert "rev 7" in _fmt("questions_delivered", {"steering_rev": 7, "qids": [QID], "mode": "send_back", "by": "dash"})
    assert _fmt("questions_delivery_skipped", {"reason": "dirty 16-CONTEXT.md"}) == \
        "delivery skipped: dirty 16-CONTEXT.md"
    assert "boom" in _fmt("questions_scan_error", {"error": "boom"})


def test_feed_is_tolerant_of_missing_keys():
    for what in ("question_answered", "question_deferred", "questions_found", "question_updated", "question_stale",
                 "question_withdrawn", "question_worker_decided", "questions_written", "questions_delivered",
                 "questions_delivery_skipped", "questions_scan_error"):
        for evidence in ({}, None, {"qid": None, "qids": None, "changed": "junk", "note": 5}):
            text = _fmt(what, evidence)
            assert text
    assert "?" in _fmt("question_answered", {})


def test_render_line_and_colours():
    ev = _ev("question_answered", {"qid": QID, "option": "a", "origin": "dash"})
    role, text = watchers.format_event(ev)
    line = watchers.render_line(ev, role, text, width=100, color=False)
    assert " question " in line and "G4 answered: a (dash)" in line
    coloured = watchers.render_line(ev, role, text, width=100, color=True)
    assert f"\x1b[{watchers._COLORS['question']}mquestion\x1b[0m" in coloured
    row = phone._feed_row(ev, role, text)
    assert ("question ", watchers._COLORS["question"]) in row and (text, "") in row


def test_collect_includes_them_in_seq_order():
    events = [_ev("question_answered", {"qid": QID, "option": "a", "origin": "cli"}, 1),
              _ev("pause instruction recorded", {"rev": 2, "origin": "cli", "text": "x"}, 2),
              _ev("question_stale", {"qid": QID}, 3)]
    got = watchers.collect(events)
    assert [r for _e, r, _t in got] == ["question", "steer", "question"]
    assert [e.seq for e, _r, _t in watchers.collect(events, since=1)] == [2, 3]


def test_steering_from_questions_shows_its_origin():
    role, text = watchers.format_event(_ev("focus instruction recorded", {"rev": 3, "origin": "questions", "text": "SAM ANSWERED 2"}))
    assert role == "steer" and "[questions]" in text


def test_real_events_reach_the_feed(steward_home):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record(QID, file="16-CONTEXT.md")])
    assert questions.apply(store, job_id, answers=[{"qid": QID, "option": "a"}], origin="moshi")["accepted"]
    assert questions.apply(store, job_id, answers=[{"qid": QID, "option": "b"}], origin="dash")["accepted"]
    lines = [t for _e, r, t in watchers.collect(store.events(job_id)) if r == "question"]
    assert lines == ["G4 answered: a (moshi)", "G4 answered: b (dash) · was a"]


def test_scan_events_carry_blocking(steward_home):
    store, job_id = make_store_job()
    scan_fixture(store, job_id)
    found = [t for _e, r, t in watchers.collect(store.events(job_id)) if r == "question"]
    assert found and all(t.startswith("found") for t in found)


# -- the operator's result line ----------------------------------------------------------------------------------


def test_result_reports_the_queue(steward_home, capsys):
    store, job_id = make_store_job()
    set_state(store, job_id, JobState.RUNNING)
    assert session.build_result(store, job_id)["questions"] is None
    seed(store, job_id, [make_record(phase16_qid(n), starred=n != 3, recommended=("a",) if n != 3 else (),
                                     blocking=n == 1, line=n) for n in range(1, 6)])
    r = session.build_result(store, job_id)
    assert r["questions"] == {"waiting": 5, "starred": 4, "needs_you": 1}
    assert f"steward questions {job_id} --json" in r["next_commands"]
    assert any("wait" in c for c in r["next_commands"])  # the existing commands stay
    text = session.render_result(r)
    assert f"questions: 5 waiting (4 ★) - steward questions {job_id} --json" in text

    assert cli.main(["result", job_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["questions"]["waiting"] == 5
    assert cli.main(["result", job_id]) == 0
    assert "questions: 5 waiting" in capsys.readouterr().out


def test_next_commands_three_arguments_unchanged():
    S = session._next_commands
    jid = "j" * 36
    for state in JobState:
        assert S(state, jid, False) == S(state, jid, False, 0)
        assert not any("questions" in c for c in S(state, jid, False))
    assert S(JobState.NEEDS_HUMAN, jid, False, 3)[-1] == f"steward questions {jid} --json"
    assert S(JobState.RUNNING, jid, False, 3)[-1] == f"steward questions {jid} --json"


def test_result_without_questions_is_unchanged(steward_home, capsys):
    store, job_id = make_store_job()
    set_state(store, job_id, JobState.RUNNING)
    text = session.render_result(session.build_result(store, job_id))
    assert "questions:" not in text
    seed(store, job_id, [make_record(status="answered")])
    assert session.build_result(store, job_id)["questions"] is None


def test_operator_json_feeds_rounds_of_4(steward_home, capsys):
    store, job_id = make_store_job()
    set_state(store, job_id, JobState.RUNNING)
    seed(store, job_id, [make_record(f"gsd_context:17-CONTEXT.md:G{n}", file="17-CONTEXT.md", line=n)
                         for n in range(1, 10)])
    assert cli._build_parser().parse_args(["questions", job_id, "--json", "--round", "2"]).round == 2
    assert cli.main(["questions", job_id, "--json", "--round", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [q["anchor"] for q in out["questions"]] == ["G5", "G6", "G7", "G8"]
    assert out["round"] == 2 and out["rounds"] == 3


def test_blocking_questions_show_in_the_found_line(steward_home):
    from questions_helpers import scan_files

    store, job_id = make_store_job()
    doc = {"questions": [{"id": "db", "question": "Which DB?", "blocking": True, "blocking_reason": "no schema yet",
                          "options": ["pg", "sqlite"], "recommended": "pg"},
                         {"id": "port", "question": "Which port?", "options": ["80", "8080"]}]}
    scan_files(store, job_id, {"STEWARD-QUESTIONS.json": json.dumps(doc)})
    (ev,) = [e for e in store.events(job_id) if e.what == "questions_found"]
    assert ev.evidence["blocking"] == ["steward_questions:STEWARD-QUESTIONS.json:db"]
    (line,) = [t for _e, r, t in watchers.collect(store.events(job_id)) if r == "question"]
    assert "BLOCKS WORK: db" in line
