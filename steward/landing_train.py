"""Landing train (2026-10-08 spec): continuous, zero-touch landing of ACCEPTED jobs.

`tick` runs on the control loop (every 5 s, self-throttled to settings["every_s"]); it never does slow work itself,
it spawns one detached `python3 -m steward.landing_train run` when the queue is non-empty and no run is alive."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from steward import config
from steward import incorporate as inc
from steward import landing_queue as q
from steward import train_build, train_gates, train_housekeeping, train_land, train_report, train_resolver
from steward.models import Judgment
from steward.store import Store

PSEUDO = "_train"
MIN_RESOLVE_S = 30.0
FIXUP_PARKS = 2                    # the second park starts the entry's one rebase fix-up (spec §5.4)
FIXUP_SPAWNING = "spawning"        # queue-row `fixup` markers that name no job: the slot is spent, the owner is asked
FIXUP_NONE = "none"
FIXUP_ASKED_KEY = "train_fixup_asked"   # kv on the parent: the fix-up (id or marker) already asked about
ASK_TRIED = ["train x3", "rebase fix-up"]


def _read_stat(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""


def _stat_fields(pid: int, read_stat=None) -> list[str]:
    """/proc/<pid>/stat fields after the `(comm)`: [0] = state (field 3), [19] = start time (field 22)."""
    raw = (read_stat or _read_stat)(pid)
    return raw.rsplit(")", 1)[1].split() if ")" in raw else []


def _start_time(pid: int, read_stat=None) -> str:
    """Field 22 of /proc/<pid>/stat (process start, in ticks); "" when unreadable."""
    f = _stat_fields(pid, read_stat)
    return f[19] if len(f) > 19 else ""


def _pid_alive(pid: int, start: str = "", read_stat=None) -> bool:
    f = _stat_fields(pid, read_stat)
    if f and f[0] in ("Z", "X"):                # a zombie (exited, not yet reaped) is not a live run
        return False
    if start and (f[19] if len(f) > 19 else "") != start:     # pid reused by an unrelated process (or gone)
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_alive(store: Store) -> bool:
    rec = store.kv_get(PSEUDO, "run") or {}
    pid = int(rec.get("pid") or 0)
    return pid > 0 and _pid_alive(pid, str(rec.get("start") or ""))


def _spawn(store: Store, cmd: str = "run") -> int:
    log = Path(store.home) / "train-report" / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a")
    try:
        p = subprocess.Popen([sys.executable, "-m", "steward.landing_train", cmd], stdout=fh,
                             stderr=subprocess.STDOUT, start_new_session=True,
                             cwd=str(Path(__file__).resolve().parent.parent))
    finally:
        fh.close()
    return p.pid


def _tg(text: str) -> bool:
    from steward import notify
    return notify.tg_send_text(text)


def tick(store: Store, runner, *, now: float | None = None, settings: dict | None = None, spawn=None) -> dict:
    s = settings or config.train_settings()
    if not (s["enabled"] and s["repo"]):
        return {"skipped": "off"}
    now = time.time() if now is None else now
    last = float(store.kv_get(PSEUDO, "tick_at") or 0.0)
    if last and now - last < s["every_s"]:
        return {"skipped": "throttle"}
    store.kv_set(PSEUDO, "tick_at", now)
    q.sync_accepted(store, s["repo"])
    head = q.queued(store, s["repo"], 1)
    parked_work = not head and _parked_work(store, s["repo"], s.get("target", ""))
    if head:
        try:
            with inc.target_lock(store, s["repo"], timeout=0, purpose="train heal lock"):
                healed = inc.heal_index_lock(store, head[0].job_id, s["repo"], now=now)
        except inc.LockBusy:
            healed = {}
        if healed.get("healed"):
            cleared = list(store.kv_get(PSEUDO, "locks_cleared") or [])
            cleared.append(healed)
            store.kv_set(PSEUDO, "locks_cleared", cleared)
    if run_alive(store):
        return {"skipped": "running"}
    if not head and not parked_work:        # a run sends the morning report itself; with no queue, spawn just that
        if not train_report.due(store, now=now, report_hour=s["report_hour"]):
            return {"skipped": "empty"}
        pid = spawn("report") if spawn else _spawn(store, "report")
        store.kv_set(PSEUDO, "run", {"pid": pid, "since": now, "start": _start_time(pid), "cmd": "report"})
        return {"spawned_report": pid}
    pid = spawn("run") if spawn else _spawn(store)
    store.kv_set(PSEUDO, "run", {"pid": pid, "since": now, "start": _start_time(pid)})
    return {"spawned": pid}


def _control_relay(store: Store):
    """The CLI write path (OPX-01): a cmd frame into the live control pane, which is the single writer."""
    def relay(op: str, args: dict) -> dict:
        from steward import cli

        try:
            return cli._relay(store, op, args, request_id=f"train-{uuid.uuid4().hex[:12]}", origin="landing-train")
        except cli._RelayFailed as exc:
            return {"accepted": False, "detail": str(exc)}
    return relay


def _default_stop(store: Store, relay_factory=None):
    """Stop a fix-up through the control pane; the relay is only built when there is something to stop."""
    relay: list = []

    def stop(job_id: str) -> dict:
        if not relay:
            relay.append((relay_factory or (lambda: _control_relay(store)))())
        return relay[0]("stop", {"job_id": job_id})
    return stop


def _once_per_head(store: Store, notify=None):
    """Notifier that sends a message at most once per train head (resume_pending retries every run)."""
    send = notify or _tg

    def notify_fn(text: str, head: str = "") -> None:
        sent = list(store.kv_get(PSEUDO, "notified") or [])
        if head in sent:                       # one notice per train head, whatever the text says
            return
        if send(text):                         # record only a delivered notice: a failed send retries next run
            sent.append(head)
            store.kv_set(PSEUDO, "notified", sent[-50:])

    return notify_fn


def _bind_head(notify_fn, head: str):
    return lambda text: notify_fn(text, head)


def _parked_work(store: Store, repo, target: str) -> bool:
    """Cheap tick check: does a parked entry need a run (retry on a moved target, its fix-up, cleanup, the ask)?"""
    from steward.machine import TERMINAL_STATES
    from steward.models import JobState

    rows = q.parked(store, repo)
    if not rows:
        return False
    th = train_build.target_sha(repo, target) if target and any(not e.fixup for e in rows) else ""
    for e in rows:
        job = store.get_job(e.job_id)
        if job is None or job.state != JobState.ACCEPTED or inc.is_incorporated(store, e.job_id):
            return True
        if not e.fixup:
            if e.parks >= FIXUP_PARKS or (th and e.last_target != th):
                return True
            continue
        fx = store.get_job(e.fixup)
        if (fx is None or fx.state in TERMINAL_STATES) and store.kv_get(e.job_id, FIXUP_ASKED_KEY) != e.fixup:
            return True
    return False


def _default_rebase_spawn(store: Store, runner, target: str, *, start_followup=None):
    """spawn(job_id) -> child id | None: the entry's one rebase fix-up through `autopilot._repair` (the repair
    episode, repair depth capped at 1), goal `Merge `<target>` into YOUR branch ... (rebase for the landing train)`."""
    def spawn(job_id: str) -> str | None:
        from steward import autopilot

        job, e = store.get_job(job_id), q.get(store, job_id)
        pf = inc.preflight(store, job_id)
        if pf.error or pf.source_branch != target:   # never a fix-up merging some other branch than the target
            store.append_event(job_id, what="train_rebase_fixup_refused",
                               evidence={"error": pf.error, "source_branch": pf.source_branch, "target": target},
                               rule="landing_train", judgment=Judgment.code)
            return None
        detail = (f"rebase for the landing train (parked {e.parks if e else '?'}x; last: "
                  f"{(e.last_reason if e else '')[:160]})")
        res = autopilot._repair(store, job, pf, detail, list(pf.conflicts), runner,
                                start_followup or autopilot._default_followup, None, max_depth=1)
        child = res.get("follow_up") if res.get("outcome") in ("followup", "pending") else None
        return child if child and store.get_job(child) is not None else None
    return spawn


def maybe_rebase_fixup(store: Store, job_id: str, *, spawn) -> str | None:
    """Start the parked entry's one rebase fix-up (from its second park on). The slot is claimed before the slow
    spawn and never given back: a spawn that fails or raises leaves a marker and the owner is asked. Returns the
    child id when one was started now, else None."""
    from steward.models import JobState

    e = q.get(store, job_id)
    if e is None or e.status != "parked" or e.fixup or e.parks < FIXUP_PARKS:
        return None
    job = store.get_job(job_id)
    if job is None or job.state != JobState.ACCEPTED or inc.is_incorporated(store, job_id):
        return None
    if not q.claim_fixup(store, job_id, FIXUP_SPAWNING):
        return None
    try:
        child = spawn(job_id)
    except Exception as exc:  # noqa: BLE001 - a failed start is the ask, never a crashed train run
        store.append_event(job_id, what="train_rebase_fixup_failed",
                           evidence={"error": f"{type(exc).__name__}: {exc}"[:300], "parks": e.parks},
                           rule="landing_train", judgment=Judgment.code)
        return None
    q.set_fixup(store, job_id, child or FIXUP_NONE)
    store.append_event(job_id, what="train_rebase_fixup" if child else "train_rebase_fixup_failed",
                       evidence={"child": child, "parks": e.parks, "last_target": e.last_target,
                                 "last_reason": e.last_reason[:200]},
                       rule="landing_train", judgment=Judgment.code)
    return child or None


def _parked_pass(store: Store, repo, target_head: str, *, spawn, ask_fn, now: float) -> dict:
    """Spec §5.4 parked entries, once per train run: clean up entries whose job left ACCEPTED or landed some other
    way, put entries parked against an older target back in the queue, start the one rebase fix-up from the second
    park, and ask the owner once when that fix-up ended without carrying the parent. Never raises per entry."""
    from steward.machine import TERMINAL_STATES
    from steward.models import JobState

    out: dict = {"landed": [], "removed": [], "requeued": [], "fixups": {}, "asked": [], "errors": {}}
    for e in q.parked(store, repo):
        try:
            commit = inc.is_incorporated(store, e.job_id)
            if commit:
                q.mark_landed(store, e.job_id, str(commit))
                inc.release_on_incorporation(store, e.job_id)
                out["landed"].append(e.job_id)
                continue
            job = store.get_job(e.job_id)
            if job is None or job.state != JobState.ACCEPTED:
                q.remove(store, e.job_id, "job is gone" if job is None else f"job is {job.state.value}")
                out["removed"].append(e.job_id)
        except Exception as exc:  # noqa: BLE001
            out["errors"][e.job_id] = f"{type(exc).__name__}: {exc}"[:200]
    out["requeued"] = q.requeue_parked(store, repo, target_head)
    for e in q.parked(store, repo):
        jid = e.job_id
        try:
            fixup = e.fixup
            if not fixup:
                child = maybe_rebase_fixup(store, jid, spawn=spawn)
                if child:
                    out["fixups"][jid] = child
                    continue
                fixup = (q.get(store, jid) or e).fixup
            if not fixup:
                continue
            fx = store.get_job(fixup)
            if fx is not None and fx.state not in TERMINAL_STATES:
                continue                         # the fix-up is the parent's actor (liveness "fixup"): wait
            if store.kv_get(jid, FIXUP_ASKED_KEY) == fixup:
                continue                         # asked once per fix-up
            if fx is not None:
                tried = list(ASK_TRIED)
            elif fixup == FIXUP_NONE:
                tried = [ASK_TRIED[0], "rebase fix-up (could not start)"]
            else:
                tried = [ASK_TRIED[0], "rebase fix-up (start failed)"]
            bid = ask_fn(store, store.get_job(jid), tried=tried, now=now)
            if bid:
                store.kv_set(jid, FIXUP_ASKED_KEY, fixup)
                store.append_event(jid, what="train_fixup_ask",
                                   evidence={"fixup": fixup, "fixup_state": fx.state.value if fx else None,
                                             "blocker_id": bid, "tried": tried, "parks": e.parks},
                                   rule="landing_train", judgment=Judgment.code)
                out["asked"].append(jid)
        except Exception as exc:  # noqa: BLE001 - one bad entry never stops the train
            out["errors"][jid] = f"{type(exc).__name__}: {exc}"[:200]
    return out


def _landable(store: Store, repo, limit: int) -> list[str]:
    """Queued jobs in order, leaving out a parent while a queued ACCEPTED merge fix-up of it exists: the fix-up's
    branch carries the parent, and landing it marks the parent incorporated."""
    from steward.models import JobState
    from steward.train_housekeeping import MERGE_GOAL_PREFIX

    entries = q.queued(store, repo, 1_000_000)
    carried = set()
    for e in entries:
        job = store.get_job(e.job_id)
        parent = store.kv_get(e.job_id, "autopilot_followup")
        if job is not None and job.state == JobState.ACCEPTED and job.goal.startswith(MERGE_GOAL_PREFIX) \
                and isinstance(parent, str):
            carried.add(parent)
    return [e.job_id for e in entries if e.job_id not in carried][:limit]


def run_once(store: Store, runner, settings: dict, **kw) -> dict:
    """rerere is switched on for this process's git children only (env), never in any repo's shared config."""
    old_env = {k: os.environ.get(k) for k in os.environ if k.startswith("GIT_CONFIG_")}
    base = int(os.environ.get("GIT_CONFIG_COUNT") or 0)
    for i, (k, v) in enumerate((("rerere.enabled", "true"), ("rerere.autoupdate", "true"))):
        os.environ[f"GIT_CONFIG_KEY_{base + i}"] = k
        os.environ[f"GIT_CONFIG_VALUE_{base + i}"] = v
    os.environ["GIT_CONFIG_COUNT"] = str(base + 2)
    try:
        return _run_once(store, runner, settings, **kw)
    finally:
        for k in [k for k in os.environ if k.startswith("GIT_CONFIG_")]:
            del os.environ[k]
        for k, v in old_env.items():
            if v is not None:
                os.environ[k] = v


class _GateError(Exception):
    """The gate itself raised (candidate-side error, not a base verdict): this one run skips, drops nothing and
    persists nothing that blocks the next run (an unusable base is NOT this: it is judged absolutely, §5.5)."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


def _ids(xs, k: int = 10) -> str:
    xs = list(xs or [])
    return repr(xs[:k]) + (f"+{len(xs) - k}" if len(xs) > k else "")


def _default_gate(store: Store, runner, s: dict, root: Path, now: float):
    """`train_gates.gate` on the built tree (a raise is handled by `_run_once`: that run skips, nothing persists)."""
    from steward import check_runs

    digest = check_runs.env_digest(execution="xdist")

    def gate_fn(wt, base_sha: str) -> dict:
        return train_gates.gate(wt, base_sha, runner, store=store, repo=s["repo"], suite_cmd=s["suite_cmd"],
                                tmp_root=root / "tmp", env_digest=digest, now=now)
    return gate_fn


def _suite_gate(suite_fn, runner):
    """The absolute test seam: `suite_fn(wt) -> bool` (no base comparison), then the tests-removed gate."""
    def gate_fn(wt, base_sha: str) -> dict:
        if not suite_fn(wt):
            return {"ok": False, "why": "red suite"}
        removed = train_gates.tests_removed(wt, base_sha, runner)
        why = ("tests removed: " + ", ".join(sorted({f.kind for f in removed}))) if removed else ""
        return {"ok": not why, "why": why, "removed": [f.kind for f in removed]}
    return gate_fn


def _self_deploy_fns(store: Store, repo):
    """(deploy_fn, preflight_fn) for landing on Steward's own repo. Both run in a fresh interpreter at the repo's
    current HEAD: this process has the pre-landing code cached, and importing a changed module into it fails on a
    correct landing (train 30), so only a genuine failure at the new HEAD is red."""
    from steward import deploy_self

    return (lambda: deploy_self.fresh_deploy(store, root=repo),
            lambda: deploy_self.fresh_preflight(store, root=repo))


def _run_once(store: Store, runner, settings: dict, *, resolve_fn=None, suite_fn=None, deploy_fn=None,
              stop_fn=None, now: float | None = None, notify=None, preflight_fn=None, rebase_spawn=None,
              ask_fn=None, gate_fn=None) -> dict:
    s, now = settings, (time.time() if now is None else now)
    repo, target = s["repo"], s["target"]
    root = Path(s["worktree_root"])
    if runner is None:
        from steward.runner_exec import SubprocessRunner
        runner = SubprocessRunner()
    deadline = [0.0]
    if resolve_fn is None:
        def resolve_fn(wt, jid, files):
            left = deadline[0] - time.time()
            if left <= MIN_RESOLVE_S:
                return False
            job = store.get_job(jid)
            return train_resolver.resolve(wt, jid, files, goal=job.goal if job else "", target=target,
                                          runner=runner, timeout_s=max(0.0, left), tmp_dir=root / "tmp" / "resolver")
    if gate_fn is None:                     # §5.5: new failures vs the base; `suite_fn` stays the absolute test seam
        gate_fn = _suite_gate(suite_fn, runner) if suite_fn is not None else _default_gate(store, runner, s, root, now)

    if deploy_fn is None and Path(repo).resolve() == Path(__file__).resolve().parent.parent:
        deploy_fn, default_preflight = _self_deploy_fns(store, repo)
        preflight_fn = preflight_fn or default_preflight
    once = _once_per_head(store, notify)
    train_report.maybe_send_daily(store, now=now, report_hour=s["report_hour"], send=notify or _tg)
    cleared = list(store.kv_get(PSEUDO, "locks_cleared") or [])

    def written() -> None:                  # healed locks are reported once, then cleared
        store.kv_set(PSEUDO, "locks_cleared", [])
        store.kv_set(PSEUDO, "last_wait", "")

    def waiting(line: str) -> None:         # a wait that repeats every run is reported once, not every run
        if store.kv_get(PSEUDO, "last_wait") != line:
            train_report.append(store, line, now=now)
            store.kv_set(PSEUDO, "last_wait", line)

    def unresolved(head: str) -> dict:
        train_report.append(store, f"pending landing unresolved: {head[:7]} - needs the operator; "
                                   f"locks cleared {cleared}", now=now)
        written()
        return {"aborted": "pending landing unresolved", "head": head}

    intent = store.kv_get(*train_land.KEY) or {}
    ihead = str(intent.get("train_head", ""))
    if intent and not intent.get("deferred"):  # a deferred deploy (never ran) does not use up the one resume
        tries = dict(store.kv_get(PSEUDO, "resume_tries") or {})
        if not intent.get("reverted") and tries.get(ihead, 0) >= 1:
            return unresolved(ihead)           # a stuck un-reverted landing must not redeploy every run
        tries[ihead] = tries.get(ihead, 0) + 1
        store.kv_set(PSEUDO, "resume_tries", dict(list(tries.items())[-50:]))
    pending = train_land.resume_pending(store, deploy_fn=deploy_fn, notify_fn=_bind_head(once, ihead),
                                        preflight_fn=preflight_fn)
    left = store.kv_get(*train_land.KEY) or {}
    if left.get("deferred"):
        lhead = str(left.get("train_head", ihead))
        waiting(f"landing {lhead[:7]} deploy deferred: {left['deferred']}")
        return {"deferred": left["deferred"], "head": lhead}
    if pending is not None:
        train_report.append(store, f"resumed landing: {pending}", now=now)
    if left:
        return unresolved(str(left.get("train_head", ihead)))
    q.prune(store, repo)                     # jobs that left ACCEPTED (or landed some other way) since queueing
    target_head = train_build.target_sha(repo, target)
    if ask_fn is None:
        from steward import liveness
        ask_fn = liveness.ask
    parked = _parked_pass(store, repo, target_head, ask_fn=ask_fn, now=now,
                          spawn=rebase_spawn or _default_rebase_spawn(store, runner, target))
    if any(parked[k] for k in parked):
        train_report.append(store, f"parked: requeued {parked['requeued']}; fix-ups {parked['fixups']}; "
                                   f"asked {parked['asked']}; landed {parked['landed']}; removed {parked['removed']}"
                                   + (f"; errors {parked['errors']}" if parked["errors"] else ""), now=now)
    why = train_land._preflight(preflight_fn)
    if why:                                 # no suites, no drops: the train waits for a deploy to be possible
        waiting(f"train waiting: deploy not possible: {why}")
        return {"skipped": f"deploy not possible: {why}"}
    deadline[0] = time.time() + float(s["resolver_timeout_s"])

    ids = _landable(store, repo, int(s["max_batch"]))
    if not ids:
        return {"skipped": "nothing to land after the queue re-check"}
    n = int(store.kv_get(PSEUDO, "train_n") or 0) + 1
    store.kv_set(PSEUDO, "train_n", n)
    wt = train_build.prepare_worktree(repo, root, target)

    first = train_build.build_train(repo, wt, target, ids, n, resolve_fn=resolve_fn)
    candidates = list(first.merged)
    green_head: dict[tuple, tuple[str, str]] = {}
    cache: dict[tuple, bool] = {}
    runs = {"green": 0, "red": 0}
    builds = [1]

    calls: list[tuple[tuple, str]] = []          # (subset, why red) per red gate run, in order

    def is_green(subset: list[str]) -> bool:
        key = tuple(subset)
        if key in cache:
            return cache[key]
        if subset == candidates and builds[0] == 1 and not first.dropped:
            res = first                          # worktree still holds the first build: just run the gates
        else:
            res = train_build.build_train(repo, wt, target, subset, n, resolve_fn=resolve_fn)
            builds[0] += 1
        why = ""
        if res.dropped:                          # a subset drop is red, not a drop
            why = "did not merge in a smaller train: " + "; ".join(f"{k[:8]} {v}" for k, v in res.dropped.items())
        else:
            try:
                g = gate_fn(wt, res.base_sha)
            except Exception as exc:  # noqa: BLE001 - e.g. the base worktree could not be added
                raise _GateError(f"gate error: {type(exc).__name__}: {exc}"[:300]) from exc
            why = "" if g.get("ok") else (g.get("why") or "red suite")
            if any(k in g for k in ("new_failures", "preexisting", "reran_green")):
                train_report.append(store, f"train {n} train_gate {len(subset)} job(s): "
                                           + (f"base {res.base_sha[:7]} unusable (no per-test results): judged "
                                              f"absolutely; " if g.get("base_unusable") else "")
                                           + f"preexisting={_ids(g.get('preexisting'))} "
                                           f"new_failures={_ids(g.get('new_failures'))} "
                                           f"reran_green={_ids(g.get('reran_green'))} fixed={_ids(g.get('fixed'))}",
                                    now=now)
            if g.get("base_unusable"):           # one notice per base head; never a skip, never a freeze (§5.5)
                once(f"{target} {res.base_sha[:7]}: base suite unusable (no per-test results); the train judges "
                     f"candidates absolutely until it recovers", f"base-unusable:{res.base_sha}")
            if g.get("preexisting"):             # a red base is a report line and one notice per base head (§5.5)
                once(f"{target} {res.base_sha[:7]} has {len(g['preexisting'])} pre-existing failing test(s); "
                     f"the train lands around them: {_ids(g['preexisting'], 5)}", f"base-red:{res.base_sha}")
        ok = not why
        if not ok:
            calls.append((key, why))
        runs["green" if ok else "red"] += 1
        if ok:
            green_head[key] = (res.head, res.base_sha)
        cache[key] = ok
        return ok

    good: list[str] = []
    bad: list[str] = []
    if candidates:
        try:
            good, bad = train_build.select_landable(candidates, is_green)
        except _GateError as e:                  # this run only: no drop, nothing persisted that blocks the next run
            waiting(f"train skipped a run at {first.base_sha[:7]}: {e.why}")   # deduped report line, not a gate
            once(f"{target} {first.base_sha[:7]}: landing train gate failed ({e.why}); retrying next run",
                 f"gate-error:{first.base_sha}")
            return {"skipped": e.why, "train": n}
    dropped: dict[str, str] = dict(first.dropped)
    for jid in bad:                              # the last red run ending in jid is its single-culprit run
        dropped.setdefault(jid, next((why for key, why in reversed(calls) if key and key[-1] == jid), "red suite"))
    for jid in good:
        dropped.pop(jid, None)
    for jid, why in dropped.items():
        if q.get(store, jid):
            q.mark_dropped(store, jid, why, target_head=first.base_sha or target_head)

    out = {"train": n, "landed": [], "dropped": dropped}
    head = ""
    if good:
        head, base = green_head[tuple(good)]
        res = train_land.land(store, repo, target, head, base, good, deploy_fn=deploy_fn,
                              notify_fn=_bind_head(once, head), preflight_fn=preflight_fn)
        out["landed"] = res["landed"]
        out["land"] = {k: res[k] for k in ("ok", "reason", "reverted")}
    _stop = stop_fn or _default_stop(store)
    stopped = train_housekeeping.stop_superseded_fixups(store, _stop)
    stopped += [j for j in train_housekeeping.stop_marked_fixups(store, _stop) if j not in stopped]
    archived = train_housekeeping.archive_orphan_branches(store, repo)
    train_report.append(store, f"train {n}: landed {len(out['landed'])} {out['landed']} at {head[:12] or '-'}; "
                               f"dropped {dropped}; land={out.get('land')}; suite runs: {runs['green']} green / "
                               f"{runs['red']} red; locks cleared {cleared}; stopped fix-ups {stopped}; "
                               f"archived {len(archived)}", now=now)
    written()
    return out


def main(argv: list[str]) -> int:
    store = Store()
    s = config.train_settings()
    if argv[:1] == ["status"]:
        print({"settings": s, "pending": store.kv_get(*train_land.KEY) or {}, "alive": run_alive(store), "queued": [e.job_id for e in q.queued(store, s["repo"], 50)],
               "parked": [(e.job_id, e.last_reason) for e in q.parked(store, s["repo"])]})
        return 0
    if argv[:1] in (["run"], ["report"]):
        if not s["enabled"]:
            return 0
        rec = store.kv_get(PSEUDO, "run") or {}
        if run_alive(store) and int(rec.get("pid") or 0) != os.getpid():
            return 0
        store.kv_set(PSEUDO, "run", {"pid": os.getpid(), "since": time.time(), "start": _start_time(os.getpid()),
                                     "cmd": argv[0]})
        try:
            if argv[0] == "report":
                print({"report_sent": train_report.maybe_send_daily(store, now=time.time(),
                                                                    report_hour=s["report_hour"], send=_tg)},
                      flush=True)
            else:
                print(run_once(store, None, s), flush=True)
        except Exception as exc:  # noqa: BLE001 - a crashed run must leave a trace, never die silently
            _crashed(store, exc, argv[0])
            return 1
        finally:
            store.kv_set(PSEUDO, "run", {})
        return 0
    print("usage: python3 -m steward.landing_train run|report|status")
    return 2


def _crashed(store: Store, exc: Exception, cmd: str) -> None:
    """Report line every time; a tg notice at most once per day per exception type."""
    import traceback

    now = time.time()
    kind = type(exc).__name__
    try:
        train_report.append(store, f"train {cmd} crashed: {kind}: {str(exc)[:300]}", now=now)
    except OSError:
        pass
    traceback.print_exc()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    seen = dict(store.kv_get(PSEUDO, "crash_notified") or {})
    if seen.get(kind) == today:
        return
    try:
        sent = _tg(f"Steward landing train {cmd} crashed: {kind}: {str(exc)[:300]} (see the train report)")
    except Exception:  # noqa: BLE001
        sent = False
    if sent:
        seen[kind] = today
        store.kv_set(PSEUDO, "crash_notified", seen)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
