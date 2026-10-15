"""Watcher authority (phase 15): the trust ladder and the code gate for custom watchers.

A watcher's model only ever *proposes*. Everything here is deterministic code that decides what a proposal may
do, from the watcher's tier (owner-confirmed), its max grants, and evidence the runner observed itself.

Ladder: T0 shadow (observe) -> T1 escalate -> T2 nudge -> T3 interrupt + pause lease -> T4 resume lease.
Decision record: docs/decisions/2026-09-29-watcher-authority.md.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Callable

from steward.models import Judgment
from steward.store import Store

# -- ladder -------------------------------------------------------------------

TIER_NAMES = {0: "shadow", 1: "escalate", 2: "nudge", 3: "interrupt", 4: "resume"}
TIER_POWERS: dict[int, frozenset] = {
    0: frozenset(),
    1: frozenset({"escalate"}),
    2: frozenset({"escalate", "nudge"}),
    3: frozenset({"escalate", "nudge", "interrupt", "pause"}),
    4: frozenset({"escalate", "nudge", "interrupt", "pause", "resume"}),
}
MAX_TIER = 4
NEW_TIER = 0
LEGACY_TIER = 2  # A1: watchers stored before phase 15 keep working
DEFAULT_GRANT = ("escalate",)  # A2 / record item 8: observe + escalate
LABELS = ("correct", "false", "severe_false")
Z95 = 1.96

# Promotion bars (record item 1). n = adjudicated cases.
BARS = {
    2: {"n": 5, "precision": 0.8, "lb": 0.5},
    3: {"n": 12, "precision": 0.9, "lb": 0.7, "severe_window": 20},
    4: {"n": 12, "precision": 0.9, "lb": 0.7, "severe_window": 20, "pause_episodes": 3},
}
DEMOTE_ORDINARY = (2, 10)  # 2 ordinary false in the last 10 cases
BREAKER = (3, 3600.0)  # 3 integrity rejections inside an hour

# Record item 7. A verdict asking for any of these is refused and recorded, whatever the tier.
HUMAN_ONLY = frozenset({
    "urgent", "indefinite_pause", "stop", "accept", "reject", "incorporate", "merge", "push", "publish",
    "contact", "email", "message", "notify_external", "destructive", "reset", "delete", "recover",
    "grant", "budget", "promote", "trust", "goal", "acceptance", "adjudicate", "resolve_conflict",
})
_FIXED_HUMAN_ACTORS = frozenset({"human", "cli", "moshi", "chatgpt"})


def human_actors() -> frozenset:
    """Actors that count as a human: the fixed ones plus the owner's actor keys (resolved at call time)."""
    from steward import owner

    return _FIXED_HUMAN_ACTORS | owner.actor_keys()

KV_PREFIX = "watcher_authority:"


class AuthorityError(ValueError):
    pass


def _key(name: str) -> str:
    return KV_PREFIX + name


def spec_hash(w: dict) -> str:
    """Identity of everything that shapes a watcher's judgment: prompt (ask), model, cadence, max grants."""
    body = json.dumps({"model": w.get("model"), "ask": w.get("ask"), "every_s": w.get("every_s"),
                       "may": sorted(w.get("may") or [])}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def get_record(store: Store, job_id: str, name: str) -> dict | None:
    v = store.kv_get(job_id, _key(name))
    return v if isinstance(v, dict) else None


def _put(store: Store, job_id: str, name: str, rec: dict) -> None:
    store.kv_set(job_id, _key(name), rec)


def init_record(store: Store, job_id: str, w: dict) -> dict:
    rec = {"tier": NEW_TIER, "spec_hash": spec_hash(w), "cases": [], "cases_mark": 0, "history": [],
           "rejections": [], "proposed": []}
    _put(store, job_id, w["name"], rec)
    return rec


def _require_human(actor: str, what: str) -> None:
    if actor not in human_actors():
        raise AuthorityError(f"{what} is human-only (actor {actor!r})")


def tier_of(store: Store, job_id: str, w: dict) -> int:
    """Current tier. A spec/prompt/model/config change demotes to T0 here, before anything else can use the tier."""
    rec = get_record(store, job_id, w["name"])
    if rec is None:
        return LEGACY_TIER
    if rec.get("spec_hash") != spec_hash(w):
        if rec["tier"] > 0:
            demote(store, job_id, w["name"], 0, "config_change", w=w)
        else:
            rec["spec_hash"] = spec_hash(w)
            rec["cases_mark"] = len(rec["cases"])
            _put(store, job_id, w["name"], rec)
        rec = get_record(store, job_id, w["name"])
    return int(rec["tier"])


def active_powers(store: Store, job_id: str, w: dict) -> set[str]:
    return set(w.get("may") or []) & TIER_POWERS[tier_of(store, job_id, w)]


# -- cases, stats, promotion ----------------------------------------------------


def wilson_lb(correct: int, n: int, z: float = Z95) -> float:
    if n <= 0:
        return 0.0
    p = correct / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / d


def stats(cases: list[dict]) -> dict:
    n = len(cases)
    correct = sum(1 for c in cases if c["label"] == "correct")
    return {"n": n, "correct": correct, "precision": (correct / n) if n else 0.0, "lb": wilson_lb(correct, n),
            "severe_last_20": any(c["label"] == "severe_false" for c in cases[-20:]),
            "pause_episodes": sum(1 for c in cases if c.get("kind") == "pause" and c["label"] == "correct")}


def _ensure_record(store: Store, job_id: str, w: dict) -> dict:
    """A legacy watcher (no record) is materialised at its grandfathered tier, never reset to T0."""
    rec = get_record(store, job_id, w["name"])
    if rec is None:
        legacy = tier_of(store, job_id, w)
        rec = init_record(store, job_id, w)
        rec["tier"] = legacy
    return rec


def _live_cases(rec: dict) -> list[dict]:
    return rec["cases"]


def bar_gaps(rec: dict, target: int) -> list[str]:
    if target not in BARS:
        return []  # T1 (escalate) has no bar in the record
    bar, s = BARS[target], stats(_live_cases(rec))
    gaps = []
    if s["n"] < bar["n"]:
        gaps.append(f"cases {s['n']}<{bar['n']}")
    if s["precision"] < bar["precision"]:
        gaps.append(f"precision {s['precision']:.2f}<{bar['precision']}")
    if not s["lb"] > bar["lb"]:
        gaps.append(f"wilson_lb {s['lb']:.2f}<={bar['lb']}")
    if bar.get("severe_window") and s["severe_last_20"]:
        gaps.append("severe false action in last 20")
    if s["pause_episodes"] < bar.get("pause_episodes", 0):
        gaps.append(f"correct pause episodes {s['pause_episodes']}<{bar['pause_episodes']}")
    return gaps


def record_case(store: Store, job_id: str, w: dict, label: str, *, actor: str, kind: str = "verdict",
                ref: str = "", now: float = 0.0, notify: Callable | None = None) -> dict:
    """A human adjudicates one watcher verdict/action. Only labels exist: silence never counts (A3)."""
    _require_human(actor, "adjudication")
    if label not in LABELS:
        raise AuthorityError(f"bad label {label!r} (allowed: {', '.join(LABELS)})")
    rec = _ensure_record(store, job_id, w)
    case = {"label": label, "kind": kind, "ref": ref, "by": actor, "ts": now}
    rec["cases"].append(case)
    _put(store, job_id, w["name"], rec)
    store.append_event(job_id, what="watcher_case_adjudicated", evidence={"name": w["name"], **case, "n": len(rec["cases"])},
                       rule="watcher_authority_case", judgment=Judgment.human)
    _auto_demote(store, job_id, w)
    if notify is not None:
        propose_promotion(store, job_id, w, notify=notify)
    return case


def _auto_demote(store: Store, job_id: str, w: dict) -> None:
    rec = get_record(store, job_id, w["name"])
    tier = int(rec["tier"])
    if tier == 0:
        return
    fresh = rec["cases"][rec["cases_mark"]:]
    if fresh and fresh[-1]["label"] == "severe_false":
        demote(store, job_id, w["name"], 0, "severe_false_action", w=w)
        return
    k, window = DEMOTE_ORDINARY
    if sum(1 for c in fresh[-window:] if c["label"] != "correct") >= k:
        demote(store, job_id, w["name"], tier - 1, "ordinary_false_2_in_10", w=w)


def demote(store: Store, job_id: str, name: str, to: int, reason: str, *, w: dict | None = None) -> dict:
    rec = get_record(store, job_id, name)
    if rec is None:
        rec = {"tier": LEGACY_TIER, "spec_hash": spec_hash(w) if w else "", "cases": [], "cases_mark": 0,
               "history": [], "rejections": [], "proposed": []}
    frm = int(rec["tier"])
    rec["tier"] = max(0, min(to, frm))
    rec["cases_mark"] = len(rec["cases"])
    if w is not None:
        rec["spec_hash"] = spec_hash(w)
    rec["history"].append({"from": frm, "to": rec["tier"], "reason": reason, "by": "code"})
    _put(store, job_id, name, rec)
    store.append_event(job_id, what="watcher_demoted", evidence={"name": name, "from": frm, "to": rec["tier"], "reason": reason},
                       rule="watcher_authority_demotion", judgment=Judgment.code)
    return rec


def propose_promotion(store: Store, job_id: str, w: dict, *, notify: Callable | None = None) -> dict | None:
    """The runner proposes; it never promotes. Emits once per (target tier, case count)."""
    rec = get_record(store, job_id, w["name"])
    if rec is None:
        return None
    target = int(rec["tier"]) + 1
    if target > MAX_TIER or target not in BARS or bar_gaps(rec, target):
        return None
    mark = [target, len(rec["cases"])]
    if mark in rec["proposed"]:
        return None
    rec["proposed"].append(mark)
    _put(store, job_id, w["name"], rec)
    s = stats(rec["cases"])
    ev = {"name": w["name"], "from": rec["tier"], "to": target, **{k: s[k] for k in ("n", "precision", "lb")}}
    store.append_event(job_id, what="watcher_promotion_proposed", evidence=ev, rule="watcher_authority_promotion",
                       judgment=Judgment.code)
    if notify is not None:
        notify(job_id, "NEEDS_YOU", f"Steward: promote {w['name']}"[:40],
               f"watcher {w['name']} meets the T{target} bar ({s['n']} cases); confirm with `steward watcher promote`"[:140])
    return ev


def promote(store: Store, job_id: str, w: dict, *, actor: str, tier: int | None = None, reason: str = "") -> dict:
    """Trust promotion is human-only. Below the bar is allowed; the gap is recorded (record item 1)."""
    _require_human(actor, "trust promotion")
    cur = tier_of(store, job_id, w)
    rec = _ensure_record(store, job_id, w)
    target = cur + 1 if tier is None else int(tier)
    if not (0 <= target <= MAX_TIER):
        raise AuthorityError(f"tier {target} out of range 0..{MAX_TIER}")
    gaps = bar_gaps(rec, target) if target > cur and target in BARS else []
    rec["history"].append({"from": cur, "to": target, "reason": reason or "confirmed", "by": actor, "gaps": gaps})
    rec.update(tier=target, spec_hash=spec_hash(w))
    if target < cur:
        rec["cases_mark"] = len(rec["cases"])
    _put(store, job_id, w["name"], rec)
    store.append_event(job_id, what="watcher_promoted", evidence={"name": w["name"], "from": cur, "to": target, "gaps": gaps,
                                                               "reason": reason},
                       rule="watcher_authority_promotion", judgment=Judgment.human)
    return {"from": cur, "to": target, "gaps": gaps}


# -- integrity rejections / circuit breaker ------------------------------------


def note_rejection(store: Store, job_id: str, w: dict, reason: str, now: float,
                   notify: Callable | None = None) -> bool:
    """Count an integrity rejection (stale epoch, bad evidence, human-only request). Returns True if the breaker tripped."""
    rec = get_record(store, job_id, w["name"])
    if rec is None:
        return False
    k, window = BREAKER
    rec["rejections"] = [t for t in rec["rejections"] if now - t < window] + [now]
    tripped = len(rec["rejections"]) >= k
    _put(store, job_id, w["name"], rec)
    if tripped:
        rec["rejections"] = []
        _put(store, job_id, w["name"], rec)
        store.append_event(job_id, what="watcher_breaker_tripped", evidence={"name": w["name"], "reason": reason},
                           rule="watcher_authority_breaker", judgment=Judgment.code)
        demote(store, job_id, w["name"], 0, "circuit_breaker", w=w)
        if notify is not None:
            notify(job_id, "NEEDS_YOU", f"Steward: watcher {w['name']}"[:40], "circuit breaker tripped; demoted to T0"[:140])
    return tripped


def human_only_requests(verdict: dict) -> list[str]:
    """Which human-only actions does this verdict ask for? (Refused, never executed.)"""
    asked = sorted(k for k in verdict if isinstance(k, str) and k.lower() in HUMAN_ONLY and verdict.get(k))
    for key in ("interrupt", "pause"):
        v = verdict.get(key)
        if isinstance(v, dict):
            if v.get("urgent"):
                asked.append("urgent")
            if key == "pause" and (v.get("indefinite") or v.get("until_resumed")):
                asked.append("indefinite_pause")
    return sorted(set(asked))


def refuse_human_only(store: Store, job_id: str, w: dict, verdict: dict, now: float,
                      notify: Callable | None = None) -> list[str]:
    asked = human_only_requests(verdict)
    if asked:
        store.append_event(job_id, what="watcher_refused", evidence={"name": w["name"], "human_only": asked},
                           rule="watcher_human_only", judgment=Judgment.code)
        note_rejection(store, job_id, w, "human_only", now, notify)
    return asked


def flow_watcher_review(store: Store, job_id: str) -> dict:
    """v2.3 Flow `watcher_review`: ask an enabled watcher the job already trusts (tier >= T1, holding at least its
    `escalate` power) for a normal run now. Quarantine blocks it; the run's own verdict still passes
    `evaluate_actions`. Flow gains no watcher power: it can only ask for a run the watcher could do anyway."""
    from steward import custom_watchers

    if quarantine(store, job_id) is not None:
        return {"ok": False, "reason": "watcher_quarantined"}
    for w in custom_watchers.list_watchers(store, job_id):
        if not w.get("enabled", True) or "escalate" not in active_powers(store, job_id, w):
            continue
        custom_watchers.request_run(store, job_id, w["name"])
        return {"ok": True, "watcher": w["name"], "tier": tier_of(store, job_id, w)}
    return {"ok": False, "reason": "no_trusted_watcher"}


def summary(store: Store, job_id: str, w: dict) -> dict:
    tier = tier_of(store, job_id, w)
    rec = get_record(store, job_id, w["name"])
    s = stats(rec["cases"]) if rec else stats([])
    nxt = tier + 1 if tier < MAX_TIER else None
    return {"name": w["name"], "tier": tier, "tier_name": TIER_NAMES[tier], "legacy": rec is None,
            "grants": list(w.get("may") or []), "active": sorted(active_powers(store, job_id, w)),
            "cases": s["n"], "precision": round(s["precision"], 3), "wilson_lb": round(s["lb"], 3),
            "next_tier_gaps": bar_gaps(rec, nxt) if rec and nxt in BARS else None,
            "history": (rec or {}).get("history", [])[-5:]}


# -- quarantine (state; the decision logic lives with the gate) ------------------------

QUARANTINE_KEY = "watcher_quarantine"


def quarantine(store: Store, job_id: str) -> dict | None:
    v = store.kv_get(job_id, QUARANTINE_KEY)
    return v if isinstance(v, dict) and v else None


def clear_quarantine(store: Store, job_id: str, *, actor: str) -> bool:
    """Final conflict adjudication is human-only (record item 7)."""
    _require_human(actor, "conflict adjudication")
    q = quarantine(store, job_id)
    if q is None:
        return False
    store.kv_set(job_id, QUARANTINE_KEY, None)
    store.append_event(job_id, what="watcher_quarantine_cleared", evidence={"was": q}, rule="watcher_authority_conflict",
                       judgment=Judgment.human)
    return True


def release_counters(store: Store, job_id: str) -> dict:
    """Monitoring signals for the release gates (record item 9 and the I4 falsifier). Counters, not enforcement."""
    from steward import custom_watchers as cw

    names = [w["name"] for w in cw.list_watchers(store, job_id)]
    runs = sum(cw.run_count(store, job_id, n) for n in names)
    false_int = 0
    for n in names:
        rec = get_record(store, job_id, n)
        false_int += sum(1 for c in (rec or {}).get("cases", []) if c.get("kind") == "interrupt" and c["label"] != "correct")
    reversals = sum(1 for e in store.events(job_id) if e.what == "watcher_pause_reversal")
    return {"runs": runs, "false_interrupts": false_int, "reversals": reversals,
            "false_interrupt_rate_per_100_runs": round(100.0 * false_int / runs, 2) if runs else 0.0,
            "revisit_lifetime_cap": bool(runs and false_int / runs > 0.01)}


# ==============================================================================
# Plan 15-02: evidence, runner-native predicates, the interrupt / pause / resume gate
# ==============================================================================

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CONFIDENCE_MIN = 0.9
SETTLE_S = 300.0  # A6
FREEZE_S = 900.0  # 15-min actuation freeze after an interrupt
WATCHER_INTERRUPT_S = 3600.0  # 1 per watcher per hour
JOB_INTERRUPTS = (2, 6 * 3600.0)  # 2 per job per rolling 6 h. NO lifetime cap (owner decision I4 = B).
PAUSE_LEASE_S = 900.0
PAUSE_JOB = (1, 6 * 3600.0)  # 1 pause per job per 6 h
CORROBORATION_S = 1800.0  # A8
CALL_WINDOW_S = 1800.0
DEADLINE_S = 1200.0  # predicate (c)
IDENTICAL_CALLS = 3  # predicate (b)
REPEATED_FAILURES = 3  # predicate (d)
SPEND_BREACH_USD_HOUR = 25.0  # predicate (e), A7
SAMPLE_CAP = 300
EVIDENCE_REFS_MAX = 8
_EXIT_RE = re.compile(r"\AExit code (\d+)\b")
PREDICATE_IDS = ("needs_human_call", "identical_calls_no_change", "no_progress_or_deadline", "repeated_failure_code",
                 "spend_breach")


# -- snapshot / epoch -------------------------------------------------------------


def _git(args: list[str], cwd) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def make_snapshot(store: Store, job_id: str, worktree, transcript_cursor: int = 0) -> dict:
    """What the watcher was shown, recorded by code at run start: {max event seq, HEAD + dirty-hash, transcript cursor}
    plus the control epoch (the job's steering revision)."""
    evs = store.events(job_id)
    dirty = _git(["status", "--porcelain"], worktree) + "\n" + _git(["diff"], worktree)
    return {"max_event_seq": evs[-1].seq if evs else 0, "head": _git(["rev-parse", "HEAD"], worktree).strip(),
            "dirty_hash": hashlib.sha1(dirty.encode()).hexdigest(), "transcript_cursor": transcript_cursor,
            "epoch": store.get_revision(job_id)}


def epoch_ok(store: Store, job_id: str, snapshot: dict | None) -> bool:
    """A proposal carries the epoch it was formed under. Anything that moved the steering revision since makes it stale."""
    return bool(snapshot) and snapshot.get("epoch") == store.get_revision(job_id)


# -- evidence schema --------------------------------------------------------------


def _sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_evidence(store: Store, job_id: str, evidence, snapshot: dict, worktree, tool_call_ids: set[str]) -> list[str]:
    """`{claim, refs:[{type:event,seq} | {type:tool_call,id} | {type:file,path,sha}]}`. Every ref must resolve.
    Resolution proves provenance only; it never makes a claim warranted."""
    errs: list[str] = []
    if not isinstance(evidence, dict):
        return ["evidence:missing"]
    claim = evidence.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        errs.append("evidence:no_claim")
    refs = evidence.get("refs")
    if not isinstance(refs, list) or not refs:
        return errs + ["evidence:no_refs"]
    if len(refs) > EVIDENCE_REFS_MAX:
        return errs + ["evidence:too_many_refs"]
    seqs = {e.seq for e in store.events(job_id)}
    max_seq = int((snapshot or {}).get("max_event_seq", 0))
    root = Path(worktree).resolve()
    for r in refs:
        if not isinstance(r, dict):
            errs.append("evidence:bad_ref")
            continue
        t = r.get("type")
        if t == "event":
            seq = r.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq not in seqs or seq > max_seq:
                errs.append(f"evidence:unresolved_event:{seq}")
        elif t == "tool_call":
            if r.get("id") not in tool_call_ids:
                errs.append(f"evidence:unresolved_tool_call:{r.get('id')}")
        elif t == "file":
            path, sha = r.get("path"), r.get("sha")
            try:
                p = (root / str(path)).resolve()
                inside = p.is_relative_to(root) and p.is_file()
                good = inside and isinstance(sha, str) and len(sha) >= 8 and _sha_of(p).startswith(sha.lower())
            except (OSError, ValueError, TypeError):
                good = False
            if not good:
                errs.append(f"evidence:unresolved_file:{path}")
        else:
            errs.append(f"evidence:unknown_ref_type:{t}")
    return errs


def ref_keys(evidence: dict) -> set[str]:
    keys = set()
    for r in (evidence or {}).get("refs") or []:
        if isinstance(r, dict):
            keys.add(f"{r.get('type')}:{r.get('seq', r.get('id', r.get('path')))}")
    return keys


# -- runner-native observations ---------------------------------------------------


@dataclass
class ToolCall:
    id: str
    tool: str
    input: dict
    ts: float
    done: bool = False
    is_error: bool = False
    code: str | None = None


def _epoch_of(ts: str) -> float:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def parse_tool_calls(lines) -> list[ToolCall]:
    """Tool calls from STRUCTURED transcript blocks only: assistant `tool_use` and user `tool_result`. Text blocks,
    thinking and result bodies are never scanned for anything that looks like a call, so prose cannot forge one."""
    calls: dict[str, ToolCall] = {}
    order: list[str] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg = obj.get("message") if isinstance(obj, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if obj.get("type") == "assistant" and b.get("type") == "tool_use" and b.get("id"):
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                if b["id"] not in calls:
                    order.append(b["id"])
                calls[b["id"]] = ToolCall(b["id"], str(b.get("name", "")), inp, _epoch_of(obj.get("timestamp", "")))
            elif obj.get("type") == "user" and b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                c = calls[b["tool_use_id"]]
                c.done, c.is_error = True, bool(b.get("is_error"))
                body = b.get("content")
                if isinstance(body, list):
                    body = "".join(x.get("text", "") for x in body if isinstance(x, dict))
                m = _EXIT_RE.match(body) if (c.is_error and isinstance(body, str)) else None
                c.code = m.group(1) if m else None
    return [calls[i] for i in order]


def load_tool_calls(store: Store, job_id: str, projects_dir: Path | None = None) -> list[ToolCall]:
    from steward import transcript

    try:
        path = transcript.worker_file(store, job_id, projects_dir)
        if path is None:
            return []
        with open(path, errors="replace") as fh:
            return parse_tool_calls(fh)[-200:]
    except OSError:
        return []


SAMPLES_KEY = "watcher_samples"


def observe_tick(store: Store, job_id: str, sig, now: float) -> None:
    """Runner hook, every tick: compact time series of what the runner's own heartbeat saw (HEAD, diff fingerprint,
    process liveness, open turn). Equal consecutive samples are merged, so a quiet hour is one entry."""
    samples = store.kv_get(job_id, SAMPLES_KEY) or []
    cur = {"head": sig.head_sha, "diff": sig.diff_fingerprint, "alive": sig.pid_alive, "open": bool(sig.turn_open)}
    if samples and all(samples[-1].get(k) == v for k, v in cur.items()):
        samples[-1]["last_t"] = now
    else:
        samples.append({**cur, "t": now, "last_t": now})
    store.kv_set(job_id, SAMPLES_KEY, samples[-SAMPLE_CAP:])


def note_steer_delivered(store: Store, job_id: str, now: float) -> None:
    """Runner hook: a non-contract steer was just delivered -> the settle lease starts (A6)."""
    store.kv_set(job_id, "watcher_settle_until", now + SETTLE_S)


def _tree_unchanged_since(samples: list[dict], ts: float) -> bool:
    if not samples or samples[0]["t"] > ts:
        return False  # the runner was not yet watching at ts: no coverage, no claim
    later = [s for s in samples if s["last_t"] >= ts]
    before = [s for s in samples if s["t"] < ts]
    group = ([before[-1]] if before else []) + later
    return bool(group) and len({(s["head"], s["diff"]) for s in group}) == 1


def _call_key(c: ToolCall) -> str:
    return c.tool + json.dumps(c.input, sort_keys=True, default=str)


def _avoid_globs(store: Store, job_id: str) -> list[str]:
    from steward.policy import parse_avoid_globs
    from steward.steering import Steering

    globs: list[str] = []
    for i in Steering(store).active_constraints(job_id):
        if i.get("kind") == "policy" and i.get("enforcement") == "hard":
            globs.extend(parse_avoid_globs(i["text"]))
    return globs


def _call_paths(c: ToolCall) -> list[str]:
    out = [str(c.input[k]) for k in ("file_path", "path", "notebook_path") if c.input.get(k)]
    cmd = c.input.get("command")
    if isinstance(cmd, str):
        out += [t.strip("\"'") for t in cmd.split()]
    return out


def runner_predicates(store: Store, job_id: str, *, now: float, calls: list[ToolCall], samples: list[dict],
                      worktree, spend_hour_usd: float = 0.0) -> list[dict]:
    """The only things that can warrant an interrupt or pause. Pure functions of runner-observed data: structured
    tool calls, heartbeat samples, policy, spend. No file, transcript-prose or verdict text is read here."""
    from steward import authority, permissions
    from steward.policy import violations

    out: list[dict] = []
    recent = [c for c in calls if c.ts >= now - CALL_WINDOW_S]

    # (a) a call Steward's policy classifies needs-human, or that hits an active avoid rule
    globs = _avoid_globs(store, job_id)
    bad = []
    for c in recent:
        cmd_or_input = c.input.get("command", "") if c.tool == "Bash" else c.input
        if not authority.decide_worker(store, job_id, c.tool, cmd_or_input, worktree).allow or violations(_call_paths(c), globs):
            bad.append(c.id)
    if bad:
        out.append({"id": "needs_human_call", "calls": bad[-3:]})

    # (b) 3 identical calls, worktree unchanged since the first of them
    if len(calls) >= IDENTICAL_CALLS:
        last = calls[-IDENTICAL_CALLS:]
        if len({_call_key(c) for c in last}) == 1 and _tree_unchanged_since(samples, last[0].ts):
            out.append({"id": "identical_calls_no_change", "calls": [c.id for c in last]})

    # (c) a call past its deadline, or 20 min with no progress while the process is live
    alive = bool(samples) and samples[-1].get("alive") is True
    stuck = [c.id for c in calls if not c.done and c.ts and now - c.ts > DEADLINE_S]
    quiet = False
    if samples and alive and samples[-1].get("open"):
        quiet = _tree_unchanged_since(samples, now - DEADLINE_S)
    if alive and (stuck or quiet):
        out.append({"id": "no_progress_or_deadline", "calls": stuck[:3]})

    # (d) repeated failure codes: the last N finished calls all failed with the same exit code
    done = [c for c in calls if c.done]
    if len(done) >= REPEATED_FAILURES:
        last = done[-REPEATED_FAILURES:]
        if all(c.is_error and c.code for c in last) and len({c.code for c in last}) == 1:
            out.append({"id": "repeated_failure_code", "calls": [c.id for c in last], "code": last[-1].code})

    # (e) spend / resource breach
    if spend_hour_usd > SPEND_BREACH_USD_HOUR:
        out.append({"id": "spend_breach", "usd_hour": round(spend_hour_usd, 2)})
    return out


# -- gate ---------------------------------------------------------------------------


def _norm(v, field: str) -> dict | None:
    if not v:
        return None
    if isinstance(v, str):
        return {field: v}
    return dict(v) if isinstance(v, dict) else {}


def _log(store: Store, job_id: str, key: str, now: float, keep_s: float, add: dict | None = None) -> list[dict]:
    items = [i for i in (store.kv_get(job_id, key) or []) if now - i["ts"] < keep_s]
    if add is not None:
        items.append({**add, "ts": now})
        store.kv_set(job_id, key, items)
    return items


@dataclass
class GateContext:
    now: float
    snapshot: dict | None
    worktree: object
    calls: list[ToolCall] = field(default_factory=list)
    spend_hour_usd: float = 0.0
    notify: Callable | None = None
    observe_only: bool = False
    unacked_pending: bool = False


def _recovery_text(text: str, preds: list[dict]) -> str:
    text = " ".join(str(text or "").split())[:400]
    why = ", ".join(p["id"] for p in preds)
    return (text or "Re-check your last step and try a different approach, then continue") + f" [watcher interrupt: {why}]"


def evaluate_actions(store: Store, job_id: str, w: dict, verdict: dict, ctx: GateContext) -> dict:
    """Decide interrupt / pause / resume for one verdict. Returns {"suppressed": [...], "actions": {...}}.
    The model's words are inputs to be checked, never authority: whatever the verdict says, every condition below is
    recomputed by code."""
    from steward.models import JobState

    name, now = w["name"], ctx.now
    result: dict = {"suppressed": [], "actions": {}}
    sup = result["suppressed"]
    want_int = _norm(verdict.get("interrupt"), "text")
    want_pause = _norm(verdict.get("pause"), "reason")
    want_resume = bool(verdict.get("resume"))
    status = verdict.get("status")

    if not (want_int or want_pause or want_resume):
        store.kv_set(job_id, f"watcher_streak:{name}", None)
        return result

    granted = set(w.get("may") or [])
    active = active_powers(store, job_id, w)
    reasons_by_kind: dict[str, list[str]] = {}

    def block(kind: str, reason: str) -> None:
        reasons_by_kind.setdefault(kind, []).append(reason)

    wanted = [k for k, v in (("interrupt", want_int), ("pause", want_pause), ("resume", want_resume or None)) if v]
    for k in wanted:
        if k not in granted:
            block(k, "not_granted")
        elif k not in active:
            block(k, f"tier_T{tier_of(store, job_id, w)}")
    live = [k for k in wanted if k not in reasons_by_kind]
    job = store.get_job(job_id)

    if live:
        if quarantine(store, job_id):
            for k in live:
                block(k, "quarantined")
        if ctx.observe_only:
            for k in live:
                block(k, "observe_only")
        if now < float(store.kv_get(job_id, "watcher_freeze_until") or 0):
            for k in live:
                block(k, "freeze")
        live = [k for k in wanted if k not in reasons_by_kind]

    # integrity: epoch (stale proposals are rejected at delivery) and evidence
    evidence_ok = False
    if live:
        if not epoch_ok(store, job_id, ctx.snapshot):
            for k in live:
                block(k, "stale_epoch")
            note_rejection(store, job_id, w, "stale_epoch", now, ctx.notify)
        else:
            errs = validate_evidence(store, job_id, verdict.get("evidence"), ctx.snapshot, ctx.worktree,
                                     {c.id for c in ctx.calls})
            if errs:
                for k in live:
                    block(k, errs[0])
                note_rejection(store, job_id, w, "bad_evidence", now, ctx.notify)
            else:
                evidence_ok = True
        live = [k for k in wanted if k not in reasons_by_kind]

    # interrupt-grade requirements
    preds: list[dict] = []
    corroborated = False
    grade_ok = False
    if live and evidence_ok and ("interrupt" in live or "pause" in live):
        halting = [k for k in live if k in ("interrupt", "pause")]
        preds = runner_predicates(store, job_id, now=now, calls=ctx.calls,
                                  samples=store.kv_get(job_id, SAMPLES_KEY) or [], worktree=ctx.worktree,
                                  spend_hour_usd=ctx.spend_hour_usd)
        conf = verdict.get("confidence")
        conf_ok = isinstance(conf, (int, float)) and not isinstance(conf, bool) and conf >= CONFIDENCE_MIN
        if status != "alarm":
            for k in halting:
                block(k, "status_not_alarm")
        if not conf_ok:
            for k in halting:
                block(k, "confidence")
        if not preds:
            for k in halting:
                block(k, "no_runner_native_predicate")
        pending = bool(ctx.unacked_pending)
        if pending:
            block("interrupt", "pending_steer")
        if now < float(store.kv_get(job_id, "watcher_settle_until") or 0):
            block("interrupt", "settle_lease")
        streak = None
        if status == "alarm" and preds:
            prev = store.kv_get(job_id, f"watcher_streak:{name}") or {"count": 0, "ids": []}
            ids = sorted(p["id"] for p in preds)
            count = prev["count"] + 1 if set(prev["ids"]) & set(ids) else 1
            streak = {"count": count, "ids": ids}
            store.kv_set(job_id, f"watcher_streak:{name}", streak)
        else:
            store.kv_set(job_id, f"watcher_streak:{name}", None)
        corroborated = bool(preds) and _corroborated(store, job_id, w, verdict, now)
        if streak is not None and conf_ok:
            _log(store, job_id, "watcher_proposals", now, 3600.0, {
                "name": name, "kind": "pause" if want_pause and not want_int else "interrupt", "model": w.get("model"),
                "ask_hash": hashlib.sha256(str(w.get("ask")).encode()).hexdigest()[:12], "preds": [p["id"] for p in preds],
                "refs": sorted(ref_keys(verdict.get("evidence")))})
        if not ((streak and streak["count"] >= 2) or corroborated):
            for k in halting:
                block(k, "needs_persistence_or_corroboration")
        live = [k for k in wanted if k not in reasons_by_kind]
        grade_ok = bool([k for k in live if k in ("interrupt", "pause")])

    if "interrupt" in live:
        ints = _log(store, job_id, "watcher_interrupts", now, JOB_INTERRUPTS[1])
        if any(i["name"] == name and now - i["ts"] < WATCHER_INTERRUPT_S for i in ints):
            block("interrupt", "cap_watcher_hour")
        if len(ints) >= JOB_INTERRUPTS[0]:
            block("interrupt", "cap_job_6h")
    if "pause" in live:
        if job is None or job.state != JobState.RUNNING:
            block("pause", "job_not_running")
        if len(_log(store, job_id, "watcher_pauses", now, PAUSE_JOB[1])) >= PAUSE_JOB[0]:
            block("pause", "cap_job_6h")
        failed_prior = any(now - i["ts"] >= FREEZE_S for i in _log(store, job_id, "watcher_interrupts", now, PAUSE_JOB[1]))
        if not (failed_prior or corroborated):
            block("pause", "needs_failed_interrupt_or_corroboration")
    if "resume" in live:
        lease = store.kv_get(job_id, LEASE_KEY)
        if not lease or now >= lease["until"]:
            block("resume", "no_active_lease")
        else:
            _log(store, job_id, "watcher_proposals", now, CONFLICT_WINDOW_S, {"name": name, "kind": "resume", "preds": [],
                                                                            "model": w.get("model"), "ask_hash": "", "refs": []})

    checked = [k for k in live if k in ("interrupt", "pause", "resume")]
    if checked:
        contrary = _conflict_check(store, job_id, w, checked, now, ctx)
        if contrary:
            for k in checked:
                block(k, contrary)

    for kind, rs in reasons_by_kind.items():
        for r in rs:
            sup.append(f"{kind}:{r}")
    live = [k for k in wanted if k not in reasons_by_kind]

    if "interrupt" in live:
        result["actions"]["interrupt"] = _do_interrupt(store, job_id, w, want_int, preds, verdict, ctx)
        live = [k for k in live if k != "interrupt"]
    if "pause" in live and "interrupt" not in result["actions"]:
        result["actions"]["pause"] = _do_pause(store, job_id, w, want_pause, preds, ctx)
    if "resume" in live:
        result["actions"]["resume"] = _do_resume(store, job_id, w, ctx)
    return result


def _corroborated(store: Store, job_id: str, w: dict, verdict: dict, now: float) -> bool:
    mine = ref_keys(verdict.get("evidence"))
    my_ask = hashlib.sha256(str(w.get("ask")).encode()).hexdigest()[:12]
    for p in _log(store, job_id, "watcher_proposals", now, CORROBORATION_S):
        if p["name"] == w["name"] or not p.get("preds"):
            continue
        if (p["model"] != w.get("model") or p["ask_hash"] != my_ask) and not (set(p["refs"]) & mine):
            return True
    return False


# Record item 4: human > safety policy > advisor(active) > doctor > watchers (by tier, then evidence).
PRIORITY = ("human", "policy", "advisor", "doctor", "watcher")
CONFLICT_WINDOW_S = 3600.0
_HALT = frozenset({"interrupt", "pause"})


def source_of(origin: str) -> str:
    if origin.startswith("watcher:") or origin == "flow":  # v2.3: Flow ranks with the lowest, never as a human
        return "watcher"
    return {"trajectory": "advisor", "diagnosis": "doctor", "steward": "policy", "contract": "policy"}.get(origin, "human")


def outranks(a: str, b: str) -> bool:
    return PRIORITY.index(source_of(a)) < PRIORITY.index(source_of(b))


def _recent_human_resume(store: Store, job_id: str, now: float) -> bool:
    return any(e.rule == "operator_resume" and 0 <= now - _epoch_of(e.ts) < CONFLICT_WINDOW_S for e in store.events(job_id))


def _conflict_check(store: Store, job_id: str, w: dict, kinds: list[str], now: float, ctx) -> str | None:
    """None, or the reason this watcher's halt/go proposal must not actuate.

    * A human's resume within the hour outranks a watcher halting the job again: the watcher yields (no quarantine).
    * Contrary valid watcher proposals within an hour (one halts, another resumes, or a different watcher resumes a
      watcher's pause lease) quarantine ALL watcher actuation on the job and escalate to the owner. The worker keeps
      running under the last higher-priority direction; only the owner clears it.
    """
    name = w["name"]
    if set(kinds) & _HALT and _recent_human_resume(store, job_id, now):
        return "yield:human"
    others = [p for p in _log(store, job_id, "watcher_proposals", now, CONFLICT_WINDOW_S) if p["name"] != name]
    contrary = None
    for p in others:
        if ("resume" in kinds and p["kind"] in _HALT) or (set(kinds) & _HALT and p["kind"] == "resume"):
            contrary = p
    lease = store.kv_get(job_id, LEASE_KEY)
    if contrary is None and "resume" in kinds and lease and lease["name"] != name and now < lease["until"]:
        contrary = {"name": lease["name"], "kind": "pause"}
    if contrary is None:
        return None
    _open_quarantine(store, job_id, w, kinds, contrary, now, ctx)
    return "quarantined"


def conflict_pause_eligible(store: Store, job_id: str, now: float) -> bool:
    """A pause lease during a conflict is allowed only after 2 interrupts, or a pause/resume reversal (record item 4)."""
    two = len(_log(store, job_id, "watcher_interrupts", now, JOB_INTERRUPTS[1])) >= 2
    rev = any(e.what == "watcher_pause_reversal" and 0 <= now - _epoch_of(e.ts) < JOB_INTERRUPTS[1] for e in store.events(job_id))
    return two or rev


def _open_quarantine(store: Store, job_id: str, w: dict, kinds: list[str], other: dict, now: float, ctx) -> None:
    from steward import approvals as approvals_module
    from steward.models import JobState

    if quarantine(store, job_id):
        return
    who = sorted({w["name"], other["name"]})
    q = {"since": now, "reason": f"{w['name']} wants {'/'.join(kinds)}, {other['name']} wants {other['kind']}", "watchers": who}
    store.kv_set(job_id, QUARANTINE_KEY, q)
    store.append_event(job_id, what="watcher_quarantined", evidence=q, rule="watcher_authority_conflict",
                       judgment=Judgment.code)
    rec = approvals_module.open_approval(store, job_id, f"watcher conflict: {q['reason']}. Adjudicate, then `steward watcher unquarantine`.")
    if ctx.notify is not None:
        ctx.notify(job_id, "NEEDS_YOU", "Steward: watcher conflict", f"approval {rec['id'][:8]}: {q['reason']}"[:140])
    job = store.get_job(job_id)
    if (conflict_pause_eligible(store, job_id, now) and job is not None and job.state == JobState.RUNNING
            and not _log(store, job_id, "watcher_pauses", now, PAUSE_JOB[1])):
        _do_pause(store, job_id, {"name": "conflict"}, {"reason": "watcher conflict"}, [], ctx)
        store.append_event(job_id, what="watcher_conflict_pause_lease", evidence=q, rule="watcher_authority_conflict",
                           judgment=Judgment.code)


LEASE_KEY = "watcher_pause_lease"


def _do_interrupt(store: Store, job_id: str, w: dict, want: dict, preds: list[dict], verdict: dict, ctx: GateContext) -> dict:
    from steward.steering import Steering

    name = w["name"]
    text = _recovery_text((want or {}).get("text", ""), preds)
    steering = Steering(store)
    receipt = steering.record(job_id, "nudge", f"[watcher {name}] {text}", origin="watcher:" + name, human_instruction="",
                              interpreted_instruction=text, urgent=True, gated_by="watcher_authority",
                              expected_revision=(ctx.snapshot or {}).get("epoch"))
    if not receipt.accepted:
        note_rejection(store, job_id, w, "stale_epoch", ctx.now, ctx.notify)
        return {"accepted": False, "detail": receipt.detail}
    rev = steering.all_instructions(job_id)[-1]["rev"]
    _log(store, job_id, "watcher_interrupts", ctx.now, JOB_INTERRUPTS[1], {"name": name, "rev": rev})
    store.kv_set(job_id, "watcher_freeze_until", ctx.now + FREEZE_S)
    store.append_event(job_id, what="watcher_interrupted", evidence={
        "name": name, "rev": rev, "predicates": preds, "evidence": verdict.get("evidence"), "snapshot": ctx.snapshot,
        "confidence": verdict.get("confidence")}, rule="watcher_authority_gate",
        judgment=Judgment.model_informed_code_authorized)
    return {"accepted": True, "rev": rev}


def _job_updated(store: Store, job_id: str) -> str:
    return store.get_job(job_id).updated_at


def _do_pause(store: Store, job_id: str, w: dict, want: dict, preds: list[dict], ctx: GateContext) -> dict:
    from steward.models import JobState

    name = w["name"]
    store.set_state(job_id, JobState.PAUSED, what="watcher pause lease", rule="watcher_pause_lease",
                    evidence={"name": name, "until": ctx.now + PAUSE_LEASE_S, "predicates": preds,
                              "reason": " ".join(str((want or {}).get("reason", "")).split())[:200]},
                    judgment=Judgment.model_informed_code_authorized)
    store.kv_set(job_id, LEASE_KEY, {"name": name, "since": ctx.now, "until": ctx.now + PAUSE_LEASE_S,
                                     "job_updated_at": _job_updated(store, job_id)})
    _log(store, job_id, "watcher_pauses", ctx.now, PAUSE_JOB[1], {"name": name})
    store.kv_set(job_id, "watcher_freeze_until", ctx.now + FREEZE_S)
    if ctx.notify is not None:
        ctx.notify(job_id, "NEEDS_YOU", f"Steward: {name} paused job"[:40],
                   f"watcher {name} paused the job for {int(PAUSE_LEASE_S // 60)} min; it auto-resumes"[:140])
    return {"accepted": True, "until": ctx.now + PAUSE_LEASE_S}


def _end_lease(store: Store, job_id: str, lease: dict, *, why: str, by: str) -> bool:
    """Resume the job only if it is still in the state the lease put it in (a human's pause/resume wins)."""
    from steward.models import JobState

    job = store.get_job(job_id)
    store.kv_set(job_id, LEASE_KEY, None)
    if job is None or job.state != JobState.PAUSED or job.updated_at != lease.get("job_updated_at"):
        return False
    store.set_state(job_id, JobState.RUNNING, what=f"watcher pause lease {why}", rule="watcher_pause_lease",
                    evidence={"name": lease["name"], "by": by}, judgment=Judgment.code)
    return True


def expire_pause_lease(store: Store, job_id: str, now: float) -> bool:
    """Runner hook, top of every tick: a lease auto-resumes when it lapses."""
    lease = store.kv_get(job_id, LEASE_KEY)
    if not lease or now < lease["until"]:
        return False
    return _end_lease(store, job_id, lease, why="expired", by="code")


def _do_resume(store: Store, job_id: str, w: dict, ctx: GateContext) -> dict:
    lease = store.kv_get(job_id, LEASE_KEY)
    resumed = _end_lease(store, job_id, lease, why="ended early", by=w["name"])
    store.append_event(job_id, what="watcher_pause_reversal", evidence={"paused_by": lease["name"], "resumed_by": w["name"]},
                       rule="watcher_authority_gate", judgment=Judgment.model_informed_code_authorized)
    return {"accepted": resumed}
