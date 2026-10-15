"""Durable autopilot merge runs: every attempt reaches a terminal event, across runner restarts (2026-10-05).

Jobs ed40c142 (06:56Z) and c1ecb696 (08:45Z) stalled after `merged_tree_verification_needed`: the merged-tree test
(the full suite, ~50 min) ran synchronously inside the job runner's closeout tick, AFTER `autopilot_attempted` was
consumed. Nothing told the operator it was alive, the operator incorporated and released by hand, the runner went
away with its tab, the test died mid-run (its temp tree left behind, no `finally`), and nothing ever resumed it or
said why: no auto_merged / autopilot_blocked / escalated event.

Now the attempt is a run record (`KEY`) owned by one process (pid + its /proc start time + a per-exec token):
- a merged-tree test predicted to take longer than the inline budget (the job's own measured verification time,
  `bg_verify.predicted_s`) runs as `python3 -m steward.autopilot_bg JOB`, detached in its own session, so a runner
  self-restart (execv), a tab close or a deploy-self never kills it; short ones stay inline;
- while it runs, `autopilot_progress` events (phase, elapsed) every `PROGRESS_EVERY_S`, besides
  `merged_tree_verification_started/finished` from the test itself;
- when it returns, a terminal event is guaranteed (`ensure_terminal`);
- a record still `running` whose owner is gone (killed, rebooted, exec'd) is an orphan: the runner's closeout and the
  control pane's tick (`sweep_all`) resume it detached (`autopilot_resumed`) up to `MAX_RESUMES` times, then escalate
  with the reason; a job that was incorporated/released meanwhile gets `autopilot_skipped` saying so.

`STEWARD_BG_AUTOPILOT=0` keeps every run inline (still recorded, still resumed).
"""

from __future__ import annotations

import contextvars
import fcntl
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from steward import bg_baseline, bg_verify
from steward.models import JobState, Judgment
from steward.store import Store

KEY = "autopilot_run"
ENV = "STEWARD_BG_AUTOPILOT"
PROGRESS_EVERY_S = 600.0     # one `autopilot_progress` event per 10 min of a running merge
STALE_S = 3 * PROGRESS_EVERY_S  # no heartbeat for this long: the owner is gone even if its pid was reused
MAX_RESUMES = 2
TERMINAL = ("auto_merged", "autopilot_blocked", "autopilot_escalated", "autopilot_skipped", "autopilot_error")
_TOKEN = uuid.uuid4().hex   # new on every exec: a runner that exec'd mid-run no longer owns its record
_LIVE = ("starting", "running")


_force_detached: contextvars.ContextVar[bool] = contextvars.ContextVar("autopilot_force_detached", default=False)


@contextmanager
def detached_only():
    """Inside this block `needed` is always true (when enabled): the caller (the control pane) never runs a suite inline."""
    tok = _force_detached.set(True)
    try:
        yield
    finally:
        _force_detached.reset(tok)


def enabled(env=None) -> bool:
    return (os.environ if env is None else env).get(ENV, "1").strip().lower() not in ("0", "off", "false", "no")


def needed(store: Store, job_id: str, runner=None, *, budget_s: float = bg_verify.INLINE_BUDGET_S, env=None) -> bool:
    """Detach when the merged-tree test is predicted to outlast the inline budget (the job's own verification time)
    and the caller runs real subprocesses: the detached process always uses `SubprocessRunner`, so a caller with any
    other runner (a scripted one) keeps the run in-process, where that runner is honoured and nothing escapes."""
    from steward.runner_exec import SubprocessRunner

    if runner is not None and not isinstance(runner, SubprocessRunner):
        return False
    if _force_detached.get():
        return enabled(env)
    pred = bg_verify.predicted_s(store, job_id)
    return enabled(env) and pred is not None and pred > budget_s


def record(store: Store, job_id: str) -> dict | None:
    rec = store.kv_get(job_id, KEY)
    return rec if isinstance(rec, dict) else None


def _event(store: Store, job_id: str, what: str, **evidence) -> None:
    store.append_event(job_id, what=what, evidence=evidence, rule="autopilot_merge", judgment=Judgment.code)


def _proc_start(pid) -> str | None:
    """The process start time (/proc/<pid>/stat field 22): tells a reused pid from the owner."""
    try:
        with open(f"/proc/{int(pid)}/stat") as fh:
            return fh.read().rsplit(") ", 1)[-1].split()[19]
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _own(**extra) -> dict:
    pid = os.getpid()
    return {"pid": pid, "pid_start": _proc_start(pid), "token": _TOKEN, **extra}


_NOTE_LOCK = threading.Lock()  # note() is read-modify-write; the heartbeat thread and the merge both call it


def note(store: Store, job_id: str, **fields) -> None:
    """Merge `fields` into a live run record (phase, temp tree, heartbeat); no-op without one."""
    with _NOTE_LOCK:  # else the heartbeat's write-back can overwrite a phase the merge just recorded
        rec = record(store, job_id)
        if not rec or rec.get("status") not in _LIVE:
            return
        rec.update({k: v for k, v in fields.items() if v is not None})
        store.kv_set(job_id, KEY, rec)


def owner_alive(rec: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if now - float(rec.get("heartbeat_at") or rec.get("started_at") or 0) > STALE_S:
        return False
    pid = rec.get("pid")
    if pid == os.getpid():
        return not rec.get("token") or rec.get("token") == _TOKEN
    if not bg_baseline._alive(pid):
        return False
    start = rec.get("pid_start")
    return not start or _proc_start(pid) in (None, start)


def ensure_terminal(store: Store, job_id: str, res: dict, since_seq: int) -> None:
    """Every attempt ends in a terminal event: when try_merge returned without one, say what happened."""
    if res.get("outcome") == "merged":
        return
    if any(e.what in TERMINAL for e in store.events_since(job_id, since_seq)):
        return
    _event(store, job_id, "autopilot_skipped", reason=str(res.get("detail") or res.get("outcome") or "no result"),
           outcome=res.get("outcome"), needs="human")


def _last_seq(store: Store, job_id: str) -> int:
    evs = store.events(job_id)
    return evs[-1].seq if evs else 0


def _heartbeat(home: Path, job_id: str, stop: threading.Event, started: float, every_s: float) -> None:
    store = Store(home)  # its own connection: this runs beside the (blocking) merge
    while not stop.wait(every_s):
        note(store, job_id, heartbeat_at=time.time())
        rec = record(store, job_id) or {}  # read after the note: the phase as of this beat, never an older one
        _event(store, job_id, "autopilot_progress", phase=rec.get("phase") or "merging",
               elapsed_s=round(time.monotonic() - started, 1), pid=os.getpid())


def _execute(store: Store, job_id: str, runner, *, mode: str, progress_every_s: float = PROGRESS_EVERY_S,
             **kw) -> dict:
    from steward import autopilot

    since = _last_seq(store, job_id)
    prev = record(store, job_id) or {}
    now = time.time()
    store.kv_set(job_id, KEY, {**_own(), "status": "running", "mode": mode, "phase": "merging",
                               "started_at": now, "heartbeat_at": now, "resumes": int(prev.get("resumes") or 0)})
    stop = threading.Event()
    hb = threading.Thread(target=_heartbeat, args=(store.home, job_id, stop, time.monotonic(), progress_every_s),
                          daemon=True, name=f"autopilot-heartbeat-{job_id[:8]}")
    hb.start()
    try:
        res = autopilot.try_merge(store, job_id, runner, **kw)  # never raises
    except Exception as exc:  # noqa: BLE001 - belt and braces: the record must still close
        res = {"outcome": "error", "detail": str(exc)[:200]}
        _event(store, job_id, "autopilot_error", error=res["detail"])
    finally:
        stop.set()
        hb.join(timeout=5)
    res = res if isinstance(res, dict) else {"outcome": "error", "detail": f"unexpected result {res!r}"[:200]}
    ensure_terminal(store, job_id, res, since)
    rec = record(store, job_id) or {}
    rec.update(status="done", outcome=res.get("outcome"), detail=str(res.get("detail") or "")[:300],
               finished_at=time.time())
    store.kv_set(job_id, KEY, rec)
    return res


def run_inline(store: Store, job_id: str, runner, **kw) -> dict:
    """A short merge in the caller's process; still recorded, so a death mid-run is resumed."""
    return _execute(store, job_id, runner, mode="inline", **kw)


def start(store: Store, job_id: str, runner=None, *, resumes: int = 0,
          spawn: Callable[[list[str]], object] | None = None) -> dict:
    """Record the run and launch `python3 -m steward.autopilot_bg JOB` detached. Returns at once."""
    from steward import pkgenv

    now = time.time()
    rec = {"status": "starting", "mode": "background", "phase": "starting", "started_at": now, "heartbeat_at": now,
           "resumes": resumes}
    store.kv_set(job_id, KEY, rec)
    _event(store, job_id, "autopilot_background_started", predicted_s=bg_verify.predicted_s(store, job_id),
           resumes=resumes)
    argv = [*pkgenv.cli_argv()[:-1], "steward.autopilot_bg", job_id]  # `<python> -m steward.autopilot_bg JOB`
    try:
        pid = (spawn or bg_baseline._spawn_detached)(argv)
    except OSError as exc:
        return _give_up(store, job_id, runner, f"background merge could not start: {exc}"[:200], None)
    if isinstance(pid, int):
        pid_start = _proc_start(pid)
        with store.immediate() as tx:  # atomic: the child may already have taken ownership
            cur = tx.kv_get(job_id, KEY)
            if isinstance(cur, dict) and cur.get("status") == "starting":
                tx.kv_set(job_id, KEY, {**cur, "pid": pid, "pid_start": pid_start})
    return {"outcome": "pending", "detail": "merged-tree verification running in the background"}


def _give_up(store: Store, job_id: str, runner, reason: str, notify) -> dict:
    from steward import autopilot
    from steward.runner_exec import SubprocessRunner

    rec = record(store, job_id) or {}
    rec.update(status="escalated", detail=reason, finished_at=time.time())
    store.kv_set(job_id, KEY, rec)
    autopilot._escalate(store, job_id, reason, autopilot.followup_depth(store, job_id), runner or SubprocessRunner(),
                        notify)
    return {"outcome": "escalated", "detail": reason}


def _cleanup_tree(store: Store, job_id: str, tree: str | None) -> None:
    """A killed run never reached its `finally`: drop its temp worktree."""
    if not tree:
        return
    from steward import incorporate as inc

    job = store.get_job(job_id)
    if job is not None:
        inc._git(job.repo, "worktree", "remove", "--force", str(Path(tree) / "tree"))
        inc._git(job.repo, "worktree", "prune")
    shutil.rmtree(tree, ignore_errors=True)


def resume_if_orphaned(store: Store, job_id: str, runner=None, *, notify=None,
                       spawn: Callable[[list[str]], object] | None = None, now: float | None = None) -> dict | None:
    """A live-looking run record whose owner is gone: resume it (detached), escalate after `MAX_RESUMES`, or close it
    with `autopilot_skipped` when the job no longer needs the merge. None when there is nothing to do."""
    rec = record(store, job_id)
    if not rec or rec.get("status") not in _LIVE or owner_alive(rec, now):
        return None
    with open(store.home / f"autopilot-resume-{job_id[:12]}.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # the runner and the control pane may both see the orphan
        try:
            rec = record(store, job_id)
            if not rec or rec.get("status") not in _LIVE or owner_alive(rec, now):
                return None
            return _resume_locked(store, job_id, rec, runner, notify, spawn)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _resume_locked(store, job_id, rec, runner, notify, spawn) -> dict:
    from steward import incorporate as inc

    phase, resumes = rec.get("phase") or "merging", int(rec.get("resumes") or 0)
    reason = f"autopilot merge process {rec.get('pid')} ({rec.get('mode')}) died during {phase} without a result"
    _event(store, job_id, "autopilot_run_lost", pid=rec.get("pid"), mode=rec.get("mode"), phase=phase,
           resumes=resumes, reason=reason)
    _cleanup_tree(store, job_id, rec.get("tree"))
    job = store.get_job(job_id)
    merged = inc.is_incorporated(store, job_id)
    if job is None or job.state != JobState.ACCEPTED or merged:
        why = (f"{reason}; job was incorporated meanwhile ({str(merged)[:12]})" if merged
               else f"{reason}; job is {job.state.value if job else 'gone'} now")
        rec.update(status="abandoned", detail=why, finished_at=time.time())
        store.kv_set(job_id, KEY, rec)
        _event(store, job_id, "autopilot_skipped", reason=why, outcome="abandoned")
        return {"outcome": "skipped", "detail": why}
    if resumes >= MAX_RESUMES:
        return _give_up(store, job_id, runner, f"{reason} (after {resumes} resumes)", notify)
    _event(store, job_id, "autopilot_resumed", attempt=resumes + 1, max=MAX_RESUMES, reason=reason)
    return start(store, job_id, runner, resumes=resumes + 1, spawn=spawn)


def sweep_all(store: Store, runner=None, *, spawn=None, now: float | None = None) -> list[str]:
    """Control-pane backstop: resume every orphaned run (its job runner may have exited long ago)."""
    out = []
    for job_id, rec in store.kv_rows(KEY):
        if isinstance(rec, dict) and rec.get("status") in _LIVE:
            if resume_if_orphaned(store, job_id, runner, spawn=spawn, now=now) is not None:
                out.append(job_id)
    return out


def _phone_notify(store: Store, runner) -> Callable[..., None]:
    """The runner's NEEDS_YOU notifier, for the detached process (same grammar as `Runner._notify`)."""
    from steward import notify
    from steward import push as push_module

    notifier = notify.notifier_from_env(runner)

    def _notify(job_id: str, kind: str, title: str, body: str) -> None:
        title, body = push_module.phone_event(store, job_id, kind, body)
        notify.emit(store, notifier, notify.PhoneEvent(
            kind=kind, job_id=job_id, title=title, body=body,
            deep_link=notify.deep_link(store.kv_get(job_id, "herdr_workspace"), store.kv_get(job_id, "herdr_tab"),
                                       store.kv_get(job_id, "herdr_pane")),
            revision=store.get_revision(job_id)))

    return _notify


def run(store: Store, job_id: str, runner, *, notify=None, **kw) -> dict | None:
    """The detached process body. None when another live process already owns the run."""
    rec = record(store, job_id) or {}
    pid = rec.get("pid")
    if rec.get("status") == "running" and pid not in (None, os.getpid()) and owner_alive(rec):
        return None
    return _execute(store, job_id, runner, mode="background", notify=notify, **kw)


def main(argv: list[str] | None = None) -> int:
    """`python3 -m steward.autopilot_bg JOB`: the detached process `start` launches."""
    import sys

    from steward.runner_exec import SubprocessRunner

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python3 -m steward.autopilot_bg JOB", file=sys.stderr)
        return 2
    store, runner = Store(), SubprocessRunner()
    res = run(store, args[0], runner, notify=_phone_notify(store, runner))
    return 0 if res is None or res.get("outcome") in ("merged", "followup", "skipped", "human") else 1


if __name__ == "__main__":
    raise SystemExit(main())
