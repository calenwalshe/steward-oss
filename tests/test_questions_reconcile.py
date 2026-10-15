"""questions.reconcile (pure) and the events questions.scan appends for each change (G9, G10, T-17-01)."""

import os
import subprocess
from pathlib import Path

from steward import questions
from steward.models import Judgment
from steward.question_adapters.base import Found
from steward.runner_exec import SubprocessRunner
from steward.store import Store

NOW = "2026-09-30T10:00:00+00:00"
LATER = "2026-09-30T11:00:00+00:00"
FILE = "docs/1-CONTEXT.md"


def found(anchor="G1", hsh="h1", marker=None, file=FILE, title="T", line=3, **kw):
    return Found(adapter="gsd_context", file=file, anchor=anchor, line=line, title=title, question=title,
                 options=kw.pop("options", [{"key": "a", "label": "x", "detail": ""}]), recommended=["a"],
                 recommended_text="x", starred=True, hash=hsh, marker=marker, **kw)


def marker(state="decided", who="Sam", text="recommended", date="2026-09-30"):
    return {"state": state, "who": who, "date": date, "text": text}


def rec_of(*fs, existing=(), head="c1", now=NOW, deleted=()):
    by_file: dict[str, list] = {}
    for f in fs:
        by_file.setdefault(f.file, []).append(f)
    return questions.reconcile(list(existing), by_file, set(deleted), head, now)


def answered(record, by="sam", status="answered"):
    r = dict(record)
    r["status"] = status
    r["answer"] = {"option": "recommended", "note": "", "by": by, "origin": "cli", "at": NOW, "date": "2026-09-30",
                   "recommended": True, "area_hash": r["source"]["hash"]}
    return r


def names(out):
    return [(w, e.get("qid")) for w, e, _rule, _j in out.events]


def test_new_area_opens_and_reports_found():
    out = rec_of(found("G1"), found("G2", "h2"))
    assert [r["qid"] for r in out.records] == ["gsd_context:1-CONTEXT.md:G1", "gsd_context:1-CONTEXT.md:G2"]
    assert out.found == {("gsd_context", FILE): ["gsd_context:1-CONTEXT.md:G1", "gsd_context:1-CONTEXT.md:G2"]}
    again = rec_of(found("G1"), found("G2", "h2"), existing=out.records, head="c2", now=LATER)
    assert again.records == out.records and again.found == {} and again.events == []


def test_open_area_changed_updates_in_place():
    first = rec_of(found("G1", "h1")).records
    out = rec_of(found("G1", "h2", title="New title", options=[{"key": "a", "label": "y", "detail": ""}],
                       rationale="why"), existing=first, head="c2", now=LATER)
    (r,) = out.records
    assert r["status"] == "open" and r["flags"]["updated"] and r["title"] == "New title"
    assert r["options"][0]["label"] == "y" and r["rationale"] == "why"
    assert r["source"]["hash"] == "h2" and r["source"]["commit"] == "c2" and r["updated_at"] == LATER
    assert out.events == [("question_updated", {"qid": r["qid"], "old_hash": "h1", "new_hash": "h2"},
                           "questions_reconcile", Judgment.code)]
    assert out.updated == [r["qid"]]
    later = rec_of(found("G1", "h2"), existing=out.records, head="c3")
    assert later.records[0]["flags"]["updated"] and later.events == []  # kept until it is answered


def test_answered_area_changed_goes_stale():
    first = rec_of(found("G1", "h1")).records
    existing = [answered(first[0])]
    out = rec_of(found("G1", "h2", title="Rewritten"), existing=existing, head="c2", now=LATER)
    (r,) = out.records
    assert r["status"] == "stale" and r["answer"]["by"] == "sam" and r["title"] == "Rewritten"
    assert r["stale_from"]["hash"] == "h1" and r["stale_from"]["answer"]["option"] == "recommended"
    assert r["history"][-1]["from"] == "answered" and r["history"][-1]["to"] == "stale"
    assert out.events == [("question_stale", {"qid": r["qid"], "old_hash": "h1", "new_hash": "h2"},
                           "questions_reconcile", Judgment.code)]
    s = questions.summary(out.records)
    assert s["waiting"] == 1 and s["stale"] == 1 and s["needs_you"] == 1 and s["open"] == 0
    # deferred goes stale the same way
    d = rec_of(found("G1", "h9"), existing=[answered(first[0], status="deferred")], now=LATER)
    assert d.records[0]["status"] == "stale"
    # a further change moves the target hash and keeps the one answer
    again = rec_of(found("G1", "h3"), existing=out.records, head="c3", now=LATER)
    (r3,) = again.records
    assert r3["status"] == "stale" and r3["source"]["hash"] == "h3"
    assert r3["stale_from"]["to_hash"] == "h3" and r3["stale_from"]["hash"] == "h1"
    assert r3["stale_from"]["answer"]["by"] == "sam" and again.events == []
    same = rec_of(found("G1", "h3"), existing=again.records, head="c4")
    assert same.records == again.records and same.events == []


def test_own_write_back_never_makes_it_stale():
    first = rec_of(found("G1", "h1")).records
    existing = [answered(first[0])]
    # the write-back changes only the header marker and an `Added by Sam` line: the adapter hash is unchanged
    out = rec_of(found("G1", "h1", marker=marker()), existing=existing, head="c9", now=LATER)
    (r,) = out.records
    assert r["status"] == "answered" and r["written"] == {"via": "scan", "commit": "c9", "at": LATER}
    assert out.events == [] and out.written == [r["qid"]] and out.stale == []
    again = rec_of(found("G1", "h1", marker=marker()), existing=out.records, head="c10", now="later still")
    assert again.records[0]["written"]["commit"] == "c9"  # once


def test_marker_vanishes_is_withdrawn_and_can_reopen():
    first = rec_of(found("G1", "h1"), found("G2", "h2")).records
    existing = [first[0], answered(first[1])]
    out = rec_of(found("G3", "h3"), existing=existing, head="c2", now=LATER)
    by = {r["source"]["anchor"]: r for r in out.records}
    assert by["G1"]["status"] == by["G2"]["status"] == "withdrawn"
    assert by["G2"]["answer"]["by"] == "sam"
    assert by["G2"]["history"][-1]["answer"]["by"] == "sam"  # the answer stays in history
    assert sorted(w for w, _ in names(out)) == ["question_withdrawn", "question_withdrawn"]
    assert by["G3"]["status"] == "open"
    back = rec_of(found("G1", "h1"), found("G3", "h3"), existing=out.records, head="c3", now="t3")
    g1 = {r["source"]["anchor"]: r for r in back.records}["G1"]
    assert g1["status"] == "open" and g1["answer"] is None and back.found[("gsd_context", FILE)] == [g1["qid"]]
    assert [h["to"] for h in g1["history"]] == ["withdrawn", "open"]


def test_deleted_file_withdraws_all_its_records():
    first = rec_of(found("G1"), found("G2", "h2"), found("G1", "x", file="other/2-CONTEXT.md")).records
    out = rec_of(existing=first, deleted={FILE})
    st = {r["source"]["file"] + r["source"]["anchor"]: r["status"] for r in out.records}
    assert st == {FILE + "G1": "withdrawn", FILE + "G2": "withdrawn", "other/2-CONTEXT.md" + "G1": "open"}
    assert len(out.withdrawn) == 2


def test_unsolicited_decided_marker_is_flagged_worker():
    first = rec_of(found("G1", "h1"), found("G2", "h1"), found("G3", "h1")).records
    out = rec_of(
        found("G1", "h1", marker=marker(who="worker", text="b", date="2026-09-30")),
        found("G2", "h1", marker=marker(who="Sam", text="recommended")),
        found("G3", "h1", marker=marker(state="deferred", who="Sam", text="")),
        existing=first, head="c2", now=LATER)
    by = {r["source"]["anchor"]: r for r in out.records}
    for a in ("G1", "G2"):
        r = by[a]
        assert r["status"] == "answered" and r["flags"]["answered_by_worker"]
        assert r["answer"]["option"] == "as_written" and r["answer"]["by"] == "worker" and r["answer"]["origin"] == "file"
    assert by["G1"]["answer"]["note"] == "b" and by["G1"]["answer"]["marker_who"] == "worker"
    assert by["G2"]["answer"]["marker_who"] == "Sam" and by["G2"]["answer"]["recommended"] is True
    assert by["G3"]["status"] == "deferred" and by["G3"]["answer"]["by"] == "worker" and by["G3"]["flags"]["answered_by_worker"]
    assert sorted(out.worker_decided) == sorted(r["qid"] for r in out.records)
    evs = [e for e in out.events if e[0] == "question_worker_decided"]
    assert len(evs) == 3 and all(j == Judgment.code for *_x, j in evs)
    assert questions.summary(out.records)["waiting"] == 0


def test_first_sight_decided_is_not_a_card():
    out = rec_of(found("G1", marker=marker(who="Sam")), found("G2", "h2", marker=marker(who="someone else", text="b")))
    assert [r["source"]["anchor"] for r in out.records] == ["G2"]
    (r,) = out.records
    assert r["status"] == "answered" and r["flags"]["answered_by_worker"] and r["answer"]["by"] == "worker"
    assert out.found == {} and [w for w, *_ in out.events] == ["question_worker_decided"]


def test_unparsed_marker_follows_the_same_table():
    u = found("unparsed-abc12345", "abc12345", unparsed=True, options=[])
    first = rec_of(u).records
    assert first[0]["flags"]["unparsed"]
    assert rec_of(u, existing=first, head="c2").events == []
    gone = rec_of(found("G1"), existing=first, head="c2")
    assert {r["source"]["anchor"]: r["status"] for r in gone.records}["unparsed-abc12345"] == "withdrawn"


def test_input_records_are_not_mutated():
    first = rec_of(found("G1", "h1")).records
    snapshot = repr(first)
    rec_of(found("G1", "h2"), existing=first)
    assert repr(first) == snapshot


# -- events through the scanner -------------------------------------------------------------------------


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def _commit(wt, rel, text):
    p = Path(wt) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "c")


def _job(steward_home):
    store = Store()
    job = store.create_job("g", "/nonexistent")
    wt = store.home / "worktrees" / job.job_id
    wt.mkdir(parents=True)
    _git(wt, "init", "-q")
    _commit(wt, "README.md", "x\n")
    store.add_check(job.job_id, commit_sha=_git(wt, "rev-parse", "HEAD"), command="p", exit_code=0, results={},
                    label="baseline")
    return store, job, wt


def _evs(store, job, what):
    return [e for e in store.events(job.job_id) if e.what == what]


def _answer_in_store(store, job, qid, by="sam"):
    recs = questions.load(store, job.job_id)
    for r in recs:
        if r["qid"] == qid:
            r["status"] = "answered"
            r["answer"] = {"option": "recommended", "note": "", "by": by, "origin": "cli", "at": NOW, "date": "d",
                           "recommended": True, "area_hash": r["source"]["hash"]}
    questions.save(store, job.job_id, recs)


AREA = "### G1 Pick one — OPEN FOR SAM\n- Options: (a) x; (b) y\n- **Recommended: (a).**\n- Rationale: r\n"


def test_scan_events_updated_stale_withdrawn_and_worker_decided(steward_home):
    store, job, wt = _job(steward_home)
    path = "docs/1-CONTEXT.md"
    _commit(wt, path, AREA)
    r = SubprocessRunner()
    questions.scan(store, job.job_id, r)
    qid = "gsd_context:1-CONTEXT.md:G1"
    h1 = questions.load(store, job.job_id)[0]["source"]["hash"]

    _commit(wt, path, AREA.replace("(b) y", "(b) z"))  # open + changed -> updated in place
    res = questions.scan(store, job.job_id, r)
    (ev,) = _evs(store, job, "question_updated")
    assert res.updated == [qid] and ev.judgment == Judgment.code and ev.evidence["old_hash"] == h1
    h2 = ev.evidence["new_hash"]
    assert h2 != h1 and questions.load(store, job.job_id)[0]["flags"]["updated"]

    _answer_in_store(store, job, qid)  # Sam answered (plan 17-02 will do this through the verb)
    _commit(wt, path, AREA.replace("(b) y", "(b) z").replace("- Rationale: r", "- Rationale: changed"))
    res = questions.scan(store, job.job_id, r)
    (st,) = _evs(store, job, "question_stale")
    assert res.stale == [qid] and st.evidence == {"qid": qid, "old_hash": h2, "new_hash": st.evidence["new_hash"]}
    assert questions.summary(questions.load(store, job.job_id))["stale"] == 1

    _commit(wt, path, "no more areas here\n")
    res = questions.scan(store, job.job_id, r)
    assert res.withdrawn == [qid] and len(_evs(store, job, "question_withdrawn")) == 1
    assert res.queue_changed and questions.summary(questions.load(store, job.job_id))["waiting"] == 0

    _commit(wt, path, AREA.replace("— OPEN FOR SAM", "— DECIDED (worker, 2026-09-30): b"))
    questions.scan(store, job.job_id, r)  # first-sight-like: the record is withdrawn, the worker marker is not a reopen
    assert questions.load(store, job.job_id)[0]["status"] == "withdrawn"


def test_scan_open_record_with_worker_marker(steward_home):
    store, job, wt = _job(steward_home)
    _commit(wt, "1-CONTEXT.md", AREA)
    r = SubprocessRunner()
    questions.scan(store, job.job_id, r)
    _commit(wt, "1-CONTEXT.md", AREA.replace("— OPEN FOR SAM", "— DECIDED (Sam, 2026-09-30): recommended"))
    res = questions.scan(store, job.job_id, r)
    (rec,) = questions.load(store, job.job_id)
    assert rec["status"] == "answered" and rec["answer"]["by"] == "worker" and rec["flags"]["answered_by_worker"]
    (ev,) = _evs(store, job, "question_worker_decided")
    assert ev.judgment == Judgment.code and ev.evidence["marker_who"] == "Sam"
    assert res.worker_decided == [rec["qid"]] and not _evs(store, job, "questions_written")


def test_scan_own_write_back_is_not_stale(steward_home):
    store, job, wt = _job(steward_home)
    _commit(wt, "1-CONTEXT.md", AREA)
    r = SubprocessRunner()
    questions.scan(store, job.job_id, r)
    _answer_in_store(store, job, "gsd_context:1-CONTEXT.md:G1")
    wb = (AREA.replace("— OPEN FOR SAM", "— DECIDED (Sam, 2026-09-30): recommended")
          + "- **Added by Sam:** keep it small\n")
    _commit(wt, "1-CONTEXT.md", wb)
    res = questions.scan(store, job.job_id, r)
    (rec,) = questions.load(store, job.job_id)
    assert rec["status"] == "answered" and rec["written"]["via"] == "scan" and res.stale == []
    assert not _evs(store, job, "question_stale") and not _evs(store, job, "questions_written")


def test_queue_changed_flag_empty_to_full_and_back(steward_home):
    store, job, wt = _job(steward_home)
    r = SubprocessRunner()
    _commit(wt, "notes.txt", "n\n")
    assert questions.scan(store, job.job_id, r).queue_changed is False  # empty stays empty
    _commit(wt, "1-CONTEXT.md", AREA)
    up = questions.scan(store, job.job_id, r)
    assert up.queue_changed and up.queue_hash == questions.queue_hash(questions.load(store, job.job_id))
    assert questions.scan(store, job.job_id, r, force=True).queue_changed is False
    _git(wt, "rm", "-q", "1-CONTEXT.md")
    _git(wt, "commit", "-qm", "rm")
    down = questions.scan(store, job.job_id, r)
    assert down.queue_changed and down.withdrawn == ["gsd_context:1-CONTEXT.md:G1"]
    assert questions.summary(questions.load(store, job.job_id))["waiting"] == 0


def test_queue_hash_depends_only_on_waiting_qids():
    a = {"qid": "a", "status": "open"}
    b = {"qid": "b", "status": "stale"}
    c = {"qid": "c", "status": "answered"}
    assert questions.queue_hash([a, b, c]) == questions.queue_hash([b, a])
    assert questions.queue_hash([a, b]) != questions.queue_hash([a])
    assert questions.queue_hash([c]) == questions.queue_hash([])
