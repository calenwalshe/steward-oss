"""One redaction and data-scope function for every capture and egress path (continuous ownership Plan 2;
spec §13 canary masking, §14 data scope).

`text`/`obj` scrub: registered values (granted keys, test canaries: `masking`), bearer tokens, `key=value` secrets
and token shapes (`diagnosis.redact`), raw token patterns (`tech_lead_floor._SECRET_VALUE_RE`) and private-key
blocks. `leaks` says which categories survive in a blob (empty after a correct scrub) — the egress guard refuses to
send a body that still leaks. `capture` applies the per-kind allowlist below, drops forbidden keys at any depth,
caps every string and lists what it dropped, so investigations report missing evidence instead of inventing it.
"""

from __future__ import annotations

import hashlib
import json
import re

from steward import masking
from steward.diagnosis import redact as _redact
from steward.tech_lead_floor import _SECRET_VALUE_RE

FIELD_MAX_CHARS = 2000            # == decision_trace.FIELD_MAX_CHARS (Plan 1); asserted by test
REPLAY_STATE_MAX_CHARS = 8192
FORBIDDEN_KEYS = ("prompt", "raw", "transcript", "environ", "env", "headers", "authorization", "api_key", "cookie")
CAPTURE_KINDS: dict[str, frozenset] = {
    "decision": frozenset({"decision", "evidence", "versions", "signal", "observed", "reason"}),
    "diagnostic": frozenset({"target", "operation", "purpose", "excerpt", "exit_code", "outcome", "refused_reason",
                             "output_digest", "truncated", "duration_s", "grant_id", "authority_rev", "requested_by"}),
    "evidence_snapshot": frozenset({"claim", "citations", "excerpts", "request_digest", "question_version", "model",
                                    "status", "label", "probabilities", "latency_ms", "discarded_reason"}),
    "jev_receipt": frozenset({"lane", "schema", "model", "question_version", "config_version", "state", "response",
                              "status", "http_status", "latency_ms", "source_refs", "request_digest", "error"}),
    "flow_snapshot": frozenset({"goal", "hb", "idle_s", "last", "turns", "steers", "verdicts", "perms"}),
}
_KIND_CAPS = {"flow_snapshot": 1000}
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_BEARER_LIVE = re.compile(r"(?i)\bBearer\s+(?!\[redacted)\S+")
SECRET_READ_RE = re.compile(
    r"(\.api-keys\b|\bkeys\.env\b|(^|[\s/'\"=])~?/?\.ssh(/|\b)|\.pem\b|(^|[\s/])\.env(\s|$|\.)|\.netrc\b|\bid_rsa\b"
    r"|\bid_ed25519\b|\bcredentials(\.json)?\b|/etc/shadow\b|/etc/gshadow\b|/proc/[^/\s]+/environ\b|\bprintenv\b"
    r"|(^|[\s;|&(])env(\s*$|\s*[;|&)])|\bsecrets?\.(toml|json|ya?ml)\b|\bMETA_API_KEY\b|\b[A-Z0-9_]*(TOKEN|SECRET|"
    r"PASSWORD|API_KEY)\b=)", re.I)


def register_canary(value: str) -> None:
    masking.register([value])


def text(value, *, max_chars: int | None = None) -> str:
    t = "" if value is None else str(value)
    t = _PRIVATE_KEY.sub("[redacted-private-key]", t)
    t = _redact(t)
    t = masking.mask(t)
    t = _SECRET_VALUE_RE.sub("[redacted-token]", t)
    if max_chars is not None and len(t) > max_chars:
        t = t[: max_chars - 1] + "…"
    return t


_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def strip_control(value: str) -> str:
    """C0/C1 control characters (and DEL) out of a string; newline and tab stay."""
    return _CONTROL_RE.sub("", value)


def control_free(value, *, max_chars: int = 12000):
    """A model-bound packet value with every string stripped of control characters and capped (SRD-12: a review
    packet must never carry the characters that made a lead's answer fail `bad_field`, bug #295)."""
    if isinstance(value, str):
        t = strip_control(value)
        return t if len(t) <= max_chars else t[: max_chars - 1] + "…"
    if isinstance(value, dict):
        return {(strip_control(k) if isinstance(k, str) else k): control_free(v, max_chars=max_chars)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [control_free(v, max_chars=max_chars) for v in value]
    return value


def obj(value, *, max_chars: int = FIELD_MAX_CHARS):
    if isinstance(value, str):
        return text(value, max_chars=max_chars)
    if isinstance(value, dict):
        return {(text(k, max_chars=200) if isinstance(k, str) else k): obj(v, max_chars=max_chars)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [obj(v, max_chars=max_chars) for v in value]
    return value


def leaks(blob: str) -> list[str]:
    out = []
    if masking.contains_registered(blob):
        out.append("registered_value")
    if _SECRET_VALUE_RE.search(blob):
        out.append("token_pattern")
    if _BEARER_LIVE.search(blob):
        out.append("bearer")
    if "PRIVATE KEY-----" in blob and "[redacted-private-key]" not in blob:
        out.append("private_key")
    return out


def _drop_forbidden(value, dropped: list[str], path: str = ""):
    if isinstance(value, dict):
        kept = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in FORBIDDEN_KEYS:
                dropped.append(f"{path}{k}")
                continue
            kept[k] = _drop_forbidden(v, dropped, f"{path}{k}.")
        return kept
    if isinstance(value, list):
        return [_drop_forbidden(v, dropped, path) for v in value]
    return value


def capture(kind: str, record: dict) -> dict:
    if kind not in CAPTURE_KINDS:
        raise ValueError(f"unknown capture kind {kind!r}; one of {sorted(CAPTURE_KINDS)}")
    allowed = CAPTURE_KINDS[kind]
    dropped = [k for k in record if k not in allowed]
    kept = {k: v for k, v in record.items() if k in allowed}
    kept = _drop_forbidden(kept, dropped)
    cap = _KIND_CAPS.get(kind, FIELD_MAX_CHARS)
    out = {}
    for k, v in kept.items():
        if kind == "jev_receipt" and k == "state":
            s = obj(v, max_chars=REPLAY_STATE_MAX_CHARS)
            blob = json.dumps(s, sort_keys=True, default=str)
            out[k] = s if len(blob) <= REPLAY_STATE_MAX_CHARS else {
                "omitted": f"state exceeds {REPLAY_STATE_MAX_CHARS} chars",
                "digest": hashlib.sha256(blob.encode()).hexdigest()[:16]}
        else:
            out[k] = obj(v, max_chars=cap)
    if dropped:
        out["_dropped"] = sorted(dropped)
    return out
