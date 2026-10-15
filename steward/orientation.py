"""Orientation, not plans (v2.6, PRD D-09, SUP-10): the brief every tech-lead review starts from.

A tech lead must know what the system is orienting toward at goal/milestone level without being fed whole plans.
The brief carries: the goal, the finish line, the contract (done_when, boundaries, authority) and the hard-floor
summary, the milestone goal, one line per phase with its status, and the current phase's must_have truths. It is
<= MAX_TOKENS (~4 chars per token), clipped deterministically (lowest-priority sections shrink first; the goal and
the floor never go), and cached in kv: rebuilt only when the plan files' hash or the contract's hash changes.

Anything deeper is pulled, never pushed: `drill_down(worktree, "34-01#tasks")` returns one named section of one
plan (or `ROADMAP#Phase 35`), bounded; `full_read(...)` returns whole files only in a takeover, scoped to the files
it touches.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from steward.models import Judgment
from steward.store import Store

MAX_TOKENS = 1500
CHARS_PER_TOKEN = 4
MAX_CHARS = MAX_TOKENS * CHARS_PER_TOKEN
CACHE_KEY = "orientation_brief"
DRILL_DEFAULT_CHARS = 2000

_PHASE_RE = re.compile(r"^###\s+Phase\s+(\d+(?:\.\d+)?)\s*:\s*(.+?)\s*$", re.M)
_MILESTONE_RE = re.compile(r"^#{1,2}\s+(Milestone\s+v[\d.]+\b.*?)\s*$", re.M)
_PLAN_LINE_RE = re.compile(r"^-\s+\[([ xX])\]\s+(\d+(?:\.\d+)?-\d+)-PLAN\.md", re.M)


class OrientationError(ValueError):
    pass


def tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _planning(worktree: Path) -> Path:
    return Path(worktree) / ".planning"


def plan_files(worktree: Path) -> list[Path]:
    root = _planning(worktree)
    if not root.is_dir():
        return []
    files = [root / "ROADMAP.md"] if (root / "ROADMAP.md").is_file() else []
    files += sorted(root.glob("phases/*/*-PLAN.md")) + sorted(root.glob("phases/*/*-SUMMARY.md"))
    return files


def _hash_files(paths: list[Path], root: Path) -> str:
    h = hashlib.sha256()
    for p in paths:
        try:
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
        except (OSError, ValueError):
            continue
    return h.hexdigest()[:16]


def _contract(store: Store, job_id: str):
    from steward.contract import load_contract

    return load_contract(store, job_id)


def hashes(store: Store, job_id: str, worktree: Path) -> tuple[str, str]:
    c = _contract(store, job_id)
    chash = hashlib.sha256(json.dumps(c.__dict__ if c else {}, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return _hash_files(plan_files(worktree), Path(worktree)), chash


def _last_milestone(roadmap: str) -> tuple[str, str, str]:
    """(heading, goal line, body) of the LAST milestone section in the roadmap."""
    ms = list(_MILESTONE_RE.finditer(roadmap))
    if not ms:
        return "", "", roadmap
    m = ms[-1]
    body = roadmap[m.end():]
    heading = re.sub(r"\s*\(append to[^)]*\)", "", m.group(1)).strip()
    goal = next((ln.strip() for ln in body.splitlines()
                 if ln.strip() and not ln.startswith(("|", "#", "-")) and "Source:" not in ln), "")
    return heading, goal, body


def phases(worktree: Path) -> list[dict]:
    """The last milestone's phases: `{num, name, goal, plans: [(id, checked)], status}` (done/current/pending)."""
    roadmap_path = _planning(worktree) / "ROADMAP.md"
    if not roadmap_path.is_file():
        return []
    _heading, _goal, body = _last_milestone(roadmap_path.read_text())
    out = []
    hits = list(_PHASE_RE.finditer(body))
    for i, m in enumerate(hits):
        section = body[m.end(): hits[i + 1].start() if i + 1 < len(hits) else len(body)]
        goal = re.search(r"\*\*Goal:?\*\*:?\s*(.+)", section)
        plans = [(p.group(2), p.group(1).lower() == "x") for p in _PLAN_LINE_RE.finditer(section)]
        out.append({"num": m.group(1), "name": m.group(2), "goal": goal.group(1).strip() if goal else "",
                    "plans": plans})
    current_set = False
    for ph in out:
        done = bool(ph["plans"]) and all(checked or _summary_exists(worktree, pid) for pid, checked in ph["plans"])
        if done:
            ph["status"] = "done"
        elif not current_set:
            ph["status"], current_set = "current", True
        else:
            ph["status"] = "pending"
    return out


def _summary_exists(worktree: Path, plan_id: str) -> bool:
    return any(_planning(worktree).glob(f"phases/*/{plan_id}-SUMMARY.md"))


def _plan_path(worktree: Path, plan_id: str) -> Path | None:
    if not re.fullmatch(r"\d+(?:\.\d+)?-\d+", plan_id or ""):
        return None
    hits = sorted(_planning(worktree).glob(f"phases/*/{plan_id}-PLAN.md"))
    return hits[0] if hits else None


def must_have_truths(text: str) -> list[str]:
    m = re.search(r"^must_haves:\s*\n\s+truths:\s*\n((?:\s+-\s+.*\n?)+)", text, re.M)
    if not m:
        return []
    return [ln.strip()[1:].strip().strip('"').strip("'") for ln in m.group(1).splitlines() if ln.strip()]


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


def build(store: Store, job_id: str, worktree: Path) -> dict:
    """Build the brief (no cache). Raises OrientationError when the job has no goal or no contract."""
    from steward import stability, tech_lead_floor

    job = store.get_job(job_id)
    contract = _contract(store, job_id)
    goal = (contract.goal if contract and contract.goal else (job.goal if job else "")).strip()
    if not goal or contract is None:
        raise OrientationError("an orientation brief needs the job's goal and contract")
    worktree = Path(worktree)
    finish = stability.prose_finish_line(goal)
    check = stability.finish_check_command(goal, worktree) if worktree.is_dir() else None
    sections: list[tuple[str, str, int]] = []  # (title, body, priority: lower = clipped later)
    sections.append(("Goal", _clip(" ".join(goal.split()), 900), 0))
    sections.append(("Finish line", (finish or "none stated") + (f" (check: `{check}`)" if check else ""), 0))
    sections.append(("Hard floor", tech_lead_floor.SUMMARY, 0))
    contract_lines = [f"done_when: {'; '.join(contract.done_when) or '-'}",
                      f"boundaries: {'; '.join(contract.boundaries) or '-'}",
                      f"authority: {'; '.join(contract.authority) or '-'}"]
    keys = getattr(contract, "keys", None) or []
    if keys:
        contract_lines.append(f"granted keys: {', '.join(keys)}")
    sections.append(("Contract", "\n".join(contract_lines), 2))
    roadmap = _planning(worktree) / "ROADMAP.md"
    if roadmap.is_file():
        heading, mgoal, _body = _last_milestone(roadmap.read_text())
        sections.append(("Milestone", f"{heading}" + (f" — {mgoal}" if mgoal else ""), 1))
        phs = phases(worktree)
        sections.append(("Phases", "\n".join(f"{p['num']} {p['name']}: {p['status']}" +
                                              (f" — {p['goal']}" if p["goal"] else "") for p in phs) or "-", 3))
        cur = next((p for p in phs if p["status"] == "current"), None)
        truths: list[str] = []
        for pid, _checked in (cur or {}).get("plans", []):
            path = _plan_path(worktree, pid)
            if path is not None:
                truths += [f"[{pid}] {t}" for t in must_have_truths(path.read_text())]
        if cur:
            sections.append((f"Current phase {cur['num']} must_have truths", "\n".join(f"- {t}" for t in truths)
                             or "- (none recorded)", 1))
    else:
        sections.append(("Milestone", "no .planning roadmap in this worktree: orient by the goal and contract", 1))

    def render(secs) -> str:
        return "\n".join(f"## {t}\n{b}" for t, b, _ in secs)

    text = render(sections)
    # deterministic clipping: shrink the lowest-priority (highest number) sections first, halving until it fits
    while len(text) > MAX_CHARS:
        idx = max(range(len(sections)), key=lambda i: (sections[i][2], len(sections[i][1])))
        t, b, pr = sections[idx]
        if pr == 0 or len(b) <= 80:
            break
        sections[idx] = (t, _clip(b, max(80, len(b) // 2)), pr)
        text = render(sections)
    if len(text) > MAX_CHARS:
        text = _clip(text, MAX_CHARS)
    plan_hash, contract_hash = hashes(store, job_id, worktree)
    return {"text": text, "tokens": tokens(text), "plan_hash": plan_hash, "contract_hash": contract_hash,
            "built_at": time.time()}


def brief(store: Store, job_id: str, worktree: Path) -> dict:
    """The cached brief; rebuilt (and recorded) only when the plan or contract hash changed."""
    plan_hash, contract_hash = hashes(store, job_id, Path(worktree))
    cached = store.kv_get(job_id, CACHE_KEY)
    if isinstance(cached, dict) and cached.get("plan_hash") == plan_hash and cached.get("contract_hash") == contract_hash:
        return {**cached, "cached": True}
    b = build(store, job_id, Path(worktree))
    store.kv_set(job_id, CACHE_KEY, b)
    store.append_event(job_id, what="orientation_brief_built",
                       evidence={"tokens": b["tokens"], "plan_hash": plan_hash, "contract_hash": contract_hash},
                       rule="orientation_brief_cache", judgment=Judgment.code)
    return {**b, "cached": False}


def drill_down(worktree: Path, ref: str, *, max_chars: int = DRILL_DEFAULT_CHARS) -> str:
    """One named section, bounded: `<plan>#tasks|must_haves|objective|threat_model|context`, `<plan>` (clipped),
    or `ROADMAP#Phase <n>`."""
    worktree = Path(worktree)
    ref = (ref or "").strip()
    name, _, section = ref.partition("#")
    if name.upper() == "ROADMAP":
        roadmap = _planning(worktree) / "ROADMAP.md"
        m = re.match(r"Phase\s+(\d+(?:\.\d+)?)$", section.strip(), re.I)
        if not roadmap.is_file() or not m:
            return f"no such plan section: {ref}"
        text = roadmap.read_text()
        hits = [h for h in _PHASE_RE.finditer(text)]
        for i, h in enumerate(hits):
            if h.group(1) == m.group(1):
                end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
                return _clip(text[h.start():end].strip(), max_chars)
        return f"no such plan section: {ref}"
    path = _plan_path(worktree, name)
    if path is None:
        return f"no such plan section: {ref}"
    text = path.read_text()
    if not section:
        return _clip(text, max_chars)
    if section == "must_haves":
        m = re.search(r"^must_haves:.*?(?=^---|\Z)", text, re.M | re.S)
        return _clip(m.group(0).strip(), max_chars) if m else f"no such plan section: {ref}"
    m = re.search(rf"<{re.escape(section)}>(.*?)</{re.escape(section)}>", text, re.S)
    return _clip(m.group(1).strip(), max_chars) if m else f"no such plan section: {ref}"


def full_read(worktree: Path, paths: list[str], *, mode: str, max_chars_each: int = 40_000) -> dict[str, str]:
    """Whole files, ONLY in a takeover, and only inside the worktree (the files it touches)."""
    if mode != "takeover":
        raise OrientationError("full reads happen only on takeover")
    worktree = Path(worktree).resolve()
    out: dict[str, str] = {}
    for p in paths:
        target = (worktree / p).resolve()
        try:
            target.relative_to(worktree)
        except ValueError:
            raise OrientationError(f"{p} is outside the worktree") from None
        out[p] = target.read_text()[:max_chars_each] if target.is_file() else ""
    return out
