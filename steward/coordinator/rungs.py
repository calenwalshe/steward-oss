"""Deterministic code rungs (spec 5.13, 6.1, 6.3): an episode's trigger + evidence -> whitelisted verbs.

``select`` walks the rungs in spec order -- ``false_red`` -> ``superseded_verify`` -> ``obsolete_fixup`` ->
``recorded_default`` -> ``dead_runner`` -> ``cockpit_down`` -- and returns the first ``Plan`` an applicable rung
builds. A rung *applies* when its trigger and evidence conditions hold (cheap reads only). An applicable rung is
skipped when it is disabled (``cdb.rung_disabled``), when it already ran ``tries_per_rung_day`` times for this key in
24 h, or when the episode's ``rungs_json`` already lists it (a re-park after acting "moves to the next rung", 6.3).
Otherwise its try is recorded **before** it builds (and before any rerun), so a crash or an ``inconclusive`` rerun
still counts. Code rungs ignore an in-flight TL review (R17 gates only the model rung). An ask-only episode gets no
plan.

Contract with the daemon (Task 12):
- ``None``: no code rung acted; go to the model rung when allowed, else close ``asked``. This includes a
  ``false_red`` rerun that came back ``inconclusive`` (try counted, the walk continues to later rungs first).
- A ``Plan`` with ``verbs == []``: the rung's evidence settles that nobody but the owner can act
  (``false_red`` -> ``env_live_red`` / ``preexisting``, or a second ``real`` within 24 h: ``false_red_real`` is
  capped at 1, so it asks rather than reaching the model rung, which could re-nudge). Close the episode ``asked`` with ``plan.diagnosis`` as the
  "what it found" part of the ask; ``plan.classification`` and ``plan.evidence`` go on the episode.
- A ``Plan`` with verbs: run them in order (stop at the first failure); when ``plan.followup`` is set, run it no
  sooner than ``plan.followup_after_s`` after the verbs succeeded (R9: the recorded-default send-back).

Settled details (documented because the spec leaves them open):
- ``false_red`` stale-red guard: the newest ``verification recorded`` must be at kv ``last_ruled_sha``. An unset
  ``last_ruled_sha`` counts as "this verification is the ruled one" (runner.py writes it right after closeout, so it
  is unset only on a job that never got that far, where the red is current by construction).
- ``false_red`` without a pending recovery approval needs a workspace for its ``send-back``; with neither the rung does
  not apply (checked before the rerun is paid for).
- ``superseded_verify`` adds the empty-commit clause when the park's ``commit_sha`` equals ``last_ruled_sha`` (R3), and
  also when the park names no sha but a ruled sha exists (an extra empty commit is cheap; a refused re-report is E4).
- ``watchdog_active`` / ``workspace`` may be values or zero-argument callables; they are resolved lazily (only a rung
  that needs them pays for a ``systemctl``/``herdr`` lookup). ``watchdog_active=None`` means unknown and is treated as
  active (one healer per component, R11).
- ``unstuck`` for ``orphan:*`` / ``liveness_ask`` ignores noise events and the coordinator's own echoes
  (``coordinator*`` events, ``runner_restarted``, anything whose evidence ``actor`` is ``operator_agent:coordinator``
  or ``origin`` is ``coordinator``): the remedy's own receipt is not proof the job moved.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .. import coordinator_guard, coordinator_link
from ..machine import TERMINAL_STATES
from ..models import JobState
from ..ownership import PARK_RULES, PARK_WHATS, park_event
from ..train_housekeeping import MERGE_GOAL_PREFIX
from . import episodes, reruns, triggers, verbs
from .verbs import Verb

log = logging.getLogger(__name__)

# ---------- texts (spec 6.1, exact) ----------

NUDGE_RESUBMIT = (
    "Steward coordinator: your work at {head8} is done as far as these checks go. The {n} failing tests ({ids3}…) "
    "pass in isolation on {head8}; they read live machine state, not your change. Do not change any code. Run "
    "`git commit --allow-empty -m \"chore: re-run verification (coordinator {ep8})\"`, then report STEWARD-DONE."
)
NUDGE_REAL = (
    "Steward coordinator: {ids3}… fail on your commit {head8} and pass on base {base8}; they are real regressions. "
    "Fix these only, commit, and report STEWARD-DONE."
)
SEND_BACK_SUPERSEDED = (
    "Your verification run was superseded by a newer run, not failed. Report STEWARD-DONE again{empty_commit_clause}."
)
SEND_BACK_ANSWERED = (
    "Your open questions {qids} were answered with their recorded recommendations. Continue from there and report "
    "STEWARD-DONE when finished."
)
RULE_FALSE_RED = (
    "yield/closeout: after a red verification whose failing ids pass in isolation on the same sha, re-run verification "
    "on the next done report instead of refusing it as same_as_last_ruled (steward/yield_lanes.py classify; "
    "steward/closeout.py). Quarantine or hermeticize: {ids}. Overlaps ZT Task 17 (hermetic conftest) and ZT §5.5 "
    "(rerun before believing, train only)."
)
# R3: the same empty commit as NUDGE_RESUBMIT, for a superseded run whose head was already ruled on.
EMPTY_COMMIT_CLAUSE = (
    " after you run `git commit --allow-empty -m \"chore: re-run verification (coordinator {ep8})\"` (this commit "
    "was already ruled on)"
)
# Ask diagnoses (6.1 table; "none -> close `asked`: <text>").
ASK_ENV_LIVE_RED = ("ids pass in isolation on {head8} but fail against this machine's live state now; "
                    "not the worker's")
ASK_PREEXISTING = "ids fail on base {base8} too right now"
ASK_REAL_AGAIN = ("{ids3}… are real regressions (fail on {head8}, pass on {base8}); already nudged once in 24 h")
RULE_ENV_LIVE_RED = ("tests: hermeticize {ids}: they pass in isolation on {head8} but read this machine's live state "
                     "(Overlaps ZT Task 17, hermetic conftest).")
RULE_PREEXISTING = "baseline: {ids} fail on base {base8} too; quarantine or fix them on the default branch."

# ---------- numbers ----------

RUNG_ORDER = ("false_red", "superseded_verify", "obsolete_fixup", "recorded_default", "dead_runner", "cockpit_down")
FALSE_RED_TRIGGERS = frozenset({"needs_human:recovery_budget_exhausted", "needs_human:diagnosis_verdict_needs_human",
                                "needs_human:verify_rounds_exhausted"})
REAL_RUNG = "false_red_real"
REAL_CAP = 1
QUESTION_AGE_S = 3600.0
FOLLOWUP_AFTER_S = 70.0  # R9: after the 60 s answer batch write
RESOLVED_STATES = (JobState.CHECKS_PASSED, JobState.ACCEPTED, JobState.RELEASED)
LAST_RULED_KEY = "last_ruled_sha"  # yield_lanes.LAST_RULED_KEY
_CHECK_RUN_ID = re.compile(r"\bcr-[0-9a-f]+\b")


@dataclass
class Plan:
    rung: str
    verbs: list[Verb]
    diagnosis: str
    evidence: dict
    rule: str | None
    classification: str | None
    followup: list[Verb] | None = None
    followup_after_s: float = 0


# ---------- context ----------


@dataclass
class _Ctx:
    ro: object
    cdb: object
    episode: dict
    trig: object
    settings: object
    now: float
    runner_alive: Callable[[str], bool]
    rerun_fn: Callable
    key: str
    job: object | None
    _watchdog: object = None
    _workspace: object = None
    _cache: dict = field(default_factory=dict)

    @property
    def job_id(self) -> str | None:
        return self.job.job_id if self.job is not None else None

    @property
    def ep8(self) -> str:
        return str(self.episode.get("episode_id") or "")[:8]

    def watchdog_active(self) -> bool:
        if "watchdog" not in self._cache:
            val = self._watchdog() if callable(self._watchdog) else self._watchdog
            self._cache["watchdog"] = True if val is None else bool(val)
        return self._cache["watchdog"]

    def workspace(self) -> str | None:
        if "workspace" not in self._cache:
            val = self._workspace() if callable(self._workspace) else self._workspace
            self._cache["workspace"] = val if isinstance(val, str) and val and not val.startswith("-") else None
        return self._cache["workspace"]


def _ids3(ids: list[str]) -> str:
    return ", ".join(ids[:3])


def _parked(ctx) -> bool:
    return ctx.job is not None and ctx.job.state is JobState.NEEDS_HUMAN


# ---------- false_red ----------

def _recovery_approval(ro, job_id) -> dict | None:
    pending = [r for r in ro.approvals(job_id, status="pending")
               if r.get("kind", "general") == "general" and _is_recovery(r)]
    return pending[-1] if pending else None


def _is_recovery(record: dict) -> bool:
    from steward import approvals  # lazy: keeps the module import light

    return approvals.is_recovery_approval(record)


def _applies_false_red(ctx) -> dict | None:
    if ctx.trig.kind not in FALSE_RED_TRIGGERS or not _parked(ctx):
        return None
    ver = ctx.ro.events_of(ctx.job_id, ["verification recorded"])
    if not ver:
        return None
    last = ver[-1]
    ids = reruns.failing_ids(last.evidence)
    head = (last.evidence or {}).get("commit_sha")
    if not ids or not isinstance(head, str) or not head:
        return None
    ruled = ctx.ro.kv_get(ctx.job_id, LAST_RULED_KEY)
    if isinstance(ruled, str) and ruled and ruled != head:
        return None  # stale red: the worker has moved on since this verification
    approval = _recovery_approval(ctx.ro, ctx.job_id)
    if approval is None and ctx.workspace() is None:
        return None  # the only remedy left would be a send-back, which needs --workspace
    base_rows = ctx.ro.checks(ctx.job_id, "baseline")
    base = base_rows[0].get("commit_sha") if base_rows else None
    return {"head": head, "base": base or None, "ids": ids, "approval": approval, "verification_seq": last.seq,
            "last_ruled_sha": ruled}


def _build_false_red(ctx, st) -> Plan | None:
    head, base, ids, approval = st["head"], st["base"], st["ids"], st["approval"]
    scratch = coordinator_link.state_dir() / "scratch" / str(ctx.episode.get("episode_id") or "episode")
    try:
        res = ctx.rerun_fn(repo=Path(ctx.job.repo), head_sha=head, base_sha=base, ids=ids, scratch=scratch,
                           settings=ctx.settings)
    except Exception as exc:  # noqa: BLE001 - a crashing rerun is an inconclusive one
        log.exception("false_red rerun crashed for %s", ctx.job_id)
        res = reruns.RerunResult("inconclusive", head, base, ids, note=f"rerun crashed: {exc}")
    evidence = {**asdict(res), "verification_seq": st["verification_seq"], "last_ruled_sha": st["last_ruled_sha"],
                "approval_id": approval["id"] if approval else None, "scratch": str(scratch),
                "trigger": ctx.trig.kind}
    outcome = res.outcome
    fmt = {"head8": head[:8], "base8": (base or "unknown")[:8], "n": len(ids), "ids3": _ids3(ids), "ep8": ctx.ep8,
           "ids": ", ".join(ids)}
    job = ctx.job_id
    if outcome in ("env_dependent", "flaky"):
        text = NUDGE_RESUBMIT.format(**fmt)
        acts = [verbs.approve(job, approval["id"]), verbs.nudge(job, text)] if approval \
            else [verbs.send_back(job, text, ctx.workspace())]
        return Plan("false_red", acts,
                    f"false red ({outcome}): {len(ids)} failing id(s) pass in isolation on {head[:8]}; {res.note}",
                    evidence, RULE_FALSE_RED.format(ids=fmt["ids"]), outcome)
    if outcome == "env_live_red":
        return Plan("false_red", [], ASK_ENV_LIVE_RED.format(**fmt), evidence, RULE_ENV_LIVE_RED.format(**fmt),
                    outcome)
    if outcome == "preexisting":
        return Plan("false_red", [], ASK_PREEXISTING.format(**fmt), evidence, RULE_PREEXISTING.format(**fmt), outcome)
    if outcome == "real":
        if ctx.cdb.try_count(ctx.key, REAL_RUNG, ctx.now) >= REAL_CAP:
            # 6.1 "a second time -> ask" (8.6 cap): never None, which would let the model rung re-nudge.
            return Plan("false_red", [], ASK_REAL_AGAIN.format(**fmt), evidence, None, outcome)
        ctx.cdb.record_try(ctx.key, REAL_RUNG, ctx.now)
        acts = ([verbs.approve(job, approval["id"])] if approval else []) + [verbs.nudge(job, NUDGE_REAL.format(**fmt))]
        return Plan("false_red", acts,
                    f"real regressions: {len(ids)} id(s) fail on {head[:8]} and pass on base {fmt['base8']}",
                    evidence, None, outcome)
    log.info("false_red inconclusive for %s: %s", job, res.note)
    return None


# ---------- superseded_verify ----------

def _park_evidence(ctx) -> dict:
    events = ctx.ro.events_of(ctx.job_id, sorted(PARK_WHATS), sorted(PARK_RULES))
    park = park_event(events)
    if park is not None and park.rule == "bg_verify_unverified" and isinstance(park.evidence, dict):
        return park.evidence
    ev = (ctx.trig.detail or {}).get("evidence") if isinstance(ctx.trig.detail, dict) else None
    return ev if isinstance(ev, dict) else {}


def _applies_superseded(ctx) -> dict | None:
    if ctx.trig.kind != "needs_human:bg_verify_unverified" or not _parked(ctx):
        return None
    ev = _park_evidence(ctx)
    reasons = ev.get("reasons")
    text = " ".join(str(r) for r in reasons) if isinstance(reasons, list) else str(reasons or "")
    if "superseded by" not in text or ctx.workspace() is None:
        return None
    head = ev.get("commit_sha") if isinstance(ev.get("commit_sha"), str) else None
    ruled = ctx.ro.kv_get(ctx.job_id, LAST_RULED_KEY)
    ruled = ruled if isinstance(ruled, str) and ruled else None
    return {"reasons": text, "check_runs": list(dict.fromkeys(_CHECK_RUN_ID.findall(text))), "head": head,
            "last_ruled_sha": ruled, "empty_commit": bool(ruled) and (head is None or head == ruled)}


def _build_superseded(ctx, st) -> Plan:
    clause = EMPTY_COMMIT_CLAUSE.format(ep8=ctx.ep8) if st["empty_commit"] else ""
    text = SEND_BACK_SUPERSEDED.format(empty_commit_clause=clause)
    return Plan("superseded_verify", [verbs.send_back(ctx.job_id, text, ctx.workspace())],
                f"verification run superseded, not failed ({', '.join(st['check_runs']) or 'run ids not named'})",
                dict(st), None, "superseded")


# ---------- obsolete_fixup ----------

def _parent_id(ro, job_id) -> str | None:
    for key in ("autopilot_followup", "parent_job"):
        val = ro.kv_get(job_id, key)
        if isinstance(val, str) and val:
            return val
    return None


def _applies_obsolete(ctx) -> dict | None:
    job = ctx.job
    if job is None or job.state in TERMINAL_STATES or not job.goal.startswith(MERGE_GOAL_PREFIX):
        return None
    done, why = coordinator_guard.parent_done(ctx.ro, job.job_id)
    if not done:
        return None
    pid = _parent_id(ctx.ro, job.job_id)
    parent = ctx.ro.get_job(pid) if pid else None
    return {"parent_id": pid, "parent_state": parent.state.value if parent else None,
            "incorporated_commit": ctx.ro.kv_get(pid, "incorporated_commit") if pid else None, "why": why}


def _build_obsolete(ctx, st) -> Plan:
    return Plan("obsolete_fixup", [verbs.stop(ctx.job_id)], f"fix-up is obsolete: {st['why']}", dict(st), None,
                "obsolete_fixup")


# ---------- recorded_default ----------

def _epoch_or_none(ts) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return triggers._epoch(ts)
    except ValueError:
        return None


def _found_at(ro, job_id) -> dict[str, float]:
    """qid -> ts of the first `questions_found` event naming it (fallback for records without `first_seen_at`)."""
    out: dict[str, float] = {}
    for e in ro.events_of(job_id, ["questions_found"]):
        qids = (e.evidence or {}).get("qids")
        ts = _epoch_or_none(e.ts)
        if ts is None or not isinstance(qids, list):
            continue
        for q in qids:
            if isinstance(q, str):
                out.setdefault(q, ts)
    return out


def _applies_recorded_default(ctx) -> dict | None:
    job = ctx.job
    if job is None or job.state not in (JobState.NEEDS_HUMAN, JobState.RUNNING):
        return None
    records = ctx.ro.kv_get(job.job_id, "questions", []) or []
    found = None
    picked = []
    for r in records if isinstance(records, list) else []:
        if not isinstance(r, dict) or r.get("status") != "open" or r.get("starred") is not True:
            continue
        qid, rec = r.get("qid"), r.get("recommended")
        if not isinstance(qid, str) or not qid or qid.startswith("-") or not rec:
            continue
        seen = _epoch_or_none(r.get("first_seen_at"))
        if seen is None:
            found = found if found is not None else _found_at(ctx.ro, job.job_id)
            seen = found.get(qid)
        if seen is None or ctx.now - seen < QUESTION_AGE_S:
            continue
        picked.append({"qid": qid, "recommended": rec, "recommended_text": r.get("recommended_text", ""),
                       "first_seen_at": r.get("first_seen_at") or seen})
    if not picked:
        return None
    if job.state is JobState.NEEDS_HUMAN and ctx.workspace() is None:
        return None  # answers alone do not resume a parked job (E9); its send-back needs --workspace
    return {"questions": picked, "state": job.state.value}


def _build_recorded_default(ctx, st) -> Plan:
    qids = [q["qid"] for q in st["questions"]]
    followup, after = None, 0.0
    if ctx.job.state is JobState.NEEDS_HUMAN:
        followup = [verbs.send_back(ctx.job_id, SEND_BACK_ANSWERED.format(qids=", ".join(qids)), ctx.workspace())]
        after = FOLLOWUP_AFTER_S
    return Plan("recorded_default", [verbs.answer_recommended(ctx.job_id, qids)],
                f"{len(qids)} starred question(s) open ≥ {QUESTION_AGE_S:.0f} s with a recorded default",
                dict(st), None, "recorded_default", followup, after)


# ---------- dead_runner ----------

def _applies_dead_runner(ctx) -> dict | None:
    job = ctx.job
    if job is None or ctx.trig.kind not in ("orphan:RUNNING", "liveness_ask") or job.state is not JobState.RUNNING:
        return None
    if ctx.runner_alive(job.job_id) or ctx.watchdog_active():
        return None
    return {"runner_code": ctx.ro.kv_get(job.job_id, "runner_code"),
            "last_non_noise_event_ts": ctx.ro.last_non_noise_event_ts(job.job_id), "trigger": ctx.trig.kind}


def _build_dead_runner(ctx, st) -> Plan:
    return Plan("dead_runner", [verbs.runner_restart(ctx.job_id)], "RUNNING job with no live runner; watchdog inactive",
                dict(st), None, "dead_runner")


# ---------- cockpit_down ----------

def _detail_text(trig) -> str:
    d = trig.detail
    if isinstance(d, dict):
        return str(d.get("detail") or "")
    return str(d or "")


def _applies_cockpit(ctx) -> dict | None:
    if ctx.trig.kind != "health:control" or "no control pane" not in _detail_text(ctx.trig):
        return None
    if ctx.watchdog_active() or ctx.workspace() is None:
        return None
    detail = ctx.trig.detail if isinstance(ctx.trig.detail, dict) else {"detail": _detail_text(ctx.trig)}
    return {"health": dict(detail), "workspace": ctx.workspace()}


def _build_cockpit(ctx, st) -> Plan:
    return Plan("cockpit_down", [verbs.up(ctx.workspace())], "control pane missing; watchdog inactive", dict(st), None,
                "cockpit_down")


_RUNGS = (
    ("false_red", _applies_false_red, _build_false_red),
    ("superseded_verify", _applies_superseded, _build_superseded),
    ("obsolete_fixup", _applies_obsolete, _build_obsolete),
    ("recorded_default", _applies_recorded_default, _build_recorded_default),
    ("dead_runner", _applies_dead_runner, _build_dead_runner),
    ("cockpit_down", _applies_cockpit, _build_cockpit),
)
assert tuple(r[0] for r in _RUNGS) == RUNG_ORDER


def _tried(episode: dict) -> set[str]:
    try:
        items = json.loads((episode or {}).get("rungs_json") or "[]")
    except (TypeError, ValueError):
        return set()
    out = set()
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and "rung" in it:
            out.add(str(it["rung"]))
        elif isinstance(it, str):
            out.add(it)
    return out


def select(ro, cdb, episode, trig, *, settings, now, runner_alive, watchdog_active, workspace,
           rerun_fn=reruns.rerun) -> Plan | None:
    """The first applicable, enabled, under-cap rung's Plan (spec 6.1 order); its try is recorded before it builds."""
    if not episode or episodes.is_ask_only(episode):
        return None
    key = trig.job_id or episode.get("job_id")
    if not key:
        return None
    job = ro.get_job(trig.job_id) if trig.job_id else None
    if trig.job_id and job is None:
        return None
    ctx = _Ctx(ro=ro, cdb=cdb, episode=episode, trig=trig, settings=settings, now=now, runner_alive=runner_alive,
               rerun_fn=rerun_fn, key=key, job=job, _watchdog=watchdog_active, _workspace=workspace)
    tried = _tried(episode)
    for name, applies, build in _RUNGS:
        if name in tried:
            continue
        try:
            state = applies(ctx)
        except Exception:  # noqa: BLE001 - one broken rung must not hide the others
            log.exception("rung %s applicability check failed for %s", name, key)
            continue
        if state is None:
            continue
        if cdb.rung_disabled(name):
            log.info("rung %s applies to %s but is disabled", name, key)
            continue
        if cdb.try_count(key, name, now) >= settings.tries_per_rung_day:
            log.info("rung %s applies to %s but used its %s tries today", name, key, settings.tries_per_rung_day)
            continue
        cdb.record_try(key, name, now)
        try:
            plan = build(ctx, state)
        except Exception:  # noqa: BLE001
            log.exception("rung %s failed to build a plan for %s", name, key)
            plan = None
        if plan is not None:
            return plan
    return None


# ---------- resolution (6.3) ----------

def _noise_matchers():
    out = []
    for pat in triggers._NOISE_LIKE:
        lit = pat.replace("\\_", "_")
        out.append((lit[:-1], True) if lit.endswith("%") else (lit, False))
    return tuple(out)


_NOISE = _noise_matchers()


def _is_progress(e) -> bool:
    """A non-noise event that is not the coordinator's own action echo."""
    what = e.what or ""
    if any(what.startswith(p) if prefix else what == p for p, prefix in _NOISE):
        return False
    if what.startswith("coordinator") or what == "runner_restarted" or e.rule == "coordinator":
        return False
    ev = e.evidence if isinstance(e.evidence, dict) else {}
    if str(ev.get("actor", "")).startswith("operator_agent:coordinator") or ev.get("origin") == "coordinator":
        return False
    return True


def _since(episode: dict) -> float | None:
    acted = episode.get("acted_at")
    return acted if acted is not None else episode.get("opened_at")


def unstuck(ro, episode, *, health=None) -> bool:
    """For `needs_human:*`, the job is not NEEDS_HUMAN; for `orphan:*` / `liveness_ask`, a non-noise event arrived
    after the action; for `health:<c>`, `<c>` is ok in `health` (the latest health read)."""
    kind = str(episode.get("trigger") or "")
    if kind.startswith("health:"):
        comp = triggers._components(health).get(kind.split(":", 1)[1])
        return bool(comp) and comp.get("ok") is True
    job_id = episode.get("job_id")
    job = ro.get_job(job_id) if job_id else None
    if job is None:
        return False
    if kind.startswith("needs_human:"):
        return job.state is not JobState.NEEDS_HUMAN
    if kind.startswith("orphan:") or kind == "liveness_ask":
        since = _since(episode)
        for e in reversed(ro.events(job_id)):
            ts = _epoch_or_none(e.ts)
            if ts is None:
                continue
            if since is not None and ts <= since:
                return False
            if _is_progress(e):
                return True
        return False
    return False


def resolved(ro, episode, *, now, settings=None) -> bool:
    """Unstuck for `resolved_watch_s` (the daemon keeps `unstuck_at` only while continuously unstuck), or the job
    reached CHECKS_PASSED / ACCEPTED / RELEASED, or a green `verification recorded` landed after `acted_at`."""
    watch = float(getattr(settings, "resolved_watch_s", 600.0))
    unstuck_at = episode.get("unstuck_at")
    if unstuck_at is not None and now - float(unstuck_at) >= watch:
        return True
    job_id = episode.get("job_id")
    job = ro.get_job(job_id) if job_id and not str(job_id).startswith("health:") else None
    if job is None:
        return False
    if job.state in RESOLVED_STATES:
        return True
    since = _since(episode)
    if since is None:
        return False
    for e in reversed(ro.events_of(job_id, ["verification recorded"])):
        ts = _epoch_or_none(e.ts)
        if ts is None or ts <= since:
            break
        nf = (e.evidence or {}).get("new_failures")
        if isinstance(nf, list) and not nf:
            return True
    return False
