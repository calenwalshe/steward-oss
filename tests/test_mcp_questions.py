"""ChatGPT's two question tools (Phase 17-06: G3, G4, G13): get_questions (read) and answer_questions (steer)."""

from __future__ import annotations

import json

import pytest
from questions_helpers import (
    CTX, events_named, git, make_record, make_store_job, phase16_qid, scan_fixture, seed, set_state,
)

from steward import cli, mcp_auth, mcp_server, questions
from steward.api import StewardAPI
from steward.control_pane import ControlPane
from steward.models import JobState


class Env:
    def __init__(self, store, steward_home, scopes=None, boom=False):
        self.store = store
        self.relayed: list[dict] = []

        def relay(op, args, *, origin, request_id):
            self.relayed.append({"op": op, "args": args, "origin": origin, "request_id": request_id})
            if boom:
                raise AssertionError("the read tool must never relay")
            pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _l: None)
            return pane.execute({"request_id": request_id or f"r{len(self.relayed)}", "op": op, "args": args, "origin": origin})

        self.server = mcp_server.MCPServer(mcp_server.build_tools(StewardAPI(store, relay=relay)), home=steward_home)
        token, _ = mcp_auth.create_token(list(scopes or mcp_auth.SCOPES), home=steward_home)
        self.info = mcp_auth.authorize(f"Bearer {token}", home=steward_home)

    def raw(self, name, arguments):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}}).encode()
        return json.loads(self.server.handle_authenticated(body, self.info)[1])

    def call(self, name, arguments):
        out = self.raw(name, arguments)
        assert "error" not in out, out
        return out["result"]["structuredContent"]


def _running(store, job_id):
    set_state(store, job_id, JobState.RUNNING)


def _queue():
    store, job_id = make_store_job()
    _running(store, job_id)
    seed(store, job_id, [
        make_record(phase16_qid(1), options=("a", "b", "c"), recommended=("b",), file="16-CONTEXT.md", line=10),
        make_record(phase16_qid(2), options=("a", "b"), recommended=(), starred=False, file="16-CONTEXT.md", line=20),
        make_record(phase16_qid(3), options=("a", "b"), recommended=("a",), blocking=True, file="16-CONTEXT.md",
                    line=30),
        make_record(phase16_qid(4), options=("a", "b"), recommended=("a",), status="answered", file="16-CONTEXT.md",
                    line=40),
    ])
    return store, job_id


# -- get_questions -----------------------------------------------------------------------------------------------


def test_get_questions_reads_the_queue(steward_home):
    store, job_id = _queue()
    env = Env(store, steward_home, boom=True)
    events_before = len(store.events(job_id))
    kv_before = store.kv_get(job_id, questions.KV_KEY)
    res = env.call("get_questions", {"job_id": job_id})
    assert res["waiting_on_sam"] == 3
    assert res["answer_with"]["tool"] == "answer_questions" and res["answer_with"]["example"]
    (job,) = res["jobs"]
    assert job["job_id"] == job_id and job["goal"] and job["summary"]["waiting"] == 3
    cards = job["questions"]
    assert [c["qid"] for c in cards] == [phase16_qid(3), phase16_qid(1), phase16_qid(2)]  # blocking first
    assert cards[0]["blocking"] is True and cards[0]["blocking_reason"]
    assert cards[1]["blocking"] is False
    opts = cards[1]["options"]
    assert opts[0]["key"] == "b" and opts[0]["recommended"] is True  # recommended option first
    assert all(c["status"] in ("open", "stale") for c in cards)
    assert env.relayed == []
    assert len(store.events(job_id)) == events_before
    assert store.kv_get(job_id, questions.KV_KEY) == kv_before

    everything = env.call("get_questions", {"job_id": job_id, "include": "all"})
    assert len(everything["jobs"][0]["questions"]) == 4


def test_get_questions_caps_text(steward_home):
    store, job_id = _queue()
    recs = questions.load(store, job_id)
    recs[0]["question"] = "q" * 900
    recs[0]["rationale"] = "r" * 900
    recs[0]["options"][0]["detail"] = "d" * 900
    questions.save(store, job_id, recs)
    cards = Env(store, steward_home).call("get_questions", {"job_id": job_id})["jobs"][0]["questions"]
    card = next(c for c in cards if c["qid"] == phase16_qid(1))
    assert len(card["question"]) <= 300 and len(card["rationale"]) <= 200
    assert all(len(o["detail"]) <= 200 for o in card["options"])


def test_get_questions_all_jobs_and_rounds_and_errors(steward_home):
    store, job_id = _queue()
    other = store.create_job("no questions here", "/nonexistent-repo")
    _running(store, other.job_id)
    empty = store.create_job("has only answered", "/nonexistent-repo")
    _running(store, empty.job_id)
    seed(store, empty.job_id, [make_record(phase16_qid(9), status="answered")])
    env = Env(store, steward_home)
    res = env.call("get_questions", {})
    assert [j["job_id"] for j in res["jobs"]] == [job_id] and res["waiting_on_sam"] == 3

    big = store.create_job("big queue", "/nonexistent-repo")
    _running(store, big.job_id)
    seed(store, big.job_id, [make_record(f"gsd_context:X-CONTEXT.md:G{n}", file="X-CONTEXT.md", line=n)
                             for n in range(1, 10)])
    page2 = env.call("get_questions", {"job_id": big.job_id, "round": 2})
    assert len(page2["jobs"][0]["questions"]) == 4 and page2["jobs"][0]["round"] == 2
    assert page2["jobs"][0]["rounds"] == 3
    assert "error" in env.raw("get_questions", {"job_id": "nope"})
    assert "error" in env.raw("get_questions", {"job_id": job_id, "round": 0})
    assert "error" in env.raw("get_questions", {"job_id": job_id, "include": "bogus"})


# -- answer_questions --------------------------------------------------------------------------------------------


def test_answer_questions_relays_with_origin_chatgpt(steward_home):
    store, job_id = _queue()
    env = Env(store, steward_home)
    res = env.call("answer_questions", {"job_id": job_id, "answers": [
        {"qid": phase16_qid(2), "option": "b", "note": "x"}]})
    assert res["accepted"] is True
    assert env.relayed[0]["op"] == "answer" and env.relayed[0]["origin"] == "chatgpt"
    rec = {r["qid"]: r for r in questions.load(store, job_id)}[phase16_qid(2)]
    assert rec["status"] == "answered" and rec["answer"]["origin"] == "chatgpt" and rec["answer"]["by"] == "sam"
    (ev,) = events_named(store, job_id, "question_answered")
    assert ev.evidence["origin"] == "chatgpt"

    bulk = env.call("answer_questions", {"job_id": job_id, "accept_recommended": True})
    assert bulk["accepted"] is True
    statuses = {r["qid"]: r["status"] for r in questions.load(store, job_id)}
    assert statuses[phase16_qid(1)] == "answered" and statuses[phase16_qid(3)] == "answered"
    assert statuses[phase16_qid(2)] == "answered"  # already answered before, untouched


def test_accept_recommended_answers_only_open_starred_cards(steward_home):
    store, job_id = _queue()
    Env(store, steward_home).call("answer_questions", {"job_id": job_id, "accept_recommended": True})
    statuses = {r["qid"]: r["status"] for r in questions.load(store, job_id)}
    assert statuses[phase16_qid(1)] == "answered" and statuses[phase16_qid(3)] == "answered"
    assert statuses[phase16_qid(2)] == "open"  # no star


def test_answer_questions_request_id_is_idempotent(steward_home):
    store, job_id = _queue()
    env = Env(store, steward_home)
    args = {"job_id": job_id, "answers": [{"qid": phase16_qid(1), "option": "a"}], "request_id": "rq-1"}
    first = env.call("answer_questions", args)
    again = env.call("answer_questions", args)
    assert first["accepted"] and again.get("duplicate") is True
    assert len(events_named(store, job_id, "question_answered")) == 1


def test_answer_questions_done_flushes_a_needs_human_worktree(steward_home):
    store, job_id = make_store_job()
    wt = scan_fixture(store, job_id)
    set_state(store, job_id, JobState.NEEDS_HUMAN)
    before = git(wt, "rev-parse", "HEAD")
    res = Env(store, steward_home).call("answer_questions", {
        "job_id": job_id, "answers": [{"qid": phase16_qid(1), "option": "a"}], "done": True})
    assert res["accepted"] is True
    assert int(git(wt, "rev-list", "--count", f"{before}..HEAD")) == 1
    assert CTX in git(wt, "show", "--name-only", "--format=", "HEAD")
    rec = {r["qid"]: r for r in questions.load(store, job_id)}[phase16_qid(1)]
    assert rec["written"]["via"] == "commit" and rec["answer"]["origin"] == "chatgpt"


def test_answer_questions_validation(steward_home):
    store, job_id = _queue()
    env = Env(store, steward_home)

    def bad(args, *needles):
        out = env.raw("answer_questions", {"job_id": job_id, **args})
        assert out["error"]["code"] == mcp_server.INVALID_PARAMS, out
        for n in needles:
            assert n in out["error"]["message"], out
        return out

    bad({}, "answers")
    bad({"answers": []}, "answers")
    bad({"answers": [{"qid": "G1", "option": "a", "other": "x"}]}, "answers[0]")
    bad({"answers": [{"qid": "G1", "option": "a"}, {"qid": "G2"}]}, "answers[1]")
    bad({"answers": [{"option": "a"}]}, "answers[0]", "qid")
    bad({"answers": [{"qid": "G1", "option": "a", "defer": True}]}, "answers[0]")
    bad({"answers": [{"qid": "G1", "other": "  "}]}, "answers[0]")
    bad({"answers": [{"qid": f"G{n}", "option": "a"} for n in range(16)]}, "15")
    for flag in ("and_continue", "then_accept", "write_source"):
        bad({"answers": [{"qid": "G1", "option": "a"}], flag: True}, "send_back_job", "accept_job")
    bad({"accept_recommended": True, "and_continue": True}, "send_back_job")
    assert env.relayed == []
    assert "error" in env.raw("answer_questions", {"job_id": "nope", "done": True})

    env.call("answer_questions", {"job_id": job_id, "answers": [
        {"qid": phase16_qid(2), "other": "o" * 900, "note": "n" * 900}]})
    rec = {r["qid"]: r for r in questions.load(store, job_id)}[phase16_qid(2)]
    assert len(rec["answer"]["other"]) <= 300 and len(rec["answer"]["note"]) <= 500
    sent = env.relayed[0]["args"]["answers"][0]
    assert len(sent["other"]) <= 300 and len(sent["note"]) <= 500


def test_defer_is_accepted(steward_home):
    store, job_id = _queue()
    env = Env(store, steward_home)
    env.call("answer_questions", {"job_id": job_id, "answers": [{"qid": phase16_qid(2), "defer": True}]})
    assert {r["qid"]: r["status"] for r in questions.load(store, job_id)}[phase16_qid(2)] == "deferred"


# -- scopes, annotations, counts --------------------------------------------------------------------------------


def test_scopes_and_annotations(steward_home):
    store, job_id = _queue()
    env = Env(store, steward_home)
    g, a = env.server.tools["get_questions"], env.server.tools["answer_questions"]
    assert g.required_scope == "runner:read" and g.annotations == {"readOnlyHint": True}
    assert a.required_scope == "runner:steer"
    assert a.annotations["readOnlyHint"] is False and a.annotations["destructiveHint"] is False

    weak = Env(store, steward_home, scopes=["runner:read"])
    out = weak.raw("answer_questions", {"job_id": job_id, "accept_recommended": True})
    assert out["error"]["code"] == mcp_server.INSUFFICIENT_SCOPE
    assert weak.relayed == []
    assert weak.call("get_questions", {"job_id": job_id})["waiting_on_sam"] == 3


def test_tool_list_counts(steward_home):
    store, _ = _queue()
    env = Env(store, steward_home)
    assert "get_questions" in mcp_server.TOOL_NAMES and "answer_questions" in mcp_server.TOOL_NAMES
    assert len(mcp_server.TOOL_NAMES) == 33 and set(env.server.tools) == set(mcp_server.TOOL_NAMES)
    for name in ("get_questions", "answer_questions"):
        assert "job_id" in env.server.tools[name].input_schema["properties"]
    assert env.server.tools["answer_questions"].input_schema["required"] == ["job_id"]
    assert "blocking" in env.server.tools["answer_questions"].description.lower()
    assert "recommended" in env.server.tools["answer_questions"].description.lower()


@pytest.mark.parametrize("missing", ["job_id"])
def test_answer_questions_needs_a_job(steward_home, missing):
    store, _ = _queue()
    out = Env(store, steward_home).raw("answer_questions", {"done": True})
    assert out["error"]["code"] == mcp_server.INVALID_PARAMS
