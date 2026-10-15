"""The completion ruling's validation (Steward v2.7, SRD-06/07): pure, no model, no store access.

The tech lead's `rule_completion` answer is a *claim*; code decides what survives. Every evidence ref must resolve
(a check row for this job at the verified SHA, an event that exists, a path in the candidate diff), the ruling's SHA must
equal the SHA that was verified, and a `finished` ruling needs at least one valid ref for every `done_when` item.
Anything else becomes `unverified` with the reasons recorded. Callers supply the sets (or a predicate for event seqs);
`diff_base` / `diff_paths` are the only helpers that touch git, through an injected runner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

KEY = "completion_ruling"
RULINGS = ("finished", "not_met", "unverified")
MAX_REFS_PER_ITEM = 20
_REF_RE = re.compile(r"^(check|event|path):(.+)$", re.S)
_ITEM_RE = re.compile(r"^o(\d+)$")


@dataclass(frozen=True)
class Validated:
    ruling: str                     # what code accepts: finished | not_met | unverified
    claimed: str                    # what the lead said
    reasons: tuple = ()
    instructions: str = ""
    evidence: tuple = field(default=())  # normalized: ({"item": 0-based int, "refs": [...]}, ...)
    sha: str = ""                   # the SHA the lead's ruling named

    def to_dict(self) -> dict:
        return {"ruling": self.ruling, "claimed": self.claimed, "reasons": list(self.reasons), "sha": self.sha,
                "instructions": self.instructions, "evidence": [dict(e) for e in self.evidence]}


def _item_index(item) -> int | None:
    """0-based `done_when` index: an int (0-based) or an `oN` outcome label (1-based, `outcome_scope`)."""
    if isinstance(item, bool):
        return None
    if isinstance(item, int):
        return item if item >= 0 else None
    m = _ITEM_RE.match(str(item or "").strip())
    return int(m.group(1)) - 1 if m and int(m.group(1)) >= 1 else None


def _resolves(ref, check_ids, event_seqs, diff_paths) -> str | None:
    """None when the ref resolves, else why not."""
    if not isinstance(ref, str):
        return f"ref {ref!r} is not a string"
    m = _REF_RE.match(ref.strip())
    if not m or not m.group(2).strip():
        return f"ref {ref[:60]!r} is not check:<id>, event:<seq> or path:<diff path>"
    kind, val = m.group(1), m.group(2).strip()
    if kind == "check":
        # `check:<n>` is the packet's check_rows id. Also accepted: `check:verify` (the verify row at this SHA) and
        # `check:<k>` for the k-th verify row at this SHA (a lead that numbers only the rows it was shown).
        ids = list(check_ids)
        if f"check:{val}" in ids or (ids and val.lower() == "verify") or (val.isdigit() and 1 <= int(val) <= len(ids)):
            return None
        return f"check:{val[:40]} is not a verification row at this SHA"
    if kind == "event":
        if not val.isdigit():
            return f"event:{val[:40]} is not an event sequence number"
        seq = int(val)
        ok = event_seqs(seq) if callable(event_seqs) else seq in event_seqs
        return None if ok else f"event:{seq} does not exist for this job"
    return None if val in diff_paths else f"path:{val[:80]} is not in the candidate diff"


def validate(params: dict, *, sha: str, verification_sha: str | None, done_when_count: int, check_ids,
             event_seqs, diff_paths) -> Validated:
    """The accepted ruling for one `rule_completion` answer. `sha` is the candidate SHA code requested a ruling for."""
    claimed = str((params or {}).get("ruling") or "")
    instructions = str((params or {}).get("instructions") or "").strip()
    reasons: list[str] = []
    if claimed not in RULINGS:
        return Validated("unverified", claimed or "?", (f"unknown ruling {claimed[:40]!r}",), instructions,
                         sha=str((params or {}).get("sha") or ""))
    if str(params.get("sha") or "") != sha or (verification_sha is not None and verification_sha != sha):
        reasons.append(f"sha mismatch: ruling {str(params.get('sha'))[:12]}, candidate {sha[:12]}, "
                       f"verified {str(verification_sha)[:12]}")
    covered: dict[int, list[str]] = {}
    evidence = params.get("evidence") or []
    if not isinstance(evidence, list):
        reasons.append("evidence is not a list")
        evidence = []
    for ent in evidence:
        if not isinstance(ent, dict):
            reasons.append("an evidence entry is not an object")
            continue
        idx = _item_index(ent.get("item"))
        refs = ent.get("refs")
        if idx is None or (done_when_count and idx >= done_when_count):
            reasons.append(f"evidence item {str(ent.get('item'))[:20]!r} is not a done_when index")
            continue
        if not isinstance(refs, list) or len(refs) > MAX_REFS_PER_ITEM:
            reasons.append(f"done_when {idx}: refs must be a list of at most {MAX_REFS_PER_ITEM}")
            continue
        for ref in refs:
            bad = _resolves(ref, check_ids, event_seqs, diff_paths)
            if bad:
                reasons.append(f"done_when {idx}: {bad}")
            else:
                covered.setdefault(idx, []).append(ref.strip())
    if claimed == "finished":
        for i in range(max(done_when_count, 0)):
            if not covered.get(i):
                reasons.append(f"done_when {i} has no valid evidence ref")
    norm = tuple({"item": i, "refs": r} for i, r in sorted(covered.items()))
    final = "unverified" if reasons else claimed
    return Validated(final, claimed, tuple(reasons[:12]), instructions, norm, str(params.get("sha") or ""))


def diff_base(store, job_id: str, runner) -> str | None:
    """What the candidate diff is measured against: the recorded baseline commit, else the merge-base of the worktree
    HEAD with the source repo's HEAD (the runner's `_diff_base_sha`)."""
    baseline = store.get_checks(job_id, label="baseline")
    if baseline:
        return baseline[0]["commit_sha"]
    job = store.get_job(job_id)
    if job is None or runner is None:
        return None
    wt = store.home / "worktrees" / job_id
    try:
        src = runner.run(["git", "rev-parse", "HEAD"], cwd=job.repo)
        head = src.stdout.strip() if src.exit_code == 0 else ""
        if not head:
            return None
        mb = runner.run(["git", "merge-base", head, "HEAD"], cwd=wt)
    except OSError:
        return None
    return (mb.stdout.strip() or None) if mb.exit_code == 0 else None


def diff_paths(store, job_id: str, runner, sha: str) -> set[str] | None:
    """`git diff --name-only base..sha` for the candidate, or None when the base/diff is unknown (path refs then fail
    closed)."""
    base = diff_base(store, job_id, runner)
    if not base or runner is None:
        return None
    try:
        res = runner.run(["git", "diff", "--name-only", f"{base}..{sha}"], cwd=Path(store.home / "worktrees" / job_id))
    except OSError:
        return None
    return {p.strip() for p in res.stdout.splitlines() if p.strip()} if res.exit_code == 0 else None


def check_ids(store, job_id: str, sha: str) -> dict[str, dict]:
    """`{check:<n>: row}` for the verify rows of this job at `sha`; <n> is the 1-based position among the job's check
    rows (append-only, so stable)."""
    return {f"check:{i}": c for i, c in enumerate(store.get_checks(job_id), start=1)
            if c.get("label") == "verify" and c.get("commit_sha") == sha}


def event_exists(store, job_id: str) -> Callable[[int], bool]:
    seqs = {e.seq for e in store.events(job_id)}
    return lambda seq: seq in seqs
