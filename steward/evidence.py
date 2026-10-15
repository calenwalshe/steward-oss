"""The evidence panel behind a job's yellow question line (dashboard + phone + why-chat).

`decision(store, job)` says what the owner is being asked (acceptance of a NEEDS_HUMAN
job, a held worker permission, or a watcher escalation); `panel(...)` is the plain
text they weigh it with (verification, commits, the worker's own "done" message,
watchers, cost, or the full command and why Steward held it). Read-only: nothing here
writes to the store, the worktree, or the job.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

from steward import approvals as approvals_module
from steward import chat as chat_module
from steward import custom_watchers, jobview
from steward.diagnosis import redact
from steward.models import JobState
from steward.store import Store

_TAIL_BYTES = 2_000_000
_DONE_LINES = 6
_GOAL_LINES = 2
_FINISH_ITEMS = 8
_CHANGED_FILES = 6
_COMMITS_MAX = 3
_GIT_TIMEOUT_S = 5

BUTTONS = {
    "acceptance": [("accept", "accept"), ("send back", "sendback"), ("stop", "stop"), ("chat", "chat")],
    "approval": [("approve", "approve"), ("deny", "deny"), ("why?", "why")],
    "escalation": [("approve", "approve"), ("deny", "deny"), ("why?", "why")],
}


def _is_acceptance(a: dict) -> bool:
    return a.get("kind") == "acceptance" or a.get("question") == approvals_module.ACCEPTANCE_QUESTION


def decision(store: Store, job_id: str) -> dict | None:
    """`{"kind": acceptance|approval|escalation, "approval": record|None}` or None
    when nothing waits on the owner. Same precedence as `jobview.reason`."""
    approvals = approvals_module.list_approvals(store, job_id)
    ask = jobview.pending_ask(approvals)
    if ask:
        kind = "approval" if ask["question"].startswith("worker permission:") else "escalation"
        return {"kind": kind, "approval": ask}
    job = store.get_job(job_id)
    if job is not None and job.state == JobState.NEEDS_HUMAN:
        from steward import ownership

        if ownership.enabled() and ownership.projection(store, job_id).get("status") in ownership.REVIEW_STATUSES:
            return None
    if job is not None and job.state == JobState.NEEDS_HUMAN:
        acc = next((a for a in approvals if a.get("status") == "pending" and _is_acceptance(a)), None)
        waiting, deliverable = _open_questions(store, job_id)
        return {"kind": "acceptance", "approval": acc, "questions": waiting, "deliverable": deliverable}
    return None


def _open_questions(store: Store, job_id: str) -> tuple[dict | None, bool]:
    """`(summary, deliverable)` for an acceptance: the question-queue summary when something waits (else None) and
    whether the job's deliverable IS the questions (G11). Never raises: the queue is decoration here."""
    try:
        from steward import questions

        s = questions.summary(questions.load(store, job_id))
        if not s["waiting"]:
            return None, False
        return s, questions.is_deliverable(store, job_id)
    except Exception:  # noqa: BLE001
        return None, False


def buttons_for(dec: dict) -> list[tuple[str, str]]:
    """`(label, action)` pairs; stop (= deny the acceptance approval) needs that approval. An acceptance with waiting
    questions gains `answer` and, when some are starred, `accept ★ + accept` (G11); a discussion job's accept reads
    `accept anyway` (a warning, not a lock)."""
    out = list(BUTTONS[dec["kind"]])
    if dec["kind"] == "acceptance":
        if dec.get("approval") is None:
            out = [b for b in out if b[1] != "stop"]
        q = dec.get("questions")
        if q and q.get("waiting"):
            if dec.get("deliverable"):
                out = [("accept anyway", "accept") if b[1] == "accept" else b for b in out]
            out.append(("answer", "answer"))
            if q.get("starred"):
                out.append(("accept \u2605 + accept", "accept_rec_accept"))
    return out


# -- data pulls ----------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def _pass_fail(store: Store, job_id: str, label: str, commit_sha: str | None = None) -> tuple[int, int] | None:
    checks = store.get_checks(job_id, label=label)
    if commit_sha:
        checks = [c for c in checks if c["commit_sha"] == commit_sha]
    if not checks:
        return None
    passed = failed = 0
    for c in {c["command"]: c for c in checks}.values():
        if c["results"]:
            for outcome in c["results"].values():
                if outcome in ("failed", "error"):
                    failed += 1
                else:
                    passed += 1
        elif c["exit_code"] == 0:
            passed += 1
        else:
            failed += 1
    return passed, failed


def verification(store: Store, job_id: str) -> dict:
    from steward.api import StewardAPI

    return StewardAPI(store).get_verification(job_id)


def commits_since_baseline(store: Store, job_id: str) -> list[str] | None:
    """`git log --oneline <baseline>..HEAD` in the job's worktree, newest first; None if unknowable."""
    baseline = store.get_checks(job_id, label="baseline")
    wt = chat_module.worktree_path(store, job_id)
    if not baseline or not wt.is_dir():
        return None
    out = _git(["log", "--oneline", f"{baseline[0]['commit_sha']}..HEAD"], wt)
    return None if out is None else [ln for ln in out.splitlines() if ln.strip()]


def _codex_last_text(store: Store, job_id: str, sessions_dir: Path | None) -> str | None:
    from steward import worker_view

    rollout = worker_view.codex_rollout(chat_module.worktree_path(store, job_id), sessions_dir)
    return worker_view.codex_last_said(rollout) if rollout else None


def last_worker_text(store: Store, job_id: str, projects_dir: Path | None = None,
                     sessions_dir: Path | None = None) -> str | None:
    """The last assistant text in the worker's transcript (its final "done" message).
    Claude JSONL first; when there is none (a Codex worker), the Codex rollout's last message."""
    text = _claude_last_text(store, job_id, projects_dir)
    return text if text else _codex_last_text(store, job_id, sessions_dir)


def _claude_last_text(store: Store, job_id: str, projects_dir: Path | None = None) -> str | None:
    sid = chat_module.worker_session_id(store, job_id, projects_dir=projects_dir)
    base = projects_dir if projects_dir is not None else Path.home() / ".claude" / "projects"
    d = base / chat_module.project_slug(chat_module.worktree_path(store, job_id))
    path = d / f"{sid}.jsonl" if sid else None
    if path is None or not path.is_file():
        files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []
        path = files[0] if files else None
    if path is None:
        return None
    try:
        with open(path, "rb") as fh:
            size = path.stat().st_size
            fh.seek(max(size - _TAIL_BYTES, 0))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(data.splitlines()):
        if '"assistant"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") != "assistant":
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        else:
            continue
        if text.strip():
            return text.strip()
    return None


_FINISH_RE = re.compile(r"finish[ -]?line\s*:?", re.I)


def split_goal(goal: str) -> tuple[str, str]:
    """`(goal text, finish-line text)`: the goal split at "FINISH LINE:" (finish line "" if absent)."""
    m = _FINISH_RE.search(goal or "")
    if not m:
        return " ".join((goal or "").split()), ""
    return " ".join(goal[: m.start()].split()).rstrip(" ;:,-—"), " ".join(goal[m.end():].split())


def finish_items(text: str) -> list[str]:
    """Split finish-line text into checklist items: on `;`, newlines, `(1)`/`1)` enumerators,
    sentence ends and commas that sit outside parentheses. Plain text; no verification implied."""
    text = re.sub(r"\s*\(\d+\)\s+|\s+\d+\)\s+", ";", " " + text.replace("\n", ";"))
    items, buf, depth = [], "", 0
    for i, ch in enumerate(text):
        depth += ch in "([{"
        depth -= ch in ")]}" and depth > 0
        nxt = text[i + 1] if i + 1 < len(text) else " "
        if depth == 0 and (ch in ";," or (ch == "." and nxt == " ")):
            items.append(buf)
            buf = ""
        else:
            buf += ch
    items.append(buf)
    out = []
    for it in items:
        it = re.sub(r"^(and|then|plus)\s+", "", it.strip(" .;,-•*"), flags=re.I).strip()
        if it:
            out.append(it)
    return out


def _numstat(store: Store, job_id: str) -> tuple[int, int, list[str]] | None:
    """`(added, deleted, paths)` of the job's own change (jobdiff), deliverables first (docs/**, *.md, new files)."""
    from steward import jobdiff

    job = store.get_job(job_id)
    found = jobdiff.job_diff_range(store, job) if job is not None else None
    if found is None:
        return None
    rng, wt = found
    stat = _git(["diff", "--numstat", rng], wt)
    status = _git(["diff", "--name-status", rng], wt)
    if stat is None:
        return None
    added_files = {ln.split("\t", 1)[1] for ln in (status or "").splitlines() if ln.startswith("A\t")}
    add = dele = 0
    files = []
    for ln in stat.splitlines():
        parts = ln.split("\t", 2)
        if len(parts) != 3:
            continue
        a, d, path = parts
        add += int(a) if a.isdigit() else 0
        dele += int(d) if d.isdigit() else 0
        files.append(path)

    def rank(path: str) -> int:
        if path.startswith("docs/") or path.endswith(".md"):
            return 0
        return 1 if path in added_files else 2

    files.sort(key=lambda p: (rank(p), p))
    return add, dele, files


def _advisor_line(store: Store, job_id: str) -> str | None:
    try:
        from steward import trajectory

        last = next((r for r in reversed(trajectory.review_history(store, job_id)) if r.get("verdict")), None)
    except Exception:  # noqa: BLE001 - decoration
        return None
    if not last:
        return None
    v = last["verdict"]
    if not isinstance(v, dict):
        return f"advisor: {redact(str(v))}"
    obs = " ".join(redact(str(v.get("observation") or "")).split())
    return f"advisor: {v.get('trajectory', 'n/a')}" + (f" — {obs}" if obs else "")


def _watcher_summary(store: Store, job_id: str) -> list[str]:
    out = []
    for w in custom_watchers.list_watchers(store, job_id):
        ev = custom_watchers.last_verdict(store, job_id, w["name"])
        status = (ev.evidence or {}).get("status", "no verdict") if ev else "no verdict"
        out.append(f"{w['name']} {custom_watchers.run_count(store, job_id, w['name'])} runs, {status}")
    return out


def job_cost_usd(store: Store, job_id: str) -> float | None:
    try:
        from steward import tokens

        return float(tokens.job_tokens(store, job_id)["total"].get("usd", 0) or 0)
    except Exception:  # noqa: BLE001
        return None


# -- approvals ------------------------------------------------------------------


def parse_permission(question: str) -> tuple[str, str, object]:
    """`worker permission: <tool> {input}` -> (tool, full text, input dict or None)."""
    body = question.removeprefix("worker permission: ")
    tool, _, rest = body.partition(" ")
    try:
        inp = ast.literal_eval(rest)
    except (ValueError, SyntaxError):
        inp = None
    if isinstance(inp, dict):
        text = str(inp.get("command") or inp.get("file_path") or inp.get("path") or rest)
        return tool, text, inp
    m = re.match(r"\{'command': (['\"])(.*)", rest, re.S)  # truncated dict: still show the command
    return tool, (m.group(2) if m else rest), None


def held_reason(store: Store, job_id: str, question: str) -> str:
    """Why Steward did not auto-allow it (`permissions.decide`'s own reason)."""
    tool, text, inp = parse_permission(question)
    try:
        from steward import authority

        d = authority.decide_worker(store, job_id, tool, text if tool == "Bash" else (inp or text),
                                    chat_module.worktree_path(store, job_id))
    except Exception:  # noqa: BLE001
        return "held for your review"
    if d.grant_id and d.allow:
        return f"covered by owner grant {d.grant_id}"
    return d.reason if not d.allow else f"held for your review ({d.reason})"


def _age(created_at: str, now: float | None = None) -> str:
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return "?"
    now_dt = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, timezone.utc)
    return jobview.ago(max((now_dt - created).total_seconds(), 0)) + " ago"


def _wrap(text: str, width: int, max_lines: int) -> list[str]:
    lines = textwrap.wrap(" ".join(text.split()), max(width, 10), break_long_words=True) or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(width - 1, 1)] + "…"
    return lines


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


# -- the panel -------------------------------------------------------------------


def panel(store: Store, job_id: str, width: int = 74, *, now: float | None = None,
          projects_dir: Path | None = None, cost_usd: float | None = None, dec: dict | None = None) -> list[str]:
    """Plain evidence lines, each at most `width` cells; [] when nothing waits on the owner."""
    dec = dec or decision(store, job_id)
    if dec is None:
        return []
    kind, rec = dec["kind"], dec.get("approval")
    lines: list[str] = []
    if kind == "acceptance":
        job = store.get_job(job_id)
        goal, finish = split_goal(job.goal if job else "")
        if goal:
            glines = _wrap(redact(goal), max(width - 6, 10), _GOAL_LINES)
            lines.append("goal: " + glines[0])
            lines += ["      " + ln for ln in glines[1:]]
        items = finish_items(redact(finish)) if finish else []
        if items:
            lines.append("finish line:")
            lines += [f"  ☐ {it}" for it in items[:_FINISH_ITEMS]]
            if len(items) > _FINISH_ITEMS:
                lines.append(f"  … {len(items) - _FINISH_ITEMS} more")
        ns = _numstat(store, job_id)
        if ns is not None:
            add, dele, files = ns
            lines.append(f"changed: {len(files)} file{'' if len(files) == 1 else 's'}, +{add}/-{dele}")
            lines += ["  " + f for f in files[:_CHANGED_FILES]]
            if len(files) > _CHANGED_FILES:
                lines.append(f"  … {len(files) - _CHANGED_FILES} more")
        v = verification(store, job_id)
        sha = (v.get("commit_sha") or "")[:7] or "n/a"
        state = "?" if v.get("dirty") is None else ("dirty" if v["dirty"] else "clean")
        cur = _pass_fail(store, job_id, "verify", v.get("commit_sha"))
        base = _pass_fail(store, job_id, "baseline")
        tests = f"{cur[0]}/{cur[1]}" if cur else "n/a"
        lines.append(f"verified {'yes' if v.get('verified') else 'no'} · commit {sha} · {state} · "
                     f"tests {tests} (baseline {base[1] if base else 'n/a'})")
        if v.get("unverified_reason"):
            lines.append(f"⚠ tests unverified: {v['unverified_reason']}")
        commits = commits_since_baseline(store, job_id)
        if commits is None:
            lines.append("commits: (no baseline to compare)")
        elif not commits:
            lines.append("commits since baseline: none")
        else:
            lines.append(f"commits since baseline ({len(commits)}):")
            lines += ["  " + c for c in commits[:_COMMITS_MAX]]
        done = last_worker_text(store, job_id, projects_dir)
        if done:
            done_lines = [ln.strip() for ln in redact(done).splitlines() if ln.strip()][:_DONE_LINES]
            lines.append("worker said: " + done_lines[0])
            lines += ["  " + ln for ln in done_lines[1:]]
        else:
            lines.append("worker said: (no transcript found)")
        adv = _advisor_line(store, job_id)
        if adv:
            lines.append(adv)
        summary = _watcher_summary(store, job_id)
        lines.append("watchers: " + (" · ".join(summary) if summary else "none"))
        cost = job_cost_usd(store, job_id) if cost_usd is None else cost_usd
        lines.append(f"cost {'$%.2f' % cost if cost is not None else 'n/a'}")
        if "questions" in dec:
            q, deliverable = dec["questions"], bool(dec.get("deliverable"))
        else:  # e.g. session.build_result passes a bare decision: read the queue here
            q, deliverable = _open_questions(store, job_id)
        if q and q.get("waiting"):
            n = q["waiting"]
            if deliverable:
                lines.append(f"\u26a0 discussion job: {n} questions are its deliverable and still wait for you")
            lines.append(f"{n} open question{'' if n == 1 else 's'} ({q['starred']} \u2605)")
    elif kind == "approval" and rec:
        tool, text, _ = parse_permission(rec["question"])
        lines.append(f"{tool}:")
        lines += ["  " + ln for ln in _wrap(text, width - 2, 3)]
        lines.append("held: " + held_reason(store, job_id, rec["question"]))
        res = rec.get("resolver")
        if res:
            lines += _wrap(f"resolver {res.get('model')} ({res.get('tier')}): escalated — {res.get('reason')}", width, 3)
            if res.get("risk_notes"):
                lines += _wrap("risk: " + res["risk_notes"], width, 2)
        lines.append("asked " + _age(rec["created_at"], now))
    elif rec:  # watcher escalation
        name = rec["question"].removeprefix("watcher ").partition(":")[0]
        ev = custom_watchers.last_verdict(store, job_id, name)
        note = (ev.evidence or {}).get("note") if ev else None
        esc = next((e for e in reversed(store.events(job_id)) if e.what == "watcher_escalated"
                    and (e.evidence or {}).get("approval_id") == rec["id"]), None)
        text = (esc.evidence or {}).get("text") if esc else None
        lines.append(f"watcher {name}" + (f" ({ev.evidence.get('status')})" if ev else "") + ":")
        lines.append("note: " + (note or "(none)"))
        lines.append("text: " + (text or rec["question"].partition(": ")[2]))
        lines.append("asked " + _age(rec["created_at"], now))
    return [_fit(ln, width) for ln in lines]


_CACHE: dict = {}


def panel_cached(store: Store, job_id: str, width: int, *, ttl: float = 10.0, **kw) -> list[str]:
    """`panel` re-run at most every `ttl` seconds per (store, job, width, pending approvals)."""
    pending = tuple(a["id"] for a in approvals_module.list_approvals(store, job_id, status="pending"))
    key = (str(store.home), job_id, width, pending)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    lines = panel(store, job_id, width, **kw)
    _CACHE[key] = (time.monotonic(), lines)
    return lines
