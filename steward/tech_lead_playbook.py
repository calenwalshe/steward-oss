"""The tech lead's cross-job playbook (v2.6, PRD D-06, SUP-15): issue types with fix recipes and success rates.

Global to the Steward home (the isolated home in proofs). One row per (issue kind, strategy): the latest recipe
(<= 400 chars), successes, failures, attempts, last use. Success strengthens; failure or an owner overrule demotes.
The score is the Laplace-smoothed success rate (s + 1) / (s + f + 2), so an untried recipe ranks in the middle and
one failure after one success drops it below an untried one. The top relevant entries (by kind, then score) go into
each review packet within the policy's token budget. Recipes are hints only: every action still passes the floor and
the policy.
"""

from __future__ import annotations

import json
import hashlib
import platform
import subprocess
import uuid

from steward.store import Store
from steward.actor import Actor

MAX_RECIPE = 400
TOP_N = 3


def score(row: dict) -> float:
    return (row["successes"] + 1) / (row["successes"] + row["failures"] + 2)


def record_attempt(store: Store, kind: str, strategy: str, recipe: str, *, job_id: str | None = None) -> None:
    store.playbook_bump(kind, strategy, recipe=" ".join(str(recipe).split())[:MAX_RECIPE], attempts=1, job_id=job_id)


def record_outcome(store: Store, kind: str, strategy: str, *, success: bool, job_id: str | None = None) -> None:
    store.playbook_bump(kind, strategy, successes=1 if success else 0, failures=0 if success else 1, job_id=job_id)


def demote(store: Store, kind: str, strategy: str, *, reason: str = "owner overrule") -> None:
    """An owner overrule (or a failure found later) counts against the recipe."""
    store.playbook_bump(kind, strategy, failures=1)


def entries(store: Store, kind: str | None = None) -> list[dict]:
    rows = store.playbook_rows(kind)
    for r in rows:
        r["score"] = round(score(r), 3)
    return sorted(rows, key=lambda r: (-r["score"], -(r["successes"]), r["kind"], r["strategy"]))


def top(store: Store, kind: str, n: int = TOP_N) -> list[dict]:
    return entries(store, kind)[:n]


def packet_entries(store: Store, kinds: list[str], policy, n: int = TOP_N) -> list[dict]:
    """The top relevant entries for these fact kinds, bounded to a quarter of the review token budget."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for kind in kinds:
        for r in top(store, kind, n):
            if (r["kind"], r["strategy"]) in seen:
                continue
            seen.add((r["kind"], r["strategy"]))
            out.append({"kind": r["kind"], "strategy": r["strategy"], "recipe": r["recipe"],
                        "successes": r["successes"], "attempts": r["attempts"], "failures": r["failures"],
                        "score": r["score"]})
    budget = max(400, int(policy.token_budget))  # chars: a quarter of the budget in tokens (4 chars per token)
    while out and len(json.dumps(out)) > budget:
        out.pop()
    return out


def render(rows: list[dict]) -> str:
    if not rows:
        return "(playbook is empty)"
    lines = []
    for r in rows:
        lines.append(f"{r['kind']:<26} {r['strategy']:<12} {r['successes']}/{r['attempts']} ok"
                     f" ({r['failures']} failed, score {r['score']:.2f}, last {str(r.get('last_used') or '')[:16]})"
                     f"\n    {r['recipe']}")
    return "\n".join(lines)


def record_lesson(store: Store, *, kind: str, strategy: str, incident: dict, finding: str, remedy: str,
                  conditions: dict, evidence=(), provenance: str, actor, now: float,
                  investigation_id: str | None = None, unresolved: bool = False) -> str:
    """Capture a bounded, evidence-backed incident lesson; uncertain findings stay unresolved."""
    from steward import masking
    evidence = list(evidence)
    conditions = dict(conditions or {})
    if provenance not in ("investigation", "owner_correction"):
        raise ValueError("lesson provenance must be investigation or owner_correction")
    if provenance == "owner_correction" and actor.kind != Actor.OWNER:
        raise ValueError("owner correction needs owner provenance")
    job_id = str(incident.get("job_id") or "")
    if not job_id or store.get_job(job_id) is None:
        raise ValueError("lesson incident must identify an existing job")
    if provenance == "investigation":
        inv = store._conn.execute("SELECT job_id,flag_id,state,report_json FROM run_investigations WHERE investigation_id=?",
                                  (investigation_id,)).fetchone() if investigation_id else None
        flag = (store._conn.execute("SELECT run_id FROM run_flags WHERE flag_id=? AND job_id=?",
                                    (inv["flag_id"], job_id)).fetchone() if inv is not None else None)
        if (inv is None or inv["job_id"] != job_id or inv["state"] != "done"
                or flag is None or not incident.get("flag_id") or inv["flag_id"] != incident.get("flag_id")
                or not incident.get("run_id") or flag["run_id"] != incident.get("run_id")):
            raise ValueError("investigation lessons require a completed linked investigation")
        try:
            report = json.loads(inv["report_json"] or "{}")
            assessment = report.get("assessment") or {}
            causal_refs = {str(ref) for cause in assessment.get("causes", [])
                           if cause.get("type") not in (None, "unknown") and str(cause.get("claim") or "").strip()
                           for ref in cause.get("evidence", [])}
            captured_refs = {f"event:{int(event['seq'])}" for event in
                             (report.get("evidence") or {}).get("events", [])
                             if event.get("seq") is not None and event.get("citable") is True}
            assumptions_complete = (bool(conditions.get("revision"))
                                    and isinstance(conditions.get("environment"), dict)
                                    and bool(conditions.get("environment", {}).get("platform"))
                                    and bool(conditions.get("environment", {}).get("python"))
                                    and isinstance(conditions.get("authority"), list))
            supported = (assessment.get("status") == "assessed" and bool(causal_refs)
                         and bool(clean_evidence := [str(e) for e in evidence if str(e).strip()])
                         and set(clean_evidence) <= causal_refs and set(clean_evidence) <= captured_refs
                         and assumptions_complete)
        except (TypeError, ValueError, KeyError, AttributeError, json.JSONDecodeError):
            supported = False
    else:
        supported = True
    if "kinds" not in conditions:
        raise ValueError("lesson applicability must name matching issue kinds")
    if not isinstance(conditions.get("kinds"), list) or not conditions["kinds"] or kind not in conditions["kinds"]:
        raise ValueError("lesson conditions do not include its issue kind")
    clean_evidence = [str(e)[:200] for e in evidence if str(e).strip()][:8]
    assumptions_complete = (bool(conditions.get("revision"))
                            and isinstance(conditions.get("environment"), dict)
                            and bool(conditions.get("environment", {}).get("platform"))
                            and bool(conditions.get("environment", {}).get("python"))
                            and isinstance(conditions.get("authority"), list))
    status = "unresolved" if (unresolved or not clean_evidence or not finding.strip() or not supported
                               or not assumptions_complete) else "active"
    lesson_id = "lesson-" + uuid.uuid4().hex[:20]
    doc = {"job_id": job_id, "flag_id": str(incident.get("flag_id") or "")[:120],
           "blocker_id": str(incident.get("blocker_id") or "")[:120],
           "run_id": str(incident.get("run_id") or "")[:160]}
    incident_json = json.dumps(masking.mask_obj(doc), sort_keys=True)
    evidence_json = json.dumps(masking.mask_obj(clean_evidence), sort_keys=True)
    conditions_json = json.dumps(masking.mask_obj(conditions), sort_keys=True)
    with store._conn:
        store._conn.execute("INSERT INTO playbook_lessons(lesson_id,kind,strategy,investigation_id,incident_json,"
                            "finding,remedy,conditions_json,evidence_json,status,provenance,created_epoch,updated_epoch)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (lesson_id, kind[:120], strategy[:120], investigation_id, incident_json,
                             masking.mask(" ".join(finding.split())[:400]),
                             masking.mask(" ".join(remedy.split())[:400]), conditions_json,
                             evidence_json, status, provenance, now, now))
        store._conn.execute("INSERT INTO playbook_lesson_history(lesson_id,from_status,to_status,actor,reason,"
                            "evidence_json,at_epoch) VALUES(?,?,?,?,?,?,?)",
                            (lesson_id, None, status, actor.label(), "lesson recorded", evidence_json, now))
    return lesson_id


def lessons(store: Store, *, kinds: list[str] | None = None, status: str | None = None) -> list[dict]:
    query, args = "SELECT * FROM playbook_lessons WHERE 1=1", []
    if kinds:
        query += f" AND kind IN ({','.join('?' for _ in kinds)})"
        args.extend(kinds)
    if status:
        query += " AND status=?"
        args.append(status)
    rows = store._conn.execute(query + " ORDER BY updated_epoch DESC,lesson_id", args).fetchall()
    return [{**dict(r), "incident": json.loads(r["incident_json"]),
             "conditions": json.loads(r["conditions_json"]), "evidence": json.loads(r["evidence_json"])}
            for r in rows]


def _current_job_context(store: Store, job_id: str) -> dict:
    from steward import authority

    job = store.get_job(job_id)
    worktree = store.home / "worktrees" / job_id
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, capture_output=True,
                             text=True, timeout=3, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = None
    eff = authority.effective(store, job_id)
    return {"revision": head, "environment": {"platform": platform.system(), "python": platform.python_version()},
            "authority": sorted(g.grant_id for g in eff.grants)}


def _conditions_match(conditions: dict, context: dict, kind: str) -> bool:
    if not conditions or kind not in (conditions.get("kinds") or []):
        return False
    if (not conditions.get("revision") or not isinstance(conditions.get("environment"), dict)
            or not conditions["environment"].get("platform") or not conditions["environment"].get("python")
            or not isinstance(conditions.get("authority"), list)):
        return False
    for key in ("revision", "authority", "environment"):
        if key not in conditions:
            return False
        expected = conditions[key]
        actual = context.get(key)
        if isinstance(expected, dict):
            if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in expected.items()):
                return False
        elif actual != expected:
            return False
    return True


def relevant_lessons(store: Store, job_id: str, kinds: list[str], *, limit: int = 4) -> list[dict]:
    """Return matching lessons as advisory evidence, never executable actions."""
    context = _current_job_context(store, job_id)
    found = []
    for row in lessons(store, kinds=kinds, status="active"):
        if not _conditions_match(row["conditions"], context, row["kind"]):
            continue
        found.append({"lesson_id": row["lesson_id"], "kind": row["kind"], "strategy": row["strategy"],
                      "finding": row["finding"], "remedy": row["remedy"], "evidence": row["evidence"],
                      "incident": row["incident"], "provenance": row["provenance"],
                      "advisory_only": True, "authority": "current job instructions and grants remain controlling"})
        if len(found) >= max(0, limit):
            break
    return found


def transition_lesson(store: Store, lesson_id: str, to_status: str, *, actor, reason: str, now: float,
                      evidence=()) -> None:
    from steward import masking

    if to_status not in ("active", "demoted", "superseded", "unresolved"):
        raise ValueError("invalid lesson status")
    if actor.kind != Actor.OWNER:
        raise ValueError("lesson status changes require owner provenance")
    row = store._conn.execute("SELECT status FROM playbook_lessons WHERE lesson_id=?", (lesson_id,)).fetchone()
    if row is None:
        raise KeyError(lesson_id)
    refs = json.dumps(masking.mask_obj([str(e)[:200] for e in evidence][:8]))
    with store._conn:
        store._conn.execute("UPDATE playbook_lessons SET status=?,updated_epoch=? WHERE lesson_id=?",
                            (to_status, now, lesson_id))
        store._conn.execute("INSERT INTO playbook_lesson_history(lesson_id,from_status,to_status,actor,reason,"
                            "evidence_json,at_epoch) VALUES(?,?,?,?,?,?,?)",
                            (lesson_id, row["status"], to_status, actor.label(), " ".join(reason.split())[:300], refs, now))


def propose_policy_change(store: Store, job_id: str, *, description: str, evidence=(), actor, now: float) -> None:
    """Record a reviewable suggestion only; a lesson cannot silently mutate active policy."""
    from steward.models import Judgment
    from steward import masking

    store.append_event(job_id, what="playbook_policy_change_proposed",
                       evidence={"proposal": " ".join(description.split())[:400], "evidence": list(evidence)[:8],
                                 "actor": actor.label(), "active": False,
                                 "evidence_digest": hashlib.sha256(json.dumps(list(evidence)[:8], sort_keys=True).encode()).hexdigest()[:16]},
                       rule="playbook_lesson", judgment=Judgment.human if actor.kind == Actor.OWNER else Judgment.code)
