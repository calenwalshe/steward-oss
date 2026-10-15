"""Risk rules match command words only (job 6bfd208c replay) and the owner is pinged only after the
resolver chain could not decide."""

from __future__ import annotations

import pytest
from test_resolver import (FLOOR_HOLD, LOW_HOLD, WT, Clock, FakePopen, _hold, _run, _setup, _tier, approvals_module, popen,  # noqa: F401
                           rs)


REPLAY = ("cp runs/notes/a.md runs/notes/b.md && .venv/bin/python - <<'EOF'\n"
          "import subprocess\nprint('ssh and scp and rsync are only words here')\n"
          "subprocess.run(['echo', 'ssh'])\nEOF")


def test_replay_6bfd208c_heredoc_text_is_low_and_auto_allows():
    from steward import permissions

    assert permissions.decide("Bash", REPLAY, WT).allow
    assert _tier(REPLAY, "remote transfer (ssh/scp/rsync)") == "low"  # even the old false-positive hold is low
    heads = permissions.command_words(REPLAY)
    assert [h.split()[0] for h in heads] == ["cp", "python"]


@pytest.mark.parametrize("cmd", [
    "git commit -m 'fix ssh and rsync docs'", "echo ssh scp rsync", "bash -c 'echo ssh host'", "grep -r ssh src/",
    "python -c \"import os; os.system('rsync x y')\"", "cat <<EOF\nssh host\nEOF",
])
def test_command_words_ignore_quoted_args_c_strings_and_bodies(cmd):
    from steward import permissions

    assert permissions.decide("Bash", cmd, WT).allow, cmd
    assert not any(h.split()[0] in ("ssh", "scp", "rsync") for h in permissions.command_words(cmd))


@pytest.mark.parametrize("cmd", [
    "ssh host uptime", "scp a.txt host:/tmp/", "rsync -a src/ host:/dst/", "cd x && ssh host", "ls | xargs -I{} ssh {} id",
    "bash -c 'ssh host'", "FOO=1 ssh host", "/usr/bin/ssh host", "sudo ssh host", "echo $(ssh host id)", "env ssh host", "sftp host",
])
def test_real_remote_commands_stay_held_and_high(cmd):
    from steward import permissions

    d = permissions.decide("Bash", cmd, WT)
    assert not d.allow, cmd
    assert _tier(cmd, d.reason) == "high", cmd
    assert _tier(cmd, "") == "high"


def test_remote_tier_from_text_alone_needs_a_command_word():
    assert _tier("git commit -m 'scp it'", "") == "high"  # unrecognised, not 'remote'
    assert rs.classify_command("Bash", {"command": "git commit -m 'scp it'"}, "default", WT)[1].startswith("unrecognised")


def test_slow_resolver_pings_after_notify_after_s_and_shows_deciding(steward_home, popen):
    from steward import jobview

    FakePopen.finish = False  # the resolver never answers
    clock = Clock()
    clock.t -= 20  # approvals are 40s old: inside the 60s window
    notes = []
    store, jid, eng = _setup(steward_home, notes=notes, clock=clock)
    rec, deferred = _hold(store, jid, eng, *LOW_HOLD)
    assert deferred == "low" and not notes
    _run(eng, jid)
    cur = approvals_module.get_approval(store, jid, rec["id"])
    assert not notes and cur["resolver_pending"]  # 45s: resolver still has time, owner not told
    assert jobview.reason([cur], "RUNNING", []).startswith("resolver deciding (")
    clock.t += 30  # 70s > 60s
    _run(eng, jid)
    cur = approvals_module.get_approval(store, jid, rec["id"])
    assert len(notes) == 1 and notes[0][1] == "NEEDS_YOU" and not cur["resolver_pending"]
    assert jobview.reason([cur], "RUNNING", []).startswith("approve? ")
    _run(eng, jid)
    assert len(notes) == 1  # once


def test_resolver_decision_in_time_never_notifies(steward_home, popen):
    notes = []
    store, jid, eng = _setup(steward_home, notes=notes)
    rec, _ = _hold(store, jid, eng, *LOW_HOLD)
    _run(eng, jid)
    assert approvals_module.get_approval(store, jid, rec["id"])["decided_by"] == "resolver:sonnet"
    assert notes == []


def test_needs_human_reply_notifies_after_the_resolver_and_only_once(steward_home, popen):
    FakePopen.reply = {"decision": "escalate_human", "reason": "unsure", "risk_notes": ""}
    notes = []
    store, jid, eng = _setup(steward_home, notes=notes)
    rec, _ = _hold(store, jid, eng, *LOW_HOLD)
    assert notes == []
    _run(eng, jid)
    assert len(notes) == 1 and "unsure" in notes[0][3]


def test_floor_pings_immediately_without_resolver(steward_home, popen):
    notes = []
    store, jid, eng = _setup(steward_home, notes=notes)
    _, deferred = _hold(store, jid, eng, *FLOOR_HOLD)
    assert deferred is None  # runner pings at once


def test_notify_after_s_is_configurable(tmp_path):
    (tmp_path / "steward.toml").write_text("[resolver]\nnotify_after_s = 15\n")
    assert rs.load_config(tmp_path).notify_after_s == 15.0
    assert rs.ResolverConfig().notify_after_s == 60.0
