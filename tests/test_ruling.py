"""SRD-06/07: the completion ruling's refs are validated by code against the store and the candidate diff."""

from steward import ruling

SHA = "a" * 40


def _ctx(**kw):
    base = dict(sha=SHA, verification_sha=SHA, done_when_count=1, check_ids={"check:3"}, event_seqs={7, 9},
                diff_paths={"steward/a.py", "docs/x.md"})
    base.update(kw)
    return base


def _params(refs, *, ruling_="finished", sha=SHA, item=0, **extra):
    return {"ruling": ruling_, "sha": sha, "evidence": [{"item": item, "refs": refs}], **extra}


def test_good_check_event_and_path_refs_are_finished():
    v = ruling.validate(_params(["check:3", "event:7", "path:steward/a.py"]), **_ctx())
    assert v.ruling == "finished" and v.claimed == "finished" and not v.reasons


def test_bad_check_id_is_unverified():
    v = ruling.validate(_params(["check:99"]), **_ctx())
    assert v.ruling == "unverified" and v.claimed == "finished"
    assert any("check:99" in r for r in v.reasons)


def test_event_seq_must_exist():
    assert ruling.validate(_params(["event:9"]), **_ctx()).ruling == "finished"
    assert ruling.validate(_params(["event:8"]), **_ctx()).ruling == "unverified"


def test_path_must_be_in_the_diff():
    assert ruling.validate(_params(["path:docs/x.md"]), **_ctx()).ruling == "finished"
    v = ruling.validate(_params(["path:steward/other.py"]), **_ctx())
    assert v.ruling == "unverified" and any("steward/other.py" in r for r in v.reasons)


def test_unprefixed_or_malformed_refs_are_unverified():
    for ref in ("steward/a.py", "check:", "event:abc", "nonsense:1", 5):
        assert ruling.validate(_params([ref]), **_ctx()).ruling == "unverified"


def test_sha_mismatch_is_unverified():
    v = ruling.validate(_params(["check:3"], sha="b" * 40), **_ctx())
    assert v.ruling == "unverified" and any("sha" in r for r in v.reasons)
    v = ruling.validate(_params(["check:3"]), **_ctx(verification_sha="c" * 40))
    assert v.ruling == "unverified"


def test_every_done_when_index_needs_a_ref_for_finished():
    v = ruling.validate(_params(["check:3"]), **_ctx(done_when_count=2))
    assert v.ruling == "unverified" and any("done_when 1" in r for r in v.reasons)
    p = _params(["check:3"])
    p["evidence"].append({"item": 1, "refs": ["path:docs/x.md"]})
    assert ruling.validate(p, **_ctx(done_when_count=2)).ruling == "finished"


def test_item_may_be_an_outcome_label_and_an_index_beyond_done_when_is_invalid():
    p = {"ruling": "finished", "sha": SHA, "evidence": [{"item": "o1", "refs": ["check:3"]}]}
    assert ruling.validate(p, **_ctx()).ruling == "finished"
    v = ruling.validate(_params(["check:3"], item=4), **_ctx())
    assert v.ruling == "unverified"


def test_empty_refs_do_not_count():
    assert ruling.validate(_params([]), **_ctx()).ruling == "unverified"


def test_not_met_keeps_instructions_and_needs_no_refs():
    v = ruling.validate({"ruling": "not_met", "sha": SHA, "instructions": "add the missing test"}, **_ctx())
    assert v.ruling == "not_met" and v.instructions == "add the missing test"


def test_not_met_with_an_invalid_ref_is_unverified():
    v = ruling.validate({"ruling": "not_met", "sha": SHA, "evidence": [{"item": 0, "refs": ["check:99"]}]}, **_ctx())
    assert v.ruling == "unverified"


def test_a_lead_unverified_passes_through():
    v = ruling.validate({"ruling": "unverified", "sha": SHA}, **_ctx())
    assert v.ruling == "unverified" and v.claimed == "unverified"


def test_unknown_ruling_value_is_unverified():
    assert ruling.validate({"ruling": "great", "sha": SHA}, **_ctx()).ruling == "unverified"


def test_event_seqs_may_be_a_predicate():
    v = ruling.validate(_params(["event:41"]), **_ctx(event_seqs=lambda seq: seq == 41))
    assert v.ruling == "finished"


def test_check_verify_alias_and_kth_verify_row_resolve():
    ctx = _ctx(check_ids={"check:2"})  # row 1 is the baseline; the verify row is the 2nd check row
    for ref in ("check:2", "check:verify", "check:VERIFY", "check:1"):
        v = ruling.validate(_params([ref]), **ctx)
        assert v.ruling == "finished", (ref, v.reasons)
    assert v.evidence[0]["refs"] == ["check:1"]


def test_genuinely_bad_check_refs_stay_fail_closed():
    for ref in ("check:9", "check:0", "check:", "check:verified", "check:-1", "check:2x"):
        assert ruling.validate(_params([ref]), **_ctx(check_ids={"check:2"})).ruling == "unverified", ref
    assert ruling.validate(_params(["check:verify"]), **_ctx(check_ids=set())).ruling == "unverified"


def test_prompt_states_the_check_ref_numbering():
    from steward import tech_lead_actions as tla
    prompt = tla.job_proposal_prompt({"allowed_actions": ["rule_completion"]})
    assert "completion.check_rows[].id" in prompt and "check:verify" in prompt
