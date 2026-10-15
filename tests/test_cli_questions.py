"""`steward answer` and `steward questions` through the (in-process) control relay (Phase 17-02, G3/G4/G9)."""

from __future__ import annotations

import json

from questions_helpers import make_record, make_store_job, seed

from steward import cli, questions
from steward.models import Judgment

Q = "gsd_context:17-CONTEXT.md:G"


def _three(store, job_id):
    return seed(store, job_id, [
        make_record(f"{Q}1", line=1),
        make_record(f"{Q}2", line=2, blocking=True),
        make_record(f"{Q}3", line=3, recommended=("b",)),
    ])


def _events(store, job_id, what):
    return [e for e in store.events(job_id) if e.what == what]


def test_cli_answer_option_end_to_end(steward_home, capsys):
    store, job_id = make_store_job()
    _three(store, job_id)
    rc = cli.main(["answer", job_id, f"{Q}2", "--option", "b", "--origin", "moshi"])
    out = capsys.readouterr()
    assert rc == 0 and "answered 1" in out.out
    rec = {r["qid"]: r for r in questions.load(store, job_id)}[f"{Q}2"]
    assert rec["status"] == "answered"
    ans = rec["answer"]
    assert ans["option"] == "b" and ans["label"] == "Option b" and ans["by"] == "sam" and ans["origin"] == "moshi"
    assert ans["recommended"] is False and ans["area_hash"] == "h1" and ans["at"] and len(ans["date"]) == 10
    assert rec["flags"]["updated"] is False and rec["written"] is None and rec["history"] == []
    evs = _events(store, job_id, "question_answered")
    assert len(evs) == 1 and evs[0].judgment == Judgment.human
    assert evs[0].evidence["origin"] == "moshi" and evs[0].evidence["qid"] == f"{Q}2"
    assert cli.main(["questions", job_id, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["waiting"] == 2 and [c["qid"] for c in data["questions"]] == [f"{Q}1", f"{Q}3"]
    assert cli.main(["questions", job_id, "--json", "--all"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert f"{Q}2" in [c["qid"] for c in data["questions"]]


def test_cli_questions_text_and_json(steward_home, capsys):
    store, job_id = make_store_job()
    _three(store, job_id)
    assert cli.main(["questions", job_id]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "? 3 questions · 3 ★ · 0 need you"
    body = lines[1:]
    assert body[0].startswith("G2  Question G2") and "[blocks work]" in body[0]  # blocking first
    assert body[1].split("  ")[0] == "G1" and body[1].endswith("★ a")
    assert body[2].endswith("★ b")
    assert cli.main(["questions", job_id, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"job_id", "summary", "questions"} and data["job_id"] == job_id
    card = data["questions"][2]
    assert card["qid"] == f"{Q}3"
    assert [o["key"] for o in card["options"]] == ["b", "a", "c"]
    assert [o["recommended"] for o in card["options"]] == [True, False, False]
    for key in ("anchor", "file", "title", "question", "recommended", "recommended_text", "starred", "rationale",
                "evidence", "blocking", "blocking_reason", "status", "flags", "answer", "stale_from"):
        assert key in card
    store2, empty_job = make_store_job()
    assert cli.main(["questions", empty_job]) == 0
    assert capsys.readouterr().out.strip() == "no open questions"


def test_cli_answer_usage_and_unknown(steward_home, capsys):
    store, job_id = make_store_job()
    _three(store, job_id)
    assert cli.main(["answer", "nosuchjob", f"{Q}1", "--option", "a"]) == 1
    capsys.readouterr()
    assert cli.main(["answer", job_id, f"{Q}99", "--option", "a"]) == 1
    assert f"{Q}99" in capsys.readouterr().err
    assert cli.main(["answer", job_id, f"{Q}1", "--option", "a", "--other", "x"]) == 2
    assert cli.main(["answer", job_id, f"{Q}1", "--option", "a", "--defer"]) == 2
    assert cli.main(["answer", job_id, f"{Q}1", "--accept-recommended"]) == 2
    assert cli.main(["answer", job_id]) == 2
    assert cli.main(["answer", job_id, f"{Q}1"]) == 2
    assert cli.main(["answer", job_id, "--option", "a"]) == 2
    err = capsys.readouterr().err
    assert err.count("usage") >= 6 or err.count("error") >= 6
    assert questions.load(store, job_id)[0]["status"] == "open"


def test_answer_is_relayed_not_called_directly(steward_home, monkeypatch):
    store, job_id = make_store_job()
    _three(store, job_id)
    calls = []

    def recorder(store, op, args, *, request_id=None, origin="cli", **_):
        calls.append((op, args, request_id, origin))
        return {"accepted": True, "detail": "ok", "kind": "answer", "duplicate": False, "skipped": []}

    monkeypatch.setattr(cli, "_relay", recorder)
    monkeypatch.setattr(questions, "apply", lambda *a, **k: (_ for _ in ()).throw(AssertionError("direct apply")))
    assert cli.main(["answer", job_id, f"{Q}1", "--option", "a", "--note", "hi", "--request-id", "r1",
                     "--origin", "dash"]) == 0
    op, args, rid, origin = calls[0]
    assert op == "answer" and rid == "r1" and origin == "dash"
    assert set(args) == {"job_id", "answers", "accept_recommended", "qids", "rescan"}
    assert args["answers"] == [{"qid": f"{Q}1", "option": "a", "note": "hi"}]
    assert args["accept_recommended"] is False and args["rescan"] is False and args["qids"] == []
    assert cli.main(["answer", job_id, "--accept-recommended", "G1", "G2"]) == 0
    _, args, _, _ = calls[1]
    assert args["accept_recommended"] is True and args["qids"] == ["G1", "G2"] and args["answers"] == []
    assert cli.main(["answer", job_id, "--rescan"]) == 0
    assert calls[2][1]["rescan"] is True


def test_qid_resolution(steward_home):
    recs = [
        make_record("gsd_context:16-CONTEXT.md:G1", file="16-CONTEXT.md"),
        make_record("gsd_context:16-CONTEXT.md:G2", file="16-CONTEXT.md"),
        make_record("gsd_context:.planning/phases/17-question-queue/17-CONTEXT.md:G2",
                    file=".planning/phases/17-question-queue/17-CONTEXT.md"),
    ]
    rec, err = questions.resolve_qid(recs, "gsd_context:16-CONTEXT.md:G1")
    assert rec is recs[0] and err is None
    rec, err = questions.resolve_qid(recs, "16-CONTEXT.md:G1")
    assert rec is recs[0]
    rec, err = questions.resolve_qid(recs, "G1")
    assert rec is recs[0]
    rec, err = questions.resolve_qid(recs, "17-CONTEXT.md:G2")
    assert rec is recs[2]
    rec, err = questions.resolve_qid(recs, "G2")
    assert rec is None and "16-CONTEXT.md:G2" in err and "17-CONTEXT.md:G2" in err
    rec, err = questions.resolve_qid(recs, "G9")
    assert rec is None and "G9" in err


def test_questions_is_read_only(steward_home, capsys):
    store, job_id = make_store_job()
    _three(store, job_id)
    cli.main(["answer", job_id, f"{Q}1", "--option", "a"])
    capsys.readouterr()

    def state():
        return len(store.events(job_id)), json.dumps(questions.load(store, job_id), sort_keys=True)

    before = state()
    for extra in ([], ["--all"], ["--round", "1"]):
        assert cli.main(["questions", job_id, "--json", *extra]) == 0
        capsys.readouterr()
        assert cli.main(["questions", job_id, *extra]) == 0
        capsys.readouterr()
    assert state() == before


def test_cli_answer_variants_and_history(steward_home, capsys):
    store, job_id = make_store_job()
    _three(store, job_id)
    assert cli.main(["answer", job_id, f"{Q}1", "--other", "replay + live trial", "--note", "trial it"]) == 0
    assert cli.main(["answer", job_id, f"{Q}2", "--defer", "--note", "later"]) == 0
    assert cli.main(["answer", job_id, "--accept-recommended"]) == 0
    out = capsys.readouterr().out
    assert "answered 1" in out and "need you" in out
    recs = {r["qid"]: r for r in questions.load(store, job_id)}
    assert recs[f"{Q}1"]["answer"]["other"] == "replay + live trial" and recs[f"{Q}2"]["status"] == "deferred"
    assert recs[f"{Q}3"]["answer"]["option"] == "b" and recs[f"{Q}3"]["answer"]["recommended"] is True
    assert cli.main(["answer", job_id, f"{Q}1", "--option", "b"]) == 0
    assert cli.main(["answer", job_id, f"{Q}9", "--option", "b"]) == 1
    capsys.readouterr()
    assert cli.main(["answer", job_id, "--accept-recommended", "G1"]) == 2
    assert "already_answered" in capsys.readouterr().err
    n = len(store.events(job_id))
    assert cli.main(["questions", job_id, "--history"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(store.events(job_id)) == n
    assert any("question_answered" in ln and f"{Q}1" in ln for ln in lines)
    assert any("question_deferred" in ln for ln in lines)
    assert any("change" in ln and f"{Q}1" in ln for ln in lines)
    assert cli.main(["questions", job_id, "--history", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) >= {"events", "changes"} and len(data["changes"]) == 1


def test_cli_questions_round_and_all(steward_home, capsys):
    store, job_id = make_store_job()
    seed(store, job_id, [make_record(f"{Q}{i}", line=i) for i in range(1, 7)])
    assert cli.main(["questions", job_id, "--round", "2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].endswith("round 2/2") and len(lines) == 3 and lines[1].startswith("G5")
    assert cli.main(["answer", job_id, "--accept-recommended"]) == 0
    capsys.readouterr()
    assert cli.main(["questions", job_id]) == 0
    assert capsys.readouterr().out.strip() == "no open questions"
    assert cli.main(["questions", job_id, "--all"]) == 0
    assert "[answered]" in capsys.readouterr().out
