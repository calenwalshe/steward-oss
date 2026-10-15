"""v2.2 Phase 20: the human-owned deadline commitment (DL-01 time rules, DL-02, DL-04). Fakes and temp stores only."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from steward import cli, deadline, masking
from steward.contract import Contract, render
from steward.models import JobState, Judgment
from steward.store import Store

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)  # 13:00 PDT
TZ = "America/Los_Angeles"


def _state(store, job_id, state):
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job_id))


def _job(store, goal="Add CSV import with validation. Optional: CSV export.", state=JobState.RUNNING):
    job = store.create_job(goal, "/tmp")
    _state(store, job.job_id, state)
    return job.job_id


def _create(store, job_id, rid="r1", when="+4h", **kw):
    due = deadline.parse_due(when, now=NOW, tz=TZ)
    return deadline.revise(store, job_id, action="create", judgment=Judgment.human, origin="cli", request_id=rid,
                           reason="initial", due=due, core_outcome=store.get_job(job_id).goal, **kw)


def _counts(store, job_id):
    events = len(store.events(job_id))
    kv = store._conn.execute("SELECT COUNT(*) FROM kv WHERE job_id = ?", (job_id,)).fetchone()[0]
    return events, kv


@pytest.fixture
def store(steward_home):
    return Store(steward_home)


# -- time ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,utc", [
    ("+4h", "2026-10-03T00:00:00Z"), ("+1h30m", "2026-10-02T21:30:00Z"), ("+2d", "2026-10-04T20:00:00Z"),
    ("17:00", "2026-10-03T00:00:00Z"), ("5pm", "2026-10-03T00:00:00Z"), ("2026-10-03 09:00", "2026-10-03T16:00:00Z"),
    ("2026-11-01T01:30-07:00", "2026-11-01T08:30:00Z"), ("+1m", "2026-10-02T20:01:00Z"),
])
def test_parse_due_resolves_at_write_instant_with_offset_and_utc(text, utc):
    due = deadline.parse_due(text, now=NOW, tz=TZ)
    assert due["due_at_utc"] == utc
    assert due["tz"] == TZ and due["input"] == text and due["resolved_at"] == "2026-10-02T20:00:00Z"
    assert datetime.fromisoformat(due["due_at"]).utcoffset() is not None  # local offset kept


@pytest.mark.parametrize("text", ["2026-11-01 01:30", "2026-03-08 02:30", "12:00", "2026-10-01 09:00", "+0m", "soon",
                                  "+h", ""])
def test_parse_due_rejects_ambiguous_nonexistent_past_and_garbage(text):
    with pytest.raises(deadline.DeadlineError):
        deadline.parse_due(text, now=NOW, tz=TZ)


def test_items_are_optional_only_by_explicit_owner_language():
    items = deadline.classify_items("Add CSV import. Optional: CSV export. Nice to have: retry UI. Write docs.")
    assert [(i["id"], i["class"]) for i in items] == [("R1", "required"), ("O1", "optional"), ("O2", "optional"),
                                                      ("R2", "required")]
    bullets = deadline.classify_items("- parser\n- endpoint\n- stretch: export")
    assert [i["class"] for i in bullets] == ["required", "required", "optional"]


# -- human-only, append-only revisions ----------------------------------------------------------------

def test_first_commitment_is_rev1_with_provenance_and_sets_deadline_ever_atomically(store):
    jid = _job(store)
    res = _create(store, jid)
    assert res["rev"] == 1 and not res["duplicate"]
    rec = deadline.current(store, jid)
    assert rec["due_at_utc"] == "2026-10-03T00:00:00Z" and rec["judgment"] == "human" and rec["request_id"] == "r1"
    assert [i["class"] for i in rec["items"]] == ["required", "optional"]
    assert deadline.ever(store, jid)
    ev = [e for e in store.events(jid) if e.what == "deadline_commitment_revised"]
    assert len(ev) == 1 and ev[0].judgment == Judgment.human and ev[0].evidence["deadline_ever_set"] is True


@pytest.mark.parametrize("judgment", [Judgment.code, Judgment.model, Judgment.model_informed_code_authorized])
def test_worker_code_or_model_writes_fail_without_db_change(store, judgment):
    jid = _job(store)
    before = _counts(store, jid)
    with pytest.raises(deadline.AuthorityError):
        deadline.revise(store, jid, action="create", judgment=judgment, origin="cli", request_id="x",
                        reason="plan says so", due=deadline.parse_due("+4h", now=NOW, tz=TZ))
    assert _counts(store, jid) == before and not deadline.ever(store, jid)


@pytest.mark.parametrize("origin", ["steer-agent", "unblock-agent", "trajectory", "steward", "deadline", "chatgpt",
                                    "watcher:tests"])
def test_non_human_origins_cannot_write_even_claiming_human_judgment(store, origin):
    jid = _job(store)
    with pytest.raises(deadline.AuthorityError):
        deadline.revise(store, jid, action="create", judgment=Judgment.human, origin=origin, request_id="x",
                        reason="r", due=deadline.parse_due("+4h", now=NOW, tz=TZ))
    assert deadline.commitment(store, jid) is None
    with pytest.raises(deadline.AuthorityError):
        deadline.set_mode(store, jid, "active", judgment=Judgment.human, origin=origin, request_id="m")


def test_set_appends_full_revision_keeps_rev1_and_records_diff(store):
    jid = _job(store)
    _create(store, jid)
    res = deadline.revise(store, jid, action="set", judgment=Judgment.human, origin="moshi", request_id="r2",
                          reason="moved after check-in", expected_rev=1,
                          due=deadline.parse_due("+5h30m", now=NOW, tz=TZ))
    assert res["rev"] == 2
    doc = deadline.commitment(store, jid)
    assert [r["rev"] for r in doc["revisions"]] == [1, 2]
    assert doc["revisions"][0]["due_at_utc"] == "2026-10-03T00:00:00Z"  # rev 1 is still readable as it was
    assert doc["revisions"][1]["due_at_utc"] == "2026-10-03T01:30:00Z"
    assert doc["revisions"][1]["items"] == doc["revisions"][0]["items"]  # full record, not a delta
    ev = [e for e in store.events(jid) if e.what == "deadline_commitment_revised"][-1]
    assert set(ev.evidence["diff"]) == {"due_at", "due_at_utc"} and ev.evidence["record"]["set_by"] == "moshi"


def test_duplicate_request_id_returns_the_same_revision(store):
    jid = _job(store)
    _create(store, jid)
    before = _counts(store, jid)
    again = _create(store, jid)
    assert again["duplicate"] and again["rev"] == 1 and _counts(store, jid) == before


def test_stale_expected_revision_is_rejected_without_change(store):
    jid = _job(store)
    _create(store, jid)
    deadline.revise(store, jid, action="set", judgment=Judgment.human, origin="cli", request_id="r2", reason="x",
                    due=deadline.parse_due("+6h", now=NOW, tz=TZ))
    before = _counts(store, jid)
    with pytest.raises(deadline.StaleRevision):
        deadline.revise(store, jid, action="set", judgment=Judgment.human, origin="cli", request_id="r3",
                        reason="x", expected_rev=1, due=deadline.parse_due("+7h", now=NOW, tz=TZ))
    assert _counts(store, jid) == before


def test_scope_reclassifies_items_by_revision_and_rejects_unknown(store):
    jid = _job(store)
    _create(store, jid)
    deadline.revise(store, jid, action="scope", judgment=Judgment.human, origin="cli", request_id="s1",
                    reason="R1 can wait", scope={"O1": "required"})
    assert [i["class"] for i in deadline.current(store, jid)["items"]] == ["required", "required"]
    with pytest.raises(deadline.DeadlineError):
        deadline.revise(store, jid, action="scope", judgment=Judgment.human, origin="cli", request_id="s2",
                        reason="x", scope={"Z9": "optional"})


def test_clear_ends_the_clock_but_keeps_history_and_deadline_ever(store):
    jid = _job(store)
    _create(store, jid)
    deadline.revise(store, jid, action="clear", judgment=Judgment.human, origin="cli", request_id="c1", reason="done")
    assert deadline.current(store, jid) is None
    assert len(deadline.commitment(store, jid)["revisions"]) == 2 and deadline.ever(store, jid)
    assert deadline.active_job(store) is None  # the slot is free
    assert deadline.merge_protected(store, jid) == "deadline_mode"


def test_mode_defaults_to_shadow_and_is_human_only(store, monkeypatch):
    jid = _job(store)
    assert deadline.mode(store, jid) == "shadow"
    deadline.set_mode(store, jid, "active", judgment=Judgment.human, origin="cli", request_id="m1")
    assert deadline.mode(store, jid) == "active"
    with pytest.raises(deadline.AuthorityError):
        deadline.set_mode(store, jid, "off", judgment=Judgment.code, origin="cli", request_id="m2")
    with pytest.raises(deadline.DeadlineError):
        deadline.set_mode(store, jid, "loud", judgment=Judgment.human, origin="cli", request_id="m3")


# -- one active deadline job ---------------------------------------------------------------------

def test_second_active_deadline_is_refused_naming_the_holder(store):
    a, b = _job(store), _job(store)
    _create(store, a, rid="a")
    with pytest.raises(deadline.AdmissionError) as exc:
        _create(store, b, rid="b")
    assert exc.value.holder == a and a[:8] in str(exc.value)
    _state(store, a, JobState.STOPPED)  # terminal: the slot frees
    assert _create(store, b, rid="b")["rev"] == 1


def test_concurrent_admission_on_separate_connections_admits_exactly_one(steward_home):
    setup = Store(steward_home)
    jobs = [_job(setup) for _ in range(6)]
    barrier = threading.Barrier(len(jobs))
    results: dict[str, str] = {}

    def admit(jid):
        own = Store(steward_home)  # its own connection, like a second process
        barrier.wait()
        try:
            _create(own, jid, rid=f"req-{jid}")
            results[jid] = "admitted"
        except deadline.AdmissionError:
            results[jid] = "refused"

    threads = [threading.Thread(target=admit, args=(j,)) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results.values()) == ["admitted"] + ["refused"] * 5
    winner = next(j for j, r in results.items() if r == "admitted")
    assert deadline.active_job(setup) == winner


def test_ordinary_queued_job_gets_a_deadline_only_once_active(store):
    jid = _job(store, state=JobState.CONTRACTED)
    with pytest.raises(deadline.DeadlineError, match="only once it is active"):
        deadline.revise(store, jid, action="set", judgment=Judgment.human, origin="cli", request_id="q1",
                        reason="x", due=deadline.parse_due("+4h", now=NOW, tz=TZ))
    assert deadline.commitment(store, jid) is None and not deadline.ever(store, jid)
    _state(store, jid, JobState.RUNNING)
    res = deadline.revise(store, jid, action="set", judgment=Judgment.human, origin="cli", request_id="q2",
                          reason="now active", due=deadline.parse_due("+4h", now=NOW, tz=TZ))
    assert res["rev"] == 1 and deadline.ever(store, jid)


# -- Store transaction semantics ----------------------------------------------------------------------

def test_immediate_rolls_back_every_write_and_is_not_reentrant(store):
    jid = _job(store)
    with pytest.raises(RuntimeError, match="boom"):
        with store.immediate() as tx:
            tx.kv_set(jid, "a", 1)
            tx.append_event(jid, what="x", evidence={}, rule="r", judgment=Judgment.code)
            raise RuntimeError("boom")
    assert store.kv_get(jid, "a") is None and not [e for e in store.events(jid) if e.what == "x"]
    with pytest.raises(RuntimeError, match="reentrant"):
        with store.immediate():
            with store.immediate():
                pass
    with store.immediate() as tx:  # usable again after the refusal
        tx.kv_set(jid, "b", 2)
    assert store.kv_get(jid, "b") == 2


def test_tx_writes_mask_secrets_in_events_and_state_changes(store):
    jid = _job(store, state=JobState.CONTRACTED)
    masking.register(["sk-SECRET-123456"])
    with store.immediate() as tx:
        tx.append_event(jid, what="saw sk-SECRET-123456", evidence={"v": "sk-SECRET-123456"}, rule="r",
                        judgment=Judgment.code)
        tx.set_state(jid, JobState.STOPPED, what="stop sk-SECRET-123456", evidence={"d": "sk-SECRET-123456"},
                     rule="r", judgment=Judgment.code)
    raw = store._conn.execute("SELECT what || evidence FROM events WHERE job_id = ?", (jid,)).fetchall()
    assert raw and all("sk-SECRET-123456" not in r[0] for r in raw)


# -- control relay + views -----------------------------------------------------------------------

def test_cli_deadline_writes_relay_and_reads_show_revisions(store, capsys, monkeypatch):
    monkeypatch.setattr(deadline, "utcnow", lambda: NOW)
    jid = _job(store)
    _create(store, jid)
    assert cli.main(["deadline", jid, "set", "+5h", "--expect-rev", "1", "--reason", "more time"]) == 0
    assert cli.main(["deadline", jid, "set", "+6h", "--expect-rev", "1"]) == 3  # stale
    assert cli.main(["deadline", jid, "scope", "O1", "required"]) == 0
    assert cli.main(["deadline", jid, "mode", "active"]) == 0
    capsys.readouterr()
    assert cli.main(["deadline", jid]) == 0
    out = capsys.readouterr().out
    assert "rev 3" in out and "rev 1 create" in out and "rev 2 set" in out and "more time" in out
    assert "mode: active" in out and "deadline_ever: yes" in out and "O1 [required]" in out
    view = deadline.view(store, jid)
    assert view["rev"] == 3 and [r["judgment"] for r in view["revisions"]] == ["human"] * 3
    assert cli.main(["deadline", jid, "clear"]) == 0
    assert deadline.view(store, jid)["active"] is False and deadline.view(store, jid)["deadline_ever"]


def test_steer_agent_cannot_relay_a_deadline_write(store, monkeypatch):
    monkeypatch.setattr(deadline, "utcnow", lambda: NOW)
    jid = _job(store)
    _create(store, jid)
    rc = cli.main(["deadline", jid, "set", "+9h", "--origin", "steer-agent"])
    assert rc != 0 and deadline.current(store, jid)["rev"] == 1


def test_contract_and_api_show_deadline_beside_unchanged_eta(store):
    from steward.api import StewardAPI

    jid = _job(store)
    _create(store, jid)
    text = render(Contract(goal="g", boundaries=[], done_when=["x"], next_step="n", authority=[], assumptions=[]),
                  deadline.contract_line(store, jid))
    assert "DEADLINE: 2026-10-02 17:00 -07:00 (rev 1)" in text.splitlines()[1]
    got = StewardAPI(store).get_job(jid)
    assert got["deadline"]["due_at_utc"] == "2026-10-03T00:00:00Z" and got["deadline"]["mode"] == "shadow"
    assert "eta" in got and got["eta"] is None  # ETA object unchanged and separate
