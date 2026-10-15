"""The outer trajectory reviewer: verdict schema, packet, protocol (TRJ-06).

`Astra` ("board member, not a manager") sees longitudinal evidence only — no
transcript, no source, no line-level advice is possible or wanted
(12-CONTEXT D1, D6). `TrajectoryVerdict` is a strict, typed schema: a parse
failure or an out-of-enum value never becomes a guessed verdict (SP-18) —
`parse_verdict` returns `None` instead. `build_packet` assembles the
longitudinal sections D3 defines from a `TrajectoryLedger.snapshot()` dict, a
current contract, the triggers that fired, open hysteresis concerns, and the
current/discarded approach names; every string is redacted
(`steward.diagnosis.redact`), and the whole thing is capped at 12 KB by
trimming sections oldest-first (never by silently dropping a whole section).

`ClaudeTrajectoryReviewer` shells `env -u ANTHROPIC_API_KEY claude -p <prompt>
--model <astra> --output-format json` through the injected `Runner` (SP-1) —
the same wrapper convention as `steward.diagnosis.ClaudeDiagnoser`. The outer
JSON's `total_cost_usd`/`duration_ms` are kept even when the inner verdict
fails to parse, so supervision spend is still accounted for a wasted review.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from steward.diagnosis import redact
from steward.runner_exec import Runner

_TRAJECTORIES = frozenset({"sound", "questionable", "off_track"})
_URGENCIES = frozenset({"low", "normal", "high"})
_CONCERNS = frozenset(
    {
        "none",
        "wrong_problem",
        "wrong_approach",
        "scope_creep",
        "acceptance_drift",
        "interface_risk",
        "dependency_risk",
        "thrash",
        "budget",
    }
)
_ACTIONS = frozenset({"continue", "refocus", "change_approach", "narrow_scope", "escalate_human"})

_OBSERVATION_MAX = 400
_INSTRUCTION_MAX = 500
_SLUG_MAX = 40
_SLUG_RE = re.compile(r"^[a-z0-9-]*$")

# Packet caps (12-CONTEXT D6/TRJ-06: <= 12 KB, sections trimmed oldest-first).
_MAX_PACKET_BYTES = 12 * 1024
_PACKET_STR_CAP = 300
_TOP_PATHS_N = 10
_TRIM_ORDER = (
    "test_trajectory",
    "decisions",
    "milestones",
    "human_steering",
    "plan_history",
    "discarded_approaches",
    "triggers",
)


@dataclass(frozen=True)
class Recommendation:
    action: str  # continue | refocus | change_approach | narrow_scope | escalate_human
    instruction: str = ""
    current_approach: str = ""
    recommended_approach: str = ""


@dataclass(frozen=True)
class TrajectoryVerdict:
    trajectory: str  # sound | questionable | off_track
    confidence: float
    urgency: str  # low | normal | high
    concern: str
    observation: str
    recommendation: Recommendation


def _valid_slug(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= _SLUG_MAX and bool(_SLUG_RE.match(value))


def parse_verdict(obj: Any) -> TrajectoryVerdict | None:
    """Strict parse (SP-18): any missing field, out-of-enum value, slug
    violation or over-long text yields `None` — never a guessed verdict."""
    try:
        if not isinstance(obj, dict):
            return None

        trajectory = obj["trajectory"]
        confidence = obj["confidence"]
        urgency = obj["urgency"]
        concern = obj["concern"]
        observation = obj["observation"]
        recommendation = obj["recommendation"]

        if trajectory not in _TRAJECTORIES:
            return None
        if type(confidence) not in (int, float) or isinstance(confidence, bool) or not (0.0 <= confidence <= 1.0):
            return None
        if urgency not in _URGENCIES:
            return None
        if concern not in _CONCERNS:
            return None
        if not isinstance(observation, str) or len(observation) > _OBSERVATION_MAX:
            return None
        if not isinstance(recommendation, dict):
            return None

        action = recommendation["action"]
        instruction = recommendation.get("instruction", "")
        current_approach = recommendation.get("current_approach", "")
        recommended_approach = recommendation.get("recommended_approach", "")

        if action not in _ACTIONS:
            return None
        if not isinstance(instruction, str) or len(instruction) > _INSTRUCTION_MAX:
            return None
        if not _valid_slug(current_approach) or not _valid_slug(recommended_approach):
            return None

        return TrajectoryVerdict(
            trajectory=trajectory,
            confidence=float(confidence),
            urgency=urgency,
            concern=concern,
            observation=observation,
            recommendation=Recommendation(
                action=action,
                instruction=instruction,
                current_approach=current_approach,
                recommended_approach=recommended_approach,
            ),
        )
    except (KeyError, TypeError):
        return None


def verdict_to_dict(verdict: TrajectoryVerdict) -> dict:
    return dataclasses.asdict(verdict)


# -- packet -----------------------------------------------------------------


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)[:_PACKET_STR_CAP]
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    return value


def _lists(snapshot: dict) -> dict:
    return snapshot.get("lists", {})


def _as_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, dict):
        return dict(obj)
    # A plain object with attributes (e.g. steward.contract.Contract-like duck type).
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")}


def _trigger_dict(trigger: Any) -> dict:
    if isinstance(trigger, dict):
        return {"kind": trigger.get("kind"), "urgent": trigger.get("urgent", False), "detail": trigger.get("detail", {})}
    return {"kind": getattr(trigger, "kind", None), "urgent": getattr(trigger, "urgent", False), "detail": getattr(trigger, "detail", {})}


def _top_churn_paths(diff_samples: list[dict], n: int = _TOP_PATHS_N) -> list[str]:
    """Names only (D6), ranked by how often a path shows up across every
    diff sample recorded so far — the closest proxy to "churn" the ledger's
    own facts support (INFERRED: no per-file line counts are sampled)."""
    counts: dict[str, int] = {}
    for sample in diff_samples:
        for path in sample.get("files", []):
            counts[path] = counts.get(path, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [path for path, _ in ranked[:n]]


def build_packet(
    snapshot: dict,
    contract: Any,
    triggers: list[Any],
    open_concerns: dict,
    current_approach: str | None,
    discarded: list[str],
    cfg: "PacketConfig | None" = None,
) -> dict:
    """Assemble the redacted, size-capped review packet (D3/D6/TRJ-06)."""
    cfg = cfg or PacketConfig()
    lists = _lists(snapshot)
    contract_d = _as_dict(contract)

    diff_samples = lists.get("diff_samples", [])
    latest_diff = diff_samples[-1] if diff_samples else {}
    diff_summary = {
        "lines": latest_diff.get("changed_lines", 0),
        "file_count": len(latest_diff.get("files", [])),
        "top_paths": _top_churn_paths(diff_samples, cfg.top_paths_n),
    }

    worker_spend = _worker_spend(lists.get("cost_points", []))
    supervision_spend = sum(p["cost_usd"] for p in lists.get("cost_points", []) if p.get("source") == "supervision")
    sample_ts = [p.get("ts", 0) for p in lists.get("cost_points", [])]
    started_at = snapshot.get("started_at")
    last_ts = max(sample_ts) if sample_ts else started_at
    elapsed_s = (last_ts - started_at) if (started_at is not None and last_ts is not None) else 0

    decisions = lists.get("decisions", [])
    human_steering = [d for d in decisions if d.get("origin") == "human"]

    ledger_discarded = list(lists.get("discarded_approaches", []))
    known_slugs = {d.get("slug") for d in ledger_discarded}
    for slug in discarded or []:
        if slug not in known_slugs:
            ledger_discarded.append({"slug": slug, "reason": "", "ts": None})
            known_slugs.add(slug)

    packet: dict = {
        "current_approach": current_approach or "",
        "intent": snapshot.get("intent") or {},
        "current_acceptance": {
            "boundaries": contract_d.get("boundaries", []),
            "done_when": contract_d.get("done_when", []),
        },
        "plan_history": lists.get("plan", [])[-cfg.plan_history_n:],
        "decisions": decisions[-cfg.decisions_n:],
        "discarded_approaches": ledger_discarded,
        "milestones": lists.get("milestones", [])[-cfg.milestones_n:],
        "human_steering": human_steering[-cfg.decisions_n:],
        "diff_summary": diff_summary,
        "test_trajectory": lists.get("test_points", [])[-cfg.test_points_n:],
        "cost_time": {
            "worker_spend_usd": worker_spend,
            "supervision_spend_usd": supervision_spend,
            "elapsed_s": elapsed_s,
        },
        "triggers": [_trigger_dict(t) for t in triggers or []],
        "open_concerns": dict(open_concerns or {}),
    }

    packet = _redact_value(packet)
    packet = _trim_to_fit(packet, cfg.max_bytes)
    return packet


@dataclass(frozen=True)
class PacketConfig:
    plan_history_n: int = 5
    decisions_n: int = 20
    milestones_n: int = 20
    test_points_n: int = 20
    top_paths_n: int = _TOP_PATHS_N
    max_bytes: int = _MAX_PACKET_BYTES


def _worker_spend(cost_points: list[dict]) -> float:
    by_attempt: dict[int, list[float]] = {}
    for point in cost_points:
        if point.get("source") != "worker":
            continue
        by_attempt.setdefault(point["attempt_no"], []).append(point["cost_usd"])
    total = 0.0
    for values in by_attempt.values():
        non_decreasing = all(values[i] <= values[i + 1] for i in range(len(values) - 1))
        total += max(values) if non_decreasing else sum(values)
    return total


def _packet_size(packet: dict) -> int:
    return len(json.dumps(packet, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))


def _trim_to_fit(packet: dict, max_bytes: int) -> dict:
    # `truncated` is reserved up front (even if empty) so its own growing
    # size is counted by every `_packet_size` check inside the loop —
    # otherwise adding it only after the loop could push a just-under-cap
    # packet back over `max_bytes`.
    packet["truncated"] = []
    truncated: set[str] = set()
    while _packet_size(packet) > max_bytes:
        trimmed_any = False
        for key in _TRIM_ORDER:
            lst = packet.get(key)
            if isinstance(lst, list) and lst:
                lst.pop(0)
                if key not in truncated:
                    truncated.add(key)
                    packet["truncated"] = sorted(truncated)
                trimmed_any = True
                if _packet_size(packet) <= max_bytes:
                    break
        if not trimmed_any:
            break
    if truncated:
        packet["truncated"] = sorted(truncated)
    else:
        packet.pop("truncated", None)
    return packet


# -- reviewer protocol --------------------------------------------------------


@dataclass(frozen=True)
class ReviewResult:
    verdict: TrajectoryVerdict | None
    cost_usd: float
    duration_ms: float
    error: str | None = None
    usage: dict | None = None  # {input, output, cache_read, cache_creation} from claude's JSON


class TrajectoryReviewer(Protocol):
    def review(self, packet: dict) -> ReviewResult: ...


_SCHEMA_TEXT = (
    '{"trajectory": "sound" | "questionable" | "off_track", "confidence": <0..1>, '
    '"urgency": "low" | "normal" | "high", '
    '"concern": "none" | "wrong_problem" | "wrong_approach" | "scope_creep" | "acceptance_drift" | '
    '"interface_risk" | "dependency_risk" | "thrash" | "budget", '
    '"observation": "<=400 chars>", '
    '"recommendation": {"action": "continue" | "refocus" | "change_approach" | "narrow_scope" | "escalate_human", '
    '"instruction": "<=500 chars>", "current_approach": "<slug, [a-z0-9-], <=40 chars>", '
    '"recommended_approach": "<slug, [a-z0-9-], <=40 chars>"}}'
)


def build_prompt(packet: dict) -> str:
    return "\n".join(
        [
            "You are a board member reviewing this project's trajectory, not another manager.",
            "You see longitudinal evidence only: no source code, no raw pane output, and there is no",
            "line-level advice to give or receive here — only whether the overall approach is sound.",
            "Decide: is the current approach sound, questionable, or off track, given the evidence below?",
            "EVIDENCE (redacted, longitudinal facts only):",
            json.dumps(packet, sort_keys=True, default=str),
            "",
            "Reply with a single strict JSON object only, no prose, no markdown fences, matching exactly:",
            _SCHEMA_TEXT,
        ]
    )


class ClaudeTrajectoryReviewer:
    """Shells `env -u ANTHROPIC_API_KEY claude -p <prompt> --model <astra>
    --output-format json` through the injected `Runner` (SP-1)."""

    def __init__(self, runner: Runner, model: str, *, timeout: float = 300, cwd: str = "."):
        self.runner = runner
        self.model = model
        self.timeout = timeout
        self.cwd = cwd

    def review(self, packet: dict) -> ReviewResult:
        prompt = build_prompt(packet)
        result = self.runner.run(
            [
                "env",
                "-u",
                "ANTHROPIC_API_KEY",
                "claude",
                "-p",
                prompt,
                "--model",
                self.model,
                "--output-format",
                "json",
            ],
            cwd=self.cwd,
            timeout=self.timeout,
        )
        return _parse_review(result.stdout)


def _parse_usage(data: Any) -> dict | None:
    u = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(u, dict):
        return None
    return {
        "input": int(u.get("input_tokens") or 0),
        "output": int(u.get("output_tokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
        "cache_creation": int(u.get("cache_creation_input_tokens") or 0),
    }


def _parse_review(stdout: str) -> ReviewResult:
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError) as exc:
        return ReviewResult(verdict=None, cost_usd=0.0, duration_ms=0.0, error=f"outer_json_parse_failed: {exc}")

    cost_usd = float(data.get("total_cost_usd", 0.0)) if isinstance(data, dict) else 0.0
    duration_ms = float(data.get("duration_ms", 0.0)) if isinstance(data, dict) else 0.0
    usage = _parse_usage(data)

    payload: Any = data
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        try:
            payload = json.loads(data["result"])
        except (ValueError, TypeError) as exc:
            return ReviewResult(
                verdict=None, cost_usd=cost_usd, duration_ms=duration_ms, error=f"result_json_parse_failed: {exc}", usage=usage
            )

    verdict = parse_verdict(payload)
    if verdict is None:
        return ReviewResult(verdict=None, cost_usd=cost_usd, duration_ms=duration_ms, error="verdict_parse_failed", usage=usage)
    return ReviewResult(verdict=verdict, cost_usd=cost_usd, duration_ms=duration_ms, error=None, usage=usage)


class FakeTrajectoryReviewer:
    """Scripted reviewer for tests. Never shells out. Each step in `steps`
    is a verdict-shaped dict, or the literal string `"unavailable"`."""

    def __init__(self, steps: list[Any]):
        self.steps = list(steps)
        self._idx = 0
        self.packets: list[dict] = []

    def review(self, packet: dict) -> ReviewResult:
        self.packets.append(packet)
        if not self.steps:
            return ReviewResult(verdict=None, cost_usd=0.0, duration_ms=0.0, error="unavailable")
        idx = min(self._idx, len(self.steps) - 1)
        self._idx += 1
        step = self.steps[idx]

        if step == "unavailable":
            return ReviewResult(verdict=None, cost_usd=0.0, duration_ms=0.0, error="unavailable")

        cost_usd = float(step.get("cost_usd", 0.0)) if isinstance(step, dict) else 0.0
        duration_ms = float(step.get("duration_ms", 0.0)) if isinstance(step, dict) else 0.0
        verdict = parse_verdict(step)
        error = None if verdict is not None else "verdict_parse_failed"
        return ReviewResult(verdict=verdict, cost_usd=cost_usd, duration_ms=duration_ms, error=error)

    @classmethod
    def from_script(cls, path: str | Path) -> "FakeTrajectoryReviewer":
        data = json.loads(Path(path).read_text())
        steps = data["steps"] if isinstance(data, dict) else data
        return cls(steps)
