import json

import pytest

from steward.review import (
    ClaudeTrajectoryReviewer,
    FakeTrajectoryReviewer,
    PacketConfig,
    Recommendation,
    TrajectoryVerdict,
    build_packet,
    build_prompt,
    parse_verdict,
    verdict_to_dict,
)
from steward.runner_exec import CommandResult, FakeRunner


def _verdict_obj(**overrides) -> dict:
    base = {
        "trajectory": "questionable",
        "confidence": 0.7,
        "urgency": "normal",
        "concern": "wrong_approach",
        "observation": "worker keeps rewriting the same module",
        "recommendation": {
            "action": "change_approach",
            "instruction": "focus on the parser bug, not the renderer",
            "current_approach": "renderer-rewrite",
            "recommended_approach": "parser-fix",
        },
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------- parse_verdict


def test_parse_verdict_accepts_a_well_formed_object():
    verdict = parse_verdict(_verdict_obj())
    assert verdict == TrajectoryVerdict(
        trajectory="questionable",
        confidence=0.7,
        urgency="normal",
        concern="wrong_approach",
        observation="worker keeps rewriting the same module",
        recommendation=Recommendation(
            action="change_approach",
            instruction="focus on the parser bug, not the renderer",
            current_approach="renderer-rewrite",
            recommended_approach="parser-fix",
        ),
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o: o.pop("trajectory"),
        lambda o: o.pop("confidence"),
        lambda o: o.pop("urgency"),
        lambda o: o.pop("concern"),
        lambda o: o.pop("observation"),
        lambda o: o.pop("recommendation"),
        lambda o: o.__setitem__("trajectory", "great"),
        lambda o: o.__setitem__("confidence", 1.5),
        lambda o: o.__setitem__("confidence", -0.1),
        lambda o: o.__setitem__("confidence", "high"),
        lambda o: o.__setitem__("confidence", True),
        lambda o: o.__setitem__("urgency", "urgent"),
        lambda o: o.__setitem__("concern", "vibes"),
        lambda o: o.__setitem__("observation", "x" * 401),
        lambda o: o["recommendation"].__setitem__("action", "rewrite_everything"),
        lambda o: o["recommendation"].__setitem__("instruction", "x" * 501),
        lambda o: o["recommendation"].__setitem__("current_approach", "Not-A-Slug"),
        lambda o: o["recommendation"].__setitem__("current_approach", "x" * 41),
        lambda o: o["recommendation"].__setitem__("recommended_approach", "has spaces"),
        lambda o: o.__setitem__("recommendation", "not a dict"),
        lambda o: o.__setitem__("observation", 42),
    ],
)
def test_parse_verdict_rejects_bad_shapes_never_guesses(mutate):
    obj = _verdict_obj()
    obj["recommendation"] = dict(obj["recommendation"])
    mutate(obj)
    assert parse_verdict(obj) is None


def test_parse_verdict_rejects_non_dict():
    assert parse_verdict("sound") is None
    assert parse_verdict(None) is None
    assert parse_verdict([1, 2, 3]) is None


def test_parse_verdict_accepts_empty_slugs_for_a_sound_verdict():
    obj = _verdict_obj(
        trajectory="sound",
        concern="none",
        recommendation={"action": "continue", "instruction": "", "current_approach": "", "recommended_approach": ""},
    )
    verdict = parse_verdict(obj)
    assert verdict is not None
    assert verdict.recommendation.current_approach == ""


def test_verdict_to_dict_round_trips_shape():
    verdict = parse_verdict(_verdict_obj())
    as_dict = verdict_to_dict(verdict)
    assert as_dict["trajectory"] == "questionable"
    assert as_dict["recommendation"]["action"] == "change_approach"


# --------------------------------------------------------------- build_packet


def _synthetic_snapshot(n_diff=400, n_test=400, n_decisions=400) -> dict:
    """An ~8h synthetic ledger: dense diff/test/decision history, enough to
    force the 12 KB packet cap to actually trim something."""
    lists = {
        "acceptance_revisions": [],
        "plan": [{"items": [f"step-{i}" for i in range(5)], "ts": i} for i in range(20)],
        "decisions": [
            {"kind": "steer", "summary": f"decision {i}" * 3, "origin": "human" if i % 3 == 0 else "trajectory",
             "judgment": "human", "ref": f"rev-{i}", "ts": i}
            for i in range(n_decisions)
        ],
        "discarded_approaches": [{"slug": f"approach-{i}", "reason": "no good", "ts": i} for i in range(10)],
        "milestones": [{"kind": "done", "ref": f"m-{i}", "ts": i} for i in range(50)],
        "diff_samples": [
            {
                "changed_lines": 100 + i,
                "files": [f"steward/module_{i % 30}.py" for _ in range(5)],
                "deleted_or_renamed": 0,
                "new_top_level_dirs": [],
                "manifests_changed": [],
                "summary_commits": [],
                "public_api_changes": [],
                "interface_files": [],
                "weakening": [],
                "parse_errors": 0,
                "ts": i,
            }
            for i in range(n_diff)
        ],
        "test_points": [
            {"passed": i, "failed": 0, "errors": 0, "source": "worker_observed", "ts": i} for i in range(n_test)
        ],
        "cost_points": [{"cost_usd": 0.01, "attempt_no": 1, "source": "worker", "ts": i} for i in range(n_test)],
        "failed_approaches": [],
        "reviews": [],
    }
    return {"started_at": 0, "intent": {"goal": "ship it", "done_when": "tests pass"}, "lists": lists, "dropped": {}}


def test_build_packet_has_all_required_sections():
    snapshot = _synthetic_snapshot(n_diff=1, n_test=1, n_decisions=1)
    contract = {"boundaries": ["no network"], "done_when": ["tests pass"]}
    packet = build_packet(snapshot, contract, [], {}, "renderer-rewrite", ["old-approach"], PacketConfig())

    for key in (
        "intent", "current_acceptance", "plan_history", "decisions", "discarded_approaches", "milestones",
        "human_steering", "diff_summary", "test_trajectory", "cost_time", "triggers", "open_concerns",
        "current_approach",
    ):
        assert key in packet


def test_build_packet_diff_summary_reports_lines_file_count_and_top_paths():
    snapshot = _synthetic_snapshot(n_diff=5, n_test=1, n_decisions=1)
    packet = build_packet(snapshot, {}, [], {}, None, [], PacketConfig())

    assert packet["diff_summary"]["lines"] == 104  # last sample: 100 + 4
    assert packet["diff_summary"]["file_count"] == 5
    assert len(packet["diff_summary"]["top_paths"]) <= 10
    assert all(isinstance(p, str) for p in packet["diff_summary"]["top_paths"])


def test_build_packet_merges_hysteresis_discarded_slugs_not_in_ledger():
    snapshot = _synthetic_snapshot(n_diff=1, n_test=1, n_decisions=1)
    packet = build_packet(snapshot, {}, [], {}, None, ["brand-new-slug"], PacketConfig())

    slugs = {d["slug"] for d in packet["discarded_approaches"]}
    assert "brand-new-slug" in slugs
    assert "approach-0" in slugs  # from the ledger


def test_build_packet_is_at_most_12kb_for_an_8_hour_synthetic_ledger():
    snapshot = _synthetic_snapshot(n_diff=400, n_test=400, n_decisions=400)
    contract = {"boundaries": ["no network"] * 5, "done_when": ["tests pass"] * 5}
    triggers = [{"kind": "cadence", "urgent": False, "detail": {"since_last_review_s": 3600}}]
    open_concerns = {"wrong_approach": {"confidence": 0.7, "evidence_score": 4.0, "sightings": 2, "last_review_idx": 3}}
    discarded = [f"discarded-approach-slug-{i}" for i in range(3000)]

    packet = build_packet(snapshot, contract, triggers, open_concerns, "renderer-rewrite", discarded, PacketConfig())

    size = len(json.dumps(packet).encode("utf-8"))
    assert size <= 12 * 1024
    assert "truncated" in packet
    assert isinstance(packet["truncated"], list) and packet["truncated"]


def test_build_packet_never_contains_output_tail_or_transcript_keys():
    snapshot = _synthetic_snapshot(n_diff=400, n_test=400, n_decisions=400)
    packet = build_packet(snapshot, {}, [], {}, None, [], PacketConfig())
    blob = json.dumps(packet)
    assert "output_tail" not in blob
    assert "transcript" not in blob


def test_build_packet_redacts_secrets_in_free_text_fields():
    snapshot = _synthetic_snapshot(n_diff=1, n_test=1, n_decisions=1)
    snapshot["lists"]["decisions"][0]["summary"] = "api_key: sk-abcdefghijklmnop leaked here"
    packet = build_packet(snapshot, {}, [], {}, None, [], PacketConfig())
    blob = json.dumps(packet)
    assert "sk-abcdefghijklmnop" not in blob


# ----------------------------------------------------------------- build_prompt


def test_build_prompt_states_board_member_role_and_schema():
    packet = {"intent": {"goal": "x"}}
    prompt = build_prompt(packet)
    assert "board member" in prompt
    assert "not another manager" in prompt
    assert "no line-level advice" in prompt.lower() or "line-level advice" in prompt
    assert '"trajectory"' in prompt
    assert '"recommendation"' in prompt


# -------------------------------------------------------- ClaudeTrajectoryReviewer


def test_claude_trajectory_reviewer_argv_uses_env_dash_u_wrapper_and_model():
    runner = FakeRunner()
    reviewer = ClaudeTrajectoryReviewer(runner, "astra-model", timeout=42)

    reviewer.review({"intent": {}})

    assert len(runner.calls) == 1
    argv = runner.calls[0]["argv"]
    assert argv[:4] == ["env", "-u", "ANTHROPIC_API_KEY", "claude"]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "astra-model"
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert runner.calls[0]["timeout"] == 42


def test_claude_trajectory_reviewer_parses_wrapped_result_and_keeps_cost_and_duration():
    verdict_payload = _verdict_obj()
    wrapped = json.dumps({"result": json.dumps(verdict_payload), "total_cost_usd": 0.42, "duration_ms": 1234})
    runner = FakeRunner(
        lambda argv, cwd: CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=wrapped, stderr="", duration_s=0.0)
    )
    reviewer = ClaudeTrajectoryReviewer(runner, "astra-model")

    result = reviewer.review({"intent": {}})

    assert result.verdict is not None
    assert result.verdict.trajectory == "questionable"
    assert result.cost_usd == 0.42
    assert result.duration_ms == 1234
    assert result.error is None


def test_claude_trajectory_reviewer_keeps_wrapper_cost_even_when_verdict_unavailable():
    wrapped = json.dumps({"result": json.dumps({"trajectory": "not_a_real_value"}), "total_cost_usd": 0.05, "duration_ms": 500})
    runner = FakeRunner(
        lambda argv, cwd: CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=wrapped, stderr="", duration_s=0.0)
    )
    reviewer = ClaudeTrajectoryReviewer(runner, "astra-model")

    result = reviewer.review({"intent": {}})

    assert result.verdict is None
    assert result.cost_usd == 0.05
    assert result.duration_ms == 500
    assert result.error == "verdict_parse_failed"


def test_claude_trajectory_reviewer_fails_closed_on_garbage_outer_json():
    runner = FakeRunner(
        lambda argv, cwd: CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout="not json", stderr="", duration_s=0.0)
    )
    reviewer = ClaudeTrajectoryReviewer(runner, "astra-model")

    result = reviewer.review({"intent": {}})

    assert result.verdict is None
    assert result.cost_usd == 0.0
    assert "outer_json_parse_failed" in result.error


# ----------------------------------------------------------- FakeTrajectoryReviewer


def test_fake_trajectory_reviewer_steps_through_scripted_verdicts_and_records_packets():
    fake = FakeTrajectoryReviewer([_verdict_obj(), "unavailable"])

    first = fake.review({"p": 1})
    second = fake.review({"p": 2})

    assert first.verdict is not None
    assert second.verdict is None and second.error == "unavailable"
    assert fake.packets == [{"p": 1}, {"p": 2}]


def test_fake_trajectory_reviewer_holds_last_step():
    fake = FakeTrajectoryReviewer([_verdict_obj(trajectory="sound", concern="none", recommendation={
        "action": "continue", "instruction": "", "current_approach": "", "recommended_approach": ""
    })])

    a = fake.review({})
    b = fake.review({})

    assert a.verdict.trajectory == "sound"
    assert b.verdict.trajectory == "sound"


def test_fake_trajectory_reviewer_from_script_loads_steps(tmp_path):
    path = tmp_path / "script.json"
    path.write_text(json.dumps({"steps": [_verdict_obj(), "unavailable"]}))

    fake = FakeTrajectoryReviewer.from_script(path)
    first = fake.review({})
    second = fake.review({})

    assert first.verdict is not None
    assert second.error == "unavailable"


def test_fake_trajectory_reviewer_from_script_accepts_a_bare_list(tmp_path):
    path = tmp_path / "script.json"
    path.write_text(json.dumps(["unavailable"]))

    fake = FakeTrajectoryReviewer.from_script(path)
    result = fake.review({})

    assert result.error == "unavailable"
