"""Plan 2 Task 10: the lead reads what the job is authorized to read, and nothing else (spec §6; §11 scenario 2;
Plan 1 guard rows G7 and G10)."""
from __future__ import annotations

from steward import authority, blockers, diagnostics, ownership
from steward import tech_lead_actions as tla
from steward import tech_lead_floor as floor
from steward import tech_lead_policy as tpol
from steward.store import Store
from authority_fixtures import grant, owner_job, ssh_runner
from ownership_fixtures import ASTRA, Clock, Notes, control_lead, park_tracks, proposal
from test_tech_lead_actions import env, prop, run  # noqa: F401 - env is a fixture

CHECK = "systemctl is-active circuit-bridge && journalctl -u circuit-bridge -n 20 --no-pager"


def _diag(target, command, purpose="verify the worker's claim"):
    return {"type": "diagnose", "params": {"target": target, "command": command, "purpose": purpose}}


def test_diagnose_parses_and_is_listed():
    p = tla.parse_job_proposal(prop(_diag("pi-post", "uptime")))
    assert p.actions[0].type == "diagnose" and "diagnose" in tla.JOB_ACTION_TYPES


def test_authorized_remote_read_disproves_worker_claim_and_mutation_stays_denied(steward_home):
    """Scenario 2 through the real control-pane lead: the Tracks worker parked saying the unit is down and it is
    waiting for Sam; the owner granted read-only diagnostics on pi-post. The lead reads the unit (active), and the
    follow-up review acts on that evidence. A read on an ungranted host and a restart are refused and never run."""
    store = Store(steward_home)
    clock = Clock()
    jid = park_tracks(store)
    grant(store, jid, "remote_read:pi-post=pipost;services=circuit-bridge")
    fr = ssh_runner({CHECK: (0, "active\nOct 05 circuit-bridge[812]: listening on :7000\n")})
    lead, scripted = control_lead(store, {ASTRA.id: [
        proposal(_diag("pi-post", CHECK), _diag("mediabox", "uptime"), confidence=0.9),
        proposal(_diag("pi-post", "systemctl restart circuit-bridge"), confidence=0.9),
    ]})
    lead.executor.diagnostic_exec = fr
    t = clock()
    ownership.sweep(store, now=t, job_lead=lead, notifier=Notes())          # classify the park (coalescing window)
    ownership.sweep(store, now=t + 120, job_lead=lead, notifier=Notes())    # review 1 starts (SyncBg runs it now)
    for _ in range(2):
        lead.on_event(jid)  # finish review 1 (its executed read starts review 2 at once), then finish review 2
    assert len(scripted.calls) == 2
    reads = diagnostics.reads_for(store, jid, limit=10)
    done = [r for r in reads if r["outcome"] == "done"]
    refused = [r for r in reads if r["outcome"] == "refused"]
    assert len(done) == 1 and "active" in done[0]["excerpt"] and done[0]["target"] == "pi-post"
    assert {r["target"] for r in refused} >= {"mediabox"}
    assert len(fr.calls) == 1 and fr.calls[0]["argv"][-1] == CHECK  # nothing else ever reached ssh
    assert len(scripted.calls) >= 2  # the executed read triggered an immediate follow-up review
    second_prompt = scripted.calls[1][1]
    assert done[0]["read_id"] in second_prompt and "listening on :7000" in second_prompt
    restart = [e for e in store.events(jid) if e.what == "tech_lead_action"
               and "restart" in str(e.evidence.get("params"))]
    assert restart and restart[0].evidence["outcome"] == "diagnostic_refused"
    assert not [e for e in store.events(jid) if e.what == "tech_lead_floor_refused"]  # not an owner floor hit


def test_lead_cannot_write_outside_worktree_or_read_secrets(env):
    store, jid, wt = env
    grant(store, jid, "remote_read:pi-post")
    fr = ssh_runner()
    cases = [(c, t) for c in ("cat ~/.api-keys", "cat /proc/1/environ", "tee /etc/motd", "journalctl -n 5 > /etc/x",
                              "printenv") for t in ("local", "pi-post")]
    cases.append((f"cat {store.home}/steward.db", "local"))  # the live store is local-only; a remote path is not ours
    for cmd, target in cases:
        p = tla.parse_job_proposal(prop(_diag(target, cmd)))
        ctx = tla.ActionContext(jid, ASTRA.id, ASTRA.model, p, wt, tpol.Policy(), entry=ASTRA,
                                authority_rev=authority.authority_rev(store, jid))
        decision, why, extra = tla.gate(p.actions[0], ctx, floor.context_for(store, jid, wt), store=store)
        assert decision == "refused" and extra["class"] in ("diagnostic_refused", "secrets"), (cmd, target, why)
    assert fr.calls == []
    # the lead's own takeover still may not touch outside the worktree (floor class 5 unchanged)
    v = floor.check({"type": "takeover", "params": {}, "paths": ["/etc/caddy/Caddyfile"]},
                    floor.context_for(store, jid, wt))
    assert not v.ok and v.klass == "outside_worktree"


def test_worker_ssh_read_on_a_granted_host_is_covered_and_a_redirect_is_not(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    wt = s.home / "worktrees" / jid
    grant(s, jid, "remote_read:pi-post")
    ok = authority.decide_worker(s, jid, "Bash", "ssh pi-post 'journalctl -u bridge -n 50 --no-pager'", wt)
    assert ok.allow and ok.basis.startswith("grant:")
    for cmd in ("ssh pi-post 'journalctl -u bridge -n 50 > /tmp/x'", "ssh pi-post 'sudo systemctl restart bridge'",
                "ssh mediabox uptime", "ssh -o ProxyCommand=evil pi-post uptime", "scp pi-post:/etc/x ."):
        assert not authority.decide_worker(s, jid, "Bash", cmd, wt).allow, cmd


def test_redirect_on_a_granted_host_is_refused(steward_home):
    s = Store(steward_home)
    jid = owner_job(s)
    grant(s, jid, "remote_read:pi-post")
    fr = ssh_runner()
    r = diagnostics.run_read(s, jid, target="pi-post", command="journalctl -u bridge -n 50 > /tmp/x",
                             purpose="x", requested_by=authority_actor(), runner_exec=fr, now=1.0)
    assert r.outcome == "refused" and "redirect" in r.refused_reason and fr.calls == []
    assert not authority.decide_worker(s, jid, "Bash", "ssh pi-post 'journalctl -u bridge -n 50 > /tmp/x'",
                                       s.home / "worktrees" / jid).allow


def authority_actor():
    from steward.actor import Actor, ActorRef

    return ActorRef(Actor.TECH_LEAD, "astra-tech-lead")


def test_diagnose_follow_ups_are_bounded(steward_home):
    store = Store(steward_home)
    jid = park_tracks(store)
    lead, scripted = control_lead(store, {ASTRA.id: [proposal(_diag("local", "uptime"), confidence=0.9)] * 6})
    lead.executor.diagnostic_exec = ssh_runner()
    t = Clock()()
    ownership.sweep(store, now=t, job_lead=lead, notifier=Notes())
    ownership.sweep(store, now=t + 120, job_lead=lead, notifier=Notes())
    for _ in range(8):
        lead.on_event(jid)
    assert len(scripted.calls) <= 1 + tla_followups()


def tla_followups():
    from steward import fleet_tech_lead

    return fleet_tech_lead.MAX_DIAGNOSE_FOLLOWUPS
