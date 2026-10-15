"""Plan 2 Task 7: the lead sees the job's authority, knows its floor is its own, and nothing it proposes executes
against stale authority (spec §6; audit RC-3/F-3; §11 scenario 3)."""
from __future__ import annotations

import json
import threading

from steward import approvals, authority, grants
from steward import fleet_runtime as rt
from steward import fleet_tech_lead as tl
from steward import tech_lead_actions as tla
from steward import tech_lead_floor as floor
from steward import tech_lead_policy as tpol
from steward.store import Store
from authority_fixtures import grant
from ownership_fixtures import ASTRA, OWNER
from test_tech_lead_actions import env, prop, run  # noqa: F401 - env is a fixture


def _lead(store, invoke=lambda e, p, t: "{}"):
    ex = tla.ActionExecutor(store, runner_exec=None)
    return tl.JobTechLead(store, [ASTRA], ex, arbiter=rt.Arbiter(store), invoke=invoke, available=lambda e: None,
                          policy_fn=lambda: tpol.Policy())


def test_packet_authority_survives_budget_trimming_and_states_floor_scope_continuous_ownership_recovery_acceptance(env):
    store, jid, wt = env
    for i in range(5):
        grant(store, jid, f"write_path:/etc/app{i}.conf")
    grant(store, jid, "run_command:git push", kind="prohibit")
    pol = tpol.Policy(token_budget=200)  # far too small: recent/orientation are trimmed, authority is not
    pkt = _lead(store).packet(jid, [], "t1", pol)
    assert pkt["authority"]["grants_rev"] == grants.current_rev(store, jid)
    assert len(pkt["authority"]["grants"]) == 5 and len(pkt["authority"]["prohibitions"]) == 1
    assert pkt["floor_binds"] == authority.FLOOR_SCOPE and "diagnose" in pkt["allowed_actions"]
    assert "binds YOUR actions" in tla.job_proposal_prompt(pkt)


def test_packet_grants_for_the_independent_reviewer(env):
    store, jid, wt = env
    g = grant(store, jid, "write_path:/etc/caddy/Caddyfile")
    p = grant(store, jid, "run_command:git push", kind="prohibit")
    rows = authority.packet_grants(store, jid)
    assert [(r["id"], r["kind"]) for r in rows] == [(p.grant_id, "prohibit"), (g.grant_id, "allow")]
    assert set(rows[0]) == {"id", "kind", "op", "scope", "source", "by"}


def test_packet_overflow_points_to_lookup(env, monkeypatch):
    store, jid, wt = env
    monkeypatch.setattr(authority, "PACKET_GRANTS_MAX", 2)
    for i in range(4):
        grant(store, jid, f"write_path:/etc/app{i}.conf")
    sec = authority.packet_section(store, jid)
    assert len(sec["grants"]) == 2 and sec["omitted"] == 2 and sec["lookup"].startswith("steward grants ")


def test_contract_scoped_lead_may_clear_a_granted_ask_but_not_an_ungranted_one(env):
    store, jid, wt = env
    grant(store, jid, "write_path:/etc/caddy/Caddyfile")
    ok = approvals.open_approval(store, jid, "worker permission: Write({'file_path': '/etc/caddy/Caddyfile'})",
                                 kind="permission")
    no = approvals.open_approval(store, jid, "worker permission: Write({'file_path': '/etc/passwd'})",
                                 kind="permission")
    pol = tpol.Policy(permissions="contract")
    for rec in (ok, no):
        run(store, jid, wt, prop({"type": "unblock", "params": {"approval_id": rec["id"], "decision": "clear",
                                                                "text": "go"}}), policy=pol)
    assert approvals.get_approval(store, jid, ok["id"])["status"] == "approved"
    assert approvals.get_approval(store, jid, no["id"])["status"] == "pending"


def test_floor_context_carries_grants_and_keys_from_one_source(env):
    store, jid, wt = env
    grant(store, jid, "key:X_API_KEY")
    grant(store, jid, "write_path:/etc/caddy/Caddyfile")
    grant(store, jid, "run_command:sudo systemctl reload caddy")
    fctx = floor.context_for(store, jid, wt)
    assert "X_API_KEY" in fctx.granted_keys
    assert "/etc/caddy/Caddyfile" in fctx.granted_paths and "sudo systemctl reload caddy" in fctx.granted_commands
    v = floor.check({"type": "unblock", "params": {"text": "cleared: run sudo systemctl reload caddy then ssh-free"}},
                    fctx)
    assert v.ok
    assert not floor.check({"type": "unblock", "params": {"text": "now run git push"}}, fctx).ok  # floor unchanged


def test_revoked_grant_invalidates_a_queued_unblock(env):
    store, jid, wt = env
    g = grant(store, jid, "write_path:/etc/caddy/Caddyfile")
    rec = approvals.open_approval(store, jid, "worker permission: Write({'file_path': '/etc/caddy/Caddyfile'})",
                                  kind="permission")
    bound = authority.authority_rev(store, jid)  # the revision the lead's packet was built at
    p = tla.parse_job_proposal(prop({"type": "unblock", "params": {"approval_id": rec["id"], "decision": "clear",
                                                                   "text": "granted: go"}}))
    ctx = tla.ActionContext(jid, ASTRA.id, ASTRA.model, p, wt, tpol.Policy(permissions="contract"), entry=ASTRA,
                            authority_rev=bound)
    grants.revoke(store, g.grant_id, by=OWNER, reason="stop caddy work", now=9.0)
    decision, why, extra = tla.gate(p.actions[0], ctx, floor.context_for(store, jid, wt), store=store)
    assert decision == "refused" and extra["class"] == "authority_changed"
    notes = []
    ex = tla.ActionExecutor(store, runner_exec=None, notify_fn=lambda *a: notes.append(a))
    out = ex.record(ctx, p.actions[0], "authority_refused", why, authority_class=extra["class"])
    assert out["outcome"] == "authority_refused" and not notes
    assert approvals.get_approval(store, jid, rec["id"])["status"] == "pending"


def test_unchanged_authority_executes_and_unrelated_steer_is_rechecked_not_refused(env):
    store, jid, wt = env
    grant(store, jid, "write_path:/etc/caddy/Caddyfile")
    rec = approvals.open_approval(store, jid, "worker permission: Write({'file_path': '/etc/caddy/Caddyfile'})",
                                  kind="permission")
    bound = authority.authority_rev(store, jid)
    store.bump_revision(jid)  # an unrelated steer between proposal and execution
    p = tla.parse_job_proposal(prop({"type": "unblock", "params": {"approval_id": rec["id"], "decision": "clear",
                                                                   "text": "go"}}))
    ctx = tla.ActionContext(jid, ASTRA.id, ASTRA.model, p, wt, tpol.Policy(permissions="contract"), entry=ASTRA,
                            authority_rev=bound)
    assert tla.gate(p.actions[0], ctx, floor.context_for(store, jid, wt), store=store)[0] == "execute"


def test_full_path_queued_review_is_refused_after_revocation_without_owner_notice_continuous_ownership_recovery_acceptance(steward_home):
    from steward.models import JobState, Judgment
    from ownership_fixtures import ScriptedLead, proposal, wait_review
    from test_runner import _contracted_job

    store = Store(steward_home)
    jid = _contracted_job(store, "fix caddy. Finish line: `pytest -q`").job_id
    store.set_state(jid, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=Judgment.code)
    g = grant(store, jid, "write_path:/etc/caddy/Caddyfile")
    rec = approvals.open_approval(store, jid, "worker permission: Write({'file_path': '/etc/caddy/Caddyfile'})",
                                  kind="permission")
    class DelayedLead:
        def __init__(self):
            self.started, self.release = threading.Event(), threading.Event()
            self.answer = ""

        def __call__(self, _entry, _prompt, _timeout):
            self.started.set()
            self.release.wait(5)
            return self.answer

    lead_answers = DelayedLead()
    notes = []
    ex = tla.ActionExecutor(store, runner_exec=None, notify_fn=lambda *a: notes.append(a))
    lead = tl.JobTechLead(store, [ASTRA], ex, arbiter=rt.Arbiter(store), invoke=lead_answers,
                          available=lambda e: None, policy_fn=lambda: tpol.Policy(permissions="contract"))
    lead.on_event(jid, sweep=True, reason="test")
    assert lead_answers.started.wait(2)
    grants.revoke(store, g.grant_id, by=OWNER, reason="stop", now=9.0)
    lead_answers.answer = proposal({"type": "unblock", "params": {"approval_id": rec["id"],
                                                                       "decision": "clear", "text": "go"}})
    lead_answers.release.set()
    wait_review(lead, jid)
    lead.on_event(jid)
    assert approvals.get_approval(store, jid, rec["id"])["status"] == "pending"
    assert not [n for n in notes if n[1] == "NEEDS_YOU"]
    assert any(e.what == "tech_lead_action" and e.evidence.get("outcome") == "authority_refused"
               for e in store.events(jid))
