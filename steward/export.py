"""Export a finished job to its own Herdr workspace: `[ export ]` / `steward export JOB [--name N] [--model M]`.

The owner, 2026-09-30: once a run is finished they want to keep working on it directly, outside the Steward lab space.
Export (1) creates a NEW Herdr workspace named after the job's short name (never touching existing ones), cwd = the
job's worktree (or the source repo at the merge commit when the job was incorporated and `STEWARD_EXPORT_CWD` is
not `worktree`), (2) writes a handoff bundle under `<cwd>/.steward/export/`, (3) opens an interactive `claude`
there primed to read `HANDOFF.md` first, and (4) records an `exported` event. The job's state never changes.
Local only: no git push, nothing published; every process goes through the injected runner.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from steward import chat as chat_module
from steward import questions, tabnames
from steward import transcript as transcript_module
from steward.diagnosis import redact
from steward.models import JobState, Judgment
from steward.store import Store

BUNDLE_DIR = ".steward/export"
EXPORT_CWD_ENV = "STEWARD_EXPORT_CWD"  # `worktree` | `source` | `auto` (default: source once incorporated)
DEFAULT_MODEL = "sonnet"
YOLO_FLAG = "--dangerously-skip-permissions"  # claude is the only agent export launches; user settings still load
PERMS_YOLO = "permissions: bypassed (yolo)"
PERMS_NORMAL = "permissions: normal"
KV_KEY = "exported_workspace"
FINISHED = (JobState.ACCEPTED, JobState.RELEASED, JobState.STOPPED, JobState.FAILED)
_UNRESOLVED = ("checks passed; acceptance unresolved", "checks unverified; acceptance unresolved")
FINAL_CAP = 6000
CONDENSED_CAP = 400
CONDENSED_MAX = 400
LIST_CAP = 60


class ExportError(Exception):
    pass


@dataclass(frozen=True)
class ExportResult:
    workspace: str
    name: str
    cwd: str
    bundle: str
    command: str
    resumed: bool
    pane: str | None = None


def exported(store: Store, job_id: str) -> str | None:
    """The workspace label/id a job was exported to, else None (what the button shows afterwards)."""
    rec = store.kv_get(job_id, KV_KEY)
    return (rec.get("name") or rec.get("workspace")) if isinstance(rec, dict) else None


def eligible(store: Store, job) -> bool:
    """A finished job: ACCEPTED, RELEASED, STOPPED, FAILED, or NEEDS_HUMAN waiting at acceptance."""
    if job is None:
        return False
    if job.state in FINISHED:
        return True
    if job.state == JobState.NEEDS_HUMAN:
        return any(e.what in _UNRESOLVED for e in store.events(job.job_id))
    return False


def _git(runner, cwd, *args: str) -> str:
    res = runner.run(["git", *args], cwd=str(cwd))
    return res.stdout if res.exit_code == 0 else ""


def resolve_cwd(store: Store, job, mode: str | None = None) -> tuple[Path, str]:
    """`(cwd, kind)`: the worktree, or the source repo at the merge commit once incorporated (`source`/`auto`)."""
    mode = (mode or os.environ.get(EXPORT_CWD_ENV) or "auto").lower()
    wt = chat_module.worktree_path(store, job.job_id)
    merged = store.kv_get(job.job_id, "incorporated_commit")
    if merged and mode in ("auto", "source") and Path(job.repo).is_dir():
        return Path(job.repo), "source"
    if wt.is_dir():
        return wt, "worktree"
    if Path(job.repo).is_dir() and mode != "worktree":
        return Path(job.repo), "source"
    raise ExportError(f"no worktree for job {job.job_id[:8]} ({wt}) and no source repo to fall back to")


def _final_message(path: Path | None) -> str:
    """The worker's last assistant text, redacted. Read straight from its session file."""
    if path is None:
        return ""
    last = ""
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                content = (obj.get("message") or {}).get("content")
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                        last = b["text"]
    except OSError:
        return ""
    last = redact(last).strip()
    return last if len(last) <= FINAL_CAP else last[:FINAL_CAP] + "\n… (truncated; see the full transcript)"


def _condensed(path: Path | None) -> list[str]:
    """Turns, tool calls, decisions and steers, one line each; thinking and tool results are left to the full file."""
    if path is None:
        return []
    try:
        turns = transcript_module._parsed(path)["turns"]
    except OSError:
        return []
    out = []
    for t in turns:
        if t["kind"] in ("result", "thinking"):
            continue
        label = {"said": "SAID", "tool": "TOOL", "steer": "STEER", "prompt": "PROMPT", "error": "ERROR"}.get(t["kind"], t["kind"])
        text = redact(" ⏎ ".join(x.strip() for x in t["text"].splitlines() if x.strip()))
        if len(text) > CONDENSED_CAP:
            text = text[:CONDENSED_CAP - 1] + "…"
        out.append(f"- {transcript_module._hms(t['ts'])} {label}: {text}")
    if len(out) > CONDENSED_MAX:  # keep the start (the brief) and the end (the outcome)
        head, tail = out[:CONDENSED_MAX // 4], out[-(CONDENSED_MAX * 3 // 4):]
        out = head + [f"- … ({len(out) - len(head) - len(tail)} turns omitted; see the full transcript)"] + tail
    return out


def _git_section(store: Store, job, runner) -> tuple[str, str]:
    """`(diffstat, commits)` of what the job changed: the merge's own range when incorporated, else branch vs source."""
    merged = store.kv_get(job.job_id, "incorporated_commit")
    if merged:
        stat = _git(runner, job.repo, "diff", "--stat", f"{merged}^1", merged)
        log = _git(runner, job.repo, "log", "--oneline", f"{merged}^1..{merged}^2")
        return stat.strip(), log.strip()
    wt = chat_module.worktree_path(store, job.job_id)
    if not wt.is_dir():
        return "", ""
    base = _git(runner, wt, "merge-base", "HEAD", _source_ref(runner, job)).strip()
    if not base:
        return "", _git(runner, wt, "log", "--oneline", "-20").strip()
    return (_git(runner, wt, "diff", "--stat", f"{base}..HEAD").strip(),
            _git(runner, wt, "log", "--oneline", f"{base}..HEAD").strip())


def _source_ref(runner, job) -> str:
    return _git(runner, job.repo, "rev-parse", "HEAD").strip() or "HEAD"


def _bullets(items, empty="none") -> str:
    items = [str(i) for i in items if str(i).strip()]
    return "\n".join(f"- {redact(i)}" for i in items) if items else empty


def _questions_section(store: Store, job_id: str) -> str:
    recs = questions.ordered(questions.load(store, job_id))
    if not recs:
        return "No open questions were recorded for this job."
    lines = []
    for r in recs:
        ans = r.get("answer")
        if isinstance(ans, dict):
            ans = ans.get("text") or ans.get("choice") or ans.get("keys") or json.dumps(ans, default=str)
        lines.append(f"- [{r.get('status')}] {redact(str(r.get('title') or r.get('qid')))}: "
                     f"{redact(str(r.get('question', '')))[:LIST_CAP * 5]}"
                     + (f"\n  answer: {redact(str(ans))}" if ans else ""))
    return "\n".join(lines)


def _steers_section(store: Store, job_id: str) -> str:
    from steward.steering import Steering

    try:
        recs = Steering(store).all_instructions(job_id)
    except Exception:  # noqa: BLE001 - a bundle without the steer list is still a bundle
        recs = []
    lines = [f"- rev {r.get('rev')} [{r.get('verb', 'nudge')}, {r.get('status', '?')}]: "
             f"{redact(str(r.get('text') or r.get('instruction') or ''))[:CONDENSED_CAP]}" for r in recs]
    return "\n".join(lines) or "No steers."


def _cost_section(store: Store, job_id: str, projects_dir: Path | None = None) -> str:
    from steward import tokens

    try:
        tj = tokens.job_tokens(store, job_id, projects_dir=projects_dir)
    except Exception:  # noqa: BLE001
        return "Cost unavailable."
    tot = tj.get("total") or {}
    return (f"- tokens: {tokens.humanize(int(tot.get('all', 0)))} total · effective "
            f"{tokens.humanize(int(tot.get('effective', 0)))} · ${float(tot.get('usd', 0) or 0):.2f}")


def _verification_section(store: Store, job_id: str) -> str:
    from steward.api import StewardAPI

    try:
        v = StewardAPI(store).get_verification(job_id)
    except Exception:  # noqa: BLE001
        return "Verification unavailable."
    lines = [f"- verified: {v.get('verified')} (commit {str(v.get('commit_sha') or '-')[:8]})",
             f"- new failures: {', '.join(v.get('new_failures') or []) or 'none'}",
             f"- preexisting failures: {len(v.get('preexisting_failures') or [])}"]
    for item in v.get("finish_line") or []:
        lines.append(f"- finish line: {redact(json.dumps(item, default=str))[:CONDENSED_CAP]}")
    return "\n".join(lines)


def build_bundle(store: Store, job_id: str, runner, *, projects_dir: Path | None = None, yolo: bool = True) -> dict[str, str]:
    """`{filename: text}`: everything the continuing session needs, secrets redacted."""
    from steward.contract import load_contract

    job = store.get_job(job_id)
    name = tabnames.job_name(store, job, persist=False)
    contract = load_contract(store, job_id)
    path = transcript_module.worker_file(store, job_id, projects_dir)
    stat, commits = _git_section(store, job, runner)
    merged = store.kv_get(job_id, "incorporated_commit")
    final = _final_message(path)
    condensed = _condensed(path)
    events = store.events(job_id)
    pointers = [f"- worker session: {path}" if path else "- worker session: (not found)"]
    pointers.append(f"- worktree: {chat_module.worktree_path(store, job_id)}")
    pointers.append(f"- events: `python3 -m steward.cli events {job_id} --json`")
    pointers.append(f"- transcript: `python3 -m steward.cli transcript {job_id} --last 200`")
    handoff = "\n\n".join([
        f"# Handoff: {name} (Steward job {job_id})",
        f"State at export: {job.state.value} · repo: {job.repo}" + (f" · merged at {merged[:12]}" if merged else " · not merged")
        + "\n" + (PERMS_YOLO if yolo else PERMS_NORMAL),
        "## Goal\n" + redact(contract.goal if contract else job.goal),
        "## Contract\n### Boundaries\n" + _bullets(contract.boundaries if contract else [])
        + "\n### Authority\n" + _bullets(contract.authority if contract else [])
        + "\n### Assumptions\n" + _bullets(contract.assumptions if contract else []),
        "## Finish line\n" + _bullets(contract.done_when if contract else []) + "\n\n### Verification\n" + _verification_section(store, job_id),
        "## The worker's final message\n" + (final or "(none found)"),
        "## What changed\n### Diff stat\n```\n" + (stat or "(none)") + "\n```\n### Commits\n```\n" + (commits or "(none)") + "\n```",
        "## Open questions and their answers\n" + _questions_section(store, job_id),
        "## Steers\n" + _steers_section(store, job_id),
        "## Efficiency / cost\n" + _cost_section(store, job_id, projects_dir) + f"\n- events recorded: {len(events)}",
        "## Full records\n" + "\n".join(pointers),
        "Other files here: TRANSCRIPT.md (condensed), CONTRACT.json. Read them as needed.",
    ])
    return {
        "HANDOFF.md": handoff + "\n",
        "TRANSCRIPT.md": f"# Condensed transcript: {name}\n\n" + ("\n".join(condensed) or "(no worker transcript found)") + "\n",
        "CONTRACT.json": redact(json.dumps(contract.__dict__ if contract else {"goal": job.goal}, indent=2, default=str)) + "\n",
    }


def write_bundle(cwd: Path, files: dict[str, str]) -> Path:
    d = cwd / BUNDLE_DIR
    d.mkdir(parents=True, exist_ok=True)
    for fname, text in files.items():
        (d / fname).write_text(text)
    return d


def first_message(job_id: str) -> str:
    return (f"You are continuing work from Steward job {job_id}. Read {BUNDLE_DIR}/HANDOFF.md first "
            f"(then TRANSCRIPT.md if you need the history), summarise where things stand in 3 short lines, "
            f"and wait for my instructions. The Steward lab is finished with this job; do not git push or publish.")


def build_command(cwd: Path, job_id: str, model: str, session_id: str | None, resume: bool, yolo: bool = True) -> str:
    msg = shlex.quote(first_message(job_id))
    base = (f"cd {shlex.quote(str(cwd))} && env -u ANTHROPIC_API_KEY claude --model {shlex.quote(model)} "
            + (f"{YOLO_FLAG} " if yolo else ""))
    if resume and session_id:
        return base + f"--resume {shlex.quote(session_id)} --fork-session " + msg
    return base + f"--append-system-prompt {shlex.quote(first_message(job_id))} " + msg


def claude_can_resume(runner, cwd) -> bool:
    """True when the installed `claude --help` lists `--resume`."""
    try:
        res = runner.run(["claude", "--help"], cwd=str(cwd), timeout=20)
    except OSError:
        return False
    return res.exit_code == 0 and "--resume" in (res.stdout + res.stderr)


def _workspace_ids(res) -> tuple[str, str | None]:
    try:
        data = json.loads(res.stdout)["result"]
        ws = data["workspace"]
        wid = ws.get("workspace_id") or ws.get("id")
        pane = (data.get("root_pane") or {}).get("pane_id")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ExportError(f"unexpected `herdr workspace create` output: {exc}") from exc
    if not wid:
        raise ExportError("`herdr workspace create` returned no workspace id")
    return str(wid), pane


def export_job(store: Store, job_id: str, runner, *, name: str | None = None, model: str | None = None,
               resume: bool = True, cwd_mode: str | None = None, projects_dir: Path | None = None,
               dry_run: bool = False, yolo: bool = True) -> ExportResult:
    """Create the workspace, write the bundle, open claude. Raises `ExportError` when the job is not finished."""
    job = store.get_job(job_id)
    if job is None:
        raise ExportError(f"unknown job: {job_id}")
    if not eligible(store, job):
        raise ExportError(f"job {job_id[:8]} is {job.state.value}; export needs a finished job "
                          "(ACCEPTED, RELEASED, STOPPED, FAILED, or NEEDS_HUMAN at acceptance)")
    cwd, kind = resolve_cwd(store, job, cwd_mode)
    label = " ".join((name or tabnames.job_name(store, job, persist=False)).split()) or job_id[:8]
    model = model or DEFAULT_MODEL
    sid = chat_module.worker_session_id(store, job_id, projects_dir=projects_dir)
    resumed = bool(resume and sid and claude_can_resume(runner, cwd))
    command = build_command(cwd, job_id, model, sid, resumed, yolo)
    if dry_run:
        return ExportResult("(dry run)", label, str(cwd), str(cwd / BUNDLE_DIR), command, resumed)
    bundle = write_bundle(cwd, build_bundle(store, job_id, runner, projects_dir=projects_dir, yolo=yolo))
    chat_module.ensure_trusted(str(cwd))
    res = runner.run(["herdr", "workspace", "create", "--cwd", str(cwd), "--label", label, "--focus"], cwd=str(store.home))
    if res.exit_code != 0:
        raise ExportError(f"herdr workspace create failed: {res.stderr.strip()}")
    wid, pane = _workspace_ids(res)
    if pane:
        run = runner.run(["herdr", "pane", "run", pane, command], cwd=str(store.home))
        if run.exit_code != 0:
            raise ExportError(f"herdr pane run failed: {run.stderr.strip()}")
    store.kv_set(job_id, KV_KEY, {"workspace": wid, "name": label, "cwd": str(cwd)})
    store.append_event(job_id, what="exported",
                       evidence={"workspace": wid, "name": label, "cwd": str(cwd), "cwd_kind": kind, "model": model,
                                 "resumed": resumed, "yolo": yolo,
                                 "permissions": "bypassed (yolo)" if yolo else "normal", "session_id": sid if resumed else None, "bundle": str(bundle)},
                       rule="operator_export", judgment=Judgment.human)
    return ExportResult(wid, label, str(cwd), str(bundle), command, resumed, pane)
