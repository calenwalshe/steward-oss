"""Write-back rendering (Phase 17-02, G5/G14): answers in the store become deterministic edits of COMMITTED text."""

from __future__ import annotations

import re
from datetime import datetime

from questions_helpers import CTX, LOG, commit_file, fixture_text, git, make_store_job, scan_fixture

from steward import questions
from steward.question_adapters.gsd_context import GsdContext
from steward.runner_exec import SubprocessRunner
from steward.question_adapters.steward_questions import _answer_row

D = datetime(2026, 9, 29, 10, 0, 0)
NOTE = ("before acceptance the efficiency watcher must also run observe-only (T0) on at least one live Steward job "
        "for at least one unit boundary, with its findings shown on the status pane and no nudges sent.")
BEFORE = fixture_text("phase16_context_before.md")
AFTER = fixture_text("phase16_context_after.md")
LOG_BEFORE = fixture_text("phase16_log_before.md")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _qid(n: int) -> str:
    return f"gsd_context:16-CONTEXT.md:G{n}"


def _scanned(steward_home):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    return store, job_id, wt


def _answer(store, job_id, spec):
    """spec: {n: kwargs}; returns [(record, answer)] in area order."""
    questions.apply(store, job_id, answers=[{"qid": _qid(n), **kw} for n, kw in spec.items()], now=D)
    recs = {r["qid"]: r for r in questions.load(store, job_id)}
    return [(recs[_qid(n)], recs[_qid(n)]["answer"]) for n in sorted(spec)]


def _phase16(store, job_id, note=NOTE):
    spec = {n: {"option": "recommended"} for n in range(1, 15)}
    spec[15] = {"other": "replay + live trial", "note": note}
    return _answer(store, job_id, spec)


def _plan(items, texts=None, date="2026-09-29"):
    return questions.writeback_for_files(texts or {CTX: BEFORE, LOG: LOG_BEFORE}, items, date)


def _edit(plan, path):
    return next(e for e in plan.edits if e.path == path)


def _headers(text):
    return [ln for ln in text.splitlines() if ln.startswith("### G")]


def _status(text):
    lines = text.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith("**Status:**"))
    j = i + 1
    while j < len(lines) and lines[j].strip():
        j += 1
    return lines[i:j]


def test_phase16_headers_equal_6bfcb52(steward_home):
    store, job_id, _ = _scanned(steward_home)
    plan = _plan(_phase16(store, job_id))
    new = _edit(plan, CTX).new_text
    assert _headers(new) == _headers(AFTER)
    assert len(_headers(new)) == 15
    assert _headers(new)[0].endswith("— DECIDED (Sam, 2026-09-29): recommended")
    assert _headers(new)[14].endswith("— DECIDED (Sam, 2026-09-29): replay + live trial")


def test_phase16_status_heading_footer(steward_home):
    store, job_id, _ = _scanned(steward_home)
    new = _edit(_plan(_phase16(store, job_id)), CTX).new_text
    heading = next(ln for ln in new.splitlines() if ln.startswith("## Gray areas"))
    assert _norm(heading) == "## Gray areas — decided defaults (Sam, 2026-09-29)"
    footer = [ln for ln in new.splitlines() if ln.strip()][-1]
    assert _norm(footer) == ("*Phase: 16-efficiency-watcher · context gathered 2026-09-29 · G1–G14 decided: recommended; "
                             "G15 decided: replay + live trial (Sam, 2026-09-29)*")
    assert _norm(footer) == _norm([ln for ln in AFTER.splitlines() if ln.strip()][-1])
    status = _status(new)
    assert all(len(ln) <= 120 for ln in status)
    want = _norm(" ".join(_status(AFTER))).replace(" (recommended replay gate plus an observe-only live trial)", "")
    assert _norm(" ".join(status)) == want
    assert _norm(" ".join(status)).startswith("**Status:** Discussed, no plans. **Decided by Sam 2026-09-29:** G1–G14 accept")


def test_no_open_for_sam_left_and_nothing_else_moves(steward_home):
    store, job_id, _ = _scanned(steward_home)
    new = _edit(_plan(_phase16(store, job_id)), CTX).new_text
    assert "OPEN FOR SAM" not in new
    old_lines, new_lines = BEFORE.split("\n"), new.split("\n")
    import difflib

    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    changed_old, changed_new = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            changed_old += old_lines[i1:i2]
            changed_new += new_lines[j1:j2]
    headers_old = [ln for ln in changed_old if ln.startswith("### G")]
    assert len(headers_old) == 15
    rest_old = [ln for ln in changed_old if not ln.startswith("### G")]
    rest_new = [ln for ln in changed_new if not ln.startswith("### G")]
    # status (3 lines -> wrapped), heading, footer, plus exactly one added note line
    assert sum(1 for ln in rest_new if ln.startswith("- **Added by Sam:**")) == 1
    assert len(rest_old) <= 5 and len(rest_new) <= 6
    added = [ln for ln in rest_new if ln.startswith("- **Added by Sam:**")]
    assert added == [f"- **Added by Sam:** {NOTE}"]


def test_note_line_placement_and_shape(steward_home):
    store, job_id, _ = _scanned(steward_home)
    new = _edit(_plan(_phase16(store, job_id)), CTX).new_text
    lines = new.split("\n")
    idx = [i for i, ln in enumerate(lines) if ln.startswith("- **Added by Sam:**")]
    assert len(idx) == 1 and lines[idx[0]] == f"- **Added by Sam:** {NOTE}"
    assert lines[idx[0] + 1].startswith("- Rationale:")
    g15 = next(i for i, ln in enumerate(lines) if ln.startswith("### G15"))
    assert idx[0] > g15
    # no Rationale bullet: after the area's last non-blank line
    text = "# X\n\n### G1 One — OPEN FOR SAM\n- Options: (a) x; (b) y.\n- **Recommended: (a)**\n\n### G2 Two — OPEN FOR SAM\n- Options: (a) x.\n"
    store2, job2 = make_store_job()
    from questions_helpers import seed, make_record

    rec = GsdContext().parse("1-CONTEXT.md", text)
    from steward.questions import _new_record

    recs = [_new_record(f, f"gsd_context:1-CONTEXT.md:{f.anchor}", "h", "t") for f in rec]
    seed(store2, job2, recs)
    questions.apply(store2, job2, answers=[{"qid": "gsd_context:1-CONTEXT.md:G1", "option": "a", "note": "because"},
                                           {"qid": "gsd_context:1-CONTEXT.md:G2", "option": "a"}], now=D)
    got = {r["qid"]: r for r in questions.load(store2, job2)}
    items = [(got[q], got[q]["answer"]) for q in sorted(got)]
    out = _edit(questions.writeback_for_files({"1-CONTEXT.md": text}, items, "2026-09-29"), "1-CONTEXT.md").new_text
    ol = out.split("\n")
    i = next(n for n, ln in enumerate(ol) if ln.startswith("- **Added by Sam:**"))
    assert ol[i - 1] == "- **Recommended: (a)**" and ol[i] == "- **Added by Sam:** because" and ol[i + 1] == ""
    assert sum(1 for ln in ol if "Added by Sam" in ln) == 1  # G2 has no note


def test_partial_batch_leaves_status_alone(steward_home):
    store, job_id, _ = _scanned(steward_home)
    plan = _plan(_answer(store, job_id, {1: {"option": "recommended"}, 2: {"option": "recommended"}, 3: {"option": "a"}}),
                 texts={CTX: BEFORE})
    new = _edit(plan, CTX).new_text
    assert new.count("OPEN FOR SAM") == BEFORE.count("OPEN FOR SAM") - 3
    old_lines, new_lines = BEFORE.split("\n"), new.split("\n")
    diffs = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
    assert len(old_lines) == len(new_lines) and len(diffs) == 3
    assert all(b.startswith("### G") for _, b in diffs)
    assert diffs[2][1].endswith("— DECIDED (Sam, 2026-09-29): recommended") or "DECIDED" in diffs[2][1]
    assert plan.qids == [_qid(1), _qid(2), _qid(3)] and plan.commit_message == "docs(16): record Sam decisions"


def test_deferred_marker_and_summary(steward_home):
    store, job_id, _ = _scanned(steward_home)
    spec = {n: {"option": "recommended"} for n in range(1, 16) if n != 4}
    spec[4] = {"defer": True}
    new = _edit(_plan(_answer(store, job_id, spec)), CTX).new_text
    assert "### G4 Status pane landing page — DEFERRED (Sam, 2026-09-29)" in new
    assert "G4 deferred" in _norm(" ".join(_status(new)))
    footer = [ln for ln in new.splitlines() if ln.strip()][-1]
    assert "G4 deferred" in footer and footer.endswith("(Sam, 2026-09-29)*")
    assert "G1–G3" in _norm(" ".join(_status(new))) and "G5–G15" in _norm(" ".join(_status(new)))


def test_reanswer_replaces_marker_and_note(steward_home):
    store, job_id, _ = _scanned(steward_home)
    first = _edit(_plan(_phase16(store, job_id)), CTX).new_text
    items = _answer(store, job_id, {2: {"other": "something else"}, 15: {"other": "replay + live trial"}})
    out = _edit(_plan(items, texts={CTX: first}), CTX).new_text
    assert "### G2 Which metrics, and how they are computed — DECIDED (Sam, 2026-09-29): something else" in out
    assert _headers(out)[0].endswith("recommended") and len(_headers(out)) == 15
    assert "- **Added by Sam:**" not in out  # G15 re-answered with no note removes the old bullet
    status = _norm(" ".join(_status(out)))
    assert 'G1 accepts the recommended default exactly as written; G2 is "something else"; G3–G14 accept' in status
    assert 'G15 is "replay + live trial"' in status


def test_render_is_idempotent(steward_home):
    store, job_id, wt = _scanned(steward_home)
    items = _phase16(store, job_id)
    plan = _plan(items)
    assert {e.path for e in plan.edits} == {CTX, LOG}
    again = _plan(items, texts={e.path: e.new_text for e in plan.edits})
    assert again.edits == []
    # the area hashes are unchanged by the rendering: re-scanning the patched text sees no staleness
    before = {f.anchor: f.hash for f in GsdContext().parse(CTX, BEFORE)}
    after = {f.anchor: f.hash for f in GsdContext().parse(CTX, _edit(plan, CTX).new_text)}
    assert before == after and len(after) == 15


def test_discussion_log_block(steward_home):
    store, job_id, _ = _scanned(steward_home)
    plan = _plan(_phase16(store, job_id))
    log = _edit(plan, LOG)
    assert log.old_text == LOG_BEFORE and log.new_text.startswith(LOG_BEFORE.rstrip("\n"))
    tail = log.new_text[len(LOG_BEFORE):]
    assert tail == (
        "\n## Sam answers (2026-09-29)\n"
        "- **G1–G14:** accepted the recommended default exactly as written; markers now read "
        "`DECIDED (Sam, 2026-09-29): recommended`.\n"
        f"- **G15:** replay + live trial. {NOTE}\n"
    ) or tail.startswith("\n## Sam answers (2026-09-29)\n- **G1–G14:**")
    assert f"- **G15:** replay + live trial. {NOTE}" in log.new_text
    # a second batch on the same date replaces the block
    more = _answer(store, job_id, {2: {"other": "changed"}})
    twice = _plan(more, texts={CTX: _edit(plan, CTX).new_text, LOG: log.new_text})
    out = _edit(twice, LOG).new_text
    assert out.count("## Sam answers (2026-09-29)") == 1 and "- **G2:** changed." in out
    assert out.startswith(LOG_BEFORE.rstrip("\n"))
    # no log file, no log edit
    assert [e.path for e in _plan(_phase16(store, job_id), texts={CTX: BEFORE}).edits] == [CTX]


def test_stale_or_missing_area_is_skipped(steward_home):
    store, job_id, _ = _scanned(steward_home)
    items = _answer(store, job_id, {1: {"option": "recommended"}, 2: {"option": "recommended"}})
    changed = BEFORE.replace("Accounting fix (tokens.py counts subagents)", "Accounting fix (tokens.py counts subagents!)")
    changed = changed.replace("### G2 Which metrics", "### G2 Which metrics").replace(
        "- Options: (a) fix inside this phase as plan 16-01;", "- Options: (a) fix inside this phase as plan 16-01 (changed);")
    plan = _plan(items, texts={CTX: changed})
    assert {s["qid"]: s["reason"] for s in plan.skipped} == {_qid(1): "area changed"}
    assert plan.qids == [_qid(2)]
    gone = _plan(items, texts={CTX: BEFORE.replace("### G2 Which metrics, and how they are computed", "### Z9 Renamed")})
    assert {s["qid"]: s["reason"] for s in gone.skipped} == {_qid(2): "area not found"}
    assert _plan(items, texts={CTX: "nothing here\n"}).edits == []


def test_summary_ranges(steward_home):
    text = "# T\n\n**Status:** Discussed, no plans. Open.\n\n## Areas (all OPEN FOR SAM)\n\n" + "".join(
        f"### {a} — OPEN FOR SAM\n- Options: (a) x.\n\n"
        for a in ("G1 Area 1", "G2 Area 2", "G3 Area 3", "G5 Area 5", "G6 Area 6", "Alpha", "Beta")
    ) + "*P · all OPEN FOR SAM*\n"
    store, job_id = make_store_job()
    from questions_helpers import seed
    from steward.questions import _new_record

    recs = [_new_record(f, f"gsd_context:1-CONTEXT.md:{f.anchor}", "h", "t") for f in GsdContext().parse("1-CONTEXT.md", text)]
    seed(store, job_id, recs)
    questions.apply(store, job_id, answers=[{"qid": r["qid"], "option": "recommended"} for r in recs], now=D)
    got = questions.load(store, job_id)
    out = _edit(questions.writeback_for_files({"1-CONTEXT.md": text}, [(r, r["answer"]) for r in got], "2026-09-29"),
                "1-CONTEXT.md").new_text
    status = _norm(" ".join(_status(out)))
    assert "G1–G3 accept the recommended default exactly as written" in status
    assert "G5–G6 accept" in status and "alpha accepts the recommended default" in status and "beta accepts" in status
    footer = out.rstrip("\n").split("\n")[-1]
    assert "G1–G3 decided: recommended; G5–G6 decided: recommended; alpha decided: recommended; beta decided: recommended" in footer
    assert "(decided, Sam, 2026-09-29)" in out


def test_commit_message_and_scope(steward_home):
    assert questions.commit_message([".planning/phases/16-x/16-CONTEXT.md"]) == "docs(16): record Sam decisions"
    assert questions.commit_message(["a/16-CONTEXT.md", "a/16-DISCUSSION-LOG.md"]) == "docs(16): record Sam decisions"
    assert questions.commit_message(["a/CONTEXT.md"]) == "docs(questions): record Sam decisions"
    assert questions.commit_message(["docs/STEWARD-ANSWERS.json"]) == "docs(questions): record Sam decisions"
    assert questions.commit_message(["a/16-CONTEXT.md", "b/17-CONTEXT.md"]) == "docs(questions): record Sam decisions"
    assert questions.commit_message(["a/2.1-CONTEXT.md"]) == "docs(2.1): record Sam decisions"


def test_render_writeback_reads_committed_head_only(steward_home):
    store, job_id, wt = _scanned(steward_home)
    _answer(store, job_id, {n: {"option": "recommended"} for n in range(1, 4)})
    (wt / CTX).write_text(BEFORE.replace("### G1 Accounting fix (tokens.py counts subagents) — OPEN FOR SAM", "### G1 DIRTY"))
    plan = questions.render_writeback(store, job_id, SubprocessRunner())
    assert plan.qids == [_qid(1), _qid(2), _qid(3)] and plan.date == "2026-09-29"
    new = _edit(plan, CTX).new_text
    assert "DIRTY" not in new and "### G1 Accounting fix (tokens.py counts subagents) — DECIDED (Sam, 2026-09-29): recommended" in new
    assert _edit(plan, LOG).new_text.count("## Sam answers (2026-09-29)") == 1
    # only answered/deferred, by sam, not stale, not yet written
    recs = questions.load(store, job_id)
    recs[1]["written"] = {"via": "commit"}
    recs[2]["status"] = "stale"
    questions.save(store, job_id, recs)
    plan = questions.render_writeback(store, job_id, SubprocessRunner())
    assert plan.qids == [_qid(1)]
    plan = questions.render_writeback(store, job_id, SubprocessRunner(), qids=[_qid(9)])
    assert plan.edits == []
    assert questions.render_writeback(make_store_job()[0], "nope", SubprocessRunner()).edits == []


# -- steward_questions and unparsed cards -----------------------------------------------------------------------

import json  # noqa: E402

from questions_helpers import scan_files  # noqa: E402

QJSON = "docs/STEWARD-QUESTIONS.json"
AJSON = "docs/STEWARD-ANSWERS.json"
QDOC = {"questions": [
    {"id": "db", "title": "Which DB", "question": "Which database?", "recommended": "b",
     "options": [{"key": "a", "label": "SQLite"}, {"key": "b", "label": "Postgres"}]},
    {"id": "cache", "question": "Add a cache?", "options": ["yes", "no"], "recommended": ["a"]},
    {"id": "naming", "question": "What should the verb be called?"},
]}


def _qtext():
    return json.dumps(QDOC, indent=2) + "\n"


def _sq(steward_home, files=None):
    store, job_id = make_store_job()
    wt = scan_files(store, job_id, files or {QJSON: _qtext()})
    return store, job_id, wt


def _sqid(anchor, path="STEWARD-QUESTIONS.json"):
    return f"steward_questions:{path}:{anchor}"


def _sq_items(store, job_id, spec):
    questions.apply(store, job_id, answers=[{"qid": _sqid(a), **kw} for a, kw in spec.items()], now=D)
    recs = {r["qid"]: r for r in questions.load(store, job_id)}
    return [(recs[_sqid(a)], recs[_sqid(a)]["answer"]) for a in spec]


def test_answers_json_created_next_to_the_questions_file(steward_home):
    store, job_id, _ = _sq(steward_home)
    items = _sq_items(store, job_id, {"cache": {"option": "recommended", "note": "cheap"}, "db": {"option": "a"}})
    plan = questions.writeback_for_files({QJSON: _qtext()}, items, "2026-09-29")
    assert [e.path for e in plan.edits] == [AJSON] and plan.edits[0].old_text == ""
    text = plan.edits[0].new_text
    assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"
    rows = json.loads(text)["answers"]
    assert [r["id"] for r in rows] == ["db", "cache"]  # the questions' order, not the answering order
    assert rows[0] == {"id": "db", "qid": _sqid("db"), "status": "decided", "option": "a", "label": "SQLite",
                       "other": "", "note": "", "by": "Sam", "date": "2026-09-29"}
    assert rows[1]["option"] == "a" and rows[1]["label"] == "yes" and rows[1]["recommended"] is True and rows[1]["note"] == "cheap"
    assert plan.qids == [_sqid("db"), _sqid("cache")] or sorted(plan.qids) == sorted([_sqid("db"), _sqid("cache")])
    assert plan.commit_message == "docs(questions): record Sam decisions"
    other = _sq_items(store, job_id, {"naming": {"other": "steward reply"}})
    rows = json.loads(questions.writeback_for_files({QJSON: _qtext()}, other, "2026-09-29").edits[0].new_text)["answers"]
    assert rows[0]["label"] == "steward reply" and rows[0]["other"] == "steward reply" and rows[0]["option"] == "other"
    dfr = _sq_items(store, job_id, {"naming": {"defer": True}})
    row = json.loads(questions.writeback_for_files({QJSON: _qtext()}, dfr, "2026-09-29").edits[0].new_text)["answers"][0]
    assert row["status"] == "deferred" and row["option"] == "defer" and row["label"] == ""


def test_answers_merge_into_an_existing_file(steward_home):
    store, job_id, _ = _sq(steward_home)
    existing = {"answers": [{"id": "zzz", "status": "decided", "option": "x"},
                            {"id": "db", "status": "decided", "option": "b", "label": "Postgres"},
                            {"id": "yyy", "status": "deferred"}]}
    old = json.dumps(existing, indent=2) + "\n"
    items = _sq_items(store, job_id, {"db": {"option": "a"}, "cache": {"option": "recommended"}})
    plan = questions.writeback_for_files({QJSON: _qtext(), AJSON: old}, items, "2026-09-29")
    rows = json.loads(plan.edits[0].new_text)["answers"]
    assert [r["id"] for r in rows] == ["zzz", "db", "yyy", "cache"]
    assert rows[1]["option"] == "a" and rows[0] == existing["answers"][0]
    again = questions.writeback_for_files({QJSON: _qtext(), AJSON: plan.edits[0].new_text}, items, "2026-09-29")
    assert again.edits == []


def test_fenced_block_questions_answer_beside_the_markdown_file(steward_home):
    md = "# Design\n\n```steward-questions\n" + json.dumps({"questions": [
        {"id": "layout", "question": "Which layout?", "options": ["grid", "list"], "recommended": "a"}]}) + "\n```\n"
    store, job_id, _ = _sq(steward_home, {"notes/design.md": md})
    qid = "steward_questions:design.md:layout"
    questions.apply(store, job_id, answers=[{"qid": qid, "option": "recommended"}], now=D)
    rec = questions.load(store, job_id)[0]
    plan = questions.writeback_for_files({"notes/design.md": md}, [(rec, rec["answer"])], "2026-09-29")
    assert [e.path for e in plan.edits] == ["notes/STEWARD-ANSWERS.json"]
    assert json.loads(plan.edits[0].new_text)["answers"][0]["id"] == "layout"


def test_unparsed_cards_produce_no_edit(steward_home):
    store, job_id, _ = _sq(steward_home, {QJSON: _qtext(), "docs/other/STEWARD-QUESTIONS.json": "{not json"})
    recs = questions.load(store, job_id)
    bad = next(r for r in recs if r["flags"]["unparsed"])
    questions.apply(store, job_id, answers=[{"qid": bad["qid"], "other": "ignore it"},
                                            {"qid": _sqid("db"), "option": "a"}], now=D)
    recs = {r["qid"]: r for r in questions.load(store, job_id)}
    items = [(recs[bad["qid"]], recs[bad["qid"]]["answer"]), (recs[_sqid("db")], recs[_sqid("db")]["answer"])]
    plan = questions.writeback_for_files({QJSON: _qtext(), "docs/other/STEWARD-QUESTIONS.json": "{not json"}, items,
                                         "2026-09-29")
    assert [s for s in plan.skipped] == [{"qid": bad["qid"], "reason": "no deterministic edit"}]
    assert [e.path for e in plan.edits] == [AJSON] and plan.qids == [_sqid("db")]
    assert recs[bad["qid"]]["status"] == "answered"  # a store-only answer


def test_two_adapters_and_two_files_in_one_batch(steward_home):
    store, job_id = make_store_job()
    from questions_helpers import LOG

    wt = scan_files(store, job_id, {CTX: BEFORE, LOG: LOG_BEFORE, QJSON: _qtext()})
    spec = [{"qid": _qid(n), "option": "recommended"} for n in range(1, 16)]
    spec += [{"qid": _sqid("db"), "option": "a"}, {"qid": _sqid("cache"), "option": "recommended"}]
    questions.apply(store, job_id, answers=spec, now=D)
    plan = questions.render_writeback(store, job_id, SubprocessRunner())
    assert [e.path for e in plan.edits] == sorted([CTX, LOG, AJSON])
    assert len(plan.qids) == 17 and plan.commit_message == "docs(questions): record Sam decisions"
    # the scan of the patched pair sees the question with a decided marker (the written-confirmation path)
    for e in plan.edits:
        (wt / e.path).parent.mkdir(parents=True, exist_ok=True)
        (wt / e.path).write_text(e.new_text)
    commit_file(wt, "touch.txt", "x\n", "apply write-back")
    res = questions.scan(store, job_id, SubprocessRunner())
    assert res.error is None and res.stale == [] and res.worker_decided == []
    recs = {r["qid"]: r for r in questions.load(store, job_id)}
    assert recs[_sqid("db")]["status"] == "answered" and recs[_sqid("db")]["written"]
    assert recs[_qid(1)]["written"]


def test_writeback_plan_is_deterministic(steward_home):
    store, job_id = make_store_job()
    from questions_helpers import LOG

    scan_files(store, job_id, {CTX: BEFORE, LOG: LOG_BEFORE, QJSON: _qtext()})
    spec = [{"qid": _qid(n), "option": "recommended"} for n in range(1, 16)]
    spec += [{"qid": _sqid("cache"), "option": "recommended"}, {"qid": _sqid("db"), "other": "own"}]
    questions.apply(store, job_id, answers=spec, now=D)
    a = questions.render_writeback(store, job_id, SubprocessRunner())
    b = questions.render_writeback(store, job_id, SubprocessRunner())
    assert [(e.path, e.old_text, e.new_text, e.diff) for e in a.edits] == [(e.path, e.old_text, e.new_text, e.diff) for e in b.edits]
    assert a.qids == b.qids and a.commit_message == b.commit_message and a.skipped == b.skipped


def test_answers_json_records_concrete_option_for_accepted_recommendation(steward_home):
    store, job_id, _ = _sq(steward_home)
    (rec, ans), = _sq_items(store, job_id, {"db": {"option": "recommended"}})
    assert (ans["option"], ans["label"], ans["recommended"]) == ("b", "Postgres", True)
    row = _answer_row("db", rec["qid"], ans, "2026-09-30")
    assert (row["option"], row["label"], row["recommended"]) == ("b", "Postgres", True)
