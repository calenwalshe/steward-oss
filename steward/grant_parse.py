"""Recover recorded owner grants from legacy prose (continuous job ownership Plan 2; spec §6).

Before Plan 2 an owner's authorization lived only in prose — the goal text, an owner steer, an answer — and every
decider ignored it (audit RC-1). Migration re-reads ONLY recorded owner sources and turns sentences that are
unambiguous into grants with provenance (event/steer reference, sanitized excerpt). It never reads worker output,
transcripts, Jev, lead/resolver text or retrieved documents, and it never asks the owner: an ambiguous sentence
becomes an `authorization_missing` candidate blocker for technical review, which may find another route, resolve
it, or confirm one precise owner question through Plan 1's `confirm_human`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from steward import grants
from steward.actor import Actor, ActorRef
from steward.models import JobState, Judgment
from steward.store import Store

MIGRATION_KEY = "authority_migrated"
MIGRATION_VERSION = 1
MIGRATOR = ActorRef(Actor.CONTROLLER, "migration")
_GRANT_VERB = re.compile(r"\b(authori[sz](?:e|es|ed|ation)|approv(?:e|es|ed|al)|covers?|permitted|allowed to|"
                         r"you may|may (?:run|write|read|edit|apply|reload|restart))\b", re.I)
_NEGATION = re.compile(r"\b(not|never|no|don'?t|do not|must not|mustn'?t|without)\b", re.I)
_CONDITION = re.compile(r"\b(if|unless|after|once|when|ask|asking|confirm|check with|provided)\b", re.I)
_PATH = re.compile(r"(?<![\w.])(/(?:etc|srv|var|opt|usr|home|root|run|boot)/[\w./@+-]+)")
_BACKTICK = re.compile(r"`([^`]{2,200})`")
_KNOWN_CMD = re.compile(r"\b((?:sudo\s+)?(?:systemctl\s+(?:reload|restart|start|stop|status)\s+[\w@.-]+|"
                        r"nft\s+-f\s+\S+|git\s+push|docker\s+(?:restart|compose\s+up)\s*[\w.-]*|"
                        r"caddy\s+reload))", re.I)
_READ_HOST = re.compile(r"\b(?:read|inspect|diagnos\w*|check|look at|logs? (?:on|from))\b[^.]*?\b(?:on|from|host)\s+"
                        r"([A-Za-z0-9][A-Za-z0-9_.-]{0,62})\b", re.I)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
_EXAMPLE_COMMAND = re.compile(r"pytest|python3? -m pytest|npm test|make test", re.I)
# The event `steward answer` writes per recorded answer, and its free-text key. Step 3 note: confirm both against
# `steward/cli.py::_h_answer` / `steward/questions.py` before committing; if answers carry no free text, set
# ANSWER_EVENT = "" so answers are simply not a source (recorded in the progress record, never invented).
ANSWER_EVENT = "answer recorded"
ANSWER_TEXT_KEY = "text"


@dataclass(frozen=True)
class ProposedGrant:
    kind: str
    operation: str
    scope: dict
    excerpt: str
    state: str          # unambiguous | ambiguous
    reason: str


@dataclass(frozen=True)
class MigrationResult:
    job_id: str
    added: tuple
    ambiguous: tuple
    skipped: tuple
    blocker_ids: tuple


def _clause_negated(sentence: str, start: int) -> bool:
    clause = re.split(r"[;,]|\band\b|\bbut\b", sentence[:start])[-1]
    return bool(_NEGATION.search(clause))


def propose_from_text(text: str) -> list[ProposedGrant]:
    out: list[ProposedGrant] = []
    for sentence in _SENTENCES.split(" ".join(str(text or "").split())):
        verb = _GRANT_VERB.search(sentence)
        if not verb:
            continue
        excerpt = sentence[:300]
        negated = _clause_negated(sentence, verb.end()) or bool(_NEGATION.search(sentence[:verb.start()][-24:]))
        conditional = bool(_CONDITION.search(sentence))
        paths = sorted(dict.fromkeys(m.group(1).rstrip(".,;:") for m in _PATH.finditer(sentence)))
        cmds = [m.group(1).strip() for m in _BACKTICK.finditer(sentence) if not _EXAMPLE_COMMAND.search(m.group(1))]
        cmds += [m.group(1).strip() for m in _KNOWN_CMD.finditer(sentence)]
        cmds = sorted(dict.fromkeys(" ".join(c.split()).rstrip(".,;:") for c in cmds if not c.startswith("/")))
        hosts = sorted(dict.fromkeys(m.group(1) for m in _READ_HOST.finditer(sentence)))
        found = []
        if paths:
            found.append(("write_path", {"paths": paths}))
        if cmds:
            found.append(("run_command", {"commands": cmds}))
        for h in hosts:
            found.append(("remote_read", {"host": h, "ssh": h}))
        if not found:
            out.append(ProposedGrant("allow", "", {}, excerpt, "ambiguous", "grant wording without a concrete target"))
            continue
        for op, scope in found:
            try:
                scope = grants.normalize_scope(op, scope)
            except grants.GrantError as exc:
                out.append(ProposedGrant("allow", op, {}, excerpt, "ambiguous", f"unusable target: {exc}"))
                continue
            if negated:
                out.append(ProposedGrant("prohibit", op, scope, excerpt, "unambiguous", "negated grant sentence"))
            elif conditional:
                out.append(ProposedGrant("allow", op, scope, excerpt, "ambiguous", "conditional grant"))
            else:
                out.append(ProposedGrant("allow", op, scope, excerpt, "unambiguous", "explicit grant and target"))
    return out


def owner_sources(store: Store, job_id: str) -> list[dict]:
    from steward.questions_delivery import HUMAN_ORIGINS
    from steward.steering import Steering

    out = []
    for e in store.events(job_id):
        if e.what == "job created" and e.judgment == Judgment.human and e.evidence.get("goal"):
            out.append({"kind": "goal_text", "ref": f"event:{e.seq}", "text": str(e.evidence["goal"]),
                        "relayed_by": str(e.evidence.get("actor") or "")})
        elif (e.what == ANSWER_EVENT and e.judgment == Judgment.human
              and e.evidence.get("origin") in HUMAN_ORIGINS and e.evidence.get(ANSWER_TEXT_KEY)):
            out.append({"kind": "owner_answer", "ref": f"event:{e.seq}", "text": str(e.evidence[ANSWER_TEXT_KEY]),
                        "relayed_by": str(e.evidence.get("actor") or "")})
    for i in Steering(store).all_instructions(job_id):
        actor_label = str(i.get("actor") or "")
        if i.get("origin") in HUMAN_ORIGINS and not actor_label.startswith(("worker", "tech_lead", "jev", "model")):
            out.append({"kind": "owner_steer", "ref": f"steer:{i.get('rev')}", "text": str(i.get("text") or ""),
                        "relayed_by": actor_label})
    return out


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def migrate_job(store: Store, job_id: str, *, now: float) -> MigrationResult:
    from steward import blockers, research

    state = store.kv_get(job_id, MIGRATION_KEY) or {"version": MIGRATION_VERSION, "sources": []}
    seen = set(state.get("sources") or [])
    added, ambiguous, skipped, blocker_ids = [], [], [], []
    for src in owner_sources(store, job_id):
        sdig = _digest(src["kind"], src["ref"], src["text"])
        if sdig in seen:
            continue
        for p in propose_from_text(src["text"]):
            if p.state == "ambiguous":
                ambiguous.append(p)
                params = ({"operation": p.operation, "target": _target(p)} if p.operation and p.scope else {})
                b, _ = blockers.raise_blocker(
                    store, job_id, kind="authorization_missing", subject=f"ambiguous:{_digest(p.excerpt)}",
                    claim=f"recorded owner text may grant something, but its scope is unclear: {p.excerpt[:300]}",
                    evidence=[src["ref"]], origin_actor=MIGRATOR, now=now, origin_rule="legacy_grant_ambiguous",
                    clearing={"predicate": "grant_covers", "params": params} if params else {})
                blocker_ids.append(b.blocker_id)
                continue
            g, created = grants.add(store, job_id, operation=p.operation, scope=p.scope, kind=p.kind, now=now,
                                    granted_by=MIGRATOR,
                                    source=grants.Source(kind=src["kind"], ref=src["ref"], excerpt=p.excerpt,
                                                         relayed_by=src["relayed_by"]))
            (added if created else skipped).append(g if created else g.grant_id)
        seen.add(sdig)
    caps = research.get_caps(store, job_id)
    cap_digest = _digest("caps", *caps["keys"])
    if caps["keys"] and cap_digest not in seen:
        g, created = grants.add(store, job_id, operation="key", scope={"names": caps["keys"]}, now=now,
                                granted_by=MIGRATOR,
                                source=grants.Source(kind="legacy_capabilities", ref="kv:capabilities",
                                                     excerpt="--grant-keys " + ",".join(caps["keys"])))
        (added if created else skipped).append(g if created else g.grant_id)
        seen.add(cap_digest)
    store.kv_set(job_id, MIGRATION_KEY, {"version": MIGRATION_VERSION, "sources": sorted(seen), "at": now})
    if added or ambiguous:
        store.append_event(job_id, what="authority_migrated",
                           evidence={"added": len(added), "ambiguous": len(ambiguous),
                                     "grant_ids": [g.grant_id for g in added][:20], "blockers": blocker_ids[:10]},
                           rule="legacy_grant_migration", judgment=Judgment.code)
    return MigrationResult(job_id, tuple(added), tuple(ambiguous), tuple(skipped), tuple(blocker_ids))


def _target(p: ProposedGrant) -> dict:
    if p.operation == "write_path":
        return {"path": p.scope["paths"][0]}
    if p.operation == "run_command":
        return {"command": p.scope["commands"][0]}
    if p.operation == "remote_read":
        return {"host": p.scope["host"]}
    return {}


_DONE_STATES = (JobState.RELEASED, JobState.FAILED, JobState.STOPPED)


def migrate_pending(store: Store, *, now: float, limit: int = 20) -> list[MigrationResult]:
    """Cheap and bounded (control-loop safe): unfinished jobs never migrated at this version, oldest first."""
    out = []
    for job in store.list_jobs():
        if len(out) >= limit:
            break
        if job.state in _DONE_STATES:
            continue
        st = store.kv_get(job.job_id, MIGRATION_KEY) or {}
        if st.get("version") == MIGRATION_VERSION:
            continue
        try:
            out.append(migrate_job(store, job.job_id, now=now))
        except Exception as exc:  # noqa: BLE001 - one broken job never stops the sweep (spec §4)
            store.append_event(job.job_id, what="authority_migration_failed", evidence={"error": str(exc)[:200]},
                               rule="legacy_grant_migration", judgment=Judgment.code)
    return out
