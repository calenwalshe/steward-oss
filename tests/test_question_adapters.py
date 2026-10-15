"""Pure adapter tests (no git): the gsd_context adapter against the Phase 16 fixture and small synthetic files."""

from pathlib import Path

from steward.question_adapters import ADAPTERS, adapter_named
from steward.question_adapters.base import Change, FileReader
from steward.question_adapters.gsd_context import GsdContext
from steward.question_adapters.steward_questions import StewardQuestions

FIX = Path(__file__).parent / "fixtures" / "questions"
ADAPTER = GsdContext()


def _before():
    return (FIX / "phase16_context_before.md").read_text()


def _area(body: str, header: str = "### G1 Title — OPEN FOR SAM") -> str:
    return f"# Ctx\n\n{header}\n{body}\n"


def _one(text: str, path: str = "9-CONTEXT.md"):
    found = ADAPTER.parse(path, text)
    assert len(found) == 1, found
    return found[0]


def test_two_adapters_and_no_others():
    assert [a.name for a in ADAPTERS] == ["gsd_context", "steward_questions"]
    assert adapter_named("gsd_context") is ADAPTERS[0]
    assert adapter_named("nope") is None


def test_wants_context_files_only():
    assert ADAPTER.wants(".planning/phases/16/16-CONTEXT.md")
    assert ADAPTER.wants("CONTEXT.md")
    assert not ADAPTER.wants("README.md")


def test_phase16_fixture_yields_15_cards_in_order():
    found = ADAPTER.parse("16-CONTEXT.md", _before())
    assert [f.anchor for f in found] == [f"G{i}" for i in range(1, 16)]
    lines = [f.line for f in found]
    assert lines == sorted(lines) and len(set(lines)) == 15
    assert all(f.marker is None and not f.unparsed and f.adapter == "gsd_context" for f in found)


def test_phase16_option_and_star_oracle():
    by = {f.anchor: f for f in ADAPTER.parse("16-CONTEXT.md", _before())}
    keys = {a: [o["key"] for o in by[a].options] for a in by}
    assert {f"G{i}": ["a", "b", "c"] for i in (1, 2, 3, 4, 5, 6, 8, 10)}.items() <= keys.items()
    for a in ("G7", "G9", "G11", "G12", "G13", "G14", "G15"):
        assert keys[a] == [], a
    star = {a: by[a].recommended for a in by}
    assert star["G1"] == ["a"] and star["G2"] == ["c"] and star["G3"] == ["c"] and star["G4"] == ["a"]
    assert star["G5"] == ["a"] and star["G6"] == ["c"] and star["G8"] == ["a", "c"] and star["G10"] == ["b"]
    for a in ("G7", "G9", "G11", "G12", "G13", "G14", "G15"):
        assert star[a] == []
    assert [o["label"] for o in by["G1"].options] == [
        "fix inside this phase as plan 16-01", "separate quick fix first",
        "leave as is, and the watcher does its own parse"]
    assert [o["label"] for o in by["G6"].options] == [
        "observe only", "Phase-15 ladder T0→T2 (observe → escalate → nudge)",
        "(b) plus a bounded `reconfigure` power"]
    assert by["G7"].recommended_text == "as written"
    assert by["G9"].recommended_text.startswith("a per-job kv")
    assert by["G11"].recommended_text.startswith("A built-in role")
    assert all(f.starred for f in by.values())
    assert by["G2"].question == by["G2"].title  # only sub-bullets and the bullets that carry the answer
    assert by["G7"].question.startswith("All thresholds live in")


def test_options_shapes():
    inline = _one(_area("- Options: (a) one; (b) two; (c) three.\n- **Recommended: (b).**"))
    assert [(o["key"], o["label"]) for o in inline.options] == [("a", "one"), ("b", "two"), ("c", "three")]
    wrapped = _one(_area("- Options: (a) alpha thing; (b) beta thing;\n  (c) gamma\n  thing.\n- Rationale: r"))
    assert [o["label"] for o in wrapped.options] == ["alpha thing", "beta thing", "gamma thing"]
    nested = _one(_area("- Options:\n  - (a) the worker declares;\n  - (b) one file, wrapped\n    onto a line;\n  - (c) both.\n- **Recommended: (c).**"))
    assert [(o["key"], o["label"]) for o in nested.options] == [
        ("a", "the worker declares"), ("b", "one file, wrapped onto a line"), ("c", "both")]
    assert nested.recommended == ["c"]
    seq = _one(_area("- Options: (a) x; (b) y; (c) (b) plus a bounded thing"))
    assert [o["label"] for o in seq.options] == ["x", "y", "(b) plus a bounded thing"]
    detail = _one(_area("- Options: (a) fast — but risky; (b) slow"))
    assert detail.options[0] == {"key": "a", "label": "fast", "detail": "but risky"}


def test_recommended_shapes():
    opts = "- Options: (a) x; (b) y; (c) z\n"
    assert _one(_area(opts + "- **Recommended: (a)**, and ship it first.")).recommended == ["a"]
    both = _one(_area(opts + "- **Recommended: (a) + (c).**\n  - the sub bullet"))
    assert both.recommended == ["a", "c"] and both.recommended_text == "the sub bullet"
    c = _one(_area(opts + "- **Recommended: (c), all deterministic code:**"))
    assert c.recommended == ["c"] and c.recommended_text.startswith("all deterministic code")
    free = _one(_area("- **Recommended:** a per-job kv list, no schema change."))
    assert free.recommended == [] and free.recommended_text == "a per-job kv list, no schema change."
    subs = _one(_area("- **Recommended:**\n  - A **built-in role** here\n  - second"))
    assert subs.recommended == [] and subs.recommended_text == "A built-in role here"
    dropped = _one(_area(opts + "- **Recommended: (a) + (z).**"))
    assert dropped.recommended == ["a"]
    none = _one(_area(opts))
    assert none.recommended == [] and none.recommended_text == "as written" and none.starred


def test_header_anchor_title_and_slug():
    assert _one(_area("body", "### G4 Status pane — OPEN FOR SAM")).anchor == "G4"
    d = _one(_area("body", "### D-01 The choice — OPEN FOR SAM"))
    assert (d.anchor, d.title) == ("D-01", "The choice")
    s = _one(_area("body", "## Which database to use — OPEN FOR SAM"))
    assert s.anchor == "which-database-to-use" and s.title == "Which database to use"
    long = _one(_area("body", "### G1 " + "very long title " * 10 + "— OPEN FOR SAM"))
    assert len(long.title) <= 60 and long.title.endswith("…")


def test_rationale_evidence_question_defaults():
    body = (
        "First paragraph is the question.\n"
        "- Options: (a) x; (b) y\n"
        "- **Recommended: (a).**\n"
        "- Rationale: because of steward/tokens.py:42 and commit 5007cfb, see https://example.com/a.\n"
        "  second rationale line ignored, again steward/tokens.py:42, foo.md:7"
    )
    f = _one(_area(body))
    assert f.question == "First paragraph is the question."
    assert f.rationale.startswith("because of steward/tokens.py:42")
    assert f.evidence == ["steward/tokens.py:42", "commit 5007cfb", "https://example.com/a", "foo.md:7"]
    many = _one(_area("- Rationale: r\n" + "\n".join(f"  a{i}.py:{i}" for i in range(1, 15))))
    assert len(many.evidence) == 8
    assert _one(_area("- Options: (a) x")).question == "Title"


def test_marker_parse():
    d = _one(_area("b", "### G1 T — DECIDED (Sam, 2026-09-29): recommended"))
    assert d.marker == {"state": "decided", "who": "Sam", "date": "2026-09-29", "text": "recommended"}
    w = _one(_area("b", "### G1 T — DECIDED (worker, 2026-09-29): b"))
    assert w.marker["who"] == "worker" and w.marker["text"] == "b"
    f = _one(_area("b", "### G1 T — DEFERRED (Sam, 2026-09-30)"))
    assert f.marker == {"state": "deferred", "who": "Sam", "date": "2026-09-30", "text": ""}
    assert _one(_area("b")).marker is None


def test_non_question_mentions_make_no_card():
    found = ADAPTER.parse("16-CONTEXT.md", _before())
    assert not any(f.unparsed for f in found)  # status prose, section heading and italic footer make nothing
    text = "# C\n\nprose OPEN FOR SAM here\n\n- OPEN FOR SAM: which DB?\n\n```\n### G9 fenced — OPEN FOR SAM\n```\n"
    (only,) = ADAPTER.parse("x-CONTEXT.md", text)
    assert only.unparsed and not only.starred and only.options == []
    assert only.evidence == ["x-CONTEXT.md:5"] and only.question == "unparsed question in x-CONTEXT.md:5"
    assert only.anchor.startswith("unparsed-")


def test_area_hash():
    base = "- Options: (a) x; (b) y\n- **Recommended: (a).**\n- Rationale: r"
    h = _one(_area(base)).hash
    assert _one(_area(base)).hash == h
    assert _one(_area(base, "### G1 Title — DECIDED (Sam, 2026-09-29): recommended")).hash == h
    assert _one(_area(base + "\n- **Added by Sam:** a note")).hash == h
    assert _one(_area(base.replace("(b) y", "(b) z"))).hash != h
    assert _one(_area(base + "\n- more")).hash != h
    assert _one(_area(base, "### G1 Other — OPEN FOR SAM")).hash != h


def test_phase16_write_back_never_changes_an_area_hash():
    before = ADAPTER.parse("16-CONTEXT.md", _before())
    after = ADAPTER.parse("16-CONTEXT.md", (FIX / "phase16_context_after.md").read_text())
    assert [f.hash for f in before] == [f.hash for f in after]  # incl. G15's wrapped `Added by Sam (live trial)` note
    assert all(f.marker and f.marker["state"] == "decided" and f.marker["who"] == "Sam" for f in after)
    assert after[14].marker["text"] == "replay + live trial"


def test_body_ends_at_rule_tag_and_next_heading_and_dedupes_anchors():
    text = "### G1 A — OPEN FOR SAM\n- Options: (a) x\n---\n- Rationale: not mine\n\n### G1 B — OPEN FOR SAM\nq\n</decisions>\nafter"
    a, b = ADAPTER.parse("1-CONTEXT.md", text)
    assert a.rationale == "" and (a.anchor, b.anchor) == ("G1", "G1-2")


def test_scan_reads_only_wanted_added_or_modified_files():
    reads = []

    def read(path):
        reads.append(path)
        return "### G1 T — OPEN FOR SAM\nq\n"

    files = FileReader(read)
    diff = [Change("a/1-CONTEXT.md", "A"), Change("b/2-CONTEXT.md", "D"), Change("c/notes.md", "M")]
    found = ADAPTER.scan(diff, files)
    assert [f.file for f in found] == ["a/1-CONTEXT.md"] and reads == ["a/1-CONTEXT.md"]


# -- steward_questions ----------------------------------------------------------------------

import json  # noqa: E402

SQ = StewardQuestions()


def _sq(files: dict, changed: list[str] | None = None, status: str = "A"):
    reads = []

    def read(path):
        reads.append(path)
        return files.get(path)

    def grep(needle, paths):
        return [p for p in paths if needle in (files.get(p) or "")]

    diff = [Change(p, status) for p in (changed or [k for k in files if k != "d/STEWARD-ANSWERS.json"])]
    return SQ.scan(diff, FileReader(read, grep)), reads


def test_steward_questions_json_file():
    full = {"questions": [{
        "id": "db", "title": "Which database", "question": "Use Postgres or SQLite?",
        "options": [{"key": "p", "label": "Postgres", "detail": "hosted"}, {"key": "s", "label": "SQLite"}],
        "recommended": "s", "rationale": "single writer", "evidence": ["a.py:3", "commit abc1234"],
        "blocking": True, "blocking_reason": "schema depends on it", "ignored": 1}]}
    (f,), _ = _sq({"x/STEWARD-QUESTIONS.json": json.dumps(full)})
    assert (f.anchor, f.title, f.question) == ("db", "Which database", "Use Postgres or SQLite?")
    assert [(o["key"], o["label"], o["detail"]) for o in f.options] == [("p", "Postgres", "hosted"), ("s", "SQLite", "")]
    assert f.recommended == ["s"] and f.starred and f.recommended_text == "SQLite"
    assert f.rationale == "single writer" and f.evidence == ["a.py:3", "commit abc1234"]
    assert f.blocking and f.blocking_reason == "schema depends on it" and f.adapter == "steward_questions"
    assert len(f.hash) == 12
    bare = [{"question": "Which port should the service listen on, really?", "options": ["8080", "9090", "other"],
             "recommended": ["a", "zz"]}]
    (g,), _ = _sq({"STEWARD-QUESTIONS.json": json.dumps(bare)})
    assert [o["key"] for o in g.options] == ["a", "b", "c"] and g.recommended == ["a"]
    assert g.title == "Which port should the service listen on, really?"
    assert g.anchor == "q1" and not g.blocking
    unstarred = [{"question": "q?", "options": ["x"], "recommended": "nope"}]
    (h,), _ = _sq({"STEWARD-QUESTIONS.json": json.dumps(unstarred)})
    assert not h.starred and h.recommended == []


def test_steward_questions_hash_is_canonical_json():
    a = [{"question": "q?", "id": "x", "options": ["1"]}]
    b = [{"options": ["1"], "id": "x", "question": "q?"}]
    (fa,), _ = _sq({"STEWARD-QUESTIONS.json": json.dumps(a)})
    (fb,), _ = _sq({"STEWARD-QUESTIONS.json": json.dumps(b)})
    (fc,), _ = _sq({"STEWARD-QUESTIONS.json": json.dumps([{**a[0], "options": ["2"]}])})
    assert fa.hash == fb.hash and fa.hash != fc.hash


def test_fenced_block_in_markdown():
    md = (
        "# Notes\n\n```steward-questions\n"
        '{"questions": [{"id": "a", "question": "First?"}]}\n```\n\n'
        "```json\n" '[{"id": "no", "question": "not mine"}]\n```\n\n'
        "```steward-questions\n" '[{"id": "b", "question": "Second?"}]\n```\n'
    )
    found, reads = _sq({"docs/n.md": md, "docs/plain.md": "# nothing to see\n"})
    assert [(f.anchor, f.question) for f in found] == [("a", "First?"), ("b", "Second?")]
    assert "docs/n.md" in reads and "docs/plain.md" not in reads  # a markdown without the string is never read


def test_duplicate_ids_get_suffix():
    data = [{"id": "db", "question": "one?"}, {"id": "db", "question": "two?"}]
    found, _ = _sq({"STEWARD-QUESTIONS.json": json.dumps(data)})
    assert [f.anchor for f in found] == ["db", "db-2"]


def test_invalid_json_becomes_one_unparsed_card():
    (f,), _ = _sq({"STEWARD-QUESTIONS.json": "{not json"})
    assert f.unparsed and f.anchor.startswith("unparsed-") and not f.starred and f.options == []
    assert f.question == "unparsed question in STEWARD-QUESTIONS.json:1"
    md = "intro\n```steward-questions\n{oops\n```\n"
    (g,), _ = _sq({"n.md": md})
    assert g.unparsed and g.question == "unparsed question in n.md:2"
    mixed = [{"id": "ok", "question": "fine?"}, {"id": "bad"}, "not a dict"]
    found, _ = _sq({"STEWARD-QUESTIONS.json": json.dumps(mixed)})
    assert [(f.anchor, f.unparsed) for f in found] == [("ok", False), ("unparsed-item2", True), ("unparsed-item3", True)]


def test_sibling_answers_mark_decided():
    qs = [{"id": "db", "question": "which?", "options": ["a1", "b1"]}, {"id": "port", "question": "which port?"},
          {"id": "left", "question": "still open?"}]
    ans = {"answers": [
        {"id": "db", "status": "decided", "option": "b", "label": "b1", "by": "sam", "date": "2026-09-30"},
        {"id": "port", "status": "deferred", "by": "sam", "date": "2026-09-30", "note": "later"}]}
    files = {"d/STEWARD-QUESTIONS.json": json.dumps(qs), "d/STEWARD-ANSWERS.json": json.dumps(ans)}
    found, _ = _sq(files, changed=["d/STEWARD-QUESTIONS.json"])
    m = {f.anchor: f.marker for f in found}
    assert m["db"] == {"state": "decided", "who": "sam", "date": "2026-09-30", "text": "b1"}
    assert m["port"]["state"] == "deferred" and m["left"] is None
    other = {"answers": [{"id": "db", "status": "decided", "option": "recommended", "by": "sam", "date": "d"}]}
    files["d/STEWARD-ANSWERS.json"] = json.dumps(other)
    found, _ = _sq(files, changed=["d/STEWARD-QUESTIONS.json"])
    assert {f.anchor: f.marker for f in found}["db"]["text"] == "recommended"


def test_per_job_question_path_is_matched_by_basename_and_answers_land_beside_it():
    from steward.question_adapters.steward_questions import StewardQuestions
    path = ".steward/questions/11ed0088/STEWARD-QUESTIONS.json"
    assert StewardQuestions().wants(path)
    from steward.question_adapters.steward_questions import sibling_answers_path
    assert sibling_answers_path(path) == ".steward/questions/11ed0088/STEWARD-ANSWERS.json"
