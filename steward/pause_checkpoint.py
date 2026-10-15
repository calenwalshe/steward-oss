"""Pause-review checkpoint (spec docs/superpowers/specs/2026-10-13-steward-pause-review-design.md §4.2).

ONE record per job: the single shared object for supervision. Other actors append to `pending_facts`; only the tech
lead's pause ruling (`steward.pause_review.apply_ruling`) writes `verdict` and `open_instruction`.

Storage (plan decision, spec §10): the existing kv table, key `pause_checkpoint`, one JSON document per job. No schema
change; every write is a read-modify-write inside `Store.immediate()`, so the runner, the tech lead and fact writers
in other processes never lose each other's updates. A pause=off job never gets a record: callers check `pause_mode()`
first and nothing here writes on its own.
"""
from __future__ import annotations

import copy
from typing import Callable

KEY = "pause_checkpoint"
PAUSE_VALUES = ("unit", "natural", "off")
MAX_PENDING_FACTS = 30
MAX_HISTORY = 40
FACT_TEXT_CAP = 400

DEFAULTS: dict = {
    # spec §4.2
    "n": 0, "unit_id": None, "last_reviewed_sha": None, "verdict": None, "verdict_reason": None,
    "open_instruction": None, "pending_facts": [], "provisional_units": [], "review_in_flight": False,
    # the pause being reviewed
    "pause_seq": 0, "last_pause_sha": None, "last_pause_at": None, "last_pause_fact": None, "done_claim": False,
    "awaiting_review": False, "review_requested_at": None, "review_started_at": None, "queued_seq": None,
    "provisional_seq": 0, "force_provisional": False, "done_sha": None, "waiting_on": None,
    # u5: the owner's loop-guard card (approval id) and the instruction it releases on approve
    "owner_approval_id": None, "owner_instruction": None, "owner_card_kind": None,
    # units and the verdict history
    "units_seen": [], "met_units": [], "dropped_units": [], "history": [],
    # the runner's pause clock
    "active_s": 0.0, "last_tick_at": None, "seen_head": None, "unit_commit_sha": None, "unit_commit_at": None,
    "soft_stop_sha": None, "backstop_stage": 0,
    # fact ids, failures, one-shot notices
    "fact_seq": 0, "review_fact_seq": 0, "tl_failures": 0, "notices": [],
}


def pause_mode(store, job_id: str) -> str:
    """The job's pause value from its stored contract. A contract stored before the field existed, or an unknown
    value, is "off": a running job never changes behaviour because new code was deployed."""
    data = store.kv_get(job_id, "contract")
    value = data.get("pause") if isinstance(data, dict) else None
    return value if value in PAUSE_VALUES else "off"


def merged(raw) -> dict:
    out = copy.deepcopy(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: copy.deepcopy(v) for k, v in raw.items() if k in DEFAULTS})
    return out


def load(store, job_id: str) -> dict:
    return merged(store.kv_get(job_id, KEY))


def exists(store, job_id: str) -> bool:
    return store.kv_get(job_id, KEY) is not None


def update(store, job_id: str, fn: Callable[[dict], None]) -> dict:
    """Atomic read-modify-write: `fn` mutates the merged record in place; the result is stored and returned."""
    with store.immediate() as tx:
        cp = merged(tx.kv_get(job_id, KEY))
        fn(cp)
        tx.kv_set(job_id, KEY, cp)
    return cp


def append_fact(store, job_id: str, *, source: str, kind: str, text: str, at: float, urgent: bool = False) -> dict:
    """Spec §4.5: another actor reports a fact for the tech lead's next review (numbered, capped, whitespace-folded)."""
    out: dict = {}

    def fn(cp: dict) -> None:
        cp["fact_seq"] = int(cp["fact_seq"]) + 1
        out.update(id=cp["fact_seq"], source=str(source)[:60], kind=str(kind)[:60], at=float(at),
                   text=" ".join(str(text).split())[:FACT_TEXT_CAP], urgent=bool(urgent))
        cp["pending_facts"] = (cp["pending_facts"] + [dict(out)])[-MAX_PENDING_FACTS:]

    update(store, job_id, fn)
    return out
