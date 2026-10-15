"""Checkpoint / handoff at recovery boundaries (SP-15).

A fresh worker must not repeat work a replaced one already did. `Handoff`
persists through `Store.kv_get`/`kv_set` (kv, no schema change) and is also
written as a human-readable markdown file under
`$STEWARD_HOME/jobs/<job_id>/handoff-<n>.md` so an operator inspecting the
job directory sees exactly what the next worker will see in its brief.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from steward.store import Store

_HANDOFF_KEY = "handoff"
_CMD_LINE_RE = re.compile(r"^[ \t]*[$❯][ \t]+(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Handoff:
    hypothesis: str
    commands_tried: list[str] = field(default_factory=list)
    results: list[str] = field(default_factory=list)
    next_action: str = ""
    attempt_no: int = 1


def from_observation(
    output_tail: str,
    *,
    hypothesis: str,
    next_action: str,
    attempt_no: int,
    results: list[str] | None = None,
) -> Handoff:
    """Build a `Handoff` from a worker's raw output tail.

    Shell commands seen in the output (lines starting `$ ` or `❯ `) are
    extracted as `commands_tried` so the checkpoint reflects what was
    actually attempted, not what the diagnosis merely guessed.
    """
    commands = _CMD_LINE_RE.findall(output_tail)
    return Handoff(
        hypothesis=hypothesis,
        commands_tried=commands,
        results=list(results) if results else [],
        next_action=next_action,
        attempt_no=attempt_no,
    )


def save(store: Store, job_id: str, handoff: Handoff) -> None:
    store.kv_set(job_id, _HANDOFF_KEY, asdict(handoff))
    _write_markdown(store, job_id, handoff)


def load(store: Store, job_id: str) -> Handoff | None:
    data = store.kv_get(job_id, _HANDOFF_KEY)
    if data is None:
        return None
    return Handoff(**data)


def _write_markdown(store: Store, job_id: str, handoff: Handoff) -> None:
    job_dir = store.home / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / f"handoff-{handoff.attempt_no}.md"

    commands = [f"- {c}" for c in handoff.commands_tried] or ["- (none observed)"]
    results = [f"- {r}" for r in handoff.results] or ["- (none recorded)"]

    lines = [
        f"# Handoff (attempt {handoff.attempt_no})",
        "",
        "## Hypothesis",
        handoff.hypothesis,
        "",
        "## Commands tried",
        *commands,
        "",
        "## Results",
        *results,
        "",
        "## Next action",
        handoff.next_action,
    ]
    path.write_text("\n".join(lines) + "\n")
