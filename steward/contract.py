"""Working contract on one screen (SP-7, SP-8).

`propose_contract` is a deterministic parse of the goal text plus discovery
and baseline results — it never asks a model, so its transition is recorded
with `judgment=code`, not `model_informed_code_authorized`. Every inference
the code makes (which command is the test runner, which discovered commands
were ignored, how many pre-existing failures the baseline had) is recorded as
an explicit assumption so nothing is silently redefined later (SP-7).

`store_contract` also seeds every boundary as a durable instruction revision
via `steward.steering` (SP-9): without this, a goal-derived boundary like
"don't touch the db" lived only inside the `Contract` dataclass and never
showed up as an active constraint in the brief or `explain-state`, and never
survived past the contract that produced it. Seeding uses
`steward.policy.classify_instruction`/`parse_avoid_globs` rather than
inventing new classification rules: a boundary that actually contains a
path/glob-shaped token (per `parse_avoid_globs`) becomes an `avoid`
constraint (`hard` only when a glob was extracted — SP-10); the fixed
`_OUTBOUND_BOUNDARY` (SP-36's outbound/irreversible list) is not a path at
all (a slash-separated list of alternatives), so it is seeded as `authority`
— enforced by the permission policy (`steward.permissions`) at execution
time, not by diffing the worktree; everything else is `focus` (which
`classify_instruction` escalates to `review` for api/interface/compat text,
else `preference`). Seeding is idempotent per contract revision — a
deterministic request id per (revision, boundary index) means re-seeding the
same revision never duplicates instructions.

SP-36 (owner decision, 2026-09-28) "defaults, not blockers" supersedes the v1 blanket
"no deploy/publish/network" boundary: `AUTHORITY` now explicitly includes
read-only network and running scripts/long-running commands in the
worktree, and the only boundary requiring the owner is the outbound/irreversible
list (deploy/publish/push/release/messages/payments/credentials/writes
outside the worktree).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace

from steward import owner
from steward.baseline import Baseline, pre_existing_failure_count
from steward.discovery import Discovery
from steward.models import JobState, Judgment
from steward.policy import parse_avoid_globs
from steward.steering import Steering
from steward.store import Store

_BOUNDARY_SENTENCE = re.compile(
    r"\b(don't|do not|never|preserve|keep|avoid)\b", re.IGNORECASE
)

# SP-36: authority is now default-allow for read-only network and running
# scripts inside the worktree; only the outbound/irreversible list still
# waits for the owner.
_READ_ONLY_NETWORK_AUTHORITY = "read-only network (fetch pages/APIs/packages)"
_RUN_SCRIPTS_AUTHORITY = "run scripts and long-running commands in the worktree"
_OUTBOUND_BOUNDARY = (
    "no deploy/publish/push/release/messages/payments/credentials/writes "
    "outside worktree without {owner}"
)


def _is_outbound_boundary(boundary: str) -> bool:
    """True for the fixed outbound boundary as written under the configured name or a legacy spelling, so a
    contract stored before the owner was renamed still seeds as an authority constraint."""
    return any(boundary == _OUTBOUND_BOUNDARY.replace("{owner}", n) for n in owner.parser_names())


# Spec 2026-10-13 §4.1/§8: unit | natural | off. Off everywhere until the trial passes; Part 5 flips this to "unit".
PAUSE_VALUES = ("unit", "natural", "off")
DEFAULT_PAUSE = "off"
# Part 5 (2026-10-14, owner decision): a job created by `steward new` pauses at each unit unless it asks otherwise.
NEW_JOB_PAUSE = "unit"
_UNIT_ID = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


@dataclass(frozen=True)
class Unit:
    """One unit of a job (spec 2026-10-13 §4.1). Intake fills these later; empty means the worker names units."""
    id: str
    title: str = ""
    finish_line: str = ""

    def __post_init__(self):
        if not isinstance(self.id, str) or not _UNIT_ID.match(self.id):
            raise ValueError(f"unit id must match {_UNIT_ID.pattern}, got {self.id!r}")


@dataclass(frozen=True)
class Contract:
    goal: str
    boundaries: list[str] = field(default_factory=list)
    done_when: list[str] = field(default_factory=list)
    next_step: str = ""
    authority: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    revision: int = 1
    research: bool = False
    keys: list[str] = field(default_factory=list)
    follows: str = ""
    units_total: int = 0  # optional hint: how many units (phases/plans/steps) the job has; enables projections
    pause: str = DEFAULT_PAUSE  # spec 2026-10-13 §4.1: unit | natural | off
    units: list[Unit] = field(default_factory=list)

    def __post_init__(self):
        if self.pause not in PAUSE_VALUES:
            raise ValueError(f"pause must be one of {PAUSE_VALUES}, got {self.pause!r}")
        units = [u if isinstance(u, Unit) else Unit(**{k: u[k] for k in ("id", "title", "finish_line") if k in u})
                 for u in self.units]
        object.__setattr__(self, "units", units)
        if units:
            object.__setattr__(self, "units_total", len(units))  # units_total stays a hint; real units win


def _split_sentences(goal: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", goal.strip())
    return [p.strip() for p in parts if p.strip()]


def propose_contract(
    goal: str,
    discovery: Discovery,
    baseline: Baseline | None,
    *,
    revision: int = 1,
    uncommitted_count: int = 0,
) -> Contract:
    boundaries = [s for s in _split_sentences(goal) if _BOUNDARY_SENTENCE.search(s)]
    boundaries.append(owner.fill(_OUTBOUND_BOUNDARY))

    checks = [c for c in discovery.commands if c.kind == "check"]
    ignored = [c for c in discovery.commands if c.kind == "unclassified"]

    done_when = ["authorized checks pass with no new failures vs baseline"]

    authority = ["edit files in job worktree"]
    authority.extend(f"run: {c.line}" for c in checks)
    authority.append(_READ_ONLY_NETWORK_AUTHORITY)
    authority.append(_RUN_SCRIPTS_AUTHORITY)

    assumptions = [
        f"'{c.line}' ({c.source}) is treated as an authorized check runner"
        for c in checks
    ]
    if discovery.python_env and any(c.line != "pytest" and "pytest" in c.line for c in checks):
        assumptions.append(f"checks run with the project's own Python environment ({discovery.python_env}); "
                           "baseline and verify use the same command")
    assumptions.extend(
        f"ignored unclassified command: '{c.line}' ({c.reason})" for c in ignored
    )
    if baseline is not None:
        pre_existing = pre_existing_failure_count(baseline)
        assumptions.append(f"baseline had {pre_existing} pre-existing failure(s)")
    if uncommitted_count > 0:
        # SP-36/NB-03: a dirty source repo never blocks the job — the
        # worktree is still created from HEAD, so anything uncommitted in
        # the source repo is simply not present in it.
        assumptions.append(
            f"{uncommitted_count} uncommitted file(s) in the source repo are not included"
        )

    next_step = "dispatch worker to execute the goal within contract boundaries"

    return Contract(
        goal=goal,
        boundaries=boundaries,
        done_when=done_when,
        next_step=next_step,
        authority=authority,
        assumptions=assumptions,
        revision=revision,
    )


def render(contract: Contract, deadline_line: str | None = None,
           grant_lines: list[str] | None = None) -> str:
    """Render the contract on one screen (<=20 lines). `deadline_line` (`DEADLINE: <owner-local> (rev N)`,
    from `steward.deadline.contract_line`) is shown after GOAL; it is never an ETA input."""

    def _section(label: str, items: list[str]) -> str:
        if not items:
            return f"{label}: (none)"
        return f"{label}: " + "; ".join(items)

    lines = [
        f"GOAL: {contract.goal}",
        *([deadline_line] if deadline_line else []),
        _section("BOUNDARIES", contract.boundaries),
        _section("DONE WHEN", contract.done_when),
        f"NEXT: {contract.next_step}",
        _section("AUTHORITY", contract.authority),
        *(grant_lines or []),
        _section("ASSUMPTIONS", contract.assumptions),
    ]
    if contract.research:
        lines.append("RESEARCH: on")
    if contract.keys:
        lines.append("KEYS: " + ", ".join(contract.keys))
    if contract.follows:
        lines.append(f"FOLLOWS: {contract.follows}")
    return "\n".join(lines)


def store_contract(store: Store, job_id: str, contract: Contract) -> None:
    previous = load_contract(store, job_id)
    store.kv_set(job_id, "contract", asdict(contract))
    if previous is None:
        store.set_state(
            job_id,
            JobState.CONTRACTED,
            what="working contract proposed",
            evidence={
                "revision": contract.revision,
                "boundaries": len(contract.boundaries),
                "authority": len(contract.authority),
            },
            rule="discovery_complete",
            judgment=Judgment.code,
        )
    seed_boundary_instructions(store, job_id, contract)
    if previous is not None and (previous.boundaries != contract.boundaries or previous.authority != contract.authority):
        import time as _time

        from steward import grants
        from steward.actor import Actor, ActorRef

        grants.bump(store, job_id, change="contract_changed", actor=ActorRef(Actor.CONTROLLER, "contract"),
                    now=_time.time(), detail={"revision": contract.revision})


def seed_boundary_instructions(store: Store, job_id: str, contract: Contract) -> None:
    """Seed each contract boundary as a durable Steering instruction.

    Idempotent per `contract.revision`: the request id is deterministic
    (`contract-boundary-<job_id>-<revision>-<index>`; job-scoped because
    request ids are deduped globally), so calling this twice for the
    same revision never records a boundary twice (`Steering.record` already
    dedupes by request id).
    """
    steering = Steering(store)
    for idx, boundary in enumerate(contract.boundaries):
        if _is_outbound_boundary(boundary):
            verb = "authority"
        elif parse_avoid_globs(boundary):
            verb = "avoid"
        else:
            verb = "focus"
        request_id = f"contract-boundary-{job_id}-{contract.revision}-{idx}"
        steering.record(job_id, verb, boundary, request_id)


def load_contract(store: Store, job_id: str) -> Contract | None:
    data = store.kv_get(job_id, "contract")
    if data is None:
        return None
    data = dict(data)
    if data.get("pause") not in PAUSE_VALUES:
        data["pause"] = "off"  # stored before the field existed (or unknown): a running job never changes behaviour
    return Contract(**data)


def with_pause(contract: Contract, value: str) -> Contract:
    """The same contract with another pause value (validated by `Contract.__post_init__`)."""
    return replace(contract, pause=value)
