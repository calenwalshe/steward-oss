"""Floor/high rules judge actual commands only (job 49425524 replay): text inside heredocs, interpreter
source and quoted arguments is never a command, while wrappers and `bash -c` strings still are."""

from __future__ import annotations

import json

import pytest
from test_resolver import FLOOR_HOLD, WT, Clock, FakePopen, _hold, _run, _setup, _tier, approvals_module, popen, rs  # noqa: F401

from steward import permissions

REPLAY = ("python3 - <<'EOF'\nimport re\n"
          "# test string: curl -X POST https://x ; git push origin main ; tg send hi\n"
          "assert 'curl -X POST https://x' in text\nEOF")


def test_replay_49425524_is_low_and_auto_allowed():
    assert permissions.decide("Bash", REPLAY, WT).allow
    assert _tier(REPLAY, "curl/wget with a write method to a remote host") == "low"  # even the old hold
    assert _tier(REPLAY, "git push") == "low"


@pytest.mark.parametrize("cmd", [
    "python3 -c \"import os; os.system('git push')\"", "node -e \"fetch('x',{method:'POST'}); // curl -d x\"",
    "git commit -m 'curl -X POST and git push and mail'", "echo 'npm publish'", "cat <<EOF\ngit push\nEOF",
    "grep -r 'tg send' src/", "echo 'cat ~/.ssh/id_rsa'", "git commit -m 'keep ~/.api-keys private'",
])
def test_prose_is_not_a_command(cmd):
    assert permissions.decide("Bash", cmd, WT).allow, cmd
    assert rs.classify_command("Bash", {"command": cmd}, "", WT)[0] != "floor"


@pytest.mark.parametrize("cmd", [
    "curl -X POST https://x", "curl -sS -d a=b https://x", "wget --post-data=a=1 https://x", "git push", "git -C repo push origin x",
    'bash -c "git push"', "sh -c 'curl -X POST https://x'", "sudo git push", "env FOO=1 git push", "ls | xargs -I{} git push {}",
    "echo hi && npm publish", "eval 'git push'", "echo $(git push)", "bash <<EOF\ngit push\nEOF",
    "sudo -u bob git push", "tg send hello", "mail -s hi a@b.c",
])
def test_real_floor_commands_stay_held_and_floor(cmd):
    assert not permissions.decide("Bash", cmd, WT).allow, cmd
    assert rs.classify_command("Bash", {"command": cmd}, "", WT)[0] == "floor", cmd


def test_real_curl_post_hold_stays_floor_and_local_is_high():
    assert _tier("curl -X POST https://x", "curl/wget with a write method to a remote host") == "floor"
    assert _tier("curl -X POST http://localhost:8080/a", "curl/wget with a write method to a remote host") == "high"
    assert _tier('bash -c "git push"', "git push") == "floor"


def test_credential_reads_still_held():
    assert not permissions.decide("Bash", "cat ~/.ssh/id_rsa", WT).allow
    assert not permissions.decide("Bash", "bash -c 'cat ~/.api-keys'", WT).allow
    assert _tier("cat ~/.ssh/id_rsa", "reads a credential path: ~/.ssh/id_rsa") == "high"


def test_parse_reply_accepts_false_positive_flag():
    r = rs.parse_reply(json.dumps({"decision": "approve", "reason": "curl is only in a string", "false_positive": True}))
    assert r.false_positive is True
    assert rs.parse_reply('{"decision":"approve","reason":"x","false_positive":"yes"}') is None


def test_astra_false_positive_on_floor_without_floor_command_is_downgraded(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    # exercise _finish directly: the item was held as floor, Astra marks it a false positive
    cmd = "echo hello"
    rec, _ = _hold(store, jid, eng, cmd, "curl/wget with a write method to a remote host")
    a = approvals_module.get_approval(store, jid, rec["id"])
    store.kv_set(jid, f"hold:{rec['id']}", {"tool": "Bash", "input": {"command": cmd}, "reason": "mail"})
    reply = rs.Reply("approve", "word curl only appears in a test string", "", True)
    eng._finish(jid, a, "floor", "gpt-6-astra", reply, None, "")
    assert approvals_module.get_approval(store, jid, rec["id"])["status"] == "approved"
    ev = [e for e in store.events(jid) if e.what == "floor_downgraded"]
    assert len(ev) == 1 and ev[0].evidence["to"] == "high" and "test string" in ev[0].evidence["reason"]


def test_astra_false_positive_cannot_downgrade_a_real_floor_command(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    rec, _ = _hold(store, jid, eng, *FLOOR_HOLD)
    a = approvals_module.get_approval(store, jid, rec["id"])
    reply = rs.Reply("approve", "it is fine", "", True)
    eng._finish(jid, a, "floor", "gpt-6-astra", reply, None, "")
    assert approvals_module.get_approval(store, jid, rec["id"])["status"] == "pending"  # escalated, not approved
