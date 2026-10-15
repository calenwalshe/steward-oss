"""The one egress guard for Jev (continuous job ownership Plan 2; spec §13; Jev import README conflict 5).

Every request to TypeSafe passes `GuardedTransport.post`, wrapped around whatever transport the observer was given
(real or fake), so no call site — triage/ETA/Flow riders, resolver risk, fleet lane, in-flight activity, evidence —
can bypass it. The lane is named by the body's own `state.schema_version` (unknown lane ⇒ refused). Every string in
`state` and `questions` is scrubbed (`sanitize.obj`); the serialized result must show no leak (`sanitize.leaks`) and
must fit its lane's byte limit, else the request is refused before any byte leaves — never silently truncated.
Diagnostic reads never flow here: only the evidence lane may carry an excerpt, and only one the controller cited,
bounded and redacted (`excerpts`). A clean body is forwarded byte-identical.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from steward import sanitize

LANE_SCHEMAS = {"steward_jev_v2": "observe", "steward_jev_risk_v1": "risk", "steward_jev_stop_v1": "stop", "steward_jev_lane_v1": "lane",
                "steward_inflight_v1": "activity", "steward_jev_evidence_v1": "evidence"}
OBSERVATION_BODY_MAX_BYTES = 65536
LANE_MAX_BYTES = {"observe": OBSERVATION_BODY_MAX_BYTES, "risk": OBSERVATION_BODY_MAX_BYTES,
                  "stop": OBSERVATION_BODY_MAX_BYTES,
                  "lane": OBSERVATION_BODY_MAX_BYTES, "activity": OBSERVATION_BODY_MAX_BYTES,
                  "evidence": 6000}  # == excerpts.REQUEST_MAX_BYTES (asserted by tests/test_excerpts.py)
_EVIDENCE_STATE_KEYS = {"schema_version", "claim", "citations", "excerpts"}


class EgressRefused(RuntimeError):
    def __init__(self, reason: str, lane: str = "?"):
        super().__init__(f"jev egress refused ({lane}): {reason}")
        self.reason, self.lane = reason, lane


def _check_evidence(state: dict) -> None:
    from steward import excerpts as ev  # limits live with the lane (Task 12); imported lazily (no cycle)

    if set(state) - _EVIDENCE_STATE_KEYS:
        raise EgressRefused("evidence_state_keys", "evidence")
    if len(str(state.get("claim") or "")) > ev.CLAIM_MAX_CHARS:
        raise EgressRefused("claim_too_long", "evidence")
    excerpts = state.get("excerpts") or []
    if len(excerpts) > ev.MAX_EXCERPTS or any(len(str(x.get("text") or "")) > ev.EXCERPT_MAX_CHARS for x in excerpts):
        raise EgressRefused("excerpt_limits", "evidence")


def guard_body(body: bytes) -> tuple[bytes, str]:
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise EgressRefused("not_json") from exc
    state = parsed.get("state") if isinstance(parsed, dict) else None
    lane = LANE_SCHEMAS.get(str((state or {}).get("schema_version") or ""))
    if lane is None:
        raise EgressRefused("unknown_lane")
    scrubbed = dict(parsed)
    for key in ("state", "questions"):
        if key in scrubbed:
            scrubbed[key] = sanitize.obj(scrubbed[key], max_chars=None)
    if lane == "evidence":
        _check_evidence(scrubbed["state"])
    out = body if scrubbed == parsed else json.dumps(scrubbed, ensure_ascii=False).encode("utf-8")
    cats = sanitize.leaks(out.decode("utf-8", "replace"))
    if cats:
        raise EgressRefused("leak:" + ",".join(cats), lane)
    limit = LANE_MAX_BYTES[lane]
    if len(out) > limit:
        raise EgressRefused(f"too_large:{len(out)}>{limit}", lane)
    return out, lane


class GuardedTransport:
    def __init__(self, inner, *, on_sent: Callable | None = None, on_refused: Callable | None = None):
        self.inner, self.on_sent, self.on_refused = inner, on_sent, on_refused

    def post(self, url, body, headers, timeout):
        try:
            guarded, lane = guard_body(body)
        except EgressRefused as exc:
            if self.on_refused is not None:
                try:
                    self.on_refused(exc.lane, exc.reason)
                except Exception:  # noqa: BLE001 - observers never break the guard
                    pass
            raise
        t0 = time.monotonic()
        resp = self.inner.post(url, guarded, headers, timeout)
        if self.on_sent is not None:
            try:
                self.on_sent(lane, guarded, resp, time.monotonic() - t0)
            except Exception:  # noqa: BLE001 - a recorder failure never costs the call
                pass
        return resp

    def __getattr__(self, name):
        return getattr(self.inner, name)
