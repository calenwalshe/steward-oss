"""Plan 2 Task 3: the owner grants once, at `new` or with `steward grant`; workers and the lead cannot (spec §6)."""
from __future__ import annotations

import dataclasses

from steward import authority, cli, grants
from steward.contract import Contract, load_contract, render, store_contract
from steward.store import Store
from authority_fixtures import grant, owner_job
from ownership_fixtures import OWNER


def _handlers(store):
    return cli._build_control_handlers(store)  # the handler table the control pane uses (`steward/cli.py:1307`)


def test_new_with_grant_renders_grants_and_keeps_outbound_boundary(steward_home, tmp_path):
    s = Store(steward_home)
    repo = tmp_path / "repo"
    repo.mkdir()
    h = _handlers(s)
    out = h["new"]({"origin": "cli", "args": {"goal": "fix caddy. Finish line: `pytest -q`", "repo": str(repo),
                                              "no_baseline": True,
                                              "grants": ["write_path:/etc/caddy/Caddyfile"],
                                              "prohibits": ["run_command:git push"]}})
    jid = out["job_id"]
    gs = grants.for_job(s, jid)
    assert [(g.kind, g.operation) for g in gs] == [("allow", "write_path"), ("prohibit", "run_command")]
    assert all(g.source.kind == "cli_flag" for g in gs)
    assert "GRANTS (rev 2):" in out["contract_text"] and "/etc/caddy/Caddyfile" in out["contract_text"]
    assert "PROHIBITED" in out["contract_text"]
    assert any("without" in b for b in load_contract(s, jid).boundaries)  # the default outbound boundary stays


def test_bad_grant_spec_is_reported_not_silently_dropped(steward_home, tmp_path):
    s = Store(steward_home)
    repo = tmp_path / "repo"
    repo.mkdir()
    out = _handlers(s)["new"]({"origin": "cli", "args": {"goal": "g", "repo": str(repo), "no_baseline": True,
                                                         "grants": ["write_path:relative"]}})
    assert out["accepted"] and out["grant_errors"] and grants.for_job(s, out["job_id"]) == []


def test_grant_verb_adds_revokes_and_reports(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    h = _handlers(s)
    r = h["grant"]({"origin": "cli", "args": {"job_id": jid, "spec": "remote_read:pi-post"}})
    assert r["accepted"] and r["rev"] == 1
    gid = r["grant_id"]
    r2 = h["grant"]({"origin": "cli", "args": {"job_id": jid, "revoke": gid, "reason": "done"}})
    assert r2["accepted"] and not grants.get(s, gid).active


def test_worker_and_lead_cannot_grant_or_revoke(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    g = grant(s, jid, "write_path:/etc/x")
    h = _handlers(s)
    for origin in ("worker", "tech_lead", "flow", "watcher"):
        r = h["grant"]({"origin": origin, "args": {"job_id": jid, "spec": "write_path:/etc/y"}})
        assert not r["accepted"] and r["error_kind"] == "refused"
        r = h["grant"]({"origin": origin, "args": {"job_id": jid, "revoke": g.grant_id, "reason": "x"}})
        assert not r["accepted"] and r["error_kind"] == "refused"
    assert [x.grant_id for x in grants.for_job(s, jid)] == [g.grant_id]


def test_operator_agent_relay_is_recorded_as_such(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    r = _handlers(s)["grant"]({"origin": "cli", "actor": "operator_agent:claude-code",
                               "args": {"job_id": jid, "spec": "key:X_API_KEY"}})
    assert r["accepted"] and grants.get(s, r["grant_id"]).granted_by == "operator_agent:claude-code"


def test_grants_read_command_lists_active_and_revoked(steward_home, capsys):
    s = Store(steward_home)
    jid = owner_job(s)
    g = grant(s, jid, "write_path:/etc/x")
    grants.revoke(s, g.grant_id, by=OWNER, reason="r", now=2.0)
    grant(s, jid, "run_command:git push", kind="prohibit")
    assert cli.main(["grants", jid]) == 0
    out = capsys.readouterr().out
    assert "PROHIBITED" in out and f"{g.grant_id} revoked" in out and "authority rev" in out


def test_contract_change_bumps_authority_revision(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    r0 = authority.authority_rev(s, jid)
    c = load_contract(s, jid)
    store_contract(s, jid, dataclasses.replace(c, revision=2, boundaries=[*c.boundaries, "never touch prod"]))
    assert authority.authority_rev(s, jid) > r0
    assert grants.history(s, jid)[-1]["change"] == "contract_changed"


def test_render_without_grants_is_unchanged():
    c = Contract(goal="g", boundaries=["b"], done_when=["d"], authority=["a"])
    assert render(c) == render(c, grant_lines=None)
    assert "GRANTS" not in render(c)
