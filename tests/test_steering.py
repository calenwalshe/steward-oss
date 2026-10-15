import json

from steward import cli
from steward.models import Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.steering import Steering
from steward.store import Store


def test_record_returns_receipt_with_pending_delivered_and_acknowledged(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    receipt = steering.record(job.job_id, "focus", "work on retry logic first", "req-1")

    assert receipt.accepted is True
    assert receipt.duplicate is False
    assert "Instruction revision 1 recorded (preference)" in receipt.detail
    assert "Delivered: pending" in receipt.detail
    assert "Acknowledged: pending" in receipt.detail


def test_duplicate_request_id_returns_original_receipt_and_does_not_re_record(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    first = steering.record(job.job_id, "avoid", "db/**", "req-dup")
    second = steering.record(job.job_id, "avoid", "some/other/glob", "req-dup")

    assert second.duplicate is True
    assert second.detail == first.detail
    assert len(steering.all_instructions(job.job_id)) == 1


def test_instruction_revisions_survive_a_brand_new_store_instance(steward_home):
    store_a = Store(steward_home)
    job = store_a.create_job("goal", "/repo")
    Steering(store_a).record(job.job_id, "avoid", "steward/store.py", "req-1")

    store_b = Store(steward_home)
    pending = Steering(store_b).pending(job.job_id)

    assert len(pending) == 1
    assert pending[0]["text"] == "steward/store.py"
    assert pending[0]["kind"] == "policy"
    assert pending[0]["enforcement"] == "hard"


def test_mark_delivered_sets_timestamp_only_when_result_ok(steward_home):
    from steward.worker import DeliveryResult

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)
    steering.record(job.job_id, "nudge", "double check the timeout", "req-1")

    steering.mark_delivered(job.job_id, 1, DeliveryResult(ok=False, detail="worker_blocked"))
    still_pending = steering.all_instructions(job.job_id)[0]
    assert still_pending["delivered_at"] is None

    steering.mark_delivered(job.job_id, 1, DeliveryResult(ok=True, detail="delivered"))
    delivered = steering.all_instructions(job.job_id)[0]
    assert delivered["delivered_at"] is not None
    assert delivered["status"] == "delivered"


def test_scan_acks_sets_acknowledged_only_on_observed_ack_text(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)
    steering.record(job.job_id, "focus", "priority X", "req-1")

    acked = steering.scan_acks(job.job_id, "some model chatter with no ack yet")
    assert acked == []
    assert steering.all_instructions(job.job_id)[0]["acknowledged_at"] is None

    acked = steering.scan_acks(job.job_id, "ok, STEWARD-ACK rev=1\ncontinuing now")
    assert acked == [1]
    record = steering.all_instructions(job.job_id)[0]
    assert record["acknowledged_at"] is not None
    assert record["status"] == "acknowledged"


def test_active_constraints_excludes_none_by_default(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)
    steering.record(job.job_id, "avoid", "db/**", "req-1")
    steering.record(job.job_id, "focus", "the retry loop", "req-2")

    active = steering.active_constraints(job.job_id)
    assert len(active) == 2


def test_check_compliance_detects_avoid_glob_violation_and_blocks_closeout(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("goal", str(tmp_path))
    steering = Steering(store)
    steering.record(job.job_id, "avoid", "steward/store.py", "req-1")

    store.add_check(
        job.job_id, commit_sha="base-sha", command="pytest", exit_code=0, results={}, label="baseline"
    )

    def respond(argv, cwd):
        if argv[:2] == ["git", "diff"]:
            return CommandResult(
                argv=argv, cwd=str(cwd), exit_code=0,
                stdout="steward/store.py\nsteward/cli.py\n", stderr="", duration_s=0.0,
            )
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    runner = FakeRunner(respond)

    hits = steering.check_compliance(job.job_id, tmp_path, runner)

    assert hits == ["steward/store.py"]
    events = store.events(job.job_id)
    violation_events = [e for e in events if e.what == "hard constraint violation detected"]
    assert len(violation_events) == 1
    assert violation_events[0].judgment == Judgment.code
    assert violation_events[0].rule == "hard_constraint_avoid"
    assert store.kv_get(job.job_id, "closeout_blocked") is True


def test_check_compliance_no_violation_when_diff_paths_dont_match(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("goal", str(tmp_path))
    steering = Steering(store)
    steering.record(job.job_id, "avoid", "db/**", "req-1")

    def respond(argv, cwd):
        if argv[:2] == ["git", "diff"]:
            return CommandResult(
                argv=argv, cwd=str(cwd), exit_code=0, stdout="steward/cli.py\n", stderr="", duration_s=0.0,
            )
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="", stderr="", duration_s=0.0)

    runner = FakeRunner(respond)
    hits = steering.check_compliance(job.job_id, tmp_path, runner)

    assert hits == []
    assert store.kv_get(job.job_id, "closeout_blocked") is None


def test_goal_verb_records_contract_revision_marker_event(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    steering.record(job.job_id, "goal", "also fix reconnect storms", "req-1")

    events = store.events(job.job_id)
    assert any(e.what == "contract revision required" for e in events)


# -- provenance + optimistic concurrency (API-02, API-03, SP-27, SP-28) ----


def test_record_defaults_origin_cli_and_human_instruction_to_text(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    steering.record(job.job_id, "focus", "work on retry logic first", "req-1")

    record = steering.all_instructions(job.job_id)[0]
    assert record["origin"] == "cli"
    assert record["human_instruction"] == "work on retry logic first"
    assert record["interpreted_instruction"] is None


def test_record_persists_explicit_provenance(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    steering.record(
        job.job_id,
        "focus",
        "prioritize the retry loop",
        "req-1",
        origin="chatgpt",
        human_instruction="please look at retries first",
        interpreted_instruction="prioritize the retry loop",
    )

    record = steering.all_instructions(job.job_id)[0]
    assert record["origin"] == "chatgpt"
    assert record["human_instruction"] == "please look at retries first"
    assert record["interpreted_instruction"] == "prioritize the retry loop"


def test_recorded_event_evidence_carries_full_provenance(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    steering.record(
        job.job_id,
        "focus",
        "prioritize the retry loop",
        "req-1",
        origin="chatgpt",
        human_instruction="please look at retries first",
        interpreted_instruction="prioritize the retry loop",
    )

    events = [e for e in store.events(job.job_id) if e.what == "focus instruction recorded"]
    assert len(events) == 1
    evidence = events[0].evidence
    assert evidence["origin"] == "chatgpt"
    assert evidence["human_instruction"] == "please look at retries first"
    assert evidence["interpreted_instruction"] == "prioritize the retry loop"


def test_contract_seeded_boundary_has_origin_contract(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    steering.record(job.job_id, "avoid", "db/**", "contract-boundary-1-0")

    record = steering.all_instructions(job.job_id)[0]
    assert record["origin"] == "contract"


def test_record_bumps_job_revision_on_every_instruction(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    assert store.get_revision(job.job_id) == 0
    steering.record(job.job_id, "focus", "first", "req-1")
    assert store.get_revision(job.job_id) == 1
    steering.record(job.job_id, "focus", "second", "req-2")
    assert store.get_revision(job.job_id) == 2


def test_record_receipt_detail_includes_job_revision(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    receipt = steering.record(job.job_id, "focus", "first", "req-1")

    assert "Job revision: 1" in receipt.detail


def test_stale_expected_revision_returns_conflict_with_no_side_effects(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)
    steering.record(job.job_id, "focus", "first", "req-1")  # bumps revision to 1

    receipt = steering.record(
        job.job_id, "focus", "second", "req-2", expected_revision=0
    )

    assert receipt.accepted is False
    assert "CONFLICT current_revision=1" in receipt.detail
    assert len(steering.all_instructions(job.job_id)) == 1
    assert store.get_revision(job.job_id) == 1
    events = [e for e in store.events(job.job_id) if e.what == "focus instruction recorded"]
    assert len(events) == 1  # only the first, successful record


def test_stale_conflict_still_dedupes_on_request_id(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)
    steering.record(job.job_id, "focus", "first", "req-1")  # revision -> 1

    first = steering.record(job.job_id, "focus", "second", "req-2", expected_revision=0)
    second = steering.record(job.job_id, "focus", "third", "req-2", expected_revision=0)

    assert first.accepted is False
    assert second.duplicate is True
    assert second.detail == first.detail
    assert len(steering.all_instructions(job.job_id)) == 1


def test_matching_expected_revision_succeeds(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)
    steering.record(job.job_id, "focus", "first", "req-1")  # revision -> 1

    receipt = steering.record(
        job.job_id, "focus", "second", "req-2", expected_revision=1
    )

    assert receipt.accepted is True
    assert len(steering.all_instructions(job.job_id)) == 2
    assert store.get_revision(job.job_id) == 2


# -- origin=trajectory provenance (12-CONTEXT D8, TRJ-09) -------------------


def test_judgment_human_for_default_cli_origin(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    Steering(store).record(job.job_id, "focus", "look at retries", "req-1")

    events = [e for e in store.events(job.job_id) if e.what == "focus instruction recorded"]
    assert events[0].judgment == Judgment.human


def test_judgment_code_for_steward_and_contract_origin(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    steering.record(job.job_id, "nudge", "retry differently", "req-steward", origin="steward")
    steering.record(job.job_id, "avoid", "db/**", "contract-boundary-1-0")

    steward_event = next(e for e in store.events(job.job_id) if e.what == "nudge instruction recorded")
    contract_event = next(e for e in store.events(job.job_id) if e.what == "avoid instruction recorded")
    assert steward_event.judgment == Judgment.code
    assert contract_event.judgment == Judgment.code


def test_judgment_model_informed_code_authorized_for_trajectory_origin(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    Steering(store).record(
        job.job_id, "focus", "narrow scope", "trajectory-rev-1", origin="trajectory"
    )

    events = [e for e in store.events(job.job_id) if e.what == "focus instruction recorded"]
    assert events[0].judgment == Judgment.model_informed_code_authorized


def test_trajectory_origin_rejects_non_focus_nudge_priority_verbs(steward_home):
    import pytest

    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    for verb, text in (("goal", "new goal"), ("avoid", "db/**"), ("authority", "no deploy")):
        with pytest.raises(ValueError):
            steering.record(job.job_id, verb, text, f"trajectory-{verb}", origin="trajectory")


def test_trajectory_origin_allows_focus_nudge_priority(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    for verb in ("focus", "nudge", "priority"):
        receipt = steering.record(
            job.job_id, verb, f"do {verb}", f"trajectory-{verb}", origin="trajectory"
        )
        assert receipt.accepted is True


def test_trajectory_origin_expected_revision_conflict(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)
    steering.record(job.job_id, "focus", "human steer", "req-human")  # revision -> 1

    receipt = steering.record(
        job.job_id, "focus", "model steer", "trajectory-rev-1",
        origin="trajectory", expected_revision=0,
    )

    assert receipt.accepted is False
    assert "CONFLICT current_revision=1" in receipt.detail
    assert len(steering.all_instructions(job.job_id)) == 1


def test_trajectory_origin_duplicate_request_id_returns_same_receipt(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    steering = Steering(store)

    first = steering.record(
        job.job_id, "focus", "narrow scope", "trajectory-rev-1", origin="trajectory"
    )
    second = steering.record(
        job.job_id, "nudge", "different text", "trajectory-rev-1", origin="trajectory"
    )

    assert second.duplicate is True
    assert second.detail == first.detail
    assert len(steering.all_instructions(job.job_id)) == 1


# -- CLI (via main()) ------------------------------------------------------


def _new_job_id(capsys) -> str:
    cli.main(["new", "demo goal", "--repo", "/repo"])
    out = capsys.readouterr().out
    return out.split()[0]


def test_cli_avoid_records_hard_instruction_and_prints_receipt(steward_home, capsys):
    # SP-36 (14-01/NB-03): `new` always stores (and seeds) a contract now,
    # even for a bogus --repo, so the outbound-boundary instruction is
    # already revision 1 by the time this `avoid` runs.
    job_id = _new_job_id(capsys)

    rc = cli.main(["avoid", job_id, "steward/store.py", "--request-id", "req-1"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "accepted=True" in out
    assert "Instruction revision 2 recorded (hard)" in out


def test_cli_focus_nudge_priority_goal_all_record_instructions(steward_home, capsys):
    job_id = _new_job_id(capsys)

    for verb, text in (
        ("focus", "work on retry logic"),
        ("nudge", "double check timeouts"),
        ("priority", "quality"),
        ("goal", "also handle reconnect storms"),
    ):
        rc = cli.main([verb, job_id, text])
        assert rc == 0
        assert "accepted=True" in capsys.readouterr().out


def test_cli_steering_request_id_dedupes(steward_home, capsys):
    job_id = _new_job_id(capsys)

    cli.main(["avoid", job_id, "db/**", "--request-id", "req-dup"])
    first = capsys.readouterr().out

    cli.main(["avoid", job_id, "some/other/glob", "--request-id", "req-dup"])
    second = capsys.readouterr().out

    assert "duplicate=True" not in first
    assert "duplicate=True" in second


def test_cli_receipts_lists_instruction_receipt_text(steward_home, capsys):
    job_id = _new_job_id(capsys)
    cli.main(["avoid", job_id, "steward/store.py"])
    capsys.readouterr()

    rc = cli.main(["receipts", job_id])

    assert rc == 0
    out = capsys.readouterr().out
    # revision 1 is the outbound-boundary instruction `new` already seeded
    # (SP-36/NB-03); this `avoid` is revision 2.
    assert "Instruction revision 2 recorded (hard)" in out
    assert "Delivered: pending" in out
    assert "Acknowledged: pending" in out


def test_cli_receipts_json_reflects_stored_instruction_fields(steward_home, capsys):
    job_id = _new_job_id(capsys)
    cli.main(["nudge", job_id, "double check the timeout"])
    capsys.readouterr()

    rc = cli.main(["receipts", job_id, "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # index 0 is the outbound-boundary instruction `new` already seeded
    # (SP-36/NB-03); the `nudge` above is the last one recorded.
    assert len(payload) == 2
    assert payload[-1]["verb"] == "nudge"
    assert payload[-1]["enforcement"] == "preference"


def test_a_standing_violation_is_logged_once_per_path_not_every_tick(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("goal", str(tmp_path))
    steering = Steering(store)
    steering.record(job.job_id, "avoid", "steward/store.py", "req-1")
    steering.record(job.job_id, "avoid", "steward/store.py", "req-2")  # the same glob twice
    diff = ["steward/store.py\n"]

    def respond(argv, cwd):
        out = diff[0] if argv[:2] == ["git", "diff"] else ""
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)

    runner = FakeRunner(respond)
    for _ in range(5):
        assert steering.check_compliance(job.job_id, tmp_path, runner) == ["steward/store.py"]
    events = [e for e in store.events(job.job_id) if e.what == "hard constraint violation detected"]
    assert len(events) == 1 and events[0].evidence["globs"] == ["steward/store.py"]
    assert store.kv_get(job.job_id, "closeout_blocked") is True

    diff[0] = "steward/store.py\nsteward/store_two.py\n"
    steering.record(job.job_id, "avoid", "steward/store_two.py", "req-3")
    steering.check_compliance(job.job_id, tmp_path, runner)
    events = [e for e in store.events(job.job_id) if e.what == "hard constraint violation detected"]
    assert len(events) == 2 and events[1].evidence["paths"] == ["steward/store_two.py"]  # only the new path
