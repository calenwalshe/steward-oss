"""Literal-value masking for keys granted to a job (research & keys).

Pattern-based redaction (`steward.diagnosis.redact`) cannot recognise an
arbitrary API key, so the literal values of every key granted to a job are
registered here (`arm_job`, names -> values via `steward.research.load_keys`)
and masked wherever Steward writes or renders text: event evidence
(`Store.append_event`), frames (`fabric.encode`), and `redact()` (transcripts,
watchers, diagnosis). Values live in process memory only.
"""

from __future__ import annotations

MASK = "[redacted-key]"
MIN_LEN = 6  # shorter values would mask ordinary words

_VALUES: set[str] = set()


def register(values) -> None:
    _VALUES.update(v for v in values if isinstance(v, str) and len(v) >= MIN_LEN)


def clear() -> None:
    _VALUES.clear()


def mask(text: str) -> str:
    if not _VALUES or not text:
        return text
    for v in sorted(_VALUES, key=len, reverse=True):
        if v in text:
            text = text.replace(v, MASK)
    return text


def contains_registered(text: str) -> bool:
    """True when any registered value appears in text (egress/capture leak check)."""
    return bool(text) and any(v in text for v in _VALUES)

def mask_obj(obj):
    """Deep-copy `obj` (dict/list/str/scalars) with every registered value masked."""
    if not _VALUES:
        return obj
    if isinstance(obj, str):
        return mask(obj)
    if isinstance(obj, dict):
        return {(mask(k) if isinstance(k, str) else k): mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_obj(v) for v in obj]
    return obj


def arm_job(store, job_id: str | None) -> None:
    """Register the granted-key values for `job_id` (no-op when none granted)."""
    if not job_id:
        return
    try:
        from steward import research

        names = research.granted_keys(store, job_id)
        if names:
            register(research.load_keys(names).values())
    except Exception:  # noqa: BLE001 - masking setup must never break a write path
        pass
