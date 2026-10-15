"""Bounded foreground waits (#3) and the strict pure-wait rule (#7), steward.wait_policy. Pure functions; the
replay reads the measured tool calls in tests/fixtures/latency/tool_calls.csv."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from steward import wait_policy as wp

DATA = Path(__file__).resolve().parent / "fixtures" / "latency"


def _deny(command, **extra):
    return wp.check("Bash", {"command": command, **extra}, cap=10).deny


@pytest.mark.parametrize("command", [
    "sleep 600",
    "sleep 11",
    "sleep 1m",
    "sleep 5; sleep 6",
    "end=$((SECONDS+585)); until [ -f X ] || [ $SECONDS -ge $end ]; do sleep 20; done",
    "t=$(( $(stat -c %Y ticks.csv) + 60 )); until [ $(date +%s) -ge $t ]; do sleep 5; done; python3 ticker.py",
    "for i in $(seq 1 30); do test -f x && break; sleep 2; done",
    "timeout 600 bash -c 'until [ -f x ]; do sleep 5; done'",
    "until git log --format=%s | grep -q done; do sleep 30; done",
    "while kill -0 123; do sleep 10; done",
    "tail -f build.log",
    "cd /w && sleep 30 && tail -5 log",
    "sleep 60 && pytest -q",
    "until [ -f x ]\ndo\n  sleep 2\ndone",
])
def test_long_foreground_waits_are_denied(command):
    assert _deny(command)


@pytest.mark.parametrize("command", [
    "sleep 5",
    "sleep 5; sleep 5",
    "sleep 0.5",
    "for i in $(seq 1 3); do test -f x && break; sleep 2; done",
    "timeout 8 tail -f log",
    "timeout 10 bash -c 'until [ -f x ]; do sleep 1; done'",
    "python3 -m pytest -q; sleep 2",
    "timeout 600 python3 -m pytest -q",
    "ls -la && git status",
])
def test_bounded_or_non_waits_are_allowed(command):
    assert not _deny(command)


@pytest.mark.parametrize("command", [
    "echo 'sleep 30'",                                      # quoted prose, not a command
    "git commit -m 'sleep 30 fix'",
    "while read l; do curl $l; sleep 30; done < urls.txt",  # a loop that does work: not a wait
    "while read l; do echo $l; sleep 1; done < f",
    "sleep 30 &",                                           # backgrounded: not a foreground wait
    "(sleep 30; kill 1) &",                                 # backgrounded group: fail open
    "nohup sleep 100",
    "sleep $DELAY",                                         # unknown bound
    "cat <<EOF > script.sh\nsleep 30\nEOF",                 # heredoc body is data
    "until [ -f x; do sleep 'unterminated",                 # unparseable: fail open
])
def test_regex_false_positives_fail_open(command):
    assert not _deny(command)


def test_run_in_background_and_other_tools_are_never_denied():
    assert not _deny("sleep 600", run_in_background=True)
    assert not wp.check("WebFetch", {"url": "https://x"}).deny
    assert not wp.check("Monitor", {}).deny


def test_tool_timeout_bounds_an_unbounded_loop():
    assert not _deny("until [ -f x ]; do sleep 2; done", timeout=8000)
    v = wp.check("Bash", {"command": "until [ -f x ]; do sleep 2; done"}, cap=10)
    assert v.deny and v.bound_s == wp.DEFAULT_BASH_TIMEOUT_S and v.form == "loop"


def test_deny_reason_is_the_replacement_instruction():
    v = wp.check("Bash", {"command": "sleep 600"}, cap=10)
    assert v.reason == wp.replacement(10)
    for needle in ("run_in_background", "Monitor", "up to 10 s", "one wait per tool batch", "Never poll"):
        assert needle in v.reason
    # S1 (2026-10-06 decision record D5): the brief must never instruct polling to keep a turn alive
    assert "Keep your turn alive" not in v.reason and "sleep 10" not in v.reason
    assert v.rule_version == wp.RULE_VERSION


def test_check_never_raises(monkeypatch):
    monkeypatch.setattr(wp, "_check", lambda *a, **k: 1 / 0)
    assert wp.check("Bash", {"command": "sleep 600"}).deny is False


def test_cap_and_mode_from_env():
    assert wp.cap_s({}) == 600.0  # S1: one long wait per call, not a 10 s poll loop
    assert wp.cap_s({wp.CAP_ENV: "30"}) == 30.0
    assert wp.cap_s({wp.CAP_ENV: "junk"}) == 600.0
    assert wp.mode({}) == "enforce"
    assert wp.mode({wp.MODE_ENV: "shadow"}) == "shadow"
    assert wp.mode({wp.MODE_ENV: "0"}) == "off"


@pytest.mark.parametrize("tool,command,expected", [
    ("Bash", "sleep 30", True),
    ("Bash", "until [ -f x ]; do sleep 5; done", True),
    ("Bash", "end=$((SECONDS+60)); until grep -q ok out.log || [ $SECONDS -ge $end ]; do sleep 5; done", True),
    ("Bash", "tail -f log", True),
    ("Bash", "sleep 30 && python3 -m pytest -q", False),   # a test after the wait
    ("Bash", "sleep 30; git commit -am x", False),          # git write
    ("Bash", "sleep 30 > out.txt", False),                  # file write
    ("Bash", "until [ -f x ]; do make; sleep 5; done", False),
    ("Bash", "sleep 30 & wait", False),                     # owns a background process
    ("Bash", "nohup sleep 30", False),
    ("Bash", "curl -s https://x; sleep 5", False),          # network
    ("Bash", "ls", False),                                  # no wait at all
    ("Bash", "frobnicate --wait", False),                   # unknown command
    ("Agent", "", False),
    ("WebFetch", "", False),
    ("Monitor", "", True),
    ("BashOutput", "", True),
])
def test_strict_pure_wait_rule(tool, command, expected):
    assert wp.is_pure_wait(tool, {"command": command} if command else {}) is expected


def test_pure_wait_respects_run_in_background():
    assert not wp.is_pure_wait("Bash", {"command": "sleep 30", "run_in_background": True})


def _bash_calls():
    with open(DATA / "tool_calls.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            # `summary` flattens newlines and truncates at 310 chars, so heredocs there are not real heredocs
            if r["name"] == "Bash" and "<<" not in r["summary"]:
                inp = {"command": r["summary"], "run_in_background": r["run_in_background"] == "True"}
                if r["timeout_ms"]:
                    inp["timeout"] = float(r["timeout_ms"])
                yield r, inp


def test_replay_measured_tool_calls():
    """The measured window: the long (>= 60 s) foreground waits are denied, and no build/test, git write or
    ordinary work call is (zero false denies). Misses are fail-open (mostly truncated summaries)."""
    long_waits = denied_long = false_denies = other = 0
    for r, inp in _bash_calls():
        deny = wp.check("Bash", inp, cap=10).deny
        if r["label_rule"] == "wait_poll":
            if float(r["dur_s"] or 0) >= 60:
                long_waits += 1
                denied_long += deny
        else:
            other += 1
            false_denies += deny
    assert long_waits >= 100 and other >= 1500
    assert denied_long / long_waits >= 0.85, (denied_long, long_waits)
    assert false_denies == 0


def test_replay_jev_labelled_calls_pure_wait_agrees_with_rule_v2():
    """Every call the strict rule calls a pure wait was a wait by the diagnostic's rule v2 (precision 1.0 on the
    300 Jev-labelled calls): the interrupt never mistakes work for a wait."""
    with open(DATA / "jev_labels_v2.csv", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["name"] == "Bash" and "<<" not in r["input"]]
    pure = [r for r in rows if wp.is_pure_wait("Bash", {"command": r["input"]})]
    assert pure
    assert all(r["label_rule_v2"] == "wait_poll" for r in pure), [r["input"][:80] for r in pure
                                                                   if r["label_rule_v2"] != "wait_poll"]


# -- enforcement at permission time (runner) and the brief ------------------------------------------------

from steward import approvals as approvals_module  # noqa: E402
from steward.brief import render_brief  # noqa: E402
from steward.contract import Contract  # noqa: E402
from steward.runner import Runner  # noqa: E402
from steward.store import Store  # noqa: E402
from test_fabric_worker import FakeApi, FakeMailbox  # noqa: E402
from test_runner_fabric import (  # noqa: E402
    _Clock, _contracted_job, _fake_diagnoser, _FabricFakeWorker, _frame, _git_runner, _obs)
from steward.fabric_worker import FabricWorker  # noqa: E402
from steward.shim import Shim  # noqa: E402


def _bash_req(seq, rid, command, **extra):
    return _frame(seq, "permission_request", {"request_id": rid, "tool": "Bash",
                                              "input": {"command": command, **extra}})


def _run_one(steward_home, *frames):
    store = Store(steward_home)
    job = _contracted_job(store)
    worker = _FabricFakeWorker(script=[_obs(lifecycle="blocked", events=tuple(frames))])
    runner = Runner(store, lambda ref: worker, _git_runner(), _fake_diagnoser(), clock=_Clock())
    runner.start(job.job_id)
    runner.tick(job.job_id)
    perms = {a["worker_request_id"]: a for a in approvals_module.list_approvals(store, job.job_id)
             if a["kind"] == "permission"}
    return store, job, worker, perms


def test_runner_denies_a_long_wait_with_the_replacement_and_allows_a_short_one(steward_home, monkeypatch):
    monkeypatch.setenv(wp.CAP_ENV, "10")  # the deny mechanism, exercised below the 600 s default
    store, job, worker, perms = _run_one(
        steward_home,
        _bash_req(3, "long", "end=$((SECONDS+585)); until [ -f X ] || [ $SECONDS -ge $end ]; do sleep 20; done"),
        _bash_req(4, "short", "sleep 5 && tail -3 out.log"))
    assert perms["long"]["status"] == "denied" and perms["long"]["decided_by"] == "code"
    assert perms["short"]["status"] == "approved"
    assert worker.answer_messages == [wp.replacement()]
    assert dict(zip(worker.answered_ids, worker.answered)) == {"long": False, "short": True}
    (ev,) = [e for e in store.events(job.job_id) if e.what == "foreground_wait_denied"]
    assert ev.evidence["cap_s"] == 10.0 and ev.evidence["form"] == "loop"
    assert ev.evidence["rule_version"] == wp.RULE_VERSION


def test_runner_shadow_mode_records_would_deny_and_allows(steward_home, monkeypatch):
    monkeypatch.setenv(wp.MODE_ENV, "shadow")
    monkeypatch.setenv(wp.CAP_ENV, "10")
    store, job, worker, perms = _run_one(steward_home, _bash_req(3, "long", "sleep 600"))
    assert perms["long"]["status"] == "approved"
    assert [e.what for e in store.events(job.job_id) if e.what.startswith("foreground_wait")] == [
        "foreground_wait_would_deny"]


def test_runner_off_mode_never_denies(steward_home, monkeypatch):
    monkeypatch.setenv(wp.MODE_ENV, "off")
    _, _, _, perms = _run_one(steward_home, _bash_req(3, "long", "sleep 600"))
    assert perms["long"]["status"] == "approved"


def test_deny_reason_travels_fabric_worker_to_shim_to_agent():
    api = FakeApi()
    fw = FabricWorker(api=api, mailbox=FakeMailbox(), runner_pane="w9:p1", job="jobA", spawn_pane=lambda w: "w1:p2")
    fw.pane_id = "w1:p2"
    fw.answer_prompt(approve=False, request_id="r1", message=wp.replacement())
    (deny,) = [f for _, f in api.sent if f.type == "deny"]
    assert deny.body == {"request_id": "r1", "message": wp.replacement()}

    import io
    import json

    class _Agent:
        stdin = io.StringIO()

    shim = Shim(job="jobA", peer_pane="w9:p1", api=FakeApi(), mailbox=FakeMailbox(), spawn=lambda a: _Agent(),
                agent_argv=["x"], print_fn=lambda line: None)
    shim._agent = _Agent()
    shim._pending_permissions["r1"] = {"tool_name": "Bash", "input": {"command": "sleep 600"}}
    shim._handle_inbound(deny)
    response = json.loads(_Agent.stdin.getvalue().splitlines()[-1])["response"]["response"]
    assert response == {"behavior": "deny", "message": wp.replacement()}


def test_fabric_brief_carries_the_same_wait_text():
    c = Contract(goal="g", boundaries=[], done_when=["d"], next_step="", authority=[], assumptions=[])
    fabric = render_brief(c, [], None, None, fabric=True)
    assert wp.replacement() in fabric and "Keep your turn alive" not in fabric
    assert wp.replacement() not in render_brief(c, [], None, None, fabric=False)


def test_default_cap_allows_one_long_wait_instead_of_a_poll_loop():
    """S1 (D5): a single wait up to the Bash max is allowed, so a worker waits once instead of every 10 s."""
    assert not wp.check("Bash", {"command": "sleep 300"}).deny
    assert not wp.check("Bash", {"command": "until [ -f done ]; do sleep 30; done", "timeout": 600000}).deny


def test_background_agent_is_denied_foreground_agent_is_not():
    """S1 (D5): Steward cannot see a background subagent, so it cannot tell when the work is finished."""
    reason = wp.background_agent_denial("Agent", {"prompt": "x", "run_in_background": True})
    assert reason and "foreground" in reason
    assert wp.background_agent_denial("Agent", {"prompt": "x"}) is None
    assert wp.background_agent_denial("Agent", {"prompt": "x", "run_in_background": False}) is None
    assert wp.background_agent_denial("Bash", {"command": "x", "run_in_background": True}) is None


def test_brief_tells_workers_to_keep_subagents_in_the_foreground():
    assert "subagents in the foreground" in wp.brief_line()


def test_runner_denies_a_background_agent_and_allows_a_foreground_one(steward_home):
    store, job, worker, perms = _run_one(
        steward_home,
        _frame(3, "permission_request", {"request_id": "bg", "tool": "Agent",
                                          "input": {"prompt": "p", "run_in_background": True}}),
        _frame(4, "permission_request", {"request_id": "fg", "tool": "Agent", "input": {"prompt": "p"}}))
    assert perms["bg"]["status"] == "denied" and perms["bg"]["decided_by"] == "code"
    assert perms["fg"]["status"] == "approved"
    assert worker.answer_messages == [wp.background_agent_denial("Agent", {"run_in_background": True})]
    assert [e.what for e in store.events(job.job_id) if e.what == "background_agent_denied"] == [
        "background_agent_denied"]


# -- Phase 42 (SRD-14): `steward check wait` is a pure wait; the brief names the three verbs ----------------------


@pytest.mark.parametrize("command", [
    "steward check wait cr-0123456789ab",
    "steward check wait cr-0123456789ab --timeout 540",
    "/home/user/.local/bin/steward check wait cr-0123456789ab",
    "steward check wait cr-0123456789ab && echo done",
])
def test_check_wait_is_a_pure_wait_and_never_denied(command):
    assert wp.is_pure_wait("Bash", {"command": command})
    assert not wp.check("Bash", {"command": command, "timeout": 600000}).deny
    assert wp.classify_call("Bash", {"command": command}) == "pure_wait"


@pytest.mark.parametrize("command", [
    "steward check wait cr-1 > out.txt",             # writes a file
    "steward check wait cr-1 & echo x",              # backgrounded
    "steward check run python3 -m pytest -q",        # not a wait: the runner intercepts it
    "steward check await cr-1",
    "steward status",
])
def test_other_steward_calls_are_not_pure_waits(command):
    assert not wp.is_pure_wait("Bash", {"command": command})


def test_parse_check_verb_recognises_only_a_lone_verb_call():
    p = wp.parse_check_verb
    assert p("Bash", {"command": "steward check run python3 -m pytest -q"}) == ("run", ["python3", "-m", "pytest", "-q"])
    assert p("Bash", {"command": "steward check run 'python3 -m pytest -q tests/a.py'"}) == (
        "run", ["python3 -m pytest -q tests/a.py"])
    assert p("Bash", {"command": "steward check await cr-1"}) == ("await", ["cr-1"])
    assert p("Bash", {"command": "steward check wait cr-1 --timeout 5"}) == ("wait", ["cr-1", "--timeout", "5"])
    assert p("Bash", {"command": "cd x && steward check await cr-1"}) is None
    assert p("Bash", {"command": "steward check await cr-1; rm -rf /"}) is None
    assert p("Bash", {"command": "steward check run $(whoami)"}) is None
    assert p("Bash", {"command": "steward check bogus"}) is None
    assert p("Read", {"command": "steward check await cr-1"}) is None


def test_brief_names_the_three_verbs_and_labels_the_cap_temporary():
    line = wp.brief_line()
    for verb in ("steward check run", "steward check await", "steward check wait"):
        assert verb in line
    assert "temporary" in line.lower() and wp.replacement() in line
    assert line.index("steward check await") < line.index(wp.replacement())  # the preferred pattern comes first


def test_brief_line_requires_single_simple_command():
    """RC-4: a chained or piped `steward check` is an ordinary Bash call, never the intercepted verb."""
    line = wp.brief_line()
    assert "single simple command" in line
    assert "no pipes" in line and "`&&`" in line
