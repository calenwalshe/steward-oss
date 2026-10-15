"""questions.apply / card / view: the answer engine (Phase 17-02, G3/G4/G7/G9/G10). No git unless rescan."""

from __future__ import annotations

from datetime import datetime

from questions_helpers import make_record, make_store_job, seed

from steward import questions

Q = "gsd_context:17-CONTEXT.md:G"
NOW = datetime(2026, 9, 30, 12, 0, 0)


def _events(store, job_id, what):
    return [e for e in store.events(job_id) if e.what == what]


def test_apply_option_receipt_and_record(steward_home):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record(f"{Q}1"), make_record(f"{Q}2")])
    rc = questions.apply(store, job_id, answers=[{"qid": f"{Q}1", "option": "b", "note": "  because \n so  "}],
                         origin="moshi", now=NOW)
    assert rc["accepted"] is True and rc["answered"] == 1 and rc["changed"] == 0 and rc["unchanged"] == 0
    assert rc["detail"] == "answered 1 · 1 waiting" and rc["skipped"] == [] and rc["summary"]["waiting"] == 1
    rec = questions.load(store, job_id)[0]
    assert rec["answer"]["date"] == "2026-09-30" and rec["answer"]["note"] == "because so"
    assert rec["answer"]["origin"] == "moshi" and rec["answer"]["by"] == "sam"


def test_apply_unknown_job_and_qid(steward_home):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record(f"{Q}1")])
    bad = questions.apply(store, "nope", answers=[{"qid": "x", "option": "a"}])
    assert bad["accepted"] is False and bad["error_kind"] == "unknown_job"
    rc = questions.apply(store, job_id, answers=[{"qid": f"{Q}7", "option": "a"}])
    assert rc["accepted"] is False and rc["error_kind"] == "unknown_question" and f"{Q}7" in rc["detail"]
    assert rc["skipped"] == [{"qid": f"{Q}7", "reason": "unknown question"}]
    assert questions.load(store, job_id)[0]["status"] == "open" and not store.events(job_id)


def test_card_puts_recommended_first(steward_home):
    rec = make_record(f"{Q}3", options=("a", "b", "c"), recommended=("c", "a"))
    card = questions.card(rec)
    assert [o["key"] for o in card["options"]] == ["a", "c", "b"]
    assert card["anchor"] == "G3" and card["file"] == "17-CONTEXT.md" and card["recommended"] == ["c", "a"]


# -- Task 2: answer kinds, accept-recommended, defer, changes, idempotence, rounds, history ----------


import pytest  # noqa: E402

from steward import masking  # noqa: E402
from steward.control_pane import ControlPane  # noqa: E402
from steward.models import Judgment  # noqa: E402
from steward.runner_exec import FakeRunner, SubprocessRunner  # noqa: E402
from questions_helpers import scan_fixture  # noqa: E402


def _one(store, job_id, qid, **kw):
    return questions.apply(store, job_id, answers=[{"qid": qid, **kw}], now=NOW)


def _rec(store, job_id, qid):
    return next(r for r in questions.load(store, job_id) if r["qid"] == qid)


def test_option_other_note_defer_variants(steward_home):
    store, job_id = make_store_job()
    seed(store, job_id, [
        make_record(f"{Q}1"), make_record(f"{Q}2"), make_record(f"{Q}3", recommended=("a", "b")),
        make_record(f"{Q}4", options=(), recommended=(), starred=False, unparsed=True),
        make_record(f"{Q}5", options=(), recommended=(), starred=True),
        make_record(f"{Q}6", starred=False, recommended=()),
    ])
    rc = _one(store, job_id, f"{Q}1", other="  replay +\n live trial ", note="n" * 700)
    assert rc["accepted"]
    a = _rec(store, job_id, f"{Q}1")["answer"]
    assert a["option"] == "other" and a["other"] == "replay + live trial" and a["label"] == "" and a["recommended"] is False
    assert len(a["note"]) == 500
    assert len(_one(store, job_id, f"{Q}2", other="x" * 400)["accepted"] and _rec(store, job_id, f"{Q}2")["answer"]["other"]) == 300
    # recommended
    _one(store, job_id, f"{Q}3", option="recommended")
    assert _rec(store, job_id, f"{Q}3")["answer"]["recommended"] is True
    store2, job2 = make_store_job()
    seed(store2, job2, [make_record(f"{Q}1")])
    _one(store2, job2, f"{Q}1", option="a")
    assert _rec(store2, job2, f"{Q}1")["answer"]["recommended"] is True  # the sole recommended key
    _one(store, job_id, f"{Q}3", option="b")
    assert _rec(store, job_id, f"{Q}3")["answer"]["recommended"] is False  # two recommended keys: b alone is not "the" star
    # refusals
    bad = _one(store, job_id, f"{Q}1", option="z")
    assert bad["skipped"] == [{"qid": f"{Q}1", "reason": "no such option"}] and not bad["accepted"]
    empty = _one(store, job_id, f"{Q}1", other="   ")
    assert empty["skipped"][0]["reason"] == "empty answer" and not empty["accepted"]
    assert _one(store, job_id, f"{Q}4", option="recommended")["skipped"][0]["reason"] == "no star"
    assert _one(store, job_id, f"{Q}4", option="a")["skipped"][0]["reason"] == "no such option"
    assert _one(store, job_id, f"{Q}4", other="free")["accepted"]
    assert _one(store, job_id, f"{Q}5", option="recommended")["accepted"]  # starred, no options
    assert _one(store, job_id, f"{Q}6", option="recommended")["skipped"][0]["reason"] == "no star"
    assert _one(store, job_id, f"{Q}6", defer=True)["accepted"]


def test_note_is_masked_in_the_event_only(steward_home):
    masking.register(["sk-secret-value-123"])
    try:
        store, job_id = make_store_job()
        seed(store, job_id, [make_record(f"{Q}1")])
        _one(store, job_id, f"{Q}1", option="a", note="use sk-secret-value-123 here")
        assert "sk-secret-value-123" not in _events(store, job_id, "question_answered")[0].evidence["note"]
        assert _rec(store, job_id, f"{Q}1")["answer"]["note"] == "use sk-secret-value-123 here"
    finally:
        masking.clear()


def test_accept_recommended_only_touches_open_starred(steward_home):
    store, job_id = make_store_job()
    seed(store, job_id, [
        make_record(f"{Q}1"), make_record(f"{Q}2", starred=False, recommended=()),
        make_record(f"{Q}3", status="answered"), make_record(f"{Q}4", status="deferred"),
        make_record(f"{Q}5", status="stale"), make_record(f"{Q}6", status="withdrawn"),
        make_record(f"{Q}7"),
    ])
    rc = questions.apply(store, job_id, accept_recommended=True, now=NOW)
    assert rc["accepted"] and rc["answered"] == 2
    got = {s["qid"]: s["reason"] for s in rc["skipped"]}
    assert got == {f"{Q}2": "no_star", f"{Q}3": "already_answered", f"{Q}4": "deferred", f"{Q}5": "stale",
                   f"{Q}6": "withdrawn"}
    assert "answered 2" in rc["detail"] and "need you" in rc["detail"]
    assert _rec(store, job_id, f"{Q}1")["answer"]["option"] == "a"
    assert _rec(store, job_id, f"{Q}2")["status"] == "open" and _rec(store, job_id, f"{Q}5")["status"] == "stale"
    # explicit qids: only those
    store2, job2 = make_store_job()
    seed(store2, job2, [make_record(f"{Q}1"), make_record(f"{Q}7"), make_record(f"{Q}8", starred=False, recommended=())])
    rc = questions.apply(store2, job2, accept_recommended=True, qids=[f"{Q}7", "G8"], now=NOW)
    assert rc["accepted"] and rc["answered"] == 1 and rc["skipped"] == [{"qid": f"{Q}8", "reason": "no_star"}]
    assert _rec(store2, job2, f"{Q}1")["status"] == "open"
    rc = questions.apply(store, job_id, accept_recommended=True, qids=["G404"], now=NOW)
    assert not rc["accepted"] and rc["skipped"] == [{"qid": "G404", "reason": "unknown question"}]
    # nothing left to accept is not an error
    rc = questions.apply(store, job_id, accept_recommended=True, now=NOW)
    assert rc["accepted"] and rc["answered"] == 0


def test_defer_records_status_and_note(steward_home):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record(f"{Q}1"), make_record(f"{Q}2")])
    rc = _one(store, job_id, f"{Q}1", defer=True, note="ask me Friday")
    assert rc["accepted"] and rc["deferred"] == 1 and rc["summary"]["waiting"] == 1 and rc["summary"]["deferred"] == 1
    rec = _rec(store, job_id, f"{Q}1")
    assert rec["status"] == "deferred" and rec["answer"]["option"] == "defer" and rec["answer"]["note"] == "ask me Friday"
    ev = _events(store, job_id, "question_deferred")
    assert len(ev) == 1 and ev[0].judgment == Judgment.human and ev[0].evidence["note"] == "ask me Friday"
    assert ev[0].evidence["origin"] == "cli" and not _events(store, job_id, "question_answered")
    assert [r["qid"] for r in questions.ordered(questions.load(store, job_id))][-1] == f"{Q}1"
    _one(store, job_id, f"{Q}1", option="b")
    rec = _rec(store, job_id, f"{Q}1")
    assert rec["status"] == "answered" and rec["history"][-1]["from"]["option"] == "defer"
    assert _events(store, job_id, "question_answered")[0].evidence["changed"]["before"]["option"] == "defer"


def test_second_different_answer_is_a_recorded_change(steward_home):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record(f"{Q}1")])
    _one(store, job_id, f"{Q}1", option="a")
    rec = _rec(store, job_id, f"{Q}1")
    rec["written"] = {"via": "x"}
    questions.save(store, job_id, [rec])
    first = dict(_rec(store, job_id, f"{Q}1")["answer"])
    rc = questions.apply(store, job_id, answers=[{"qid": f"{Q}1", "option": "b"}], origin="dash", now=NOW)
    assert rc["changed"] == 1 and rc["answered"] == 0
    rec = _rec(store, job_id, f"{Q}1")
    assert rec["answer"]["option"] == "b" and rec["written"] is None
    h = rec["history"][-1]
    assert h["from"] == first and h["to"]["option"] == "b" and h["origin"] == "dash" and h["at"]
    ev = _events(store, job_id, "question_answered")
    assert len(ev) == 2 and ev[1].evidence["changed"]["before"]["option"] == "a"
    assert ev[1].evidence["changed"]["after"]["option"] == "b"
    n = len(store.events(job_id))
    rc = questions.apply(store, job_id, answers=[{"qid": f"{Q}1", "option": "b"}], now=NOW)
    assert rc["unchanged"] == 1 and rc["accepted"] and len(store.events(job_id)) == n
    assert len(_rec(store, job_id, f"{Q}1")["history"]) == 1


def test_stale_is_reconfirmed_by_answering(steward_home):
    store, job_id = make_store_job()
    stale = make_record(f"{Q}1", status="stale", hash="new-hash")
    stale["answer"] = {"option": "a", "label": "Option a", "other": "", "note": "", "by": "sam", "origin": "cli",
                       "at": "t", "date": "2026-09-01", "recommended": True, "area_hash": "old-hash"}
    stale["stale_from"] = {"hash": "old-hash", "answer": stale["answer"], "to_hash": "new-hash"}
    stale["written"] = {"via": "commit"}
    seed(store, job_id, [stale, make_record(f"{Q}2", status="withdrawn")])
    rc = _one(store, job_id, f"{Q}1", option="a")
    assert rc["accepted"] and rc["answered"] == 1
    rec = _rec(store, job_id, f"{Q}1")
    assert rec["status"] == "answered" and "stale_from" not in rec and rec["answer"]["area_hash"] == "new-hash"
    assert rec["written"] is None and rec["history"] == []  # same answer: a re-confirmation, not a change
    gone = _one(store, job_id, f"{Q}2", option="a")
    assert not gone["accepted"] and gone["skipped"] == [{"qid": f"{Q}2", "reason": "withdrawn"}]


def test_request_id_idempotence(steward_home):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record(f"{Q}1")])
    first = questions.apply(store, job_id, answers=[{"qid": f"{Q}1", "option": "a"}], request_id="r1", now=NOW)
    n = len(store.events(job_id))
    again = questions.apply(store, job_id, answers=[{"qid": f"{Q}1", "option": "b"}], request_id="r1", now=NOW)
    assert again["duplicate"] is True and again["detail"] == first["detail"] and again["answered"] == 1
    assert len(store.events(job_id)) == n and _rec(store, job_id, f"{Q}1")["answer"]["option"] == "a"

    from steward import cli

    pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _l: None)
    cmd = {"request_id": "r2", "op": "answer", "origin": "moshi",
           "args": {"job_id": job_id, "answers": [{"qid": f"{Q}1", "option": "b"}], "accept_recommended": False,
                    "qids": [], "rescan": False}}
    one, two = pane.execute(cmd), pane.execute(cmd)
    assert one["duplicate"] is False and two["duplicate"] is True and one["changed"] == 1
    assert len(_events(store, job_id, "question_answered")) == 2


def test_answer_never_touches_git_or_the_worktree(steward_home):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record(f"{Q}1"), make_record(f"{Q}2")])
    fake = FakeRunner()
    questions.apply(store, job_id, answers=[{"qid": f"{Q}1", "option": "a"}], runner=fake, now=NOW)
    questions.apply(store, job_id, accept_recommended=True, runner=fake, now=NOW)
    assert fake.calls == []


def test_rescan_flag_forces_a_scan(steward_home, tmp_path):
    store, job_id = make_store_job()
    scan_fixture(store, job_id, tmp_path)
    n = len(questions.load(store, job_id))
    assert n == 15
    rc = questions.apply(store, job_id, rescan=True, runner=SubprocessRunner())
    assert rc["accepted"] and rc["rescan"]["found"] == [] and rc["rescan"]["stale"] == []
    assert "rescan" in rc["detail"]
    other_store, other = make_store_job()
    rc = questions.apply(other_store, other, rescan=True, runner=SubprocessRunner())
    assert rc["accepted"] and rc["rescan"]["skipped"] == "no worktree" and "no worktree" in rc["detail"]


def test_view_rounds_and_all(steward_home):
    store, job_id = make_store_job()
    recs = [make_record(f"{Q}{i}", line=i, blocking=(i == 9)) for i in range(1, 11)]
    recs += [make_record(f"{Q}{i}", status=s, line=i) for i, s in
             ((11, "answered"), (12, "deferred"), (13, "stale"), (14, "withdrawn"))]
    seed(store, job_id, recs)
    r1 = questions.view(store, job_id, round_no=1)
    assert r1["rounds"] == 3 and r1["round"] == 1 and len(r1["questions"]) == 4
    assert r1["questions"][0]["qid"] == f"{Q}9"  # blocking first
    assert [c["qid"] for c in r1["questions"][1:]] == [f"{Q}1", f"{Q}2", f"{Q}3"]
    assert len(questions.view(store, job_id, round_no=3)["questions"]) == 3  # 11 waiting (10 open + 1 stale)
    beyond = questions.view(store, job_id, round_no=9)
    assert beyond["questions"] == [] and beyond["rounds"] == 3
    everything = questions.view(store, job_id, include_all=True)
    assert [c["status"] for c in everything["questions"]][-4:] == ["stale", "deferred", "answered", "withdrawn"]
    assert len(everything["questions"]) == 14


def test_history_lists_every_question_event_and_change(steward_home):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record(f"{Q}1"), make_record(f"{Q}2")])
    store.append_event(job_id, what="questions_found", evidence={"qids": [f"{Q}1"]}, rule="r", judgment=Judgment.code)
    store.append_event(job_id, what="unrelated", evidence={}, rule="r", judgment=Judgment.code)
    _one(store, job_id, f"{Q}1", option="a")
    _one(store, job_id, f"{Q}1", option="b", note="changed my mind")
    _one(store, job_id, f"{Q}2", defer=True)
    h = questions.history(store, job_id)
    whats = [e["what"] for e in h["events"]]
    assert whats == ["questions_found", "question_answered", "question_answered", "question_deferred"]
    assert [e["seq"] for e in h["events"]] == sorted(e["seq"] for e in h["events"])
    assert h["events"][2]["qid"] == f"{Q}1" and h["events"][2]["ts"]
    assert len(h["changes"]) == 1 and h["changes"][0]["qid"] == f"{Q}1" and h["changes"][0]["from"]["option"] == "a"
    assert h["changes"][0]["to"]["option"] == "b"


def test_accepting_recommended_records_the_concrete_choice(steward_home):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record(f"{Q}1", options=("en", "fr"), recommended=("en",)),
                         make_record(f"{Q}2", options=("a", "b", "c"), recommended=("c", "a")),
                         make_record(f"{Q}3", options=(), recommended=(), starred=True),
                         make_record(f"{Q}4", options=("a", "b"), recommended=(), starred=False)])
    _one(store, job_id, f"{Q}1", option="recommended")
    a = _rec(store, job_id, f"{Q}1")["answer"]
    assert (a["option"], a["label"], a["recommended"]) == ("en", "Option en", True)
    _one(store, job_id, f"{Q}2", option="recommended")
    a = _rec(store, job_id, f"{Q}2")["answer"]
    assert (a["option"], a["label"], a["recommended"]) == ("c+a", "Option c + Option a", True)
    _one(store, job_id, f"{Q}3", option="recommended")
    assert _rec(store, job_id, f"{Q}3")["answer"]["option"] == "as_written"
    bad = _one(store, job_id, f"{Q}4", option="recommended")
    assert not bad["accepted"] and bad["skipped"][0]["reason"] == "no star"
    assert _rec(store, job_id, f"{Q}4")["status"] == "open"


def test_accept_all_records_the_concrete_choice(steward_home):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record(f"{Q}1", options=("en", "fr"), recommended=("en",))])
    questions.apply(store, job_id, accept_recommended=True, now=NOW)
    a = _rec(store, job_id, f"{Q}1")["answer"]
    assert (a["option"], a["recommended"]) == ("en", True)
