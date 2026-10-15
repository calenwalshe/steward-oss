"""Spec 2026-10-13 §4.2: one checkpoint record per job, in kv, written read-modify-write; pause_mode from the contract."""
from __future__ import annotations

from steward import pause_checkpoint as pc
from steward.store import Store


def _job(store, pause=None):
    job = store.create_job("g", "/repo")
    contract = {"goal": "g"}
    if pause is not None:
        contract["pause"] = pause
    store.kv_set(job.job_id, "contract", contract)
    return job.job_id


def test_pause_mode_reads_the_stored_contract(steward_home):
    store = Store(steward_home)
    assert pc.pause_mode(store, _job(store)) == "off"            # stored before the field existed
    assert pc.pause_mode(store, _job(store, "unit")) == "unit"
    assert pc.pause_mode(store, _job(store, "natural")) == "natural"
    assert pc.pause_mode(store, _job(store, "weird")) == "off"
    assert pc.pause_mode(store, "no-such-job") == "off"


def test_load_without_a_record_is_the_defaults_and_writes_nothing(steward_home):
    store = Store(steward_home)
    jid = _job(store, "unit")
    cp = pc.load(store, jid)
    assert cp == pc.DEFAULTS and not pc.exists(store, jid)
    for k in ("n", "unit_id", "last_reviewed_sha", "verdict", "verdict_reason", "open_instruction",
              "pending_facts", "provisional_units", "review_in_flight"):
        assert k in cp                                           # the spec §4.2 fields
    cp["pending_facts"].append({"x": 1})
    assert pc.DEFAULTS["pending_facts"] == []                    # defaults are never shared


def test_update_is_read_modify_write(steward_home):
    store = Store(steward_home)
    jid = _job(store, "unit")
    pc.update(store, jid, lambda c: c.update(unit_id="u1", n=1))
    out = pc.update(store, jid, lambda c: c.update(n=c["n"] + 1))
    assert out["n"] == 2 and out["unit_id"] == "u1" and pc.load(store, jid)["n"] == 2 and pc.exists(store, jid)


def test_unknown_keys_are_dropped_on_load(steward_home):
    store = Store(steward_home)
    jid = _job(store, "unit")
    store.kv_set(jid, pc.KEY, {"n": 3, "surprise": True})
    cp = pc.load(store, jid)
    assert cp["n"] == 3 and "surprise" not in cp


def test_append_fact_numbers_caps_and_collapses_whitespace(steward_home):
    store = Store(steward_home)
    jid = _job(store, "unit")
    first = pc.append_fact(store, jid, source="flow", kind="steer_nudge", text="  read\n the   log ", at=5.0)
    assert first == {"id": 1, "source": "flow", "kind": "steer_nudge", "at": 5.0, "text": "read the log",
                     "urgent": False}
    for i in range(pc.MAX_PENDING_FACTS + 5):
        pc.append_fact(store, jid, source="s", kind="k", text="x" * 1000, at=float(i), urgent=True)
    cp = pc.load(store, jid)
    assert len(cp["pending_facts"]) == pc.MAX_PENDING_FACTS and cp["fact_seq"] == pc.MAX_PENDING_FACTS + 6
    assert all(len(f["text"]) == pc.FACT_TEXT_CAP and f["urgent"] for f in cp["pending_facts"])
