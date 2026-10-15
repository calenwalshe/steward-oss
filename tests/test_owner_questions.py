"""The question pipeline under a configured owner name (Phase 19-01, G7): both spellings parse, the configured one is
written, stored actor keys stay `sam` for him. Phase 17 tests run unchanged under the pinned legacy name."""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime

import pytest
from questions_helpers import CTX, LOG, fixture_text, make_record, make_store_job, scan_fixture, seed

from steward import questions, questions_delivery
from steward.contract import Contract
from steward.question_adapters import gsd_context
from steward.question_adapters.gsd_context import GsdContext
from steward.question_adapters.base import Found
from steward.question_adapters.steward_questions import _answer_row
from steward.store import Store

D = datetime(2026, 9, 29, 10, 0, 0)
BEFORE = fixture_text("phase16_context_before.md")
AFTER = fixture_text("phase16_context_after.md")
LOG_BEFORE = fixture_text("phase16_log_before.md")
LOG_AFTER = fixture_text("phase16_log_after.md")
NOTE = ("before acceptance the efficiency watcher must also run observe-only (T0) on at least one live Steward job "
        "for at least one unit boundary, with its findings shown on the status pane and no nudges sent.")


@pytest.fixture(autouse=True)
def _sam_is_a_legacy_spelling(monkeypatch):
    """The pinned suite name (Sam) is the older spelling here: parsers accept it next to the configured Alex."""
    monkeypatch.setenv("STEWARD_OWNER_LEGACY_NAMES", "Sam")


@pytest.fixture
def alex(monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Alex")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _qid(n: int) -> str:
    return f"gsd_context:16-CONTEXT.md:G{n}"


def _ctx(*headers: str) -> str:
    return "# X\n\n" + "".join(f"### {h}\n- Options: (a) one; (b) two.\n- **Recommended: (a)**\n\n" for h in headers)


def _found(anchor="G1", marker=None):
    return Found(adapter="gsd_context", file="docs/1-CONTEXT.md", anchor=anchor, line=3, title="T", question="T",
                 options=[{"key": "a", "label": "x", "detail": ""}], recommended=["a"], recommended_text="x",
                 starred=True, hash="h1", marker=marker)


def _marker(who, state="decided", text="recommended"):
    return {"state": state, "who": who, "date": "2026-09-29", "text": text}


def test_parse_accepts_configured_and_legacy_headers(alex):
    text = _ctx("G1 Area — OPEN FOR ALEX", "G2 Area — OPEN FOR SAM")
    found = GsdContext().parse("1-CONTEXT.md", text)
    assert [f.anchor for f in found] == ["G1", "G2"] and all(f.marker is None for f in found)


def test_parse_under_sam_ignores_other_names(monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    text = _ctx("G1 Area — OPEN FOR ALEX", "G2 Area — OPEN FOR SAM")
    found = GsdContext().parse("1-CONTEXT.md", text)
    assert [f.anchor for f in found] == ["G2"]


def test_unparsed_bullet_markers_use_both_names(alex):
    text = "# X\n\n- OPEN FOR ALEX: which DB?\n- **OPEN FOR SAM** which cache?\n- a thing — OPEN FOR ALEX\n"
    found = GsdContext().parse("1-CONTEXT.md", text)
    assert len(found) == 3 and all(f.unparsed for f in found)


def test_grammar_is_lazy_and_cached_by_the_names(monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Alex")
    g1 = gsd_context._grammar()
    assert gsd_context._grammar() is g1
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Ann")
    g2 = gsd_context._grammar()
    assert g2 is not g1 and g2.header.match("## x — OPEN FOR ANN") and not g2.header.match("## x — OPEN FOR ALEX")


def _answered(rec, status="answered", by="sam"):
    r = dict(rec)
    r["status"] = status
    r["answer"] = {"option": "recommended", "note": "", "by": by, "origin": "cli", "at": "t", "date": "2026-09-29",
                   "recommended": True, "area_hash": r["source"]["hash"]}
    return r


def test_decided_markers_by_either_name_are_the_owners(alex):
    names = ("G1", "G2", "G3", "G4")
    first = questions.reconcile([], {"docs/1-CONTEXT.md": [_found(a) for a in names]}, set(), "c1",
                                "2026-09-29T10:00:00+00:00").records
    existing = [_answered(r, status="deferred" if r["source"]["anchor"] == "G3" else "answered") for r in first]
    out = questions.reconcile(existing, {"docs/1-CONTEXT.md": [
        _found("G1", _marker("Sam")),
        _found("G2", _marker("Alex")),
        _found("G3", _marker("Alex", state="deferred", text="")),
        _found("G4", _marker("someone else", text="b")),
    ]}, set(), "c2", "2026-09-29T11:00:00+00:00")
    by = {r["source"]["anchor"]: r for r in out.records}
    for a in ("G1", "G2", "G3"):  # the owner's marker under either spelling: already decided, written, never stale
        assert by[a]["written"]["via"] == "scan" and by[a]["status"] != "stale"
        assert not by[a]["flags"]["answered_by_worker"]
    assert out.stale == [] and sorted(out.written) == sorted(by[a]["qid"] for a in ("G1", "G2", "G3"))
    assert by["G4"]["status"] == "stale" or by["G4"]["flags"]["answered_by_worker"] or by["G4"].get("written") is None


def test_first_sight_decided_by_alex_is_not_a_card(alex):
    out = questions.reconcile([], {"docs/1-CONTEXT.md": [_found("G1", _marker("Alex")), _found("G2", _marker("Sam"))]},
                              set(), "c1", "2026-09-29T10:00:00+00:00")
    assert out.records == []


def _phase16_answers(store, job_id):
    spec = [{"qid": _qid(n), "option": "recommended"} for n in range(1, 15)]
    spec.append({"qid": _qid(15), "other": "replay + live trial", "note": NOTE})
    questions.apply(store, job_id, answers=spec, now=D)
    recs = {r["qid"]: r for r in questions.load(store, job_id)}
    return [(recs[_qid(n)], recs[_qid(n)]["answer"]) for n in range(1, 16)]


def _render(name, monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", name)
    store, job_id = make_store_job()
    scan_fixture(store, job_id)  # the fixture says OPEN FOR SAM: the legacy spelling still parses under any name
    plan = questions.writeback_for_files({CTX: BEFORE, LOG: LOG_BEFORE}, _phase16_answers(store, job_id), "2026-09-29")
    return (next(e for e in plan.edits if e.path == CTX).new_text, next(e for e in plan.edits if e.path == LOG).new_text,
            plan.commit_message)


def test_writeback_uses_the_configured_name(monkeypatch):
    new, new_log, message = _render("Alex", monkeypatch)
    assert "— DECIDED (Alex, 2026-09-29): recommended" in new
    assert "- **Added by Alex:** " in new
    assert "decided defaults (Alex, 2026-09-29)" in new
    assert "(Alex, 2026-09-29)*" in new
    assert "**Decided by Alex 2026-09-29:**" in new
    assert "OPEN FOR" not in new and "DECIDED (Sam" not in new and "Added by Sam" not in new
    assert "## Alex answers (2026-09-29)" in new_log
    assert "`DECIDED (Alex, 2026-09-29): recommended`" in new_log
    assert message == "docs(16): record Alex decisions"


def test_writeback_differs_from_the_legacy_output_only_by_the_name(monkeypatch):
    alex_new, alex_log, alex_msg = _render("Alex", monkeypatch)
    sam_new, sam_log, sam_msg = _render("Sam", monkeypatch)
    assert re.sub(r"\bAlex\b", "Sam", alex_new) == sam_new
    assert re.sub(r"\bAlex\b", "Sam", alex_log) == sam_log
    assert sam_msg == "docs(16): record Sam decisions" == re.sub(r"\bAlex\b", "Sam", alex_msg)
    # and the legacy output is the Phase 16 oracle (headers, status, footer, log heading)
    heads = lambda t: [ln for ln in t.splitlines() if ln.startswith("### G")]  # noqa: E731
    assert heads(sam_new) == heads(AFTER)
    last = lambda t: [ln for ln in t.splitlines() if ln.strip()][-1]  # noqa: E731
    assert _norm(last(sam_new)) == _norm(last(AFTER))
    assert "## Sam answers (2026-09-29)" in sam_log


def test_rewriting_a_file_the_legacy_name_decided_keeps_the_heading_working(alex):
    done = "## Gray areas — decided defaults (Sam, 2026-09-28)\n\nx\n\n*Phase: 1 · G1 decided: recommended (Sam, 2026-09-28)*\n"
    lines = done.split("\n")
    gsd_context._heading(lines, "2026-09-29")
    assert lines[0] == "## Gray areas — decided defaults (Alex, 2026-09-29)"


def test_a_note_line_written_by_either_name_never_makes_a_card_stale(alex):
    base = "# X\n\n### G1 Area — OPEN FOR ALEX\n- Options: (a) one; (b) two.\n- **Recommended: (a)**\n"
    h0 = GsdContext().parse("1-CONTEXT.md", base)[0].hash
    for who in ("Sam", "Alex"):
        noted = base + f"- **Added by {who}:** because\n  wrapped note\n"
        assert GsdContext().parse("1-CONTEXT.md", noted)[0].hash == h0


def test_actor_key_in_the_store(alex):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record("gsd_context:17-CONTEXT.md:G1"), make_record("gsd_context:17-CONTEXT.md:G2")])
    questions.apply(store, job_id, answers=[{"qid": "gsd_context:17-CONTEXT.md:G1", "option": "a", "note": "n"}], now=D)
    rec = questions.load(store, job_id)[0]
    assert rec["answer"]["by"] == "alex"
    ev = [e for e in store.events(job_id) if e.what == "question_answered"]
    assert ev and all(e.evidence.get("by") == "alex" for e in ev if "by" in e.evidence)
    questions.apply(store, job_id, answers=[{"qid": "gsd_context:17-CONTEXT.md:G1", "option": "b", "note": "changed"}], now=D)
    assert questions.load(store, job_id)[0]["answer"]["by"] == "alex"


def test_actor_key_under_sam_stays_sam(monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    store, job_id = make_store_job()
    seed(store, job_id, [make_record("gsd_context:17-CONTEXT.md:G1")])
    questions.apply(store, job_id, answers=[{"qid": "gsd_context:17-CONTEXT.md:G1", "option": "a"}], now=D)
    assert questions.load(store, job_id)[0]["answer"]["by"] == "sam"


def test_old_rows_by_sam_are_the_owners_under_alex(monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    store, job_id = make_store_job()
    seed(store, job_id, [make_record("gsd_context:17-CONTEXT.md:G1")])
    questions.apply(store, job_id, answers=[{"qid": "gsd_context:17-CONTEXT.md:G1", "option": "a"}], now=D)
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Alex")
    assert [r["qid"] for r in questions_delivery.pending(store, job_id)] == ["gsd_context:17-CONTEXT.md:G1"]
    assert len(questions._pending(questions.load(store, job_id), None)) == 1


def test_steward_answers_row_is_written_by_the_configured_name(alex):
    row = _answer_row("Q1", "qid", {"option": "a", "label": "x"}, "2026-09-29")
    assert row["by"] == "Alex"
    assert questions.owner.is_owner_actor(row["by"])


@pytest.mark.parametrize("goal,expect", [
    ("Leave the gray areas open for alex", True),
    ("Leave the gray areas open for sam", True),
    ("Write the questions for Alex", True),
    ("Fix the parser and ask questions for Jo", False),
])
def test_discussion_heuristic(alex, goal, expect):
    store = Store()
    job = store.create_job(goal, "/nonexistent")
    assert questions.is_deliverable(store, job.job_id) is expect


def test_discussion_heuristic_under_default_operator(monkeypatch):
    monkeypatch.delenv("STEWARD_OWNER_NAME")
    from steward import owner

    monkeypatch.setattr(owner, "_git_user_name", lambda: "")
    owner.reset_cache()
    store = Store()
    for goal, expect in [("List the open questions for the operator", True), ("questions for operator", True),
                         ("Fix the parser", False)]:
        job = store.create_job(goal, "/nonexistent")
        assert questions.is_deliverable(store, job.job_id) is expect


def test_steer_text_uses_the_name(alex):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record("gsd_context:16-CONTEXT.md:G4", recommended=("a",)),
                         make_record("gsd_context:16-CONTEXT.md:G5", recommended=("a",))])
    questions.apply(store, job_id, answers=[{"qid": "gsd_context:16-CONTEXT.md:G4", "option": "b"},
                                            {"qid": "gsd_context:16-CONTEXT.md:G5", "option": "recommended"}], now=D)
    recs = questions.ordered(questions.load(store, job_id))
    text = questions_delivery.steer_text(questions.WritebackPlan(), recs)
    assert text.splitlines()[0] == "ALEX ANSWERED 2 QUESTION(S) (recorded in Steward)."
    assert "Alex chose (b) Option b. Switch to it." in text and "Sam" not in text and "SAM" not in text


def test_steer_text_under_sam(monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    store, job_id = make_store_job()
    seed(store, job_id, [make_record("gsd_context:16-CONTEXT.md:G4", recommended=("a",))])
    questions.apply(store, job_id, answers=[{"qid": "gsd_context:16-CONTEXT.md:G4", "option": "b"}], now=D)
    text = questions_delivery.steer_text(questions.WritebackPlan(), questions.load(store, job_id))
    assert text.splitlines()[0] == "SAM ANSWERED 1 QUESTION(S) (recorded in Steward)."
    assert "Sam chose (b) Option b. Switch to it." in text


def test_commit_message_names_the_owner(alex):
    assert questions.commit_message(["a/16-CONTEXT.md"]) == "docs(16): record Alex decisions"
    assert questions.commit_message(["x/NOTES.md"]) == "docs(questions): record Alex decisions"
