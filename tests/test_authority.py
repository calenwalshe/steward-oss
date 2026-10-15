"""Plan 2 Task 2: the single effective-authority function (spec §6)."""
from __future__ import annotations

import pytest

from steward import authority, blockers, grants, ownership, permissions
from steward.models import JobState, Judgment
from steward.store import Store
from authority_fixtures import grant, owner_job
from ownership_fixtures import CONTROLLER, OWNER


def _wt(store, jid):
    return store.home / "worktrees" / jid


def test_write_targets_lists_every_outside_target_not_just_the_first(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    got = permissions.write_targets("rm /etc/caddy/Caddyfile /etc/passwd && echo ok > /etc/motd", wt)
    assert got == ["/etc/caddy/Caddyfile", "/etc/passwd", "/etc/motd"]
    assert permissions.write_targets("cp a.txt b.txt; echo x > out.log; cat /etc/hosts", wt) == []
    assert permissions.write_targets("echo x > $(mktemp)", wt) == [permissions.UNVERIFIABLE]
    assert permissions.write_targets("sudo tee /etc/msi-guard.nft < rules", wt) == ["/etc/msi-guard.nft"]


def test_command_hold_reason_matches_the_deny_list():
    assert permissions.command_hold_reason("sudo systemctl reload caddy") == "sudo"
    assert permissions.command_hold_reason("git push origin main") == "git push"
    assert permissions.command_hold_reason("ls -la") is None


def test_granted_live_write_is_allowed_with_the_grant_id(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    g = grant(s, jid, "write_path:/etc/caddy/Caddyfile")
    v = authority.decide_worker(s, jid, "Bash", "sudo tee /etc/caddy/Caddyfile < Caddyfile.new", _wt(s, jid))
    assert not v.allow and v.basis == "not_granted"  # `sudo` itself is not granted, only the write target
    grant(s, jid, "run_command:sudo tee /etc/caddy/Caddyfile")
    v = authority.decide_worker(s, jid, "Bash", "sudo tee /etc/caddy/Caddyfile < Caddyfile.new", _wt(s, jid))
    assert v.allow and v.basis.startswith("grant:") and v.rev == authority.authority_rev(s, jid)
    w = authority.decide_worker(s, jid, "Write", {"file_path": "/etc/caddy/Caddyfile"}, _wt(s, jid))
    assert w.allow and w.grant_id == g.grant_id


def test_partially_covered_command_is_not_allowed(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "write_path:/etc/caddy/Caddyfile")
    v = authority.decide_worker(s, jid, "Bash", "rm /etc/caddy/Caddyfile /etc/passwd", _wt(s, jid))
    assert not v.allow and "/etc/passwd" in v.reason


def test_wrapped_command_is_covered_by_its_granted_wrapper(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "run_command:sudo systemctl reload caddy")
    assert authority.decide_worker(s, jid, "Bash", "sudo systemctl reload caddy", _wt(s, jid)).allow
    assert not authority.decide_worker(s, jid, "Bash", "sudo systemctl restart caddy", _wt(s, jid)).allow
    assert not authority.decide_worker(s, jid, "Bash", "systemctl stop caddy", _wt(s, jid)).allow


def test_prohibition_beats_grant_and_default_policy(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "run_command:git")
    p = grant(s, jid, "run_command:git push", kind="prohibit")
    v = authority.decide_worker(s, jid, "Bash", "git -C . push origin HEAD", _wt(s, jid))
    assert not v.allow and v.basis == f"prohibited:{p.grant_id}"
    grant(s, jid, "write_path:/tmp/never", kind="prohibit")  # /tmp is default-allowed; a prohibition still wins
    assert not authority.decide_worker(s, jid, "Bash", "echo x > /tmp/never", _wt(s, jid)).allow


def test_credential_reads_and_substitutions_are_never_covered(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "run_command:cat")
    assert not authority.decide_worker(s, jid, "Bash", "cat ~/.api-keys", _wt(s, jid)).allow
    grant(s, jid, "write_path:/etc/")
    assert not authority.decide_worker(s, jid, "Bash", "echo x > $(echo /etc/x)", _wt(s, jid)).allow


def test_default_policy_is_unchanged_without_grants(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    for tool, inp in (("Bash", "pytest -q"), ("Bash", "curl https://example.com"), ("WebFetch", {"url": "x"}),
                      ("Bash", "git push"), ("Bash", "ssh host ls"), ("Write", {"file_path": "/etc/x"})):
        v = authority.decide_worker(s, jid, tool, inp, _wt(s, jid))
        assert v.allow == permissions.decide(tool, inp, _wt(s, jid)).allow


def test_stop_beats_every_grant(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "write_path:/etc/caddy/Caddyfile")
    s.set_state(jid, JobState.RUNNING, what="dispatch", evidence={}, rule="worker_dispatch", judgment=Judgment.code)
    s.set_state(jid, JobState.STOPPING, what="owner stop", evidence={}, rule="stop", judgment=Judgment.human)
    v = authority.decide_worker(s, jid, "Write", {"file_path": "/etc/caddy/Caddyfile"}, _wt(s, jid))
    assert not v.allow and v.basis == "job_stopped"
    assert authority.revalidate(s, jid, expected_rev=authority.authority_rev(s, jid)).basis == "job_stopped"


def test_effective_authority_combines_contract_grants_keys_and_is_digest_stable(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    a0 = authority.effective(s, jid)
    assert a0.grants == () and "run: pytest -q" in a0.authority and a0.boundaries
    grant(s, jid, "key:X_API_KEY")
    grant(s, jid, "run_command:git push", kind="prohibit")
    a1 = authority.effective(s, jid)
    assert a1.keys == ("X_API_KEY",) and len(a1.prohibitions) == 1 and a1.digest != a0.digest
    assert authority.effective(s, jid).digest == a1.digest
    assert a1.rev > a0.rev and a1.grants_rev == 2


def test_authority_rev_is_monotonic_over_grants_and_job_revision(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    r0 = authority.authority_rev(s, jid)
    s.bump_revision(jid)
    r1 = authority.authority_rev(s, jid)
    grant(s, jid, "write_path:/etc/x")
    r2 = authority.authority_rev(s, jid)
    assert r0 < r1 < r2


def test_revalidate_rechecks_on_change_and_refuses_when_the_check_now_fails(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    g = grant(s, jid, "write_path:/etc/caddy/Caddyfile")
    rev = authority.authority_rev(s, jid)

    def check(auth):
        return authority.decide_worker(s, jid, "Write", {"file_path": "/etc/caddy/Caddyfile"}, _wt(s, jid), auth=auth)

    assert authority.revalidate(s, jid, expected_rev=rev, check=check).allow
    s.bump_revision(jid)  # an unrelated steer: re-checked, still allowed
    v = authority.revalidate(s, jid, expected_rev=rev, check=check)
    assert v.allow and v.basis.startswith("revalidated:grant:")
    grants.revoke(s, g.grant_id, by=OWNER, reason="no longer", now=9.0)
    v = authority.revalidate(s, jid, expected_rev=rev, check=check)
    assert not v.allow and v.basis == "authority_changed"
    assert not authority.revalidate(s, jid, expected_rev=rev).allow  # no check: any change refuses


def test_delivery_and_remote_read_lookups(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    assert not authority.delivery_authorized(s, jid, "incorporate", "fleet-registry").allow
    grant(s, jid, "delivery:incorporate=fleet-registry")
    assert authority.delivery_authorized(s, jid, "incorporate", "fleet-registry").allow
    assert not authority.delivery_authorized(s, jid, "activate", "fleet-registry").allow
    grant(s, jid, "remote_read:pi-post")
    assert authority.remote_read_authorized(s, jid, "pi-post").allow
    assert not authority.remote_read_authorized(s, jid, "mediabox").allow


def test_render_lines_and_preauthorized_run_lines(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "write_path:/etc/caddy/Caddyfile")
    grant(s, jid, "run_command:pytest", kind="prohibit")
    auth = authority.effective(s, jid)
    lines = authority.render_lines(auth)
    assert any("write_path /etc/caddy/Caddyfile" in ln and "owner" in ln for ln in lines)
    assert any(ln.startswith("PROHIBITED") and "pytest" in ln for ln in lines)
    assert authority.preauthorized_run_lines(auth) == []  # `run: pytest -q` is covered by the prohibition


def test_install_wires_plan1_seams(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    authority.install()
    assert ownership.authority_rev(s, jid) == authority.authority_rev(s, jid)
    assert blockers.spec("authorization_missing").predicate == "grant_covers"
    assert "grant_covers" in ownership.CHEAP_CHECKS and "grant_covers" in ownership.PREDICATES
    b, _ = blockers.raise_blocker(s, jid, kind="authorization_missing", subject="write:/etc/x",
                                  claim="worker needs to write /etc/x", evidence=["approval:none"],
                                  origin_actor=CONTROLLER, now=1.0,
                                  clearing={"predicate": "grant_covers",
                                            "params": {"operation": "write_path", "target": {"path": "/etc/x"}}})
    assert ownership.PREDICATES["grant_covers"](s, jid, b, b.clearing["params"]) == "holds"
    grant(s, jid, "write_path:/etc/x")
    assert ownership.PREDICATES["grant_covers"](s, jid, b, b.clearing["params"]) == "cleared"
    assert ownership.PREDICATES["grant_covers"](s, jid, b, {}) == "uncertain"


def test_adapters_under_the_names_plans_3_and_4_consume(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "delivery:incorporate=fleet-registry")
    grant(s, jid, "delivery:activate=control", kind="prohibit")
    eff = grants.effective(s, jid)
    assert eff.revision == authority.authority_rev(s, jid)
    d = eff.permits("incorporate", "/home/user/work/repo#fleet-registry")
    assert d.allowed and not d.prohibited and d.source.startswith("grt-")
    d = eff.permits("activate", "control")
    assert d.prohibited and not d.allowed
    d = eff.permits("incorporate", "/repo#master")
    assert not d.allowed and not d.prohibited


@pytest.mark.legacy_ownership_off
def test_kill_switch_ignores_grants_but_keeps_default_policy(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "write_path:/etc/caddy/Caddyfile")
    v = authority.decide_worker(s, jid, "Write", {"file_path": "/etc/caddy/Caddyfile"}, _wt(s, jid))
    assert not v.allow and v.basis == "default_policy"
