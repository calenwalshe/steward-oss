"""G16 acceptance gate (17-07): a deterministic replay of the Phase 16 discussion job on the real committed artifacts
(49fb32c before, 6bfcb52 after Sam answered by hand). Real git in tmp dirs only; no model, no herdr, no network."""

from __future__ import annotations

import json

import questions_replay as qr

from steward import questions

_CACHE: dict = {}


def replay(tmp_path_factory, via: str = "engine") -> qr.ReplayResult:
    """The first replay of a kind is computed once, inside a test (so the conftest env isolation is active)."""
    if via not in _CACHE:
        _CACHE[via] = qr.run_replay(tmp_path_factory.mktemp(f"replay_{via}"), via=via)
    return _CACHE[via]


# -- the oracle and the 15 cards ---------------------------------------------------------------------------------


def test_gate_oracle_shape():
    oracle = qr.load_oracle()
    assert "49fb32c" in oracle["source"]
    cards = oracle["cards"]
    assert [c["anchor"] for c in cards] == [f"G{n}" for n in range(1, 16)]
    with_options = [c["anchor"] for c in cards if c["options"]]
    assert with_options == ["G1", "G2", "G3", "G4", "G5", "G6", "G8", "G10"]
    for c in cards:
        keys = [o["key"] for o in c["options"]]
        assert c["has_options"] == bool(keys)
        assert keys in ([], ["a", "b", "c"])
        assert all(k in keys for k in c["recommended"])
        assert not keys or c["recommended"], c["anchor"]  # every option list names a star


def test_gate_15_cards_match_the_oracle(tmp_path_factory):
    res = replay(tmp_path_factory)
    oracle = qr.load_oracle()["cards"]
    cards = res.cards
    assert [c["qid"] for c in cards] == [qr.qid(n) for n in range(1, 16)]
    assert all(c["status"] == "open" and c["starred"] for c in cards)
    for c, want in zip(cards, oracle):
        a = want["anchor"]
        assert c["source"]["anchor"] == a
        assert [o["key"] for o in c["options"]] == [o["key"] for o in want["options"]], a
        assert [o["label"] for o in c["options"]] == [o["label"] for o in want["options"]], a
        assert list(c["recommended"]) == want["recommended"], a
        assert bool(c["options"]) == want["has_options"], a
    by = {c["source"]["anchor"]: c for c in cards}
    assert [o["label"] for o in by["G1"]["options"]] == [
        "fix inside this phase as plan 16-01", "separate quick fix first",
        "leave as is, and the watcher does its own parse"]
    assert [o["label"] for o in by["G6"]["options"]] == [
        "observe only", "Phase-15 ladder T0→T2 (observe → escalate → nudge)",
        "(b) plus a bounded `reconfigure` power"]
    assert list(by["G8"]["recommended"]) == ["a", "c"]
    for a in ("G7", "G9", "G11", "G12", "G13", "G14", "G15"):
        assert by[a]["options"] == [], a


def test_gate_one_questions_found_event_and_summary(tmp_path_factory):
    res = replay(tmp_path_factory)
    found = [ev for what, ev in res.events if what == "questions_found"]
    assert len(found) == 1
    ev = found[0]
    assert ev["adapter"] == "gsd_context" and ev["file"] == qr.CTX and ev["sha"]
    assert ev["qids"] == [qr.qid(n) for n in range(1, 16)]
    s = questions.summary(res.cards)
    assert (s["waiting"], s["starred"], s["needs_you"]) == (15, 15, 0)
    assert json.loads(res.digest())["cards"]  # the digest carries the cards


# -- the patch equals 6bfcb52's markers --------------------------------------------------------------------------

_HUMAN_PAREN = " (recommended replay gate plus an observe-only live trial)"


def _after(name: str) -> str:
    return (qr.FIX / name).read_text()


def _header_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("### G")]


def _norm(text: str) -> str:
    return " ".join(text.split())


def _status_paragraph(text: str) -> str:
    return next(ln for ln in text.splitlines() if ln.startswith("**Status:**"))


def _paragraph(text: str, start: str) -> list[str]:
    lines = text.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith(start))
    j = i
    while j + 1 < len(lines) and lines[j + 1].strip():
        j += 1
    return lines[i:j + 1]


def _heading(text: str) -> str:
    return next(ln for ln in text.splitlines() if ln.startswith("## Gray areas"))


def _footer(text: str) -> str:
    return [ln for ln in text.splitlines() if ln.strip()][-1]


def test_gate_headers_equal_6bfcb52(tmp_path_factory):
    res = replay(tmp_path_factory)
    got = _header_lines(res.context_text)
    want = _header_lines(_after("phase16_context_after.md"))
    assert len(got) == len(want) == 15
    assert got == want
    for ln in got[:14]:
        assert ln.endswith(" — DECIDED (Sam, 2026-09-29): recommended"), ln
    assert got[14].endswith(" — DECIDED (Sam, 2026-09-29): replay + live trial"), got[14]


def test_gate_status_heading_footer_and_marker_count(tmp_path_factory):
    res = replay(tmp_path_factory)
    before = _after("phase16_context_before.md")
    after = _after("phase16_context_after.md")
    text = res.context_text
    assert _heading(text) == "## Gray areas — decided defaults (Sam, 2026-09-29)" == _heading(after)
    assert _footer(text) == _footer(after)
    want_status = _norm(_status_paragraph(after)).replace(_norm(_HUMAN_PAREN), "")
    assert _norm(_status_paragraph(text)) == _norm(want_status)
    assert before.count("OPEN FOR SAM") == 18
    assert text.count("OPEN FOR SAM") == 0

    # the only differences from the 49fb32c file: the 15 headers, the Status paragraph, the heading, the footer and
    # exactly one added line (Sam's G15 note) immediately before G15's Rationale line. Build that expectation from
    # the before text by those substitutions alone and demand the patched text equals it.
    note = f"- **Added by Sam:** {qr.NOTE}"
    lines = before.splitlines()
    patched_headers = iter(_header_lines(text))
    out, i = [], 0
    in_g15 = False
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("### G"):
            in_g15 = ln.startswith("### G15 ")
            out.append(next(patched_headers))
        elif ln.startswith("**Status:**"):
            out.extend(_paragraph(text, "**Status:**"))
            while i + 1 < len(lines) and lines[i + 1].strip():
                i += 1
        elif ln.startswith("## Gray areas"):
            out.append(_heading(text))
        elif ln == _footer(before):
            out.append(_footer(text))
        elif in_g15 and ln.startswith("- Rationale:"):
            out.extend([note, ln])
            in_g15 = False
        else:
            out.append(ln)
        i += 1
    assert "\n".join(out) == "\n".join(text.splitlines())
    assert text.count(note) == 1


def test_gate_discussion_log_block(tmp_path_factory):
    res = replay(tmp_path_factory)
    before = _after("phase16_log_before.md")
    assert res.log_text.startswith(before)
    tail = res.log_text[len(before):]
    assert tail.startswith("\n## Sam answers (2026-09-29)") or tail.startswith("\n\n## Sam answers (2026-09-29)")
    assert "G1–G14" in tail and "recommended" in tail
    assert "G15" in tail and "replay + live trial" in tail and qr.NOTE in " ".join(tail.split())


# -- delivery, audit, determinism --------------------------------------------------------------------------------


def test_gate_delivery_is_one_commit_and_the_scan_confirms(tmp_path_factory):
    res = replay(tmp_path_factory)
    assert res.commits_added == 1
    assert res.commit_message == "docs(16): record Sam decisions"
    assert sorted(res.files_committed) == sorted([qr.CTX, qr.LOG])
    assert len(res.records_after) == 15
    for r in res.records_after:
        assert r["status"] == "answered", r["qid"]
        assert r["written"]["via"] == "commit", r["qid"]
    assert not {"question_stale", "question_worker_decided", "question_updated"} & {w for w, _ in res.events}
    assert res.scan_events_final == []
    written = [ev for w, ev in res.events if w == "questions_written"]
    assert len(written) == 1 and len(written[0]["qids"]) == 15
    answered = [ev for w, ev in res.events if w == "question_answered"]
    assert len(answered) == 15
    assert {ev["origin"] for ev in answered} == {"moshi"} and {ev["by"] for ev in answered} == {"sam"}
    assert all(r["answer"]["by"] == "sam" and r["answer"]["origin"] == "moshi" for r in res.records_after)


def test_gate_answers_are_judged_human(tmp_path_factory):
    res = replay(tmp_path_factory)
    judged = [j for w, j in res.judgments if w == "question_answered"]
    assert len(judged) == 15 and set(judged) == {"human"}
    assert {j for w, j in res.judgments if w == "questions_found"} == {"code"}  # detection is code, no model


def test_gate_cli_path_equals_engine_path(tmp_path_factory):
    engine = replay(tmp_path_factory, "engine")
    cli_res = replay(tmp_path_factory, "cli")
    assert cli_res.digest() == engine.digest()
    assert cli_res.context_text == engine.context_text


def test_gate_three_runs_identical(tmp_path_factory):
    digests = [qr.run_replay(tmp_path_factory.mktemp(f"run{i}")).digest() for i in range(3)]
    assert digests[0] == digests[1] == digests[2]
    assert len(digests[0]) > 10_000


def test_gate_only_git_subprocesses(tmp_path_factory):
    for via in ("engine", "cli"):
        res = replay(tmp_path_factory, via)
        assert res.argv0, via
        assert set(res.argv0) == {"git"}, (via, set(res.argv0))  # G15: no claude, codex, herdr, tg or network


def test_gate_guard_raises_on_a_non_git_subprocess():
    import subprocess

    import pytest

    calls: list = []
    with qr.subprocess_guard(calls):
        with pytest.raises(AssertionError):
            subprocess.run(["herdr", "workspace", "list"])
    assert calls == ["herdr"]


def test_gate_runtime_budget(tmp_path_factory):
    for via in ("engine", "cli"):
        assert replay(tmp_path_factory, via).wall_s < 30.0, via


def test_gate_no_steward_test_session_left(tmp_path_factory):
    # nothing in the replay can call herdr: the guard records git only, so no `steward-test-*` session can exist
    for via in ("engine", "cli"):
        assert not [a for a in replay(tmp_path_factory, via).argv0 if a != "git"]
