from pathlib import Path

from steward.heartbeat import Class, HeartbeatPolicy, Signals, classify, collect, extract_permission_request
from steward.runner_exec import CommandResult, FakeRunner
from steward.worker import WorkerObservation


class _StubWorker:
    def __init__(self, observation: WorkerObservation):
        self._observation = observation

    def observe(self) -> WorkerObservation:
        return self._observation


def _git_runner(*, head="sha-1", diff="", untracked=()):
    def respond(argv, cwd):
        if argv[:2] == ["git", "merge-base"]:  # the job's start commit (differs from `head`: a new commit exists)
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout="base-0\n", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=head + "\n", stderr="", duration_s=0.0)
        if argv[:2] == ["git", "diff"]:
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=diff, stderr="", duration_s=0.0)
        if argv[:2] == ["git", "status"]:
            porcelain = "".join(f"?? {u}\n" for u in untracked)
            return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=porcelain, stderr="", duration_s=0.0)
        return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout="", stderr="", duration_s=0.0)

    return FakeRunner(respond)


# -- collect() ----------------------------------------------------------


def test_collect_first_call_has_zero_since_last_change():
    worker = _StubWorker(WorkerObservation(lifecycle="working", output_tail="doing stuff", pid_alive=True, ts="t"))
    runner = _git_runner(head="sha-1")

    sig, last_change_at = collect(worker, Path("/tmp"), runner, prev=None, now=100.0)

    assert sig.head_sha == "sha-1"
    assert sig.since_last_change_s == 0.0
    assert last_change_at == 100.0


def test_collect_same_head_and_diff_accumulates_since_last_change():
    worker = _StubWorker(WorkerObservation(lifecycle="working", output_tail="", pid_alive=True, ts="t"))
    runner = _git_runner(head="sha-1")

    sig1, last_change_at = collect(worker, Path("/tmp"), runner, prev=None, now=100.0)
    sig2, last_change_at = collect(
        worker, Path("/tmp"), runner, prev=sig1, now=250.0, last_change_at=last_change_at
    )

    assert sig2.since_last_change_s == 150.0


def test_collect_head_change_resets_since_last_change():
    worker = _StubWorker(WorkerObservation(lifecycle="working", output_tail="", pid_alive=True, ts="t"))
    runner1 = _git_runner(head="sha-1")

    sig1, last_change_at = collect(worker, Path("/tmp"), runner1, prev=None, now=100.0)

    runner2 = _git_runner(head="sha-2")
    sig2, last_change_at = collect(
        worker, Path("/tmp"), runner2, prev=sig1, now=250.0, last_change_at=last_change_at
    )

    assert sig2.since_last_change_s == 0.0
    assert last_change_at == 250.0


def test_collect_repeated_identical_error_increments_same_error_count():
    worker = _StubWorker(
        WorkerObservation(lifecycle="working", output_tail="boom\nError: bad thing at line 12", pid_alive=True, ts="t")
    )
    runner = _git_runner(head="sha-1")

    sig1, last_change_at = collect(worker, Path("/tmp"), runner, prev=None, now=0.0)
    worker2 = _StubWorker(
        WorkerObservation(lifecycle="working", output_tail="boom\nError: bad thing at line 99", pid_alive=True, ts="t")
    )
    sig2, last_change_at = collect(
        worker2, Path("/tmp"), runner, prev=sig1, now=1.0, last_change_at=last_change_at
    )
    sig3, last_change_at = collect(
        worker2, Path("/tmp"), runner, prev=sig2, now=2.0, last_change_at=last_change_at
    )

    # digits stripped -> same normalized fingerprint despite differing line numbers
    assert sig1.error_fingerprint == sig2.error_fingerprint == sig3.error_fingerprint
    assert sig3.same_error_count == 3


def test_collect_detects_permission_prompt():
    worker = _StubWorker(
        WorkerObservation(lifecycle="blocked", output_tail="Allow this action? [y/n]", pid_alive=True, ts="t")
    )
    runner = _git_runner()

    sig, _ = collect(worker, Path("/tmp"), runner, prev=None, now=0.0)

    assert sig.permission_prompt is True


def test_collect_detects_running_process_without_prompt():
    worker = _StubWorker(
        WorkerObservation(lifecycle="working", output_tail="===== test session starts =====", pid_alive=True, ts="t")
    )
    runner = _git_runner()

    sig, _ = collect(worker, Path("/tmp"), runner, prev=None, now=0.0)

    assert sig.running_process == "pytest"


# -- classify() -----------------------------------------------------------


def _sig(**overrides) -> Signals:
    base = dict(
        lifecycle="working",
        pid_alive=True,
        head_sha="sha",
        diff_fingerprint="fp",
        last_check_exit=None,
        error_fingerprint="",
        same_error_count=0,
        permission_prompt=False,
        running_process=None,
        since_last_change_s=0.0,
        output_tail="",
    )
    base.update(overrides)
    return Signals(**base)


def test_classify_dead_when_exited():
    assert classify(_sig(lifecycle="exited")) == Class.dead


def test_classify_dead_when_pid_not_alive():
    assert classify(_sig(pid_alive=False, since_last_change_s=5)) == Class.dead


def test_classify_waiting_on_permission_prompt():
    assert classify(_sig(permission_prompt=True, since_last_change_s=5000)) == Class.waiting


def test_classify_waiting_when_blocked():
    assert classify(_sig(lifecycle="blocked", since_last_change_s=5000)) == Class.waiting


def test_classify_progressing_when_change_just_seen():
    assert classify(_sig(since_last_change_s=0.0)) == Class.progressing


def test_classify_quiet_known_process_below_long_process_threshold():
    sig = _sig(since_last_change_s=1000, running_process="pytest")
    assert classify(sig, HeartbeatPolicy(long_process_s=1800, stall_s=600)) == Class.quiet_known_process


def test_classify_suspect_stall_on_repeated_error():
    sig = _sig(since_last_change_s=5, same_error_count=3)
    assert classify(sig) == Class.suspect_stall


def test_classify_suspect_stall_on_long_quiet_with_no_known_process():
    sig = _sig(since_last_change_s=700)
    assert classify(sig, HeartbeatPolicy(stall_s=600)) == Class.suspect_stall


def test_classify_known_process_past_long_threshold_becomes_suspect_stall():
    sig = _sig(since_last_change_s=2000, running_process="make")
    assert classify(sig, HeartbeatPolicy(long_process_s=1800, stall_s=600)) == Class.suspect_stall


def test_classify_idle_when_quiet_but_below_stall_threshold_and_no_process():
    sig = _sig(since_last_change_s=100)
    assert classify(sig, HeartbeatPolicy(stall_s=600)) == Class.idle


# -- extract_permission_request() ------------------------------------------

_REAL_CLAUDE_PROMPT = (
    " Bash command\n"
    "   ls tests && python -m pytest -q 2>&1 | tail -40\n"
    "   List tests and run pytest baseline\n"
    " This command requires approval\n"
    " Do you want to proceed?\n"
    " ❯ 1. Yes\n"
)


def test_extract_permission_request_pulls_command_and_description():
    request = extract_permission_request(_REAL_CLAUDE_PROMPT)

    assert "ls tests && python -m pytest -q 2>&1 | tail -40" in request
    assert "List tests and run pytest baseline" in request
    assert "Bash command" not in request
    assert "This command requires approval" not in request
    assert "Do you want to proceed?" not in request


def test_extract_permission_request_falls_back_when_no_recognizable_prompt():
    out = extract_permission_request("nothing interesting here")
    assert out.startswith("worker requests permission (see pane)")
    assert "nothing interesting here" in out  # the pane tail rides along so a resolver can judge
    assert extract_permission_request("") == "worker requests permission (see pane)"


def test_unparsed_prompt_carries_redacted_capped_last_15_lines():
    text = "\n".join(f"line {i}" for i in range(40)) + "\nexport TOKEN=example-placeholder\n"
    out = extract_permission_request(text)
    assert "line 39" in out and "line 26" in out and "line 25" not in out and "line 0" not in out
    assert "example-placeholder" not in out
    assert len(extract_permission_request("x" * 5000)) <= 1200


# Codex cards, shaped like the 2026-10-05 job 6369d958 stall (no real capture was on disk to copy verbatim).
_CODEX_MCP_CARD = (
    "\n"
    "  Field 1/1\n"
    "  Allow Stew to run tool \"stew.get_recent_events\"?\n"
    "\n"
    "  {\n"
    "    \"job_id\": \"6369d958\",\n"
    "    \"limit\": 20\n"
    "  }\n"
    "\n"
    "  \u203a 1. Allow\n"
    "    2. Allow for this session\n"
    "    3. Always allow\n"
    "    4. Cancel\n"
    "  enter to submit | esc to cancel\n"
)

_CODEX_SHELL_CARD = (
    "  Would you like to run the following command?\n"
    "\n"
    "  Reason: needs network to fetch the baseline\n"
    "\n"
    "  $ git fetch origin master\n"
    "\n"
    "  \u203a 1. Yes, proceed (y)\n"
    "    2. Yes, and don't ask again for commands that start with `git fetch origin master` (p)\n"
    "    3. No, and tell Codex what to do differently (esc)\n"
)


def test_extract_codex_mcp_card_names_tool_and_args():
    out = extract_permission_request(_CODEX_MCP_CARD)
    assert out.startswith("stew.get_recent_events ")
    assert '"job_id": "6369d958"' in out and '"limit": 20' in out
    assert "Allow for this session" not in out and "see pane" not in out


def test_extract_codex_shell_card_pulls_command_and_reason():
    out = extract_permission_request(_CODEX_SHELL_CARD)
    assert "git fetch origin master" in out and "needs network" in out
    assert "$ " not in out and "Yes, proceed" not in out


def test_extract_codex_card_redacts_args():
    card = _CODEX_MCP_CARD.replace('"limit": 20', '"to" + "ken": "example-placeholder"')
    assert "example-placeholder" not in extract_permission_request(card)


def test_extract_prefers_the_later_card_across_runtimes():
    out = extract_permission_request(_REAL_CLAUDE_PROMPT + "\n" + _CODEX_MCP_CARD)
    assert out.startswith("stew.get_recent_events")
    out = extract_permission_request(_CODEX_MCP_CARD + "\n" + _REAL_CLAUDE_PROMPT)
    assert "pytest" in out and "stew." not in out


def test_codex_mcp_card_reports_session_option():
    from steward.heartbeat import codex_mcp_card

    assert codex_mcp_card(_CODEX_MCP_CARD) == ("stew.get_recent_events", True)
    assert codex_mcp_card(_CODEX_SHELL_CARD) is None
    assert codex_mcp_card(_CODEX_MCP_CARD.replace("2. Allow for this session", "2. Deny")) == ("stew.get_recent_events", False)


def test_extract_permission_request_caps_at_300_chars():
    long_line = "x" * 500
    text = f" Bash command\n   {long_line}\n Do you want to proceed?\n"

    request = extract_permission_request(text)

    assert len(request) <= 300


def test_extract_permission_request_redacts_secrets():
    text = (
        " Bash command\n"
        "   curl -H 'Authorization: Bearer " + "example-placeholder" + ' https://example.com\n"
        " Do you want to proceed?\n"
    )

    request = extract_permission_request(text)

    assert "example-placeholder" not in request


def test_answered_prompt_in_scrollback_is_not_a_live_prompt_when_herdr_says_working():
    from steward.heartbeat import _detect_permission_prompt

    tail = "Bash command\n  pytest\nThis command requires approval\nDo you want to proceed?\n❯ 1. Yes\n" + "\n".join(
        f"working line {i}" for i in range(30)
    )
    assert _detect_permission_prompt("working", tail) is False
    assert _detect_permission_prompt("blocked", "") is True
    # unknown lifecycle: only the bottom of the screen counts
    assert _detect_permission_prompt("unknown", tail) is False


def test_extract_uses_last_card_not_first():
    from steward.heartbeat import extract_permission_request

    tail = (
        " Bash command\n   uname -r\n This command requires approval\n Do you want to proceed?\n ❯ 1. Yes\n"
        "● Kernel: 7.0\n Ran 1 shell command\n"
        " Bash command\n Tip: auto mode handles these prompts for you\n   python3 -m pytest -q\n   Run test suite\n"
        " This command requires approval\n Do you want to proceed?\n ❯ 1. Yes\n"
    )
    out = extract_permission_request(tail)
    assert "python3 -m pytest -q" in out and "uname" not in out
