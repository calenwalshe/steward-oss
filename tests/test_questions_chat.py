"""The per-job chat reads and answers the question queue (Phase 17-06: G3, G13)."""

from __future__ import annotations

import shlex

from questions_helpers import make_record, make_store_job, seed

from steward import chat as chat_module

CLI = "Bash(python3 -m steward.cli "


def _job(n_open: int = 5, *, blocking_first: bool = True, stale: bool = True):
    store, job_id = make_store_job()
    recs = []
    for n in range(1, n_open + 1):
        recs.append(make_record(f"gsd_context:17-CONTEXT.md:G{n}", file="17-CONTEXT.md", line=n * 10,
                                starred=n != 2, recommended=("a",) if n != 2 else (),
                                blocking=blocking_first and n == 3))
    if stale:
        recs[-1]["status"] = "stale"
        recs[-1]["answer"] = {"option": "b", "label": "Option b", "other": "", "note": "", "origin": "dash"}
    seed(store, job_id, recs)
    return store, job_id


def _block(primer: str) -> str:
    start = primer.index("Questions waiting for Sam")
    end = primer.find("\nWatchers feed", start)
    return primer[start:end]


def test_job_allowed_tools_gain_questions_and_answer():
    tools = chat_module.job_allowed_tools("JOB123")
    assert f"{CLI}questions JOB123*)" in tools
    assert f"{CLI}answer JOB123 *)" in tools
    assert f"{CLI}steer JOB123 *)" in tools and f"{CLI}status JOB123*)" in tools
    assert tools[:3] == ["Read", "Glob", "Grep"]
    assert all("JOB123" in t for t in tools[3:]) and not any("OTHER" in t for t in tools)


def test_primer_lists_waiting_questions():
    store, job_id = _job()
    primer = chat_module.build_job_primer(store, job_id)
    block = _block(primer)
    assert "Questions waiting for Sam: 5 (3 ★)" in block  # G5 is stale, G2 has no star
    lines = [ln for ln in block.splitlines() if ln.startswith("  Q")]
    assert len(lines) == 5 and all(len(ln) <= 200 + 2 for ln in lines)
    assert "BLOCKS WORK" in lines[0] and "[G3]" in lines[0]  # blocking first
    assert "a★ Option a" in lines[1] and "| b Option b" in lines[1]
    assert any("STALE (was Option b)" in ln for ln in lines)
    assert f"python3 -m steward.cli answer {job_id} <QID> --option K|--other TEXT [--note TEXT] --origin job-chat" in block
    assert len(block) <= 2500 and len(primer) <= chat_module._JOB_PRIMER_MAX


def test_primer_caps_lines_and_block_size():
    store, job_id = make_store_job()
    recs = []
    for n in range(1, 16):
        r = make_record(f"gsd_context:17-CONTEXT.md:G{n}", file="17-CONTEXT.md", line=n,
                        options=("a", "b", "c", "d"), title="T" * 150)
        for o in r["options"]:
            o["label"] = "L" * 100
        recs.append(r)
    seed(store, job_id, recs)
    primer = chat_module.build_job_primer(store, job_id)
    block = _block(primer)
    lines = [ln for ln in block.splitlines() if ln.startswith("  Q")]
    assert len(lines) == 8 and "+7 more" in block
    assert len(block) <= 2500 and all(len(ln) <= 202 for ln in lines)
    assert len(primer) <= chat_module._JOB_PRIMER_MAX


def test_primer_with_nothing_waiting():
    store, job_id = make_store_job()
    primer = chat_module.build_job_primer(store, job_id)
    assert primer.count("Questions waiting for Sam") == 1
    assert "Questions waiting for Sam: none" in primer
    seed(store, job_id, [make_record(status="answered")])
    assert "Questions waiting for Sam: none" in chat_module.build_job_primer(store, job_id)


def test_primer_redacts_card_text():
    store, job_id = make_store_job()
    r = make_record()
    r["title"] = "use sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 now"
    seed(store, job_id, [r])
    primer = chat_module.build_job_primer(store, job_id)
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789" not in primer


def test_primer_confirmation_rules():
    store, job_id = _job()
    primer = chat_module.build_job_primer(store, job_id)
    for needle in ("--accept-recommended", "--then-accept", "--and-continue", "--write-source"):
        assert needle in primer
    assert "need one confirmation in chat first" in primer
    assert "no confirmation" in primer
    assert "answer" in primer.split("Append `--origin job-chat`", 1)[1].split("\n", 1)[0]
    verbs = next(ln for ln in primer.splitlines() if ln.startswith("You may run only"))
    assert "questions" in verbs and "answer" in verbs


def test_job_chat_plan_uses_the_new_tools():
    store, job_id = _job()
    plan = chat_module.build_chat(store, job_id, "job")
    argv = shlex.split(plan.command)
    tools = argv[argv.index("--allowedTools") + 1:argv.index("--permission-mode")]
    assert f"{CLI}questions {job_id}*)" in tools and f"{CLI}answer {job_id} *)" in tools
    assert "Questions waiting for Sam: 5" in open(plan.primer_path).read()


def test_chats_unchanged_for_a_job_without_questions():
    store, job_id = make_store_job()
    plan = chat_module.build_chat(store, job_id, "job")
    text = open(plan.primer_path).read()
    assert text.count("Questions waiting for Sam") == 1
    assert "Questions waiting for Sam: none" in text
