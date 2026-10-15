"""Phase 19-02 (G7): owner-named text is byte-equal under the legacy name and names the configured owner otherwise.

The golden files were captured from the unmodified code by tests/owner_golden.py before the sweep began.
"""

from __future__ import annotations

import json
import re

import owner_golden as og
import pytest


def _golden(key: str):
    return json.loads(og.GOLDEN_FILES[key].read_text())


def _names_sam(text: str) -> bool:
    """True when the text names Sam as a word or a key part (`waiting_on_sam`), not inside `same`/`sample`."""
    return bool(re.search(r"(?<![a-z])sam(?![a-z])", text.lower()))


def _flat(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _sam_is_a_legacy_spelling(monkeypatch):
    """The pinned suite name (Sam) is the older spelling here: parsers accept it next to the configured Alex."""
    monkeypatch.setenv("STEWARD_OWNER_LEGACY_NAMES", "Sam")


@pytest.fixture
def alex(monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Alex")
    from steward import owner

    owner.reset_cache()
    yield "Alex"
    owner.reset_cache()


# -- chat and MCP (task 1) ----------------------------------------------------------------------------------------


def test_chat_and_mcp_goldens_under_sam():
    assert og.render(og.chat_text()) == og.GOLDEN_FILES["chat_text"].read_text()
    assert og.render(og.mcp_tools()) == og.GOLDEN_FILES["mcp_tools"].read_text()


def test_golden_builders_are_deterministic():
    assert og.render(og.chat_text()) == og.render(og.chat_text())


def test_chat_and_mcp_under_alex(alex):
    chat = og.chat_text()
    assert not _names_sam(_flat(chat))
    job = chat["primer_job"]
    assert "Alex opened this chat" in job and "Alex's explicit words" in job and "wait for Alex's yes" in job
    assert "as soon as they ask" in job
    send_back = chat["commands"]["job_send_back"]["command"]
    assert "Alex wants to send this job back. Ask them in one short question" in send_back
    assert "Alex opened a side chat" in chat["commands"]["worker"]["command"]

    mcp = og.mcp_tools()
    assert not _names_sam(_flat(mcp["tools"]))
    by_name = {t["name"]: t for t in mcp["tools"]}
    for tool in ("get_decision", "incorporate_job", "steer_job", "answer_questions"):
        assert "Alex" in by_name[tool]["description"] or "Alex" in _flat(by_name[tool]["input_schema"]), tool
    assert "waiting_on_alex" in mcp["payloads"]["get_decision"]
    assert "waiting_on_alex" in mcp["payloads"]["get_questions"]
    assert "waiting_on_sam" not in _flat(mcp["payloads"])


def test_waiting_payload_key_is_unchanged_for_sam():
    mcp = og.mcp_tools()
    assert "waiting_on_sam" in mcp["payloads"]["get_decision"]
    assert "waiting_on_sam" in mcp["payloads"]["get_questions"]


# -- everything else (task 2) -------------------------------------------------------------------------------------


def test_misc_text_golden_under_sam():
    assert og.render(og.misc_text()) == og.GOLDEN_FILES["misc_text"].read_text()


def test_misc_text_under_alex(alex):
    misc = og.misc_text()
    assert not _names_sam(_flat(misc))
    assert "waiting_on_alex" in misc["result_keys"] and "waiting_on_sam" not in misc["result_keys"]
    assert "Alex" in misc["efficiency_findings"][0]["remedy_text"]
    assert "Alex" in misc["efficiency_findings"][2]["remedy_short"]
    assert "without Alex" in misc["contract_boundaries"][-1]
    assert "Alex" in misc["efficiency_prompt"] and "Alex" in misc["resolver_brief"]
    assert "asking Alex (a human)" in misc["watcher_prompt"]
    assert "Alex labels a watcher verdict" in misc["help_watcher"]


def test_human_actors_include_the_owner_key(monkeypatch):
    from steward import watcher_authority as wa

    fixed = {"human", "cli", "moshi", "chatgpt"}
    assert wa.human_actors() == frozenset(fixed | {"sam"})
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Alex")
    assert wa.human_actors() == frozenset(fixed | {"alex", "sam"})
    wa._require_human("alex", "promote")  # no AuthorityError
    with pytest.raises(wa.AuthorityError):
        wa._require_human("worker", "promote")


def test_cli_help_has_no_legacy_name(alex):
    help_watcher = og.misc_text()["help_watcher"]
    assert "Alex labels a watcher verdict/action" in help_watcher
    assert "Alex confirms a trust promotion" in help_watcher
    assert "Alex adjudicates a conflict" in help_watcher
    import contextlib
    import io

    from steward import cli

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
        cli.main(["mcp", "bootstrap", "--help"])
    text = buf.getvalue()
    assert "example.com" in text and "cairn" "labs" not in text and not _names_sam(text)


def test_fill_replaces_placeholders(alex):
    from steward import owner

    assert owner.fill("for {owner} to see; {owner_poss} call; {Owner} decides; {Owner_poss}") == \
        "for Alex to see; Alex's call; Alex decides; Alex's"
    assert owner.fill("no placeholders {x}") == "no placeholders {x}"


def test_outbound_boundary_seeds_as_authority_under_any_spelling(alex):
    from steward import contract as contract_module
    from steward.discovery import Discovery
    from steward.steering import Steering
    from steward.store import Store

    store = Store()
    job = store.create_job("fix the bug", "/repo")
    discovery = Discovery(commands=[], instruction_files=[], languages=[])
    c = contract_module.propose_contract("fix the bug", discovery, None)
    assert c.boundaries[-1].endswith("without Alex")
    legacy = contract_module.Contract(goal="g", boundaries=[c.boundaries[-1].replace("Alex", "Sam")], revision=2)
    contract_module.store_contract(store, job.job_id, contract_module.Contract(goal="g", boundaries=c.boundaries))
    contract_module.seed_boundary_instructions(store, job.job_id, legacy)
    verbs = {i["text"]: i["verb"] for i in Steering(store).active_constraints(job.job_id)}
    assert set(verbs.values()) == {"authority"} and len(verbs) == 2
