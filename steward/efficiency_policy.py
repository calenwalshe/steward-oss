"""The job efficiency policy (phase 16, G6/G9): the only thing the efficiency watcher's `reconfigure` may change.

A per-job kv `efficiency_policy` with four keys and nothing else:

* `respawn`: off | per_unit | max_calls=N
* `effort`: {stage type: level}; the watcher may set only high|medium at or above the envelope floor
* `plan_ahead`: on | off (the watcher may turn it on only when the envelope allows it)
* `output_hygiene`: on | off

It applies to future spawns only (16-06 renders it into briefs and the relaunch argv). The schema is closed, so a
model change, a quality gate, killing an agent, budget, stop/pause or the tier is refused structurally
(`not_policy_key:<k>`). A refused watcher request is recorded as `efficiency_refused`. Every change is logged in kv
`efficiency_policy_log` and as an `efficiency_policy_changed` event with before and after, and a human reverts it.
"""
from __future__ import annotations

import copy
import re
import time

from steward import efficiency_detect as detect
from steward import watcher_authority as wa
from steward.models import Judgment
from steward.store import Store

KV_KEY = "efficiency_policy"
LOG_KEY = "efficiency_policy_log"
RULE = "efficiency_policy"
KEYS = ("respawn", "effort", "plan_ahead", "output_hygiene")
DEFAULT_POLICY: dict = {"respawn": "off", "effort": {}, "plan_ahead": "off", "output_hygiene": "off"}
WATCHER_EFFORT = ("high", "medium")  # G9: the watcher may only set high|medium
_MAX_CALLS = re.compile(r"^max_calls=(\d+)$")


class PolicyError(ValueError):
    pass


def _human(actor: str) -> bool:
    return actor in wa.human_actors()


def load(store: Store, job_id: str) -> dict:
    """kv efficiency_policy merged over the defaults (unknown or wrongly shaped stored keys are ignored)."""
    out = copy.deepcopy(DEFAULT_POLICY)
    stored = store.kv_get(job_id, KV_KEY)
    if isinstance(stored, dict):
        for k in KEYS:
            if k not in stored:
                continue
            if k == "effort":
                if isinstance(stored[k], dict):
                    out[k] = {str(s): str(v) for s, v in stored[k].items()}
            elif isinstance(stored[k], str):
                out[k] = stored[k]
    return out


def validate(patch, envelope: dict | None, *, actor: str) -> list[str]:
    """Violations of the closed schema (any actor) and of the owner's envelope (non-human actors only)."""
    env = {**detect.DEFAULTS["envelope"], **(envelope or {})}
    if not isinstance(patch, dict) or not patch:
        return ["empty_or_not_a_mapping"]
    human = _human(actor)
    out = []
    for k, v in patch.items():
        if k not in KEYS:
            out.append(f"not_policy_key:{k}")
        elif k == "respawn":
            m = _MAX_CALLS.match(v) if isinstance(v, str) else None
            if not (v in ("off", "per_unit") or m):
                out.append(f"respawn_invalid:{v}")
            elif m and int(m.group(1)) < (1 if human else int(env["min_max_calls"])):
                out.append(f"respawn_below_min_max_calls:{v}")
        elif k == "effort":
            if not isinstance(v, dict) or not v:
                out.append("effort_not_a_mapping")
                continue
            floor = env["effort_floor"] if env["effort_floor"] in detect.EFFORT_LEVELS else "medium"
            for stage, level in v.items():
                if not isinstance(stage, str) or not stage or level not in detect.EFFORT_LEVELS:
                    out.append(f"effort_invalid:{stage}={level}")
                elif not human and level not in WATCHER_EFFORT:
                    out.append(f"effort_not_settable:{stage}={level}")
                elif not human and detect.EFFORT_LEVELS.index(level) < detect.EFFORT_LEVELS.index(floor):
                    out.append(f"effort_below_floor:{stage}={level}")
        elif k == "plan_ahead":
            if v not in ("on", "off"):
                out.append(f"plan_ahead_invalid:{v}")
            elif v == "on" and not human and not env["plan_ahead"]:
                out.append("plan_ahead_not_allowed")
        elif k == "output_hygiene" and v not in ("on", "off"):
            out.append(f"output_hygiene_invalid:{v}")
    return out


def _merge(policy: dict, patch: dict) -> dict:
    out = copy.deepcopy(policy)
    for k, v in patch.items():
        out[k] = {**out["effort"], **v} if k == "effort" else v
    return out


def change(store: Store, job_id: str, patch: dict, *, actor: str, finding: str | None = None,
           now: float | None = None, envelope: dict | None = None) -> dict:
    """Apply one policy change inside the envelope; returns its log entry. A violation raises PolicyError (and, for a
    non-human actor, is recorded as `efficiency_refused`)."""
    now = time.time() if now is None else now
    if envelope is None:
        envelope = detect.load_config(store, job_id)["envelope"]
    reasons = validate(patch, envelope, actor=actor)
    if reasons:
        if not _human(actor):
            store.append_event(job_id, what="efficiency_refused",
                               evidence={"actor": actor, "request": patch, "reasons": reasons, "finding": finding},
                               rule=RULE, judgment=Judgment.code)
        raise PolicyError("; ".join(reasons))
    before_all = load(store, job_id)
    after_all = _merge(before_all, patch)
    before = {k: before_all[k] for k in patch}
    after = {k: after_all[k] for k in patch}
    log = list(store.kv_get(job_id, LOG_KEY) or [])
    entry = {"id": f"pc-{len(log) + 1}", "ts": now, "actor": actor, "before": before, "after": after,
             "finding": finding, "reverted": False}
    store.kv_set(job_id, KV_KEY, after_all)
    store.kv_set(job_id, LOG_KEY, [*log, entry])
    store.append_event(job_id, what="efficiency_policy_changed",
                       evidence={"change_id": entry["id"], "before": before, "after": after, "actor": actor,
                                 "finding": finding},
                       rule=RULE, judgment=Judgment.human if _human(actor) else Judgment.code)
    return entry


def revert(store: Store, job_id: str, change_id: str | None = None, *, actor: str,
           now: float | None = None) -> dict:
    """Human-only: restore a change's before values for its keys (default: the last unreverted change)."""
    if not _human(actor):
        raise PolicyError(f"revert is human-only (actor {actor!r})")
    now = time.time() if now is None else now
    log = list(store.kv_get(job_id, LOG_KEY) or [])
    if change_id is None:
        idx = next((i for i in range(len(log) - 1, -1, -1) if not log[i].get("reverted")), None)
        if idx is None:
            raise PolicyError("no policy change to revert")
    else:
        idx = next((i for i, e in enumerate(log) if e.get("id") == change_id), None)
        if idx is None:
            raise PolicyError(f"unknown change {change_id!r}")
        if log[idx].get("reverted"):
            raise PolicyError(f"{change_id} is already reverted")
    entry = log[idx]
    cur = load(store, job_id)
    restored = {**cur, **copy.deepcopy(entry["before"])}
    before = {k: cur[k] for k in entry["before"]}
    after = {k: restored[k] for k in entry["before"]}
    store.kv_set(job_id, KV_KEY, restored)
    log[idx] = {**entry, "reverted": True, "reverted_at": now, "reverted_by": actor}
    store.kv_set(job_id, LOG_KEY, log)
    store.append_event(job_id, what="efficiency_policy_changed",
                       evidence={"change_id": entry["id"], "before": before, "after": after, "actor": actor,
                                 "reverts": entry["id"]},
                       rule=RULE, judgment=Judgment.human)
    return log[idx]


def policy_line(policy: dict) -> str:
    """Only the non-defaults: `respawn per_unit; effort gsd-planner=high; plan ahead on; quiet output on`."""
    parts = []
    if policy.get("respawn", "off") != "off":
        parts.append(f"respawn {policy['respawn']}")
    if policy.get("effort"):
        parts.append("effort " + ", ".join(f"{k}={v}" for k, v in sorted(policy["effort"].items())))
    if policy.get("plan_ahead") == "on":
        parts.append("plan ahead on")
    if policy.get("output_hygiene") == "on":
        parts.append("quiet output on")
    return "; ".join(parts)


def brief_lines(store: Store, job_id: str) -> list[str]:
    """One plain generic sentence per non-default policy key, for the worker's brief (G8c). [] for the default."""
    policy = load(store, job_id)
    out: list[str] = []
    respawn = policy.get("respawn", "off")
    if respawn == "per_unit":
        out.append("respawn: end each helper agent when its unit is done and spawn a fresh one for the next unit; "
                   "hand over through a short file, not the conversation history")
    elif respawn.startswith("max_calls="):
        out.append(f"respawn: end each helper agent after about {respawn.split('=', 1)[1]} model calls and spawn a "
                   "fresh one; hand over through a short file, not the conversation history")
    for stage, level in sorted(policy.get("effort", {}).items()):
        if stage == "worker":
            out.append(f"effort worker: this session runs at effort {level} (applied at relaunch)")
        else:
            out.append(f"effort {stage}: {level} (advisory: request it when you spawn this helper type)")
    if policy.get("plan_ahead") == "on":
        out.append("plan ahead: start planning unit N+1 while unit N builds")
    if policy.get("output_hygiene") == "on":
        out.append("output hygiene: keep command output quiet (quiet flags, `| tail -n 80`, or write to a file and "
                   "grep it) so large results do not fill the context")
    return out


def worker_effort(store: Store, job_id: str) -> str | None:
    """The effort level for the worker session itself, only from the closed EFFORT_LEVELS list (T-16-19)."""
    level = load(store, job_id).get("effort", {}).get("worker")
    return level if level in detect.EFFORT_LEVELS else None
