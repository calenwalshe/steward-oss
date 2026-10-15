"""Which finish-line outcomes a verification actually exercised (continuous job ownership §8; Plan 4).

"Verification proves what it exercised": a unit-green suite proves `unit` outcomes only; an idle-session check does
not prove an active-transfer recovery; unit checks never establish activation. `live`/`manual` outcomes unproven at
the candidate become an acceptance reason that Plan 1 classifies as the technical blocker `outcome_unproven`;
`activation` outcomes make activation a required delivery milestone (steward.delivery). Proof is deterministic: a
finish-line pass of the outcome's own command since the worker's last run, or recorded evidence whose reference was
validated against durable receipts. Worker text and model claims are never evidence here.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from steward.actor import ActorRef
from steward.models import Judgment
from steward.store import Store

SCOPES = ("unit", "live", "activation", "manual")
REASON_PREFIX = "outcome unproven"
EVIDENCE_WHAT = "outcome_evidence"
_TAG = re.compile(r"\((unit|live|activation|manual)\)\s*$", re.I)
_MANUAL = re.compile(r"\b(physically|by hand|unplug\w*|plug\w* in|power[- ]cycl\w*|manually|owner confirms|"
                     r"in person)\b", re.I)
_ACTIVATION = re.compile(r"\b(deployed|redeployed|restart\w*|re-?exec\w*|activated|running (in|on) (the )?"
                         r"(control|production|service|mcp|dash)|in production|after (a )?reload)\b", re.I)
_LIVE = re.compile(r"\b(live|end-to-end|e2e|real (device|hardware|account|traffic|transfer)|on the device|"
                   r"active (transfer|session|connection)|against (the )?(real|live|production)|in the browser|"
                   r"over the network)\b", re.I)
_IMPLEMENTATION_DELIVERABLE = re.compile(r"\b(implement(?:ation)?|deliverables?|approved scope|required outcomes?)\b",
                                        re.I)
_CMD = re.compile(r"`([^`\n]+)`")
_RESTART_WHATS = ("worker attempt started", "worker dispatch started")


@dataclass(frozen=True)
class Outcome:
    oid: str
    text: str
    requires: str
    command: str | None


def _scope(text: str) -> str:
    tag = _TAG.search(text)
    if tag:
        return tag.group(1).lower()
    if _MANUAL.search(text):
        return "manual"
    if _ACTIVATION.search(text):
        return "activation"
    if _LIVE.search(text):
        return "live"
    # An implementation/scope statement without a testable command is not proved by a green suite. Treat it as
    # owner-reviewed work until the contract names a concrete scope or an approved evidence reference exists.
    if _IMPLEMENTATION_DELIVERABLE.search(text):
        return "manual"
    return "unit"


def outcomes(contract) -> list[Outcome]:
    out = []
    for i, text in enumerate(list(getattr(contract, "done_when", None) or [])):
        m = _CMD.search(text)
        out.append(Outcome(f"o{i + 1}", str(text), _scope(str(text)), m.group(1).strip() if m else None))
    return out


def _script(command: str) -> str:
    """The command's identifying script/executable, ignoring the interpreter (`python3 x.py` == `x.py`)."""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    parts = [p for p in parts if not p.startswith("-")]
    while parts and parts[0].rsplit("/", 1)[-1] in ("python", "python3", "bash", "sh", "uv", "run", "-m"):
        parts = parts[1:]
    return parts[0] if parts else command.strip()


def _same_command(a: str | None, b: str | None) -> bool:
    return bool(a and b) and _script(a) == _script(b)


def _last_run_seq(events) -> int:
    return max((e.seq for e in events if e.what in _RESTART_WHATS), default=0)


def last_verified_commit(store: Store, job_id: str) -> str | None:
    for e in reversed(store.events(job_id)):
        if e.what == "checks passed at exact commit":
            return (e.evidence or {}).get("commit_sha")
    return None


def coverage(store: Store, job_id: str, contract, *, commit_sha: str | None) -> dict[str, dict]:
    events = store.events(job_id)
    since = _last_run_seq(events)
    finish = [e for e in events if e.what == "finish_line_met" and e.seq > since]
    recorded = [e for e in events if e.what == EVIDENCE_WHAT and (e.evidence or {}).get("commit_sha") == commit_sha]
    out: dict[str, dict] = {}
    for o in outcomes(contract):
        proven_by: list[str] = []
        if o.requires == "unit":
            proven_by = ["verification at candidate"] if commit_sha else []
        else:
            proven_by += [f"event:{e.seq}" for e in recorded if (e.evidence or {}).get("oid") == o.oid]
            if o.requires == "live":
                proven_by += [f"finish_line_met:{e.seq}" for e in finish
                              if _same_command(o.command, (e.evidence or {}).get("command"))]
        out[o.oid] = {"requires": o.requires, "text": o.text, "status": "proven" if proven_by else "unproven",
                      "proven_by": proven_by}
    return out


def unproven(store: Store, job_id: str, contract, *, commit_sha: str | None) -> list[Outcome]:
    cov = coverage(store, job_id, contract, commit_sha=commit_sha)
    return [o for o in outcomes(contract) if cov[o.oid]["status"] != "proven" and o.requires in ("live", "manual")]


def acceptance_reasons(store: Store, job_id: str, contract, *, commit_sha: str | None) -> list[str]:
    return [f"{REASON_PREFIX} ({o.requires}): {o.text[:160]}"
            for o in unproven(store, job_id, contract, commit_sha=commit_sha)]


def _validate(store: Store, job_id: str, outcome: Outcome, scope: str, ref: str) -> str:
    kind, _, ident = ref.partition(":")
    if kind == "check_run" and scope == "live":
        from steward import check_runs

        row = check_runs.get(store, ident)
        if row is None or row.state != "done" or row.exit_code != 0 or row.broken:
            raise ValueError(f"{ref} is not a green check-run receipt")
        if not _same_command(outcome.command, row.command):
            raise ValueError(f"{ref} ran `{row.command}`, not the outcome's `{outcome.command}`")
        return ref
    if kind == "activation" and scope in ("activation", "live"):
        row = store._conn.execute("SELECT seq FROM delivery_receipts WHERE job_id = ? AND milestone = 'activation'"
                                  " AND seq = ?", (job_id, int(ident or 0))).fetchone()
        if row is None:
            raise ValueError(f"{ref} is not an activation receipt of this job")
        return ref
    if kind == "approval" and scope == "manual":
        from steward import approvals

        rec = approvals.get_approval(store, job_id, ident)
        if rec is None or rec.get("status") != "approved" or rec.get("decided_by", "human") != "human":
            raise ValueError(f"{ref} is not an owner-approved decision")
        return ref
    raise ValueError(f"unsupported evidence reference {ref!r} for a {scope} outcome")


def record_evidence(store: Store, job_id: str, *, oid: str, scope: str, ref: str, commit_sha: str | None,
                    actor: ActorRef, now: float) -> dict:
    from steward.contract import load_contract

    contract = load_contract(store, job_id)
    outcome = next((o for o in outcomes(contract) if o.oid == oid), None)
    if outcome is None or scope != outcome.requires or scope not in SCOPES:
        raise ValueError(f"outcome {oid} does not require {scope} evidence")
    _validate(store, job_id, outcome, scope, ref)
    evidence = {"oid": oid, "scope": scope, "ref": ref, "commit_sha": commit_sha, "actor": actor.label(),
                "at": now}
    store.append_event(job_id, what=EVIDENCE_WHAT, evidence=evidence, rule="outcome_scope", judgment=Judgment.code)
    return evidence


def _p_outcome_proven(store, job_id, b, facts) -> str:
    from steward.contract import load_contract

    contract = load_contract(store, job_id)
    if contract is None:
        return "uncertain"
    return "cleared" if not unproven(store, job_id, contract, commit_sha=last_verified_commit(store, job_id)) \
        else "holds"


def _register() -> None:
    from steward import blockers, ownership

    blockers.register_kind("outcome_unproven",
                           blockers.KindSpec(False, 2, ("completion",), "outcome_proven", "tech_lead"))
    ownership.register_predicate("outcome_proven", _p_outcome_proven)


_register()
