"""Plan 2 Task 9: read-only diagnostics — every piece of the line is validated; receipts are sanitized (spec §6)."""
from __future__ import annotations

import pytest

from steward import authority, diagnostics, grants, masking, sanitize
from steward.actor import Actor, ActorRef
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from authority_fixtures import CANARY, grant, owner_job, ssh_runner
from ownership_fixtures import OWNER

LEAD = ActorRef(Actor.TECH_LEAD, "astra-tech-lead")


@pytest.mark.parametrize("cmd", [
    "systemctl is-active circuit-bridge",
    "systemctl status circuit-bridge --no-pager && journalctl -u circuit-bridge -n 50 --no-pager",
    "journalctl -u caddy --since '-10 min' --no-pager | grep -i error | tail -n 20",
    "ss -ltnp; df -h; uptime",
    "cat /var/log/syslog | tail -n 100",
    "curl -sS -m 5 http://127.0.0.1:8080/healthz 2>&1",
    "git -C /srv/app log --oneline -5",
    "docker logs --tail 50 bridge 2>/dev/null",
    "find /var/log -name '*.log' -mmin -30",
    "ping -c 3 192.168.1.1",
])
def test_read_only_lines_pass(cmd):
    assert diagnostics.validate_read(cmd).ok, diagnostics.validate_read(cmd).reason


@pytest.mark.parametrize("cmd,why", [
    ("journalctl -u bridge -n 50 > /tmp/x", "redirect"),
    ("cat /var/log/syslog >> /var/log/syslog.bak", "redirect"),
    ("systemctl restart circuit-bridge", "systemctl restart"),
    ("systemctl status bridge; systemctl stop bridge", "systemctl stop"),
    ("cat x | tee /etc/motd", "tee"),
    ("echo $(cat /etc/hostname)", "substitution"),
    ("ls `pwd`", "substitution"),
    ("sudo journalctl -u bridge", "sudo"),
    ("bash -c 'rm -rf /tmp/x'", "bash"),
    ("python3 -c 'import os'", "python3"),
    ("find /var/log -name x -delete", "-delete"),
    ("find / -exec rm {} ;", "-exec"),
    ("curl -X POST http://x/api", "write"),
    ("curl -o /tmp/f http://x", "-o"),
    ("cat ~/.api-keys", "credential"),
    ("cat /proc/1/environ", "credential"),
    ("printenv", "credential"),
    ("cat ~/.ssh/id_ed25519", "credential"),
    ("FOO=1 cat /etc/hostname", "assignment"),
    ("sleep 100 &", "background"),
    ("git branch -D main", "git branch -D"),
    ("rm -rf /var/log/x", "rm"),
    ("ping 1.1.1.1", "ping"),
    ("x" * 1001, "too long"),
    ("ls; ls; ls; ls; ls; ls; ls; ls; ls", "segments"),
])
def test_mutating_compound_and_secret_lines_are_refused(cmd, why):
    r = diagnostics.validate_read(cmd)
    assert not r.ok and r.reason


def test_service_and_path_scopes_from_the_grant():
    assert diagnostics.validate_read("journalctl -u bridge -n 5", services=("bridge",)).ok
    assert not diagnostics.validate_read("journalctl -u caddy -n 5", services=("bridge",)).ok
    assert not diagnostics.validate_read("systemctl status sshd", services=("bridge",)).ok
    assert diagnostics.validate_read("tail -n 5 /var/log/bridge/out.log", paths=("/var/log/bridge/",)).ok
    assert not diagnostics.validate_read("tail -n 5 /var/log/auth.log", paths=("/var/log/bridge/",)).ok


def test_local_reads_cannot_reach_the_live_store_or_other_worktrees(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    other = owner_job(s)
    ok, v = diagnostics.authorize(s, jid, "local", "tail -n 5 /var/log/syslog")
    assert ok.ok and v.allow
    for cmd in (f"cat {s.home}/steward.db", f"ls {s.home / 'worktrees' / other}", "cat ~/.config/steward/steward.toml"):
        chk, _ = diagnostics.authorize(s, jid, "local", cmd)
        assert not chk.ok, cmd


def test_remote_read_needs_a_grant_for_that_host(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    chk, v = diagnostics.authorize(s, jid, "pi-post", "systemctl is-active bridge")
    assert chk.ok and not v.allow and v.basis == "not_granted"
    grant(s, jid, "remote_read:pi-post=pipost;services=bridge")
    chk, v = diagnostics.authorize(s, jid, "pi-post", "systemctl is-active bridge")
    assert chk.ok and v.allow
    chk, v = diagnostics.authorize(s, jid, "pi-post", "systemctl is-active caddy")
    assert not chk.ok  # outside the grant's services


def test_run_read_executes_over_ssh_records_a_sanitized_receipt(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "remote_read:pi-post=pipost")
    masking.register([CANARY])
    try:
        fr = ssh_runner({"systemctl is-active bridge": (0, f"active\nTOKEN={CANARY}\n")})
        r = diagnostics.run_read(s, jid, target="pi-post", command="systemctl is-active bridge",
                                 purpose="check the worker's claim", requested_by=LEAD, runner_exec=fr, now=100.0)
    finally:
        masking.clear()
    call = fr.calls[0]
    assert call["argv"][:1] == ["ssh"] and "pipost" in call["argv"] and call["argv"][-2:] == ["--", "systemctl is-active bridge"]
    assert call["timeout"] == diagnostics.READ_TIMEOUT_S and "PATH" in call["env"] and len(call["env"]) <= 3
    assert r.outcome == "done" and r.exit_code == 0 and "active" in r.excerpt and CANARY not in r.excerpt
    row = s._conn.execute("SELECT * FROM diagnostic_reads WHERE read_id = ?", (r.read_id,)).fetchone()
    assert row["target"] == "pi-post" and row["grant_id"] and row["authority_rev"] == authority.authority_rev(s, jid)
    assert CANARY not in str(tuple(row))
    ev = [e for e in s.events(jid) if e.what == "diagnostic_read"]
    assert len(ev) == 1 and ev[0].evidence["read_id"] == r.read_id and ev[0].evidence["requested_by"] == LEAD.label()


def test_refused_read_never_runs_and_is_recorded(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "remote_read:pi-post")
    fr = ssh_runner()
    r = diagnostics.run_read(s, jid, target="pi-post", command="systemctl restart bridge", purpose="x",
                             requested_by=LEAD, runner_exec=fr, now=1.0)
    r2 = diagnostics.run_read(s, jid, target="mediabox", command="uptime", purpose="x", requested_by=LEAD,
                              runner_exec=fr, now=1.0)
    assert fr.calls == [] and r.outcome == r2.outcome == "refused"
    assert "not_granted" in r2.refused_reason
    assert len([e for e in s.events(jid) if e.what == "diagnostic_read_refused"]) == 2


def test_revocation_between_proposal_and_execution_refuses_the_read(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    g = grant(s, jid, "remote_read:pi-post")
    bound = authority.authority_rev(s, jid)
    grants.revoke(s, g.grant_id, by=OWNER, reason="no", now=2.0)
    fr = ssh_runner()
    r = diagnostics.run_read(s, jid, target="pi-post", command="uptime", purpose="x", requested_by=LEAD,
                             runner_exec=fr, now=3.0, expected_rev=bound)
    assert r.outcome == "refused" and fr.calls == []


def test_rate_limit_is_a_wait_not_an_owner_request(steward_home, monkeypatch):
    s = Store(steward_home)
    jid = owner_job(s)
    monkeypatch.setattr(diagnostics, "MAX_READS_PER_HOUR", 2)
    fr = ssh_runner()
    for i in range(2):
        assert diagnostics.run_read(s, jid, target="local", command="uptime", purpose="x", requested_by=LEAD,
                                    runner_exec=fr, now=10.0 + i).outcome == "done"
    r = diagnostics.run_read(s, jid, target="local", command="uptime", purpose="x", requested_by=LEAD,
                             runner_exec=fr, now=12.0)
    assert r.outcome == "refused" and r.refused_reason.startswith("rate")
    assert diagnostics.run_read(s, jid, target="local", command="uptime", purpose="x", requested_by=LEAD,
                                runner_exec=fr, now=10.0 + 3601).outcome == "done"


def test_timeout_and_long_output_are_bounded(steward_home):
    import subprocess

    s = Store(steward_home)
    jid = owner_job(s)

    def slow(argv, cwd):
        raise subprocess.TimeoutExpired(argv, diagnostics.READ_TIMEOUT_S)
    r = diagnostics.run_read(s, jid, target="local", command="uptime", purpose="x", requested_by=LEAD,
                             runner_exec=FakeRunner(slow), now=1.0)
    assert r.outcome == "timeout"
    big = FakeRunner(lambda argv, cwd: CommandResult(argv, cwd, 0, "y" * 100_000, "", 0.1))
    r = diagnostics.run_read(s, jid, target="local", command="uptime", purpose="x", requested_by=LEAD,
                             runner_exec=big, now=2.0)
    assert len(r.excerpt) <= diagnostics.EXCERPT_CHARS and r.truncated and r.output_digest
