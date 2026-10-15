"""The question queue, detection layer (Phase 17, G1/G2/G10/G13/G14/G15).

Steward finds the design questions a job COMMITTED, turns each into a stored record and keeps those records honest as
the files change. Nothing here answers, delivers or displays a question.

- `scan` reads only committed content: `git diff --raw <base> <head>` for the changed files and `git show <head>:<path>`
  for their text. It never looks at the dirty tree, never calls a model or the network, and never raises.
- Records live in the job kv (`questions`), the approvals kv-list pattern; no schema change.
- `reconcile` merges what the adapters found with the stored records by content hash (G10).
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from steward import owner
from steward import store as store_module
from steward.models import Judgment, Receipt
from steward.question_adapters import ADAPTERS, adapter_named
from steward.question_adapters.base import (
    Change, Edit, FileReader, Found, basename, digest, dirname, make_edit, one_line,
)
from steward.question_adapters.steward_questions import ANSWERS_FILE, QUESTIONS_FILE
from steward.runner_exec import Runner, SubprocessRunner
from steward.sendback import cap_text
from steward.store import Store

KV_KEY = "questions"
SCANNED_HEAD_KEY = "questions_scanned_head"
QUEUE_HASH_KEY = "questions_queue_hash"
LAST_ERROR_KEY = "questions_last_error"

_GREP_CHUNK = 200

WAITING = ("open", "stale")
ROUND_SIZE = 4
NOTE_CAP = 500
OTHER_CAP = 300


# -- persistence --------------------------------------------------------------------


def load(store: Store, job_id: str) -> list[dict]:
    return store.kv_get(job_id, KV_KEY, default=[])


def save(store: Store, job_id: str, records: list[dict]) -> None:
    store.kv_set(job_id, KV_KEY, records)


# -- views ----------------------------------------------------------------------------


def summary(records: list[dict]) -> dict:
    counts = {"open": 0, "stale": 0, "deferred": 0, "answered": 0, "withdrawn": 0}
    starred = blocking = 0
    for r in records:
        st = r.get("status")
        if st in counts:
            counts[st] += 1
        if st == "open":
            if r.get("starred"):
                starred += 1
            if r.get("blocking"):
                blocking += 1
    waiting = counts["open"] + counts["stale"]
    return {"waiting": waiting, **counts, "starred": starred, "needs_you": waiting - starred, "blocking": blocking}


_RANK = {"open": 1, "stale": 2, "deferred": 3, "answered": 4, "withdrawn": 5}


def ordered(records: list[dict]) -> list[dict]:
    """Blocking questions first, then source order (file, line); stale and deferred after the open ones."""

    def key(r: dict):
        rank = 0 if (r.get("status") == "open" and r.get("blocking")) else _RANK.get(r.get("status"), 6)
        src = r.get("source") or {}
        return (rank, src.get("file") or "", src.get("line") or 0, r.get("qid") or "")

    return sorted(records, key=key)


_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def pane_summary(store: Store, job_id: str) -> dict:
    """What the status pane needs, from the kv list only (no git, no events): the `summary` counts plus the anchors of
    questions the WORKER decided (G3/G10: only the owner decides, so those are flagged, never counted as waiting).
    Anchors lose control characters before they are drawn (T-17-23)."""
    records = load(store, job_id)
    worker = [_CTRL.sub("", (r.get("source") or {}).get("anchor") or r.get("qid") or "")
              for r in ordered(records)
              if r.get("status") == "answered" and (r.get("flags") or {}).get("answered_by_worker")]
    return {**summary(records), "worker_decided": worker}


# A "discussion job" is one whose deliverable IS the questions (G11). Deterministic: a false positive only relabels a
# button and adds a warning line.
_deliverable_cache: dict = {}


def _deliverable_re() -> "re.Pattern[str]":
    """Built from the owner names at first use (configured plus legacy) and cached by them (G7)."""
    names = owner.parser_names()
    hit = _deliverable_cache.get(names)
    if hit is None:
        forms = {w for n in names for w in (n, n[4:] if n.lower().startswith("the ") else n)}
        who = "(?:" + "|".join(r"\s+".join(re.escape(p) for p in n.split()) for n in sorted(forms)) + ")"
        hit = re.compile(
            rf"\b(?:discuss(?:ion)?|gray\s+areas?|open\s+for\s+{who}|(?:open|design)\s+questions?|questions?\s+for\s+{who})\b",
            re.IGNORECASE)
        _deliverable_cache.clear()
        _deliverable_cache[names] = hit
    return hit


def is_deliverable(store: Store, job_id: str) -> bool:
    """True when the job's goal or its contract's done_when says the questions are the deliverable."""
    from steward.contract import load_contract

    job = store.get_job(job_id)
    texts = [job.goal if job else ""]
    try:
        contract = load_contract(store, job_id)
    except Exception:  # noqa: BLE001 - an unreadable contract only means the goal alone decides
        contract = None
    if contract is not None:
        texts += [str(x) for x in contract.done_when]
    pattern = _deliverable_re()
    return any(pattern.search(t or "") for t in texts)


def total_waiting(store: Store, job_ids) -> int:
    """Questions waiting on the owner across `job_ids` (the ALL JOBS count); a job whose queue cannot be read counts 0."""
    total = 0
    for jid in job_ids:
        try:
            total += summary(load(store, jid))["waiting"]
        except Exception:  # noqa: BLE001
            pass
    return total


def source_unwritten(store: Store, job_id: str) -> list[str]:
    """Answers of a merged job that live only in the store (G6): the qids still to write onto the source branch."""
    got = store.kv_get(job_id, "questions_source_unwritten", default=[])
    return list(got) if isinstance(got, list) else []


def merged_branch(store: Store, job_id: str) -> str:
    """The branch the job was merged into: the last `incorporated` event's source_branch, else ''."""
    ev = store.last_event(job_id, ("incorporated",))
    return str((ev.evidence or {}).get("source_branch") or "") if ev is not None else ""


NOTHING_TO_ACCEPT = "nothing to accept"


def review_line(records: list[dict]) -> str:
    """The accept-all review (G4): what a single accept would take and what still needs the owner. Only OPEN starred cards
    are accepted; stale cards and unstarred ones need him."""
    accepted = sum(1 for r in records if r.get("status") == "open" and r.get("starred"))
    needs = [r for r in ordered(records) if r.get("status") in WAITING and not (r.get("status") == "open" and r.get("starred"))]
    if not accepted:
        return NOTHING_TO_ACCEPT
    line = f"accept {accepted} recommended"
    if needs:
        anchors = [(r.get("source") or {}).get("anchor") or r["qid"] for r in needs]
        shown = anchors[:3] + ([f"+{len(anchors) - 3}"] if len(anchors) > 3 else [])
        line += f", {len(needs)} needs you ({', '.join(shown)})"
    return line


def queue_hash(records: list[dict]) -> str:
    """Identity of the waiting queue: only the SET of waiting qids, so a rescan that finds nothing new is silent."""
    return digest("\n".join(sorted(r["qid"] for r in records if r.get("status") in WAITING)))


@dataclass
class ScanResult:
    skipped: str | None = None
    error: str | None = None
    head: str = ""
    base: str = ""
    found: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)
    worker_decided: list[str] = field(default_factory=list)
    queue_hash: str = ""
    queue_changed: bool = False
    summary: dict = field(default_factory=dict)


@dataclass
class Reconciled:
    records: list[dict]
    found: dict[tuple[str, str], list[str]] = field(default_factory=dict)  # (adapter, file) -> new qids
    updated: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)
    worker_decided: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    events: list[tuple[str, dict, str, Judgment]] = field(default_factory=list)


# -- reconcile --------------------------------------------------------------------------


def _key(adapter: str, file: str, anchor: str) -> tuple[str, str, str]:
    return (adapter, file, anchor)


def _record_key(r: dict) -> tuple[str, str, str]:
    src = r.get("source") or {}
    return _key(r.get("adapter", ""), src.get("file", ""), src.get("anchor", ""))


def _new_record(f: Found, qid: str, head: str, now: str) -> dict:
    return {
        "qid": qid,
        "adapter": f.adapter,
        "title": f.title,
        "question": f.question,
        "options": [dict(o) for o in f.options],
        "recommended": list(f.recommended),
        "recommended_text": f.recommended_text,
        "starred": bool(f.starred),
        "rationale": f.rationale,
        "evidence": list(f.evidence),
        "blocking": bool(f.blocking),
        "blocking_reason": f.blocking_reason,
        "source": {"file": f.file, "anchor": f.anchor, "commit": head, "hash": f.hash, "line": f.line},
        "status": "open",
        "answer": None,
        "flags": {"updated": False, "answered_by_worker": False, "unparsed": bool(f.unparsed)},
        "first_seen_at": now,
        "updated_at": now,
        "written": None,
        "history": [],
    }


def _assign_qids(found: list[Found], existing: list[dict]) -> None:
    """qid = <adapter>:<file>:<anchor> with file = the basename, unless two files share basename AND anchor (then the
    repo-relative path, for every colliding file). Existing records keep their qid."""
    groups: dict[tuple[str, str, str], set[str]] = {}
    for f in found:
        groups.setdefault((f.adapter, basename(f.file), f.anchor), set()).add(f.file)
    taken: dict[str, str] = {r["qid"]: (r.get("source") or {}).get("file", "") for r in existing}
    for f in found:
        short = f"{f.adapter}:{basename(f.file)}:{f.anchor}"
        collide = len(groups[(f.adapter, basename(f.file), f.anchor)]) > 1 or (
            short in taken and taken[short] != f.file
        )
        f.qid = f"{f.adapter}:{f.file}:{f.anchor}" if collide else short
        taken.setdefault(f.qid, f.file)


def _marker_is_owner(marker: dict) -> bool:
    return owner.is_owner_actor(marker.get("who"))


def _apply(rec: dict, f: Found, head: str, now: str) -> None:
    """Replace the card content with what the file says now."""
    rec.update(title=f.title, question=f.question, options=[dict(o) for o in f.options],
               recommended=list(f.recommended), recommended_text=f.recommended_text, starred=bool(f.starred),
               rationale=f.rationale, evidence=list(f.evidence), blocking=bool(f.blocking),
               blocking_reason=f.blocking_reason, updated_at=now)
    rec["source"] = {**rec["source"], "line": f.line, "hash": f.hash, "commit": head}


def _move(rec: dict, to: str, why: str, now: str, **extra) -> None:
    rec["history"].append({"at": now, "from": rec["status"], "to": to, "why": why, **extra})
    rec["status"] = to
    rec["updated_at"] = now


def _worker_answer(rec: dict, marker: dict, f: Found, now: str) -> None:
    """A DECIDED/DEFERRED marker nobody at Steward answered: recorded as the worker's, never as the owner's (T-17-01)."""
    text = marker.get("text") or ""
    rec["answer"] = {"option": "as_written", "note": text, "by": "worker", "origin": "file", "at": now,
                     "date": marker.get("date", ""), "recommended": text.strip().lower() == "recommended",
                     "area_hash": f.hash, "marker_who": marker.get("who", "")}
    rec["flags"]["answered_by_worker"] = True


def reconcile(
    existing: list[dict],
    found_by_file: dict[str, list[Found]],
    deleted_files: set[str] | list[str],
    head: str,
    now: str,
) -> Reconciled:
    """Merge what the adapters found in the scanned files with the stored records, by content hash (G10). Pure.

    Decision table per found area p against its stored record e (matched by adapter, file and anchor); every
    comparison is a hash or a marker state, no model:
      no e:            open marker -> new open record; DECIDED/DEFERRED by the owner -> nothing (already decided);
                       by anyone else -> answered/deferred by "worker", flagged.
      e open:          same hash -> unchanged; changed -> updated in place; a DECIDED/DEFERRED marker -> answered or
                       deferred by "worker", flagged (a marker in a job commit proves nothing about who wrote it).
      e answered/deferred: same hash -> unchanged (an owner marker just records `written`); changed -> stale.
      e stale:         only the hashes move.  e withdrawn: an open marker reopens it.
      e without a p in a scanned or deleted file: withdrawn.
    """
    out = Reconciled(records=copy.deepcopy(existing))
    by_key = {_record_key(r): r for r in out.records}
    all_found = [f for fl in found_by_file.values() for f in fl]
    _assign_qids([f for f in all_found if _key(f.adapter, f.file, f.anchor) not in by_key], out.records)
    ev = out.events
    seen: set[tuple[str, str, str]] = set()

    def note(what: str, evidence: dict, rule: str = "questions_reconcile") -> None:
        ev.append((what, evidence, rule, Judgment.code))

    for f in all_found:
        key = _key(f.adapter, f.file, f.anchor)
        seen.add(key)
        e = by_key.get(key)
        mk = f.marker
        if e is None:
            if mk is None:
                rec = _new_record(f, f.qid, head, now)
                out.records.append(rec)
                by_key[key] = rec
                out.found.setdefault((f.adapter, f.file), []).append(rec["qid"])
            elif not _marker_is_owner(mk):
                rec = _new_record(f, f.qid, head, now)
                out.records.append(rec)
                by_key[key] = rec
                _worker_answer(rec, mk, f, now)
                _move(rec, "answered" if mk["state"] == "decided" else "deferred", "marker written by " + mk["who"], now)
                out.worker_decided.append(rec["qid"])
                note("question_worker_decided", {"qid": rec["qid"], "state": mk["state"], "marker_who": mk["who"],
                                                  "date": mk.get("date", ""), "text": mk.get("text", "")})
            continue

        old_hash = e["source"].get("hash", "")
        status = e["status"]
        if status == "open":
            if mk is not None:
                _apply(e, f, head, now)
                _worker_answer(e, mk, f, now)
                _move(e, "answered" if mk["state"] == "decided" else "deferred", "marker written by " + mk["who"], now)
                out.worker_decided.append(e["qid"])
                note("question_worker_decided", {"qid": e["qid"], "state": mk["state"], "marker_who": mk["who"],
                                                  "date": mk.get("date", ""), "text": mk.get("text", "")})
            elif f.hash != old_hash:
                _apply(e, f, head, now)
                e["flags"]["updated"] = True
                out.updated.append(e["qid"])
                note("question_updated", {"qid": e["qid"], "old_hash": old_hash, "new_hash": f.hash,
                                          **({"blocking": True} if e.get("blocking") else {})})
            elif f.line != e["source"].get("line"):
                e["source"] = {**e["source"], "line": f.line, "commit": head}
        elif status in ("answered", "deferred"):
            if f.hash != old_hash:
                e["stale_from"] = {"hash": old_hash, "answer": e.get("answer"), "to_hash": f.hash}
                _apply(e, f, head, now)
                _move(e, "stale", "the area changed after it was answered", now)
                out.stale.append(e["qid"])
                note("question_stale", {"qid": e["qid"], "old_hash": old_hash, "new_hash": f.hash})
            else:
                w = e.get("written")
                if mk is not None and _marker_is_owner(mk) and not w:
                    e["written"] = {"via": "scan", "commit": head, "at": now}
                    e["updated_at"] = now
                    out.written.append(e["qid"])
                elif (mk is not None and _marker_is_owner(mk) and w.get("via") in ("steering", "send_back")
                      and not w.get("confirmed_at")):
                    # the worker applied the steered patch: the marker is in its commit now (17-03, G6)
                    w["confirmed_at"] = now
                    w["commit"] = head
                    e["updated_at"] = now
                    out.confirmed.append(e["qid"])
                if f.line != e["source"].get("line"):
                    e["source"] = {**e["source"], "line": f.line, "commit": head}
        elif status == "stale":
            if f.hash != old_hash:
                _apply(e, f, head, now)
                if isinstance(e.get("stale_from"), dict):
                    e["stale_from"]["to_hash"] = f.hash
        elif status == "withdrawn" and mk is None:
            e["answer"] = None
            e["written"] = None
            e.pop("stale_from", None)
            e["flags"] = {"updated": False, "answered_by_worker": False, "unparsed": bool(f.unparsed)}
            _apply(e, f, head, now)
            _move(e, "open", "the marker reappeared", now)
            out.found.setdefault((f.adapter, f.file), []).append(e["qid"])

    gone = set(found_by_file) | set(deleted_files)
    for rec in out.records:
        key = _record_key(rec)
        if key in seen or rec["source"].get("file") not in gone or rec["status"] == "withdrawn":
            continue
        _move(rec, "withdrawn", "the marker disappeared", now, answer=rec.get("answer"))
        out.withdrawn.append(rec["qid"])
        note("question_withdrawn", {"qid": rec["qid"], "file": rec["source"].get("file", ""),
                                     "hash": rec["source"].get("hash", "")})
    return out


# -- scan ------------------------------------------------------------------------------------


class _GitError(Exception):
    pass


def _git(runner: Runner, cwd: Path | str, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    res = runner.run(["git", *args], cwd=cwd)
    if res.exit_code not in ok:
        raise _GitError(f"git {' '.join(args[:2])} failed ({res.exit_code}): {(res.stderr or '').strip()[:120]}")
    return res.stdout


def _parse_raw_diff(out: str) -> list[Change]:
    parts = out.split("\0")
    changes: list[Change] = []
    i = 0
    while i < len(parts):
        meta = parts[i]
        if meta.startswith(":") and i + 1 < len(parts):
            fields = meta[1:].split()
            if len(fields) >= 5:
                changes.append(Change(path=parts[i + 1], status=fields[4][0], blob=fields[3]))
            i += 2
        else:
            i += 1
    return changes


def _parse_ls_tree(out: str) -> list[Change]:
    changes = []
    for rec in out.split("\0"):
        if "\t" not in rec:
            continue
        meta, path = rec.split("\t", 1)
        fields = meta.split()
        if len(fields) >= 3 and fields[1] == "blob":
            changes.append(Change(path=path, status="A", blob=fields[2]))
    return changes


def _scan_base(store: Store, job_id: str, job, head: str, runner: Runner) -> str:
    checks = store.get_checks(job_id, label="baseline")
    if checks and checks[0].get("commit_sha"):
        return checks[0]["commit_sha"]
    try:  # the fork point of the job branch from the repo it was cut from
        return _git(runner, job.repo, "merge-base", "HEAD", f"steward/{job_id}").strip()
    except Exception:  # noqa: BLE001 - no repo / no branch: scan the whole HEAD tree
        return ""


def _with_answers_rescans(changes: list[Change], files: FileReader, runner: Runner, worktree: Path, head: str) -> list[Change]:
    """A changed STEWARD-ANSWERS.json makes the question files next to it rescan (their markers moved)."""
    have = {c.path for c in changes if c.status != "D"}
    extra: list[Change] = []
    for c in changes:
        if basename(c.path) != ANSWERS_FILE:
            continue
        d = dirname(c.path)
        cands = [f"{d}/{QUESTIONS_FILE}" if d else QUESTIONS_FILE]
        listing = runner.run(["git", "ls-tree", "--name-only", "-z", head] + ([f"{d}/"] if d else []), cwd=worktree)
        if listing.exit_code == 0:
            cands += [p for p in listing.stdout.split("\0") if p.lower().endswith(".md")]
        for path in cands:
            if path not in have and files.read(path) is not None:
                have.add(path)
                extra.append(Change(path=path, status="M"))
    return changes + extra


def _with_recorded_files(changes: list[Change], existing: list[dict], runner: Runner, worktree: Path, head: str) -> list[Change]:
    """A file that holds live records but is not in the net diff any more was deleted (added and removed since the
    base) or reverted: look at it at HEAD so its records follow (withdrawn, or re-read)."""
    have = {c.path for c in changes}
    extra: list[Change] = []
    for path in sorted({(r.get("source") or {}).get("file", "") for r in existing if r.get("status") != "withdrawn"}):
        if not path or path in have:
            continue
        here = runner.run(["git", "cat-file", "-e", f"{head}:{path}"], cwd=worktree).exit_code == 0
        extra.append(Change(path=path, status="M" if here else "D"))
    return changes + extra


def record_error(store: Store, job_id: str, message: str) -> bool:
    """One questions_scan_error event per distinct message (a persistent failure is not repeated each tick)."""
    message = one_line(message, 200)
    if store.kv_get(job_id, LAST_ERROR_KEY) == message:
        return False
    store.kv_set(job_id, LAST_ERROR_KEY, message)
    store.append_event(job_id, what="questions_scan_error", evidence={"error": message},
                       rule="questions_scan", judgment=Judgment.code)
    return True


def scan(store: Store, job_id: str, runner: Runner, *, force: bool = False) -> ScanResult:
    """Scan the job's committed diff for questions and reconcile the stored records. Never raises."""
    try:
        return _scan(store, job_id, runner, force)
    except Exception as exc:  # noqa: BLE001 - a scan failure must never break a tick
        message = f"{type(exc).__name__}: {exc}" if not isinstance(exc, _GitError) else str(exc)
        record_error(store, job_id, message)
        return ScanResult(error=one_line(message, 200))


def _scan(store: Store, job_id: str, runner: Runner, force: bool) -> ScanResult:
    job = store.get_job(job_id)
    if job is None:
        return ScanResult(skipped="no job")
    worktree = store.home / "worktrees" / job_id
    if not worktree.is_dir():
        return ScanResult(skipped="no worktree")
    head = _git(runner, worktree, "rev-parse", "HEAD").strip()
    if not head:
        raise _GitError("git rev-parse HEAD returned nothing")
    if not force and store.kv_get(job_id, SCANNED_HEAD_KEY) == head:
        return ScanResult(skipped="unchanged", head=head)

    base = _scan_base(store, job_id, job, head, runner)
    if base:
        changes = _parse_raw_diff(_git(runner, worktree, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", base, head))
    else:
        changes = _parse_ls_tree(_git(runner, worktree, "ls-tree", "-r", "-z", head))

    def read_fn(path: str) -> str | None:
        res = runner.run(["git", "show", f"{head}:{path}"], cwd=worktree)
        return res.stdout if res.exit_code == 0 else None

    def grep_fn(needle: str, paths: list[str]) -> list[str]:
        hits: list[str] = []
        for i in range(0, len(paths), _GREP_CHUNK):
            res = runner.run(["git", "grep", "-l", "-z", "-F", needle, head, "--", *paths[i:i + _GREP_CHUNK]], cwd=worktree)
            if res.exit_code not in (0, 1):
                raise _GitError(f"git grep failed ({res.exit_code})")
            prefix = f"{head}:"
            for rec in res.stdout.split("\0"):
                if rec:
                    hits.append(rec[len(prefix):] if rec.startswith(prefix) else rec)
        return hits

    files = FileReader(read_fn, grep_fn)
    existing = load(store, job_id)
    changes = _with_answers_rescans(changes, files, runner, worktree, head)
    changes = _with_recorded_files(changes, existing, runner, worktree, head)
    deleted = {c.path for c in changes if c.status == "D"}
    scanned = {c.path for c in changes if c.status != "D" and any(a.wants(c.path) for a in ADAPTERS)}
    found_by_file: dict[str, list[Found]] = {p: [] for p in sorted(scanned)}
    for adapter in ADAPTERS:
        for f in adapter.scan(changes, files):
            found_by_file.setdefault(f.file, []).append(f)
    for path in [p for p, fl in found_by_file.items() if not fl and files.unreadable(p)]:
        del found_by_file[path]  # a file we could not read says nothing: its records stay as they are

    now = store_module.now_iso()
    before = store.kv_get(job_id, QUEUE_HASH_KEY, default=queue_hash([]))
    rec = reconcile(existing, found_by_file, deleted, head, now)
    blocking_qids = {r["qid"] for r in rec.records if r.get("blocking")}
    for (adapter, file), qids in sorted(rec.found.items()):
        blocking = [q for q in qids if q in blocking_qids]  # G13: the feed line says which ones block work
        store.append_event(job_id, what="questions_found",
                           evidence={"adapter": adapter, "file": file, "sha": head, "qids": qids,
                                     **({"blocking": blocking} if blocking else {})},
                           rule="questions_scan", judgment=Judgment.code)
    for what, evidence, rule, judgment in rec.events:
        store.append_event(job_id, what=what, evidence=evidence, rule=rule, judgment=judgment)
    if rec.confirmed:
        store.append_event(job_id, what="questions_written",
                           evidence={"commit": head, "qids": list(rec.confirmed), "via": "scan"},
                           rule="operator_question_delivery", judgment=Judgment.human)
    save(store, job_id, rec.records)
    store.kv_set(job_id, SCANNED_HEAD_KEY, head)
    store.kv_delete(job_id, LAST_ERROR_KEY)
    after = queue_hash(rec.records)
    store.kv_set(job_id, QUEUE_HASH_KEY, after)
    return ScanResult(
        head=head, base=base, found=[q for qs in rec.found.values() for q in qs], updated=rec.updated,
        stale=rec.stale, withdrawn=rec.withdrawn, worker_decided=rec.worker_decided,
        queue_hash=after, queue_changed=(after != before), summary=summary(rec.records),
    )


# -- answering (Phase 17-02: G3, G4, G7, G9, G10) ------------------------------------------------


def _anchor_of(rec: dict) -> str:
    return (rec.get("source") or {}).get("anchor", "")


def _file_of(rec: dict) -> str:
    return (rec.get("source") or {}).get("file", "")


def resolve_qid(records: list[dict], text: str) -> tuple[dict | None, str | None]:
    """A record from what a person typed: the full qid, else `<file>:<anchor>` (the file may be a suffix of the path),
    else a bare anchor. Several matches name the candidates; none names what was typed."""
    text = (text or "").strip()
    for r in records:
        if r["qid"] == text:
            return r, None
    matches: list[dict] = []
    if ":" in text:
        file_part, _, anchor = text.rpartition(":")
        matches = [r for r in records if _anchor_of(r) == anchor and (
            _file_of(r) == file_part or _file_of(r).endswith("/" + file_part) or r["qid"].endswith(":" + text))]
    else:
        matches = [r for r in records if _anchor_of(r) == text]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        names = ", ".join(sorted(f"{_file_of(r)}:{_anchor_of(r)}" for r in matches))
        return None, f"ambiguous question {text}: {names}"
    return None, f"unknown question: {text}"


def card(record: dict) -> dict:
    """The display card: what a surface shows for one question. Recommended options first, order kept otherwise."""
    rec_keys = list(record.get("recommended") or [])
    options = [{"key": o["key"], "label": o.get("label", ""), "detail": o.get("detail", ""),
                "recommended": o["key"] in rec_keys} for o in record.get("options") or []]
    options.sort(key=lambda o: 0 if o["recommended"] else 1)
    src = record.get("source") or {}
    return {
        "qid": record["qid"], "anchor": src.get("anchor", ""), "file": src.get("file", ""),
        "title": record.get("title", ""), "question": record.get("question", ""), "options": options,
        "recommended": rec_keys, "recommended_text": record.get("recommended_text", ""),
        "starred": bool(record.get("starred")), "rationale": record.get("rationale", ""),
        "evidence": list(record.get("evidence") or []), "blocking": bool(record.get("blocking")),
        "blocking_reason": record.get("blocking_reason", ""), "status": record.get("status", ""),
        "flags": dict(record.get("flags") or {}), "answer": record.get("answer"),
        "stale_from": record.get("stale_from"),
    }


def view(store: Store, job_id: str, *, include_all: bool = False, round_no: int | None = None) -> dict:
    """Read-only: the waiting queue (or every record with include_all) as cards, optionally one page of ROUND_SIZE."""
    records = load(store, job_id)
    shown = ordered(records) if include_all else [r for r in ordered(records) if r.get("status") in WAITING]
    out: dict = {"job_id": job_id, "summary": summary(records)}
    if round_no is not None:
        rounds = math.ceil(len(shown) / ROUND_SIZE)
        start = (max(round_no, 1) - 1) * ROUND_SIZE
        shown = shown[start:start + ROUND_SIZE]
        out.update(round=round_no, rounds=rounds)
    out["questions"] = [card(r) for r in shown]
    return out


def _clean(text, cap: int) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:cap].rstrip()


_ACCEPT_SKIP = {"open": "no_star", "answered": "already_answered", "deferred": "deferred", "stale": "stale",
                "withdrawn": "withdrawn"}
_RECEIPT_KEY = "questions_receipt:"


def _snapshot(answer: dict | None) -> dict:
    a = answer or {}
    return {"option": a.get("option", ""), "label": a.get("label", ""), "other": a.get("other", ""),
            "note": a.get("note", "")}


def _same_answer(old: dict | None, new: dict) -> bool:
    return bool(old) and (old.get("option"), old.get("other", ""), old.get("note", "")) == (
        new["option"], new["other"], new["note"])


def apply(
    store: Store,
    job_id: str,
    *,
    answers=(),
    accept_recommended: bool = False,
    qids=None,
    rescan: bool = False,
    origin: str = "cli",
    request_id: str | None = None,
    now: datetime | None = None,
    runner: Runner | None = None,
) -> dict:
    """Record the owner's answers in the store (the store is the record; the file is projected from it, G5). No git, no
    model, unless `rescan`. Refusals go to `skipped` with a reason and never raise. Idempotent by `request_id`."""
    if store.get_job(job_id) is None:
        return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
    if request_id:
        cached = store.kv_get(job_id, _RECEIPT_KEY + request_id)
        if cached is not None:
            return {**cached, "duplicate": True}
        prior = store.get_receipt(request_id)
        if prior is not None:
            return {"accepted": prior.accepted, "detail": prior.detail, "duplicate": True, "kind": prior.kind}
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    records = load(store, job_id)
    result: dict = {"accepted": False, "detail": "", "answered": 0, "changed": 0, "deferred": 0, "unchanged": 0,
                    "skipped": [], "duplicate": False}
    skipped = result["skipped"]

    def skip(qid: str, reason: str) -> None:
        skipped.append({"qid": qid, "reason": reason})

    items = [dict(a) for a in answers]
    if accept_recommended:
        if qids:
            for typed in qids:
                rec, err = resolve_qid(records, typed)
                if rec is None:
                    skip(typed, "unknown question" if (err or "").startswith("unknown") else err)
                elif rec["status"] == "open" and rec.get("starred"):
                    items.append({"qid": rec["qid"], "option": "recommended"})
                else:
                    skip(rec["qid"], _ACCEPT_SKIP.get(rec["status"], rec["status"]) if rec["status"] != "open"
                         else "no_star")
        else:
            for rec in records:
                if rec["status"] == "open" and rec.get("starred"):
                    items.append({"qid": rec["qid"], "option": "recommended"})
                else:
                    skip(rec["qid"], _ACCEPT_SKIP.get(rec["status"], rec["status"]))
    for item in items:
        typed = item.get("qid", "")
        rec, err = resolve_qid(records, typed)
        if rec is None:
            skip(typed, "unknown question" if (err or "").startswith("unknown") else err or "unknown question")
            continue
        _answer_one(store, job_id, rec, item, origin, date, request_id, result, skip)
    save(store, job_id, records)

    applied = result["answered"] + result["changed"] + result["deferred"] + result["unchanged"]
    bits = []
    for key in ("answered", "changed", "deferred", "unchanged"):
        if result[key]:
            bits.append(f"{key} {result[key]}")
    if accept_recommended and not qids and not result["answered"]:
        bits.insert(0, "answered 0")
    result["accepted"] = applied > 0 or (accept_recommended and not qids)
    if rescan:
        _rescan(store, job_id, runner, result, bits)
    counts = summary(load(store, job_id))
    result["summary"] = counts
    if bits and (applied or rescan or not skipped):
        tail = f"{counts['needs_you']} need you" if accept_recommended else f"{counts['waiting']} waiting"
        result["detail"] = " · ".join(bits + ([tail] if applied or accept_recommended else []))
    elif skipped:
        result["error_kind"] = "unknown_question" if skipped[0]["reason"] == "unknown question" else "refused"
        result["detail"] = f"{skipped[0]['reason']}: {skipped[0]['qid']}"
    if request_id:
        store.kv_set(job_id, _RECEIPT_KEY + request_id, {k: v for k, v in result.items() if k != "duplicate"})
        store.put_receipt(Receipt(request_id=request_id, job_id=job_id, kind="answer", accepted=result["accepted"],
                                  detail=result["detail"], ts=store_module.now_iso()))
    return result


def _rescan(store: Store, job_id: str, runner: Runner | None, result: dict, bits: list[str]) -> None:
    res = scan(store, job_id, runner or SubprocessRunner(), force=True)
    result["rescan"] = {"found": res.found, "updated": res.updated, "stale": res.stale, "withdrawn": res.withdrawn,
                        "worker_decided": res.worker_decided, "skipped": res.skipped, "error": res.error}
    if res.error:
        bits.append(f"rescan failed: {res.error}")
    elif res.skipped and res.skipped != "unchanged":
        bits.append(f"rescan skipped: {res.skipped}")
        result["accepted"] = True
    else:
        bits.append(f"rescan: {len(res.found)} new · {len(res.stale)} stale · {len(res.withdrawn)} withdrawn")
        result["accepted"] = True


def _answer_one(store, job_id, rec, item, origin, date, request_id, result, skip) -> None:
    qid = rec["qid"]
    status = rec["status"]
    if status == "withdrawn":
        return skip(qid, "withdrawn")
    keys = [o["key"] for o in rec.get("options") or []]
    unparsed = bool((rec.get("flags") or {}).get("unparsed"))
    key, other, label, recommended = item.get("option"), item.get("other"), "", False
    if item.get("defer"):
        option, other = "defer", ""
    elif other is not None:
        other = _clean(other, OTHER_CAP)
        if not other:
            return skip(qid, "empty answer")
        option = "other"
    elif key == "recommended":
        if not rec.get("starred"):
            return skip(qid, "no star")
        rec_keys = [k for k in rec.get("recommended") or [] if k in keys]
        option, recommended = "+".join(rec_keys) or "as_written", True  # the concrete choice, not the word
        label = " + ".join(o.get("label", "") for k in rec_keys for o in rec["options"] if o["key"] == k) \
            or rec.get("recommended_text", "")
    elif key in keys and not unparsed:
        option = key
        label = next(o.get("label", "") for o in rec["options"] if o["key"] == key)
        recommended = rec.get("recommended") == [key]
    else:
        return skip(qid, "no such option")
    at = store_module.now_iso()
    new = {
        "option": option, "label": label, "other": other or "", "note": _clean(item.get("note"), NOTE_CAP),
        "by": owner.actor_key(), "origin": origin, "at": at, "date": date, "request_id": request_id,
        "recommended": recommended, "area_hash": (rec.get("source") or {}).get("hash", ""),
    }
    old = rec.get("answer")
    if status in ("answered", "deferred") and _same_answer(old, new):
        result["unchanged"] += 1
        return
    changed = None
    if old:
        if not _same_answer(old, new):
            changed = {"before": _snapshot(old), "after": _snapshot(new)}
            rec["history"].append({"at": at, "from": old, "to": new, "origin": origin})
            result["changed"] += 1
        else:  # a stale card confirmed with the same answer
            result["deferred" if option == "defer" else "answered"] += 1
    else:
        result["deferred" if option == "defer" else "answered"] += 1
    rec["answer"] = new
    rec["status"] = "deferred" if option == "defer" else "answered"
    rec["written"] = None
    rec["updated_at"] = at
    rec.pop("stale_from", None)
    rec["flags"] = {**(rec.get("flags") or {}), "updated": False, "answered_by_worker": False}
    ev: dict = {"qid": qid, "note": cap_text(new["note"]), "by": owner.actor_key(), "origin": origin}
    if option == "defer":
        what = "question_deferred"
    else:
        what = "question_answered"
        ev["option"] = option
        if option == "other":
            ev["other"] = cap_text(other)
    if changed:
        ev["changed"] = changed
    store.append_event(job_id, what=what, evidence=ev, rule="operator_question_answer", judgment=Judgment.human)


# -- audit ---------------------------------------------------------------------------------------


def _detail_of(ev) -> str:
    e = ev.evidence or {}
    if ev.what == "question_answered":
        text = e.get("other") or e.get("option", "")
        return f"{text} {e.get('note', '')}".strip() + f" [{e.get('origin', '')}]" + (" (changed)" if e.get("changed") else "")
    if ev.what == "question_deferred":
        return f"{e.get('note', '')} [{e.get('origin', '')}]".strip()
    if ev.what == "questions_found":
        return f"{len(e.get('qids') or [])} in {e.get('file', '')}"
    return one_line(", ".join(f"{k}={v}" for k, v in e.items() if k != "qid"), 120)


def history(store: Store, job_id: str) -> dict:
    """Read-only audit: every question_* and questions_* event in seq order, plus each record's answer changes."""
    events = []
    for ev in store.events(job_id):
        if not (ev.what.startswith("question_") or ev.what.startswith("questions_")):
            continue
        e = ev.evidence or {}
        qid = e.get("qid") or (", ".join(e.get("qids")) if isinstance(e.get("qids"), list) else "")
        events.append({"seq": ev.seq, "ts": ev.ts, "what": ev.what, "qid": qid, "detail": _detail_of(ev),
                       "judgment": ev.judgment.value if hasattr(ev.judgment, "value") else str(ev.judgment),
                       "evidence": e})
    changes = []
    for rec in load(store, job_id):
        for h in rec.get("history") or []:
            if isinstance(h.get("from"), dict):
                changes.append({"qid": rec["qid"], "at": h.get("at", ""), "from": h["from"], "to": h.get("to"),
                                "origin": h.get("origin", "")})
    changes.sort(key=lambda c: c["at"])
    return {"job_id": job_id, "events": events, "changes": changes}


# -- write-back (G5): the store's answers as deterministic edits of COMMITTED text ------------------------------


@dataclass
class WritebackPlan:
    edits: list[Edit] = field(default_factory=list)
    qids: list[str] = field(default_factory=list)  # every question an edit renders
    skipped: list[dict] = field(default_factory=list)  # [{qid, reason}]
    commit_message: str = ""
    date: str = ""


_SCOPE = re.compile(r"^(\d+(?:\.\d+)?)-")


def commit_message(paths: list[str]) -> str:
    """`docs(<N>): record <name> decisions` when every edited file belongs to one numbered phase, else `docs(questions)`."""
    scopes = {(_SCOPE.match(basename(p)) or [None, "questions"])[1] for p in paths}
    scope = scopes.pop() if len(scopes) == 1 else "questions"
    return f"docs({scope}): record {owner.name()} decisions"


def writeback_for_files(texts: dict[str, str], items: list[tuple[dict, dict]], date: str) -> WritebackPlan:
    """Pure. `texts` = committed text by path (question files and their siblings), `items` = [(record, answer)].
    An item whose area hash or anchor no longer matches the committed text (T-17-10), or that has no deterministic
    edit (an unparsed card), is skipped with a reason and never blocks the rest of the batch."""
    plan = WritebackPlan(date=date)
    by_file: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
    for rec, ans in items:
        by_file.setdefault((rec.get("adapter", ""), (rec.get("source") or {}).get("file", "")), []).append((rec, ans))
    edits: dict[str, Edit] = {}
    rendered: list[str] = []
    working = dict(texts)  # what each later group sees: an answers file shared by two question files is merged, not lost
    for (name, path), group in sorted(by_file.items()):
        adapter = adapter_named(name)
        text = texts.get(path)
        if adapter is None or text is None:
            plan.skipped += [{"qid": r["qid"], "reason": "area not found"} for r, _ in group]
            continue
        areas = {f.anchor: f for f in adapter.areas(path, text)}
        ok: list[tuple[dict, dict]] = []
        for rec, ans in group:
            f = areas.get((rec.get("source") or {}).get("anchor", ""))
            if (rec.get("flags") or {}).get("unparsed"):
                plan.skipped.append({"qid": rec["qid"], "reason": "no deterministic edit"})
            elif f is None:
                plan.skipped.append({"qid": rec["qid"], "reason": "area not found"})
            elif f.hash != ans.get("area_hash"):
                plan.skipped.append({"qid": rec["qid"], "reason": "area changed"})
            else:
                ok.append((rec, ans))
        if not ok:
            continue
        try:
            made = adapter.patch(working, ok, date)
        except NotImplementedError:
            plan.skipped += [{"qid": r["qid"], "reason": "no deterministic edit"} for r, _ in ok]
            continue
        for e in made:
            edits[e.path] = make_edit(e.path, edits[e.path].old_text, e.new_text) if e.path in edits else e
            working[e.path] = e.new_text
        if made:
            rendered += [r["qid"] for r, _ in ok]
    plan.edits = [edits[p] for p in sorted(edits)]
    plan.qids = rendered
    plan.commit_message = commit_message([e.path for e in plan.edits]) if plan.edits else ""
    return plan


def _pending(records: list[dict], qids, include_store_only: bool = False) -> list[tuple[dict, dict]]:
    wanted = set(qids) if qids else None
    out = []
    for r in records:
        a = r.get("answer") or {}
        w = r.get("written")
        open_for_write = not w or (include_store_only and w.get("via") == "store_only")
        if (r.get("status") in ("answered", "deferred") and owner.is_owner_actor(a.get("by")) and open_for_write
                and (wanted is None or r["qid"] in wanted)):
            out.append((r, a))
    return out


def render_writeback(store: Store, job_id: str, runner: Runner, *, date: str | None = None, qids=None,
                     cwd: Path | str | None = None, ref: str = "HEAD", include_store_only: bool = False) -> WritebackPlan:
    """The edit for every answer not yet written, from `git show <ref>:<path>` in the job worktree (never the dirty
    tree); `cwd` and `ref` point it at another checkout (the source repo, 17-03) and `include_store_only` also takes
    answers that were kept in the store. Never raises: a failed read skips the item."""
    worktree = Path(cwd) if cwd is not None else store.home / "worktrees" / job_id
    items = _pending(load(store, job_id), qids, include_store_only)
    if not items or not worktree.is_dir():
        return WritebackPlan()
    date = date or max((a.get("date") or "" for _, a in items), default="")

    def show(path: str) -> str | None:
        try:
            res = runner.run(["git", "show", f"{ref}:{path}"], cwd=worktree)
        except Exception:  # noqa: BLE001
            return None
        return res.stdout if res.exit_code == 0 else None

    texts: dict[str, str] = {}
    for rec, _ in items:
        path = (rec.get("source") or {}).get("file", "")
        adapter = adapter_named(rec.get("adapter", ""))
        if not path or adapter is None:
            continue
        for p in [path, *adapter.sibling_paths(path)]:
            if p not in texts:
                got = show(p)
                if got is not None:
                    texts[p] = got
    return writeback_for_files(texts, items, date)
