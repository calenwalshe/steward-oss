"""Shared isolated fixtures for the Plan 3 tests (recovery and advisory budgets). Never a real model, never the
network, never production state. Builds on Plan 1's `ownership_fixtures`."""
from __future__ import annotations

import json

from steward import research
from steward.jev_observer import TransportResponse
from steward.store import Store

from ownership_fixtures import contracted  # noqa: F401  (re-exported for Plan 3 tests)


def claude_envelope(text: str, *, cost: float | None = 0.05, is_error: bool = False, subtype: str = "success") -> str:
    """The `claude -p --output-format json` result envelope shape already parsed by `flow_investigator` and
    `resolver.parse_output`. The `error_max_budget_usd` subtype is an ASSUMPTION about the exhaustion shape (see the
    plan's evidence table); tests label it as such."""
    env = {"type": "result", "subtype": subtype, "is_error": is_error, "result": text}
    if cost is not None:
        env["total_cost_usd"] = cost
    return json.dumps(env)


class CostedInvoker:
    """A tech-lead / reviewer invoker returning `(text, cost_usd | None)`; per entry id a list of answers (str,
    `(str, cost)`, or an Exception). Records every call."""

    def __init__(self, plan: dict[str, list]):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, entry, prompt, timeout_s):
        self.calls.append((entry.id, prompt))
        nxt = self.plan[entry.id].pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt if isinstance(nxt, tuple) else (nxt, None)


def jev_answer(question: str, choice: str, confidence: float, probabilities: dict | None = None) -> bytes:
    probs = probabilities if probabilities is not None else {choice: confidence}
    return json.dumps({"answers": {question: {"choice": choice, "confidence": confidence,
                                              "probabilities": probs}}}).encode()


class JevScript:
    """A Jev transport (the `jev_observer.Transport` protocol) answering from a list; never the network."""

    def __init__(self, bodies: list):
        self.bodies = list(bodies)
        self.posts: list[bytes] = []

    def post(self, url, body, headers, timeout):
        self.posts.append(body)
        nxt = self.bodies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, TransportResponse):
            return nxt
        return TransportResponse(status_code=200, body=nxt)


def research_job(store: Store, cap: float | None) -> str:
    """A contracted job with research on; `cap=None` means the owner gave no cap (the $5 default applies)."""
    jid = contracted(store)
    research.set_caps(store, jid, research=True, keys=[], cap_usd=cap)
    return jid
