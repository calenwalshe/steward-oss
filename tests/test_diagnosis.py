import json

from steward.diagnosis import ClaudeDiagnoser, Diagnosis, FakeDiagnoser, build_prompt, redact
from steward.runner_exec import CommandResult, FakeRunner


def test_redact_masks_bearer_token_api_key_and_known_token_shapes():
    text = (
        "Authorization: Bearer sk-abcdefghijklmnop\n"
        "api_key: sk-abcdefghijklmnop\n"
        "found gh_token=ghp_ABCDEFGHIJKLMNOPQRST12\n"
    )

    out = redact(text)

    assert "sk-abcdefghijklmnop" not in out
    assert "ghp_ABCDEFGHIJKLMNOPQRST12" not in out
    assert "Bearer [redacted]" in out
    assert "api_key=[redacted]" in out


def test_build_prompt_includes_goal_signals_and_redacted_output_and_asks_for_json():
    context = {
        "goal": "fix reconnect logic",
        "signals": {"same_error_count": 3},
        "output_tail": "password: hunter2\nstill stuck",
    }

    prompt = build_prompt(context)

    assert "fix reconnect logic" in prompt
    assert "same_error_count" in prompt
    assert "hunter2" not in prompt
    assert "still stuck" in prompt
    assert "strict JSON object" in prompt


def test_claude_diagnoser_uses_env_dash_u_wrapper_and_output_format_json():
    runner = FakeRunner()

    diagnoser = ClaudeDiagnoser(runner, cwd="/repo")
    diagnoser.diagnose({"goal": "g", "signals": {}, "output_tail": ""})

    assert len(runner.calls) == 1
    argv = runner.calls[0]["argv"]
    assert argv[:4] == ["env", "-u", "ANTHROPIC_API_KEY", "claude"]
    assert "--output-format" in argv and "json" in argv
    assert runner.calls[0]["cwd"] == "/repo"


def test_claude_diagnoser_adds_model_flag_when_set():
    runner = FakeRunner()

    diagnoser = ClaudeDiagnoser(runner, cwd="/repo", model="sol-model")
    diagnoser.diagnose({"goal": "g", "signals": {}, "output_tail": ""})

    argv = runner.calls[0]["argv"]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "sol-model"


def test_claude_diagnoser_omits_model_flag_when_not_set():
    runner = FakeRunner()

    diagnoser = ClaudeDiagnoser(runner, cwd="/repo")
    diagnoser.diagnose({"goal": "g", "signals": {}, "output_tail": ""})

    assert "--model" not in runner.calls[0]["argv"]


def test_claude_diagnoser_parses_wrapped_result_json():
    payload = {"verdict": "retry_with_guidance", "guidance": "add a timeout", "hypothesis": "network hang"}
    wrapped = json.dumps({"result": json.dumps(payload)})
    runner = FakeRunner(
        lambda argv, cwd: CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=wrapped, stderr="", duration_s=0.0)
    )
    diagnoser = ClaudeDiagnoser(runner)

    diagnosis = diagnoser.diagnose({"goal": "g", "signals": {}, "output_tail": ""})

    assert diagnosis == Diagnosis(
        verdict="retry_with_guidance", guidance="add a timeout", hypothesis="network hang"
    )


def test_claude_diagnoser_parses_bare_json_object():
    payload = {"verdict": "replace_worker", "guidance": "", "hypothesis": "process wedged"}
    runner = FakeRunner(
        lambda argv, cwd: CommandResult(
            argv=argv, cwd=cwd, exit_code=0, stdout=json.dumps(payload), stderr="", duration_s=0.0
        )
    )
    diagnoser = ClaudeDiagnoser(runner)

    diagnosis = diagnoser.diagnose({"goal": "g", "signals": {}, "output_tail": ""})

    assert diagnosis.verdict == "replace_worker"


def test_claude_diagnoser_fails_closed_to_needs_human_on_garbage_output():
    runner = FakeRunner(
        lambda argv, cwd: CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout="not json at all", stderr="", duration_s=0.0)
    )
    diagnoser = ClaudeDiagnoser(runner)

    diagnosis = diagnoser.diagnose({"goal": "g", "signals": {}, "output_tail": ""})

    assert diagnosis.verdict == "needs_human"
    assert "diagnosis_parse_failed" in diagnosis.hypothesis


def test_claude_diagnoser_fails_closed_on_unrecognized_verdict():
    runner = FakeRunner(
        lambda argv, cwd: CommandResult(
            argv=argv, cwd=cwd, exit_code=0,
            stdout=json.dumps({"verdict": "do_whatever", "guidance": "", "hypothesis": "h"}),
            stderr="", duration_s=0.0,
        )
    )
    diagnoser = ClaudeDiagnoser(runner)

    diagnosis = diagnoser.diagnose({"goal": "g", "signals": {}, "output_tail": ""})

    assert diagnosis.verdict == "needs_human"


def test_fake_diagnoser_records_context_and_returns_scripted_diagnosis():
    diagnosis = Diagnosis(verdict="needs_human", guidance="", hypothesis="stuck")
    fake = FakeDiagnoser(diagnosis)

    result = fake.diagnose({"goal": "g"})

    assert result == diagnosis
    assert fake.contexts == [{"goal": "g"}]


def test_fake_diagnoser_steps_through_list_and_holds_last():
    d1 = Diagnosis(verdict="retry_with_guidance", guidance="x", hypothesis="h1")
    d2 = Diagnosis(verdict="needs_human", guidance="", hypothesis="h2")
    fake = FakeDiagnoser([d1, d2])

    assert fake.diagnose({}) == d1
    assert fake.diagnose({}) == d2
    assert fake.diagnose({}) == d2
