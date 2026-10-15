"""Job tab titles: `<glyph> <short name>[ <one live fact>]`, at most 24 cells.

Pure functions (no Herdr, no store writes except `job_name`'s one-time kv
write). The tab label is the only surface Herdr gives a tab, so the glyph
carries urgency and the trailing fact carries the one number worth a glance:

    ◆ P18 ETA ?3     ● P19 Installable ~2h     ⚠ job runners 14m     ✓ lost-approval

The short name lives in kv `short_name` (set at launch, editable with
`steward name` or by renaming the tab in Herdr, which `parse_label` adopts).
"""

from __future__ import annotations

import re

from steward import owner
from steward.jobview import fit, vlen

LABEL_CAP = 24
NAME_CAP = 16
INFO_CAP = 5
GLYPH_CELLS = 2  # ⚠/⏸ can render wide: always budget two cells

NEEDS_YOU, STUCK, RUNNING, PAUSED, ACCEPTED, FAILED = "◆", "⚠", "●", "⏸", "✓", "■"
GLYPHS = (NEEDS_YOU, STUCK, RUNNING, PAUSED, ACCEPTED, FAILED, "⚙")  # ⚙ = the legacy launch label

PINNED_OPS, PINNED_STEWARD, PINNED_PHONE, PINNED_DONE = "⌂ ops", "⚕ steward", "📱 phone", "✓ done"

NAME_KEY = "short_name"
LABEL_KEY = "herdr_tab_label"

_LEAD_INS = frozenset(
    "use gsd-quick gsd to build start make add implement fix the a an steward new and then in of for with".split())
# Words a name must not end on ("Extend the" says nothing): articles are dropped from the middle, the rest only trail.
_ARTICLES = frozenset("the a an".split())
_STOPWORDS = _ARTICLES | frozenset(
    "of to for with in on and or by at from into as is its this that my your our their these those".split())
_PHASE = re.compile(r"\bPhase\s+(\d+)\s*[\"“'‘]([^\"”'’]+)[\"”'’]")
_CAPS = re.compile(r"\b[A-Z][A-Z0-9-]+(?: [A-Z][A-Z0-9-]+)+\b")
_QUOTED = re.compile(r"[\"“]([^\"”]{2,%d})[\"”]" % NAME_CAP)
_FOLLOW = re.compile(r"^\s*Follow-up of\s+([0-9a-f]{4,})", re.I)
_BOILERPLATE = frozenset({"FINISH LINE", "DONE WHEN", "NEEDS YOU", "NEVER GIT PUSH"})
_INFO = re.compile(r"^(\?\d+|ok\?|~\d+h\d\d|~\d+[mh]|\d+[–-]\d+h|\+\d+m|\d+/\d+|\d+m|\d+h(\d\d)?)$")


def derive_name(goal: str, parent_name: str | None = None) -> str:
    """A short human name from a goal; deterministic, at most NAME_CAP cells."""
    goal = " ".join((goal or "").replace("`", "").split())  # backticks around commands made names like "`steward"
    m = _FOLLOW.match(goal)
    if m:
        return fit("↳ " + (parent_name or m.group(1)[:8]), NAME_CAP)
    # Only the first sentence names the job: later text holds finish lines ("FINISH LINE:"), quoted examples and
    # paths, which made names like 'FINISH LINE' or another job's 'P18 ETA'.
    head = re.split(r"(?<=[.!?])\s|\s\(|;\s", goal, maxsplit=1)[0][:160]
    m = _PHASE.search(head)
    if m:
        first = m.group(2).split()[0] if m.group(2).split() else ""
        return fit(f"P{m.group(1)} {first}".strip(), NAME_CAP)
    boiler_owner = {f"OPEN FOR {u}" for u in owner.parser_uppers()}
    m = next((c for c in _CAPS.finditer(head) if c.group(0) not in _BOILERPLATE and c.group(0) not in boiler_owner), None)
    if m:  # SHOUTED feature names read better in sentence case ("EXPORT TO WORKSPACE" -> "Export to workspace")
        return fit(m.group(0).capitalize(), NAME_CAP)
    m = _QUOTED.search(head)
    if m:
        return fit(m.group(m.lastindex or 0), NAME_CAP)
    words = head.split(",")[0].split()  # the clause before the first comma names the work
    while words and words[0].lower().strip(",.:;") in _LEAD_INS:
        words.pop(0)
    words = [w for w in words if w.lower().strip(",.:;") not in _ARTICLES] or words
    kept: list[str] = []
    for w in words:
        if vlen(" ".join(kept + [w])) > NAME_CAP:
            break
        kept.append(w)
    while len(kept) > 1 and kept[-1].lower().strip(",.:;") in _STOPWORDS:
        kept.pop()  # never end on a stopword: "Extend AI" rather than "Extend AI of"
    if not kept and words:
        return fit(words[0], NAME_CAP)
    return " ".join(kept) or "job"


def job_name(store, job, persist: bool = True) -> str:
    """The job's short name: kv `short_name`, else derived once (deduped against the other jobs' names) and stored."""
    name = store.kv_get(job.job_id, NAME_KEY)
    if name:
        return name
    parent = None
    m = _FOLLOW.match(job.goal or "")
    if m:
        parent_job = next((j for j in store.list_jobs() if j.job_id.startswith(m.group(1))), None)
        parent = store.kv_get(parent_job.job_id, NAME_KEY) if parent_job else None
    name = derive_name(job.goal, parent)
    taken = {other for jid, other in store.kv_rows(NAME_KEY) if jid != job.job_id}  # one query, not one per job
    base, n = name, 2
    while name in taken:
        suffix = f" {n}"
        name, n = fit(base, NAME_CAP - len(suffix)) + suffix, n + 1
    if persist:
        store.kv_set(job.job_id, NAME_KEY, name)
    return name


def status_glyph(state: str, kind: str, reason: str | None = None, eta: dict | None = None) -> str:
    """Precedence: needs you > stuck/overdue > running > paused > accepted > failed."""
    if reason or kind == "waiting":
        return NEEDS_YOU
    if kind == "quiet" or (eta or {}).get("overdue_min"):
        return STUCK
    if kind == "live":
        return RUNNING
    if kind == "paused":
        return PAUSED
    if kind == "done" or state == "ACCEPTED":
        return ACCEPTED
    return FAILED


def _minutes(m: int) -> str:
    m = max(int(m), 0)
    if m >= 60:
        m = round(m / 10) * 10
        return f"{m // 60}h{m % 60:02d}"
    return f"{max(round(m / 5) * 5, 5)}m"


def eta_text(eta: dict | None) -> str:
    """Phase 18 ETA display: overdue `+12m`, P50 countdown `~1h40`/`~45m`, range-only `2–5h`."""
    if not eta:
        return ""
    if eta.get("tab"):   # the finishing tail: `fin`, or `+12m` once past its P90
        return str(eta["tab"])
    if eta.get("overdue_min"):
        return f"+{int(eta['overdue_min'])}m"
    if eta.get("p50_min") is not None:
        return "~" + _minutes(eta["p50_min"])
    if eta.get("low_h") is not None and eta.get("high_h") is not None:
        return f"{eta['low_h']:g}–{eta['high_h']:g}h"
    return ""


def info_text(waiting: int = 0, reason: str | None = None, eta: dict | None = None, kind: str = "",
              quiet_age: str = "", units: str = "") -> str:
    """The one live fact, first non-empty wins, at most INFO_CAP cells."""
    if waiting:
        out = f"?{waiting}"
    elif reason:
        out = "ok?" if reason.startswith(("accept", "approve")) else ""
    elif kind == "live" and eta_text(eta):
        out = eta_text(eta)
    elif kind == "live":
        out = units
    elif kind == "quiet":
        out = eta_text(eta) if (eta or {}).get("overdue_min") else quiet_age
    else:
        out = ""
    return out if vlen(out) <= INFO_CAP else ""


def tab_label(glyph: str, name: str, info: str = "") -> str:
    """`<glyph> <name>[ <info>]` within LABEL_CAP cells (glyph budgeted as two); info is dropped first."""
    name = fit(" ".join(name.split()), NAME_CAP)
    if info and GLYPH_CELLS + 1 + vlen(name) + 1 + vlen(info) > LABEL_CAP:
        info = ""
    name = fit(name, LABEL_CAP - GLYPH_CELLS - 1)
    return f"{glyph} {name}" + (f" {info}" if info else "")


def parse_label(label: str) -> str:
    """The short name inside a label we (or the owner) wrote: strips the leading glyph and a trailing live fact."""
    text = " ".join((label or "").split())
    for g in GLYPHS:
        if text.startswith(g + " "):
            text = text[len(g) + 1:]
            break
    parts = text.split(" ")
    if len(parts) > 1 and _INFO.match(parts[-1]):
        parts.pop()
    return " ".join(parts)
