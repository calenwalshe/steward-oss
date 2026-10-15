from steward.brief import render_brief
from steward.contract import propose_contract
from steward.discovery import Discovery


def _contract():
    discovery = Discovery(commands=[], instruction_files=[], languages=[])
    return propose_contract("Make reconnect reliable. Don't touch the db.", discovery, None)


def test_render_brief_has_all_sections():
    brief = render_brief(_contract(), constraints=[], handoff=None, skills=None)

    for header in ("TASK", "RELEVANT FILES", "DONE WHEN", "CONSTRAINTS", "HANDOFF", "METHOD", "PROTOCOL"):
        assert header in brief


def test_render_brief_is_at_most_80_lines():
    brief = render_brief(_contract(), constraints=[], handoff=None, skills=None)
    assert len(brief.splitlines()) <= 80


def test_render_brief_labels_constraints_by_enforcement():
    constraints = [
        {"text": "db/**", "enforcement": "hard", "kind": "policy"},
        {"text": "keep it fast", "enforcement": "preference", "kind": "execution"},
        {"text": "preserve public API", "enforcement": "review", "kind": "execution"},
    ]
    brief = render_brief(_contract(), constraints=constraints, handoff=None, skills=None)

    assert "[HARD] db/**" in brief
    assert "[PREFERENCE] keep it fast" in brief
    assert "[REVIEW] preserve public API" in brief


def test_render_brief_includes_handoff_when_present():
    handoff = {
        "hypothesis": "flaky retry due to backoff jitter",
        "commands_tried": "pytest -k retry",
        "results": "intermittent failure",
        "next_action": "add deterministic clock injection",
    }
    brief = render_brief(_contract(), constraints=[], handoff=handoff, skills=None)

    assert "flaky retry due to backoff jitter" in brief
    assert "add deterministic clock injection" in brief


def test_render_brief_defaults_skills_to_superpowers_pair():
    brief = render_brief(_contract(), constraints=[], handoff=None, skills=None)

    assert "systematic-debugging" in brief
    assert "verification-before-completion" in brief


def test_render_brief_protocol_names_ack_and_done_and_worktree_boundary():
    brief = render_brief(_contract(), constraints=[], handoff=None, skills=None)

    assert "STEWARD-ACK rev=" in brief
    assert "STEWARD-DONE" in brief
    assert "Do not modify files outside this worktree" in brief


def _questions_section(brief: str) -> list[str]:
    lines = brief.splitlines()
    start = lines.index("QUESTIONS")
    return [ln for ln in lines[start + 1:] if ln.strip()]


def test_brief_carries_the_two_line_questions_note():
    for fabric in (False, True):
        brief = render_brief(_contract(), constraints=[], handoff=None, skills=None, fabric=fabric)
        assert brief.index("PROTOCOL") < brief.index("QUESTIONS")
        note = _questions_section(brief)
        assert len(note) == 2
        first, second = note
        for token in ("STEWARD-QUESTIONS.json", "steward-questions", "id", "title", "question", "options",
                      "recommended", "rationale", "blocking", "blocking_reason", "CONTEXT.md", "— OPEN FOR SAM",
                      ".steward/questions/<job8>/", "never at the repo root"):
            assert token in first, token
        assert "assumed (recommended)" in second and "blocking" in second and "stop" in second
        assert len(brief.splitlines()) <= 80
