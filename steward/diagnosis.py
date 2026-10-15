"""One bounded, budgeted diagnosis per suspect stall (SP-12, SP-13, SP-21).

`Diagnoser.diagnose` is the only place a model is asked anything during
watch/recover, and only ever in response to a `suspect_stall` heartbeat
class — never on every tick (SP-12). `ClaudeDiagnoser` shells out through the
injected `Runner` seam (never a raw `subprocess` call, never in tests);
`env -u ANTHROPIC_API_KEY` forces subscription billing per project
convention. A parse failure fails closed to `needs_human` rather than
guessing (SP-18: a code `if` around a model answer is a hybrid decision, and
an unparseable one carries no authority).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from steward import masking
from steward.runner_exec import Runner

# Reimplementation of herdloop/jev.py's redact() regexes (read, not
# imported, per the executor brief) — masks common credentials before a
# diagnosis prompt is shelled out to a model.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+"
)
_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{12,})\b"
)
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")

_VERDICTS = frozenset({"retry_with_guidance", "replace_worker", "needs_human"})


def redact(text: str) -> str:
    """Mask common credentials before text is sent to a model or logged."""
    text = masking.mask(text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _TOKEN.sub("[redacted-token]", text)
    return _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=[redacted]", text)


@dataclass(frozen=True)
class Diagnosis:
    verdict: str  # "retry_with_guidance" | "replace_worker" | "needs_human"
    guidance: str
    hypothesis: str


class Diagnoser(Protocol):
    def diagnose(self, context: dict) -> Diagnosis: ...


def build_prompt(context: dict) -> str:
    """Bounded prompt: contract goal, last signals, redacted output tail."""
    goal = context.get("goal", "")
    signals = context.get("signals", {})
    output_tail = redact(str(context.get("output_tail", "")))
    return "\n".join(
        [
            "A coding worker under a durable job runner appears stalled.",
            "Diagnose the stall and decide exactly one action.",
            f"GOAL: {goal}",
            f"SIGNALS: {json.dumps(signals, sort_keys=True, default=str)}",
            "RECENT OUTPUT (redacted, most recent last):",
            output_tail,
            "",
            "Reply with a single strict JSON object only, no prose, no markdown fences:",
            '{"verdict": "retry_with_guidance" | "replace_worker" | "needs_human",'
            ' "guidance": "<short instruction for the next attempt, empty string if none>",'
            ' "hypothesis": "<one sentence: why it stalled>"}',
        ]
    )


class ClaudeDiagnoser:
    """Shells `env -u ANTHROPIC_API_KEY claude -p <prompt> --output-format json`."""

    def __init__(self, runner: Runner, *, timeout: float = 180, cwd: str = ".", model: str | None = None):
        self.runner = runner
        self.timeout = timeout
        self.cwd = cwd
        self.model = model

    def diagnose(self, context: dict) -> Diagnosis:
        prompt = build_prompt(context)
        argv = [
            "env",
            "-u",
            "ANTHROPIC_API_KEY",
            "claude",
            "-p",
            prompt,
        ]
        if self.model:
            argv += ["--model", self.model]
        argv += ["--output-format", "json"]
        result = self.runner.run(argv, cwd=context.get("cwd", self.cwd), timeout=self.timeout)
        return _parse(result.stdout)


def _parse(stdout: str) -> Diagnosis:
    try:
        data = json.loads(stdout)
        payload = data
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            # `claude --output-format json` wraps the model's answer in a
            # `result` string field; unwrap one more level of JSON.
            payload = json.loads(data["result"])
        verdict = payload["verdict"]
        if verdict not in _VERDICTS:
            raise ValueError(f"unrecognized verdict: {verdict!r}")
        return Diagnosis(
            verdict=verdict,
            guidance=str(payload.get("guidance", "")),
            hypothesis=str(payload.get("hypothesis", "")),
        )
    except Exception as exc:  # fail-closed: never invent a verdict (SP-18)
        return Diagnosis(
            verdict="needs_human",
            guidance="",
            hypothesis=f"diagnosis_parse_failed: {exc}",
        )


class FakeDiagnoser:
    """Scripted diagnoser for tests. Never shells out, never touches a model."""

    def __init__(self, diagnosis: Diagnosis | list[Diagnosis]):
        self._diagnoses = list(diagnosis) if isinstance(diagnosis, list) else [diagnosis]
        self._idx = 0
        self.contexts: list[dict] = []

    def diagnose(self, context: dict) -> Diagnosis:
        self.contexts.append(context)
        idx = min(self._idx, len(self._diagnoses) - 1)
        self._idx += 1
        return self._diagnoses[idx]
