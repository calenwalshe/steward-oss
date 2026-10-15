"""Activation evidence (continuous job ownership §9; Plan 4).

Incorporated is not activated, and a successful deploy-self is not activated either. Activation is proven only when
every required target reports a loaded revision that contains the merge commit and the required live checks are
green; the receipt names the running revision. Probes are read-only (control `ping`, runner-code records, `steward
health`). MCP and the dashboard report no loaded revision: listed as unprobed, never counted. Deploy runs at most once
per merge commit; verification re-probes on a bounded schedule; an unproven activation is a technical blocker for
the lead (e.g. a runner that has not restarted), never an owner page by itself.
"""
from __future__ import annotations

import time

from steward import delivery, incorporate as inc
from steward.actor import Actor, ActorRef
from steward.contract import load_contract
from steward.models import Judgment
from steward.store import Store

PROBE_TRIES = 10
PROBE_WAIT_S = 1.0
PROBE_TIMEOUT_S = 10.0
TARGETS = ("control", "runners")
UNPROBED = ("mcp", "dash")
VERIFY_EVERY_S = 300.0
VERIFY_MAX = 6
ACTIVATE_LEASE_S = 900.0
UNPROVEN_KIND = "activation_unproven"
ACTOR = ActorRef(Actor.CONTROLLER, "activation")


def contains(repo, commit: str | None, running_head: str | None) -> bool:
    if not commit or not running_head:
        return False
    return inc._git(repo, "merge-base", "--is-ancestor", commit, running_head).returncode == 0


def probe_control(store: Store, *, relay=None) -> dict:
    def _relay(op, args):
        from steward import cli

        try:
            return cli._relay(store, op, args, origin="activation")
        except cli._RelayFailed as exc:
            return {"accepted": False, "detail": str(exc)}

    rec = (relay or _relay)("ping", {})
    head = str(rec.get("head") or "")
    return {"target": "control", "ok": bool(rec.get("accepted")) and bool(head), "head": head, "pid": rec.get("pid"),
            "detail": str(rec.get("detail") or "")[:120]}


def probe_runners(store: Store) -> list[dict]:
    from steward import stability
    from steward.bg_baseline import _alive
    from steward.models import JobState

    out = []
    for job in store.list_jobs({JobState.RUNNING}):
        rec = store.kv_get(job.job_id, stability.RUNNER_CODE_KEY)
        if not isinstance(rec, dict):
            continue
        out.append({"target": f"runner:{job.job_id[:8]}", "head": str(rec.get("head") or ""), "pid": rec.get("pid"),
                    "ok": bool(rec.get("head")) and _alive(rec.get("pid"))})
    return out


def live_checks(store: Store, *, health_fn=None) -> dict:
    from steward import deploy_self

    ok, detail = (health_fn or deploy_self.default_health(store))()
    return {"ok": bool(ok), "detail": str(detail)[:200]}


def _probe(store: Store, targets, probes) -> list[dict]:
    probes = probes or {"control": lambda: [probe_control(store)], "runners": lambda: probe_runners(store)}
    observed: list[dict] = []
    for t in targets:
        observed += list(probes[t]())
    return observed


def verify_activation(store: Store, job_id: str, merge_commit: str, *, probes=None, health_fn=None, now: float,
                      tries: int = PROBE_TRIES, sleep=time.sleep) -> dict:
    from steward import blockers, outcome_scope

    job = store.get_job(job_id)
    targets = [t for t in delivery.activation_targets(store, job_id) if t in TARGETS]
    observed, stale, health = [], [], {"ok": False, "detail": "not run"}
    for i in range(max(1, tries)):
        observed = _probe(store, targets, probes)
        stale = [o for o in observed if not (o["ok"] and contains(job.repo, merge_commit, o["head"]))]
        health = live_checks(store, health_fn=health_fn)
        if not stale and health["ok"]:
            break
        if i + 1 < tries:
            sleep(PROBE_WAIT_S)
    control = next((o for o in observed if o["target"] == "control"), None)
    proven = not stale and health["ok"] and ("control" not in targets or control is not None)
    evidence = {"contains": merge_commit if proven else None, "merge_commit": merge_commit, "targets": targets,
                "observed": observed, "live_checks": health, "unprobed": list(UNPROBED), "proven": proven}
    store.append_event(job_id, what="activation_verified" if proven else "activation_unproven",
                       evidence={k: evidence[k] for k in ("merge_commit", "targets", "live_checks", "proven")}
                       | {"stale": [f"{o['target']}@{str(o['head'])[:12]}" for o in stale]},
                       rule="activation", judgment=Judgment.code)
    if proven:
        seq = delivery.record_receipt(store, job_id, "activation", actor=ACTOR,
                                      revision=(control or {}).get("head") or merge_commit, evidence=evidence, now=now)
        for o in outcome_scope.outcomes(load_contract(store, job_id)):
            if o.requires == "activation":
                outcome_scope.record_evidence(store, job_id, oid=o.oid, scope="activation", ref=f"activation:{seq}",
                                              commit_sha=merge_commit, actor=ACTOR, now=now)
        from steward import ownership

        ownership.reconcile_job(store, job_id, now=now)
        return {"proven": True, "receipt": seq, "observed": observed, "live_checks": health}
    why = "; ".join(f"{o['target']} runs {str(o['head'])[:12] or 'unknown'}" for o in stale) or \
        f"live checks: {health['detail']}"
    blockers.raise_blocker(store, job_id, kind=UNPROVEN_KIND, subject=merge_commit,
                           claim=f"activation of {merge_commit[:12]} not proven: {why}"[:400],
                           evidence=[f"merge:{merge_commit}"], origin_actor=ACTOR, now=now, origin_rule="activation",
                           clearing={"predicate": "activation_proven", "params": {"merge_commit": merge_commit}})
    return {"proven": False, "observed": observed, "live_checks": health, "why": why}


def _deploy_key(job_id: str, merge_commit: str) -> str:
    from steward import intents

    return intents.intent_key(job_id, "activate-deploy", merge_commit)


def state(store: Store, job_id: str, merge_commit: str, *, now: float) -> str:
    """deploy (never deployed for this merge) | verify (deployed; a probe is due) | wait | exhausted."""
    from steward import intents

    deployed = intents.get(store, _deploy_key(job_id, merge_commit))
    auto = any(e.what == "auto_deployed" and (e.evidence or {}).get("merge_commit") == merge_commit
               for e in store.events_of(job_id, ("auto_deployed",)))
    if deployed is None and not auto:
        return "deploy"
    if deployed is not None and deployed.state == "claimed":
        return "wait"
    probes = [e for e in store.events_of(job_id, ("activation_unproven", "activation_verified"))
              if (e.evidence or {}).get("merge_commit") == merge_commit]
    if len(probes) >= VERIFY_MAX:
        return "exhausted"
    last = store.kv_get(job_id, "activation_last_probe") or 0.0
    return "verify" if now - float(last) >= VERIFY_EVERY_S or not probes else "wait"


def activate(store: Store, job_id: str, merge_commit: str, *, deploy=None, probes=None, health_fn=None, now: float,
             sleep=time.sleep, authorized: bool | None = None) -> dict:
    from steward import deploy_self, intents

    if authorized is None:
        authorized = delivery.activation_authority(store, job_id).allowed
    if not authorized:
        return {"activated": False, "reason": "no activation authority"}
    st = state(store, job_id, merge_commit, now=now)
    if st == "deploy":
        key = _deploy_key(job_id, merge_commit)
        if intents.claim(store, key=key, job_id=job_id, kind="activate-deploy", lease_s=ACTIVATE_LEASE_S, now=now) is None:
            return {"activated": False, "reason": "deploy already in progress"}
        job = store.get_job(job_id)
        try:
            res = (deploy or deploy_self.deploy)(store, root=job.repo)
        except Exception as exc:  # noqa: BLE001 - a failed deploy is a recorded failure, never a crash
            res = {"ok": False, "summary": f"{type(exc).__name__}: {exc}"}
        if not res.get("ok"):
            intents.fail(store, key, reason=str(res.get("summary"))[:300], now=now, retryable=True)
            store.append_event(job_id, what="activation_deploy_failed", evidence={"merge_commit": merge_commit,
                               "summary": str(res.get("summary"))[:200]}, rule="activation",
                               judgment=Judgment.code)
            return {"activated": False, "reason": f"deploy failed: {res.get('summary')}"}
        intents.complete(store, key, receipt={"summary": str(res.get("summary"))[:200]}, now=now)
    elif st in ("wait", "exhausted"):
        return {"activated": False, "reason": st}
    store.kv_set(job_id, "activation_last_probe", now)
    v = verify_activation(store, job_id, merge_commit, probes=probes, health_fn=health_fn, now=now, sleep=sleep)
    return {"activated": v["proven"], **v}


def attest(store: Store, job_id: str, merge_commit: str, *, approval_id: str, now: float) -> dict:
    """A `declared` target (no Steward-known mechanism): the owner's approved delivery decision is the evidence,
    recorded as owner-attested with the revision the owner confirmed."""
    from steward import approvals

    rec = approvals.get_approval(store, job_id, approval_id)
    if (rec is None or rec.get("status") != "approved" or rec.get("decided_by") != "human"
            or rec.get("kind") != delivery.APPROVAL_KIND or rec.get("milestone") != "activation"
            or rec.get("revision") != merge_commit or inc.is_incorporated(store, job_id) != merge_commit):
        raise ValueError("activation attestation needs the owner's approved decision for this merged revision")
    who = ActorRef(Actor.OWNER, "approval")
    seq = delivery.record_receipt(store, job_id, "activation", actor=who, revision=merge_commit, now=now,
                                  evidence={"contains": merge_commit, "attested": True, "approval_id": approval_id,
                                            "targets": ["declared"], "running_revision": "owner-attested"})
    from steward import outcome_scope

    for o in outcome_scope.outcomes(load_contract(store, job_id)):
        if o.requires == "activation":
            outcome_scope.record_evidence(store, job_id, oid=o.oid, scope="activation", ref=f"activation:{seq}",
                                          commit_sha=merge_commit, actor=who, now=now)
    from steward import ownership

    ownership.reconcile_job(store, job_id, now=now)
    return {"proven": True, "receipt": seq}


def _p_activation_proven(store, job_id, b, facts) -> str:
    merge = ((b.clearing or {}).get("params") or {}).get("merge_commit")
    return "cleared" if any(r["revision_contains"] == merge for r in delivery.receipts(store, job_id, "activation")) \
        else "holds"


def _register() -> None:
    from steward import blockers, ownership

    blockers.register_kind(UNPROVEN_KIND, blockers.KindSpec(False, 2, ("delivery",), "activation_proven", "tech_lead"))
    ownership.DELIVERY_KINDS.add(UNPROVEN_KIND)
    ownership.register_predicate("activation_proven", _p_activation_proven)


_register()
