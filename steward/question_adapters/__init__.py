"""Question adapters (Phase 17, G1/G14): the formats a job may use to leave a question for the owner.

Exactly two: `gsd_context` (a GSD `*-CONTEXT.md` area marked `— OPEN FOR <OWNER>`) and `steward_questions` (the generic
structured format). More are deferred until a real job needs one.
"""

from __future__ import annotations

from steward.question_adapters.base import Adapter, Change, FileReader, Found
from steward.question_adapters.gsd_context import GsdContext
from steward.question_adapters.steward_questions import StewardQuestions

ADAPTERS: tuple[Adapter, ...] = (GsdContext(), StewardQuestions())


def adapter_named(name: str) -> Adapter | None:
    return next((a for a in ADAPTERS if a.name == name), None)


__all__ = ["ADAPTERS", "Adapter", "Change", "FileReader", "Found", "adapter_named"]
