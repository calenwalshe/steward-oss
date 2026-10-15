"""Plan 2 Task 1: revisioned grants with provenance; owner-only rights; additive, guarded storage (spec §6)."""
from __future__ import annotations

import sqlite3

import pytest

from steward import grants, masking
from steward import store as store_mod
from steward.actor import Actor, ActorRef
from steward.store import Store
from authority_fixtures import CANARY, grant, owner_job
from ownership_fixtures import CONTROLLER, JEV, LEAD, OPERATOR, OWNER

TABLES = ("job_authority", "authority_grants", "authority_history", "diagnostic_reads", "evidence_snapshots")
WORKER = ActorRef(Actor.WORKER, "w1")


def _names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_pre_plan2_database_gains_authority_tables_without_touching_rows(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    conn = sqlite3.connect(home / "steward.db")
    conn.executescript(store_mod._SCHEMA)
    conn.execute("INSERT INTO jobs VALUES ('J1','goal','/r','RUNNING','t0','t0')")
    conn.commit()
    assert not set(TABLES) & _names(conn)
    conn.close()
    s = Store(home)
    Store(home).close()
    assert set(TABLES) <= _names(s._conn)
    assert [tuple(r) for r in s._conn.execute("SELECT * FROM jobs")] == [("J1", "goal", "/r", "RUNNING", "t0", "t0")]


def test_add_is_idempotent_and_bumps_the_revision_once(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    assert grants.current_rev(s, jid) == 0
    g1 = grant(s, jid, "write_path:/etc/caddy/Caddyfile")
    g2 = grant(s, jid, "write_path:/etc/caddy/Caddyfile", now=2.0)
    assert g1.grant_id == g2.grant_id and g1.created_rev == 1 and grants.current_rev(s, jid) == 1
    assert [h["change"] for h in grants.history(s, jid)] == ["grant_added"]
    ev = [e for e in s.events(jid) if e.what == "authority_grant_added"]
    assert len(ev) == 1 and ev[0].evidence["grant_id"] == g1.grant_id and ev[0].evidence["rev"] == 1


@pytest.mark.parametrize("who", [OWNER, OPERATOR])
def test_owner_and_operator_relay_may_grant(steward_home, who):
    s = Store(steward_home)
    jid = owner_job(s)
    g = grant(s, jid, "run_command:sudo systemctl reload caddy", by=who)
    assert g.granted_by == who.label() and g.active


@pytest.mark.parametrize("who", [LEAD, WORKER, JEV])
def test_lead_worker_and_jev_cannot_grant_or_revoke(steward_home, who):
    s = Store(steward_home)
    jid = owner_job(s)
    with pytest.raises(grants.GrantError):
        grant(s, jid, "write_path:/etc/caddy/Caddyfile", by=who)
    g = grant(s, jid, "write_path:/etc/caddy/Caddyfile")
    with pytest.raises(grants.GrantError):
        grants.revoke(s, g.grant_id, by=who, reason="no", now=3.0)
    assert grants.get(s, g.grant_id).active


def test_controller_may_only_migrate_a_recorded_owner_source(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    with pytest.raises(grants.GrantError):
        grants.add(s, jid, operation="key", scope={"names": ["X_API_KEY"]}, now=1.0, granted_by=CONTROLLER,
                   source=grants.Source(kind="grant_verb", ref="cli"))
    with pytest.raises(grants.GrantError):  # a migration source needs a reference to the recorded owner text
        grants.add(s, jid, operation="key", scope={"names": ["X_API_KEY"]}, now=1.0, granted_by=CONTROLLER,
                   source=grants.Source(kind="goal_text", ref=""))
    g, created = grants.add(s, jid, operation="key", scope={"names": ["X_API_KEY"]}, now=1.0, granted_by=CONTROLLER,
                            source=grants.Source(kind="legacy_capabilities", ref="kv:capabilities"))
    assert created and g.source.kind == "legacy_capabilities"


def test_revoke_bumps_the_revision_and_is_final(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    g = grant(s, jid, "write_path:/etc/caddy/Caddyfile")
    r = grants.revoke(s, g.grant_id, by=OWNER, reason="done with caddy", now=5.0)
    assert not r.active and r.revoked_rev == 2 and grants.current_rev(s, jid) == 2
    with pytest.raises(grants.GrantError):
        grants.revoke(s, g.grant_id, by=OWNER, reason="again", now=6.0)
    assert grants.for_job(s, jid) == [] and len(grants.for_job(s, jid, include_revoked=True)) == 1
    with pytest.raises(sqlite3.DatabaseError):  # the database refuses rewriting a grant's scope or un-revoking it
        s._conn.execute("UPDATE authority_grants SET revoked_rev = NULL WHERE grant_id = ?", (g.grant_id,))
    with pytest.raises(sqlite3.DatabaseError):
        s._conn.execute("DELETE FROM authority_grants")
    with pytest.raises(sqlite3.DatabaseError):
        s._conn.execute("DELETE FROM authority_history")


def test_active_grant_scope_cannot_be_rewritten_in_place(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    g = grant(s, jid, "write_path:/etc/caddy/Caddyfile")
    with pytest.raises(sqlite3.DatabaseError):
        s._conn.execute("UPDATE authority_grants SET scope_json = '{\"paths\": [\"/\"]}' WHERE grant_id = ?",
                        (g.grant_id,))


@pytest.mark.parametrize("op,scope", [
    ("write_path", {"paths": ["/"]}), ("write_path", {"paths": ["relative/x"]}), ("write_path", {"paths": []}),
    ("run_command", {"commands": ["cat x > /etc/passwd"]}), ("run_command", {"commands": ["a; b"]}),
    ("run_command", {"commands": ["$(curl x)"]}), ("remote_read", {"host": "local"}), ("remote_read", {"host": "a b"}),
    ("key", {"names": ["lower"]}), ("delivery", {"milestone": "accept", "target": "master"}),
    ("teleport", {"x": 1}), ("write_path", {"paths": [f"/p{i}" for i in range(21)]}),
])
def test_malformed_scopes_are_refused(op, scope):
    with pytest.raises(grants.GrantError):
        grants.normalize_scope(op, scope)


def test_parse_spec_grammar():
    assert grants.parse_spec("write_path:/etc/caddy/") == ("write_path", {"paths": ["/etc/caddy/"]})
    assert grants.parse_spec("run_command:sudo  systemctl reload caddy") == (
        "run_command", {"commands": ["sudo systemctl reload caddy"]})
    assert grants.parse_spec("remote_read:pi-post=pipost;services=bridge,caddy;paths=/var/log") == (
        "remote_read", {"host": "pi-post", "ssh": "pipost", "services": ["bridge", "caddy"], "paths": ["/var/log"]})
    assert grants.parse_spec("remote_read:kripke") == ("remote_read", {"host": "kripke", "ssh": "kripke"})
    assert grants.parse_spec("key:SCRAPECREATORS_API_KEY") == ("key", {"names": ["SCRAPECREATORS_API_KEY"]})
    assert grants.parse_spec("delivery:incorporate=fleet-registry") == (
        "delivery", {"milestone": "incorporate", "target": "fleet-registry"})
    with pytest.raises(grants.GrantError):
        grants.parse_spec("nonsense")


def test_source_excerpt_is_sanitized_and_bounded(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    masking.register([CANARY])
    try:
        g, _ = grants.add(s, jid, operation="write_path", scope={"paths": ["/etc/x"]}, now=1.0, granted_by=OWNER,
                          source=grants.Source(kind="grant_verb", ref="cli", excerpt=f"use {CANARY} " + "y" * 900))
    finally:
        masking.clear()
    assert CANARY not in g.source.excerpt and len(g.source.excerpt) <= grants.SOURCE_EXCERPT_MAX
    assert CANARY not in s._conn.execute("SELECT source_json FROM authority_grants").fetchone()[0]


def test_conditions_expire_a_grant_without_revoking_it(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    g, _ = grants.add(s, jid, operation="write_path", scope={"paths": ["/etc/x"]}, now=1.0, granted_by=OWNER,
                      conditions={"until_epoch": 100.0}, source=grants.Source(kind="grant_verb", ref="cli"))
    assert g.live(50.0) and not g.live(100.0) and g.active


def test_grant_count_is_bounded(steward_home, monkeypatch):
    s = Store(steward_home)
    jid = owner_job(s)
    monkeypatch.setattr(grants, "MAX_GRANTS_PER_JOB", 2)
    grant(s, jid, "write_path:/a")
    grant(s, jid, "write_path:/b")
    with pytest.raises(grants.GrantError):
        grant(s, jid, "write_path:/c")
