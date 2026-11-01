"""Plan 2 Task 8: one sanitizer for every capture path (spec §13 canaries, §14 data scope)."""
from __future__ import annotations

import pytest

from steward import blockers, decision_trace, jev_observer, masking, sanitize
from steward.store import Store
from authority_fixtures import CANARY, owner_job
from ownership_fixtures import CONTROLLER


@pytest.fixture(autouse=True)
def _canary():
    sanitize.register_canary(CANARY)
    yield
    masking.clear()


def test_text_masks_canaries_tokens_bearer_assignments_and_private_keys():
    raw = (f"key {CANARY} token sk-ABCDEFGHIJKLMNOPQRSTUV Authorization: Bearer abc.def password=hunter2 "
           ("-----" + "BEGIN OPENSSH PRIVATE " + "KEY" + "-----\nnot-a-real-key\n-----" + "END OPENSSH PRIVATE " + "KEY" + "----- ghp_") + "a" * 30)
    out = sanitize.text(raw)
    for secret in (CANARY, "sk-ABCDEFGHIJKLMNOPQRSTUV", "abc.def", "hunter2", "not-a-real-key", "ghp_" + "a" * 30):
        assert secret not in out
    assert sanitize.leaks(out) == [] and sanitize.leaks(raw) != []


def test_obj_scrubs_keys_and_values_and_caps():
    out = sanitize.obj({CANARY: [f"x {CANARY}", {"y": "z" * 5000}]}, max_chars=100)
    blob = repr(out)
    assert CANARY not in blob and max(len(v) for v in out[masking.MASK][1].values()) <= 100


def test_capture_keeps_allowlisted_keys_drops_forbidden_and_reports():
    rec = sanitize.capture("diagnostic", {"target": "pi-post", "excerpt": f"ok {CANARY}", "prompt": "full prompt",
                                          "headers": {"Authorization": "Bearer x"}, "raw": "everything",
                                          "extra": 1})
    assert set(rec) == {"target", "excerpt", "_dropped"} and CANARY not in rec["excerpt"]
    assert sorted(rec["_dropped"]) == ["extra", "headers", "prompt", "raw"]


def test_capture_bounds_replay_state():
    big = {"output_tail": "x" * (sanitize.REPLAY_STATE_MAX_CHARS + 10)}
    rec = sanitize.capture("jev_receipt", {"lane": "observe", "state": big})
    assert rec["state"]["omitted"] and rec["state"]["digest"]
    small = sanitize.capture("jev_receipt", {"lane": "observe", "state": {"output_tail": f"t {CANARY}"}})
    assert CANARY not in small["state"]["output_tail"]


def test_unknown_capture_kind_is_refused():
    with pytest.raises(ValueError):
        sanitize.capture("transcript_archive", {"text": "x"})


def test_field_cap_matches_the_decision_trace():
    assert sanitize.FIELD_MAX_CHARS == decision_trace.FIELD_MAX_CHARS


def test_decision_trace_and_blockers_never_store_a_canary(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    decision_trace.record(s, job_id=jid, actor=CONTROLLER, stage="signal", decision=f"saw {CANARY}", now=1.0,
                          evidence_refs=[f"text:{CANARY}"], signal={"note": f"Bearer {CANARY}"})
    blockers.raise_blocker(s, jid, kind="unclassified_hold", subject="x", claim=f"claim {CANARY} password=pw1",
                           evidence=[f"ref {CANARY}"], origin_actor=CONTROLLER, now=1.0)
    dump = "\n".join(str(tuple(r)) for t in ("decision_records", "blockers", "blocker_history")
                     for r in s._conn.execute(f"SELECT * FROM {t}"))
    assert CANARY not in dump and "pw1" not in dump


def test_flow_snapshot_uses_the_shared_scrubber():
    snap = jev_observer.build_snapshot({"goal": f"g {CANARY}", "last": "said", "turns": [["said", f"t {CANARY}"]]},
                                      {"label": "wrong_direction", "confidence": 0.9})
    assert CANARY not in repr(snap)
