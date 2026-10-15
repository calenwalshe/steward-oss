"""The coordinator daemon (spec 5.16, §4): heartbeat, triggers, episodes, holds, rungs, model rung, close.

The main loop never blocks on episode work: ``tick`` writes the heartbeat, scans, opens/attaches episodes, relays
holds, preempts and enforces the ask deadline, then hands at most ONE episode step to a single worker thread. While
that step is in flight the tick does bookkeeping only (and never touches the episode being stepped). ``inline=True``
runs the step synchronously inside ``tick`` (tests).

Episode step (6.3), kept on the episode row:
- acted: run a due follow-up; ``unstuck`` sets ``unstuck_at`` (cleared when stuck again); ``resolved`` closes
  ``resolved``; a re-park after acting (park event newer than the action, or an attached park trigger) relays a fresh
  hold and walks on to the next rung;
- not acted: ``rungs.select`` -> run its verbs (stop at the first failure) / close ``asked`` on an empty Plan; no Plan
  -> the model rung when allowed (``model.plan_episode``) -> else close ``asked`` (``gave_up`` when the model budget
  blocked it). Job-less ``health:*`` episodes never hold and never ask: they act or close ``noted`` (R18).

Holds: every job episode relays ``coordinator hold`` when it opens and records ``held`` in its evidence; a failed
relay is retried on later steps. An ask-only episode (cap or cooldown) closes ``asked`` only once its hold is
recorded (the relay handler sends the owner line only for a held episode), or at ``ask_by_s`` regardless. Every
``asked``/``gave_up`` close of a never-held episode tries the hold once more first.
Acting re-checks the ask deadline and the Steward-side hold just before the verbs run (R1/R2: never act after the
owner was told); a rerun's budget is capped to the time left before ``ask_by_s``; the model rung waits while a
tech-lead review is in flight (R17).
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from .. import coordinator_link, deploy_pending, health as health_mod, launch, stability
from . import episodes, lifecycle, model, reruns, rungs, triggers, verbs
from .lifecycle import BUDGET_TEXT, MODEL_RUNG, apply_precision, ev_of, job_of, merge_ev  # noqa: F401

log = logging.getLogger("steward.coordinator.daemon")

WATCHDOG_UNIT = "steward-watchdog.service"
CACHE_S = 60.0
HEALTH_FRESH_S = 120.0
HEALTH_CLI_EVERY_S = 300.0
OPEN_LOOKBACK_S = 7 * 86400.0


def _add(closed: list, result) -> None:
    """Record a close; None means the episode was already closed elsewhere (nothing happened)."""
    if result:
        closed.append(result)


def _x(eid=None, job=None) -> dict:
    return {"extra": {"episode": str(eid or "-")[:12], "job8": str(job or "-")[:8]}}


class Daemon:
    def __init__(self, settings, *, ro_factory, cdb, run=subprocess.run, clock=time.time, runner_alive=None,
                 health_fn=None, watchdog_active=None, workspace_fn=None, head_fn=None, rerun_fn=reruns.rerun,
                 execv=os.execv, tg_send=None, digest_fn=None, inline=False):
        self.settings = settings
        self.ro_factory = ro_factory
        self.cdb = cdb
        self.run = run
        self.clock = clock
        self.runner_alive = runner_alive or (lambda job_id: launch.runner_alive(settings.steward_home, job_id))
        self.health_fn = health_fn or self._default_health
        self._watchdog_fn = watchdog_active if watchdog_active is not None else self._systemctl_watchdog
        self._workspace_fn = workspace_fn or (lambda: verbs.workspace(settings.steward_home, run=self.run))
        self.head_fn = head_fn or stability.repo_head
        self.rerun_fn = rerun_fn
        self.execv = execv
        self.tg_send = tg_send
        self.digest_fn = digest_fn
        self.inline = inline
        self.root = str(settings.steward_repo)
        self.process_head = self.head_fn(self.root) or ""
        self._pool = None if inline else concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                                               thread_name_prefix="coord-step")
        self._future = None
        self._inflight: str | None = None
        self._stepped: dict[str, float] = {}
        self._cache: dict[str, tuple[float, object]] = {}
        self._ro_main = None
        self._worker_ro = None
        self._worker_cdb = None
        self._health_cli: tuple[float, dict | None] = (0.0, None)
        self._budget_blocked = False

    # ---------- plumbing ----------

    def _ro(self):
        if self._ro_main is None:
            self._ro_main = self.ro_factory()
        return self._ro_main

    def _wro(self):
        if self.inline:
            return self._ro()
        if self._worker_ro is None:
            self._worker_ro = self.ro_factory()
        return self._worker_ro

    def _wcdb(self):
        if self.inline:
            return self.cdb
        if self._worker_cdb is None:
            from .db import CoordDB

            self._worker_cdb = CoordDB(self.cdb.path)
        return self._worker_cdb

    def _cached(self, key, fn):
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < CACHE_S:
            return hit[1]
        val = fn()
        self._cache[key] = (now, val)
        return val

    def watchdog_active(self):
        """R11: the watchdog unit's state (True/False), None when unknown (rungs treat None as active)."""
        fn = self._watchdog_fn
        return self._cached("watchdog", fn) if callable(fn) else fn

    def _systemctl_watchdog(self):
        try:
            r = self.run(["systemctl", "--user", "is-active", WATCHDOG_UNIT], capture_output=True, text=True,
                         timeout=10)
        except Exception:  # noqa: BLE001
            return None
        return (r.stdout or "").strip() == "active"

    def workspace(self) -> str | None:
        return self._cached("workspace", self._workspace_fn)

    def _default_health(self):
        """health.json when ≤120 s old, else `steward health --json --no-ping` at most every 300 s (spec 5.9)."""
        now = self.clock()
        try:
            data = json.loads((Path(self.settings.steward_home) / health_mod.STATE_FILE).read_text())
            if isinstance(data, dict) and now - float(data.get("checked_at") or 0) <= HEALTH_FRESH_S:
                return data
        except (OSError, ValueError, TypeError):
            pass
        at, cached = self._health_cli
        if now - at < HEALTH_CLI_EVERY_S:
            return cached
        data = None
        try:
            r = self.run([sys.executable, "-m", "steward.cli", "health", "--json", "--no-ping"],
                         env={**os.environ, "STEWARD_HOME": str(self.settings.steward_home)},
                         cwd=self.settings.steward_repo, capture_output=True, text=True, timeout=60)
            data = json.loads(r.stdout or "")
        except Exception as exc:  # noqa: BLE001 - health is best effort
            log.info("health read failed: %s", exc, **_x())
        self._health_cli = (now, data if isinstance(data, dict) else None)
        return self._health_cli[1]

    def idle(self) -> bool:
        return self._future is None or self._future.done()

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)

    # ---------- the loop ----------

    def run_forever(self, *, sleep=time.sleep) -> int:
        try:
            while True:
                try:
                    out = self.tick()
                    if "stopped" in out:
                        log.info("stopping: %s", out["stopped"], **_x())
                        return 0
                except Exception:  # noqa: BLE001 - the loop never dies on one bad tick
                    log.exception("tick failed", **_x())
                sleep(self.settings.poll_s)
        finally:
            self.shutdown()

    def tick(self) -> dict:
        if coordinator_link.killed():
            return {"stopped": "KILL"}
        if not self.settings.enabled:
            return {"stopped": "disabled"}
        now = self.clock()
        out = {"triggers": 0, "opened": [], "attached": [], "acted": [], "closed": [], "cursor": None}
        self._harvest(out)
        cursor = int(self.cdb.cursor_get("events", 0) or 0)
        coordinator_link.write_heartbeat(cursor=cursor, head=self.process_head, root=self.root, now=now)
        ro = self._ro()
        if self.idle() and self._stale_and_deployed(ro, now):
            log.warning("re-exec onto deployed code (loaded %s)", self.process_head[:12], **_x())
            self.shutdown()
            self.execv(sys.executable, [sys.executable, "-m", "steward.coordinator", "run"])
            out["reexec"] = True
            return out
        health = None
        try:
            health = self.health_fn()
        except Exception:  # noqa: BLE001
            log.exception("health read failed", **_x())
        try:
            trigs, new_cursor = triggers.scan(ro, self.cdb, now=now, health=health, runner_alive=self.runner_alive,
                                              settings=self.settings)
        except Exception:  # noqa: BLE001 - a locked DB: try again next tick, cursor untouched
            log.exception("trigger scan failed", **_x())
            trigs, new_cursor = [], None
        out["triggers"] = len(trigs)
        for trig in trigs:
            try:
                self._on_trigger(ro, trig, now, out)
            except Exception:  # noqa: BLE001
                log.exception("trigger %s failed", trig.kind, **_x(None, trig.job_id))
        if new_cursor is not None:
            self.cdb.cursor_set("events", new_cursor)
            out["cursor"] = new_cursor
        self._bookkeeping(ro, now, out)
        self._submit(health, out)
        self._daily(now)
        return out

    def _stale_and_deployed(self, ro, now) -> bool:
        """R15: the loaded HEAD differs from the deployed one and no Steward merge awaits its deploy."""
        try:
            stale = health_mod.stale_code({"head": self.process_head, "root": self.root}, self.head_fn)
            return stale is not None and deploy_pending.pending(ro, now=now) is None
        except Exception:  # noqa: BLE001
            log.exception("re-exec check failed", **_x())
            return False

    # ---------- step 3: triggers ----------

    def _on_trigger(self, ro, trig, now, out) -> None:
        eid, how = episodes.open_or_attach(self.cdb, trig, now=now, settings=self.settings)
        if eid is None:
            log.info("%s: %s", how, trig.kind, **_x(None, trig.job_id))
            return
        key = episodes.episode_key(trig)
        ep = self.cdb.open_for(key)
        if ep is None or ep["episode_id"] != eid:
            return
        last = {"kind": trig.kind, "seq": trig.seq, "detail": trig.detail or {}}
        job = trig.job_id
        if how in ("opened", "ask_only"):
            merge_ev(self.cdb, ep, trigger_detail=trig.detail or {}, open_seq=ro.max_seq())
            out["opened"].append(eid)
            log.info("opened %s (%s)", trig.kind, how, **_x(eid, job))
            if how == "ask_only":
                self._ask_only(self.cdb, ro, ep, now, out["closed"])
                return
            if job is not None:
                self._hold(self.cdb, ep, job, trig.kind)
            return
        out["attached"].append(eid)
        fields = {"last_trigger": last}
        reparked = (job is not None and ep.get("acted_at") is not None and eid != self._inflight
                    and trig.kind.startswith("needs_human:"))
        if reparked:
            ok = self._relay(verbs.hold(job, eid, trig.kind), eid, job)["ok"]
            fields["repark"] = {"seq": trig.seq, "held": ok}
            log.info("re-parked after acting (%s); hold relayed again (%s)", trig.kind, "ok" if ok else "failed",
                     **_x(eid, job))
        merge_ev(self.cdb, ep, **fields)

    def _relay(self, verb, eid, job) -> dict:
        res = verbs.run(verb, settings=self.settings, workspace=self.workspace(), run=self.run)
        if not res["ok"]:
            log.warning("%s failed rc=%s: %s", verb.label, res["rc"], res["err"][:200], **_x(eid, job))
        return res

    def _hold(self, cdb, ep, job, kind) -> bool:
        """Relay `coordinator hold` for the episode and record `held` in its evidence. True when it was recorded."""
        eid = ep["episode_id"]
        ok = bool(self._relay(verbs.hold(job, eid, kind or ep.get("trigger") or ""), eid, job)["ok"])
        merge_ev(cdb, ep, held=ok)
        return ok

    def _ask_only_found(self, ep) -> str:
        if episodes.ask_only_why(ep) == "cooldown":
            return (f"re-parked within the {int(self.settings.cooldown_s // 60)}-min cooldown after the last "
                    f"episode; not retried")
        return f"episode cap reached ({self.settings.episodes_per_job_day} in 24 h)"

    def _ask_only(self, cdb, ro, ep, now, closed) -> None:
        """Close an ask-only episode: job-less -> `noted`; a job episode first needs its hold recorded (the relay
        handler sends the owner line only for a held episode), retried on later steps until `ask_by_s`."""
        eid, job = ep["episode_id"], job_of(ep)
        if job is None:
            _add(closed, self._close(cdb, ro, ep, "noted", self._ask_only_found(ep), now))
            return
        held = bool(ev_of(ep).get("held")) or self._hold(cdb, ep, job, lifecycle.trigger_of(ep).kind)
        if not held:
            if now - float(ep["opened_at"]) < self.settings.ask_by_s:
                log.warning("ask-only: hold not recorded yet; retrying on a later tick", **_x(eid, job))
                return
            log.warning("ask-only: hold never recorded by the ask deadline; closing anyway (no owner line)",
                        **_x(eid, job))
        _add(closed, self._close(cdb, ro, ep, "asked", self._ask_only_found(ep), now))

    # ---------- step 4 + (f): preemption and the ask deadline ----------

    def _open_episodes(self, cdb, now) -> list[dict]:
        return [e for e in cdb.episodes_since(now - OPEN_LOOKBACK_S) if e.get("closed_at") is None]

    def _bookkeeping(self, ro, now, out) -> None:
        for ep in self._open_episodes(self.cdb, now):
            if ep["episode_id"] == self._inflight and not self.idle():
                continue
            try:
                res = self._deadline_or_preempt(self.cdb, ro, ep, now)
                if res:
                    _add(out["closed"], res)
            except Exception:  # noqa: BLE001
                log.exception("bookkeeping failed", **_x(ep["episode_id"], job_of(ep)))

    def _deadline_or_preempt(self, cdb, ro, ep, now) -> dict | None:
        if lifecycle.preempted(ro, ep):
            return self._close(cdb, ro, ep, "preempted", "the owner acted after the park", now, ask=False)
        watching = ep.get("acted_at") is not None and ep.get("unstuck_at") is not None
        if now - float(ep["opened_at"]) >= self.settings.ask_by_s and not watching:
            outcome = "noted" if job_of(ep) is None else "asked"
            return self._close(cdb, ro, ep, outcome, "not settled within the ask deadline", now)
        return None

    def _close(self, cdb, ro, ep, outcome, found, now, **kw) -> dict:
        job = job_of(ep)
        if (job is not None and outcome in ("asked", "gave_up") and kw.get("ask") is not False
                and not ev_of(ep).get("held") and lifecycle._still_open(cdb, ep)):
            # the relay handler sends the owner line only for an episode whose hold it recorded
            if not self._hold(cdb, ep, job, lifecycle.trigger_of(ep).kind):
                log.warning("closing %s with no hold recorded: no owner line will go out", outcome,
                            **_x(ep["episode_id"], job))
        return lifecycle.close(cdb, ro, ep, outcome, found, now=now, settings=self.settings,
                               workspace=self.workspace(), run=self.run, **kw)

    # ---------- step 5: the worker ----------

    def _harvest(self, out) -> None:
        if self._future is None or not self._future.done():
            return
        fut, self._future, self._inflight = self._future, None, None
        try:
            res = fut.result() or {}
        except Exception:  # noqa: BLE001 - _step already logs; belt and braces
            log.exception("episode step raised", **_x())
            return
        out["acted"] += res.get("acted", [])
        out["closed"] += res.get("closed", [])

    def _submit(self, health, out) -> None:
        if not self.idle():
            return
        self._harvest(out)
        now = self.clock()
        eps = self._open_episodes(self.cdb, now)
        if not eps:
            return
        ep = min(eps, key=lambda e: (self._stepped.get(e["episode_id"], 0.0), e["opened_at"]))
        eid, key = ep["episode_id"], ep["job_id"]
        self._stepped[eid] = time.monotonic()
        for gone in set(self._stepped) - {e["episode_id"] for e in eps}:
            self._stepped.pop(gone, None)
        if self.inline:
            res = self._step(eid, key, health)
            out["acted"] += res.get("acted", [])
            out["closed"] += res.get("closed", [])
            return
        self._inflight = eid
        self._future = self._pool.submit(self._step, eid, key, health)

    def _step(self, eid, key, health) -> dict:
        res = {"acted": [], "closed": []}
        try:
            self._step_inner(self._wcdb(), self._wro(), eid, key, health, res)
        except Exception:  # noqa: BLE001 - a locked DB or a broken rung: retry on a later tick
            log.exception("episode step failed", **_x(eid, None if lifecycle.is_jobless(key) else key))
        return res

    def _step_inner(self, cdb, ro, eid, key, health, res) -> None:
        ep = cdb.open_for(key)
        if ep is None or ep["episode_id"] != eid:
            return
        now = self.clock()
        job = job_of(ep)
        if episodes.is_ask_only(ep):
            self._ask_only(cdb, ro, ep, now, res["closed"])
            return
        if job is not None and not ev_of(ep).get("held"):
            self._hold(cdb, ep, job, lifecycle.trigger_of(ep).kind)   # the opening relay failed: retry
        if ep.get("acted_at") is not None:
            if not self._watch(cdb, ro, ep, health, now, res):
                return
        if now - float(ep["opened_at"]) >= self.settings.ask_by_s:
            return  # the main tick's deadline closes it
        trig = lifecycle.trigger_of(ep)
        plan = rungs.select(ro, cdb, ep, trig, settings=self.settings, now=now, runner_alive=self.runner_alive,
                            watchdog_active=self.watchdog_active, workspace=self.workspace,
                            rerun_fn=self._capped_rerun(float(ep["opened_at"]) + self.settings.ask_by_s))
        now = self.clock()  # a rerun may have taken minutes
        if plan is not None:
            lifecycle.append_rung(cdb, ep, plan.rung, plan.classification)
            fields = {"classification": plan.classification, "rung_evidence": plan.evidence,
                      "found": plan.diagnosis}
            if plan.rule:
                fields["rule"] = plan.rule
            merge_ev(cdb, ep, **fields)
            if not plan.verbs:
                _add(res["closed"], self._close(cdb, ro, ep, "noted" if job is None else "asked",
                                                 plan.diagnosis, now))
                return
            self._act(cdb, ro, ep, plan.rung, plan.verbs, now, res, followup=plan.followup,
                      followup_after_s=plan.followup_after_s)
            return
        if job is None:
            _add(res["closed"], self._close(cdb, ro, ep, "noted", "no code rung applies; a job-less episode "
                                             "never asks (R18)", now))
            return
        if model.tl_waiting(ro, job, trig):
            log.info("model_waiting_tl: a tech-lead review is in flight; the model rung waits",
                     **_x(ep["episode_id"], job))
            return  # R17: wait; the main tick's ask deadline still applies
        why = self._model_gate(cdb, ep, now)
        if why == BUDGET_TEXT:
            self._budget_blocked = True
            _add(res["closed"], self._close(cdb, ro, ep, "gave_up", BUDGET_TEXT, now))
            return
        if why:
            _add(res["closed"], self._close(cdb, ro, ep, "asked", f"no rung applies ({why})", now))
            return
        self._model(cdb, ro, ep, trig, now, res)

    def _capped_rerun(self, deadline_at: float):
        """`rerun_fn` with `rerun_total_s` capped to the time left before the ask deadline (at call time)."""
        base = self.rerun_fn

        def fn(**kw):
            s = kw.get("settings") or self.settings
            left = max(0.0, deadline_at - self.clock())
            kw["settings"] = dataclasses.replace(s, rerun_total_s=min(float(s.rerun_total_s), left))
            return base(**kw)
        return fn

    def _watch(self, cdb, ro, ep, health, now, res) -> bool:
        """An acted episode: follow-up, unstuck/resolved, re-park. True = walk on to the next rung now."""
        eid, job = ep["episode_id"], job_of(ep)
        ev = ev_of(ep)
        fu = ev.get("followup")
        if isinstance(fu, dict) and now >= float(fu.get("due") or 0):
            merge_ev(cdb, ep, followup=None)
            fverbs = [verbs.Verb(list(a), str(lbl)) for a, lbl in zip(fu.get("argv") or [], fu.get("labels") or [])]
            self._run_verbs(cdb, ep, fverbs, res, rung=str(fu.get("rung") or "followup"))
        hp = health if lifecycle.is_jobless(ep.get("job_id")) else None
        un = rungs.unstuck(ro, ep, health=hp)
        if un and ep.get("unstuck_at") is None:
            cdb.mark(eid, unstuck_at=now)
            ep["unstuck_at"] = now
            log.info("unstuck", **_x(eid, job))
        elif not un and ep.get("unstuck_at") is not None:
            cdb.mark(eid, unstuck_at=None)
            ep["unstuck_at"] = None
        if rungs.resolved(ro, ep, now=now, settings=self.settings):
            _add(res["closed"], self._close(cdb, ro, ep, "resolved", ev_of(ep).get("found") or "unstuck", now))
            return False
        if un or job is None:
            return False
        rep = ev.get("repark") if isinstance(ev.get("repark"), dict) else None
        newer = lifecycle.newest_park_seq(ro, job) > int(ev.get("acted_seq") or 0)
        if rep is None and not newer:
            return False
        if not (rep or {}).get("held"):
            if not self._relay(verbs.hold(job, eid, lifecycle.trigger_of(ep).kind), eid, job)["ok"]:
                merge_ev(cdb, ep, repark={**(rep or {"seq": 0}), "held": False})
                log.warning("re-parked after acting: hold relay failed; retrying on a later tick", **_x(eid, job))
                return False
        merge_ev(cdb, ep, repark=None, reparks=int(ev.get("reparks") or 0) + 1)
        cdb.mark(eid, acted_at=None, unstuck_at=None)
        ep["acted_at"] = ep["unstuck_at"] = None
        log.info("re-parked after acting: next rung", **_x(eid, job))
        return True

    def _act(self, cdb, ro, ep, rung, vlist, now, res, *, followup=None, followup_after_s=0.0) -> bool:
        """Run `vlist` in order (stop at the first failure); on success mark acted and check unstuck at once."""
        eid, job = ep["episode_id"], job_of(ep)
        if not lifecycle._still_open(cdb, ep):
            log.info("%s not run: the episode was closed meanwhile", rung, **_x(eid, job))
            return False
        if lifecycle.preempted(ro, ep):
            _add(res["closed"], self._close(cdb, ro, ep, "preempted", "the owner acted after the park", now,
                                             ask=False))
            return False
        late = self._too_late(ro, ep)
        if late:
            log.warning("%s not run: %s", rung, late, **_x(eid, job))
            _add(res["closed"], self._close(cdb, ro, ep, "asked", f"{rung} not run: {late}", self.clock()))
            return False
        acted_seq = ro.max_seq()
        ok = self._run_verbs(cdb, ep, vlist, res, rung=rung)
        if not ok:
            merge_ev(cdb, ep, found=f"{rung}: a verb failed")
            return False
        fields = {"acted_seq": acted_seq}
        if followup:
            fields["followup"] = {"due": now + float(followup_after_s or 0), "rung": rung,
                                  "argv": [v.argv for v in followup], "labels": [v.label for v in followup]}
        merge_ev(cdb, ep, **fields)
        cdb.mark(eid, acted_at=now, unstuck_at=None)
        ep["acted_at"], ep["unstuck_at"] = now, None
        if job is not None and rungs.unstuck(ro, ep):
            cdb.mark(eid, unstuck_at=now)
            ep["unstuck_at"] = now
        return True

    def _too_late(self, ro, ep) -> str | None:
        """Why acting now would come after the owner was (or is about to be) told, else None: the ask deadline has
        passed, or Steward's hold for this episode is no longer open (the sweep delivered it, or it closed)."""
        elapsed = self.clock() - float(ep["opened_at"])
        if elapsed >= self.settings.ask_by_s:
            return f"the ask deadline passed ({int(elapsed)} s >= {int(self.settings.ask_by_s)} s)"
        job = job_of(ep)
        if job is None:
            return None
        try:
            rec = ro.kv_get(job, coordinator_link.HOLD_KEY)
        except Exception:  # noqa: BLE001 - unreadable: the deadline check above still bounds us
            return None
        if isinstance(rec, dict) and rec.get("episode_id") == ep["episode_id"] and rec.get("state") != "open":
            return f"the hold is {rec.get('state')} ({rec.get('how')}) on Steward's side"
        return None

    def _run_verbs(self, cdb, ep, vlist, res, *, rung) -> bool:
        eid, job = ep["episode_id"], job_of(ep)
        labels, ok = [], True
        for v in vlist:
            r = verbs.run(v, settings=self.settings, workspace=self.workspace(), run=self.run)
            lbl = lifecycle.label(v, job)
            if not r["ok"]:
                labels.append(f"{lbl} (failed rc {r['rc']})")
                log.warning("%s failed rc=%s: %s", lbl, r["rc"], r["err"][:200], **_x(eid, job))
                ok = False
                break
            labels.append(lbl)
            log.info("ran %s", lbl, **_x(eid, job))
        lifecycle.add_actions(cdb, ep, labels)
        res["acted"].append({"eid": eid, "rung": rung, "actions": labels, "ok": ok})
        return ok

    # ---------- the model rung ----------

    def _budget_exhausted(self, cdb, ep, now) -> bool:
        s = self.settings
        b = cdb.budget_today(now)
        claude_out = b["usd"] + s.budget_call_usd > s.budget_daily_usd
        if ep.get("reopened"):
            return claude_out if s.model_reopen.startswith("claude") else b["codex_calls"] >= s.budget_daily_codex_calls
        return claude_out and b["codex_calls"] >= s.budget_daily_codex_calls

    def _model_gate(self, cdb, ep, now) -> str | None:
        s = self.settings
        if cdb.rung_disabled(MODEL_RUNG):
            return "model rung disabled"
        if int(ep.get("model_calls") or 0) >= s.model_calls_per_episode:
            return "model calls for this episode used"
        if now - float(ep["opened_at"]) >= s.ask_by_s - s.model_timeout_s:
            return "too close to the ask deadline"
        if self._budget_exhausted(cdb, ep, now):
            return BUDGET_TEXT
        return None

    def _model(self, cdb, ro, ep, trig, now, res) -> None:
        eid, job = ep["episode_id"], job_of(ep)
        before = int(ep.get("model_calls") or 0)
        kept, plan, refused = model.plan_episode(ro, cdb, ep, trig, settings=self.settings, workspace=self.workspace(),
                                                 repo=self.settings.steward_repo, now=now, run=self.run,
                                                 watchdog_active=self.watchdog_active)
        for r in refused:
            log.warning("plan_action_refused %s: %s", json.dumps(r.get("action"))[:200], r.get("why"), **_x(eid, job))
        fresh = cdb.open_for(job) or ep
        ep.update({k: fresh.get(k) for k in ("model_calls", "cost_usd")})
        now = self.clock()
        if plan is None:
            if model.tl_waiting(ro, job, trig):
                log.info("model_waiting_tl: a tech-lead review started meanwhile; the model rung waits", **_x(eid, job))
                return
            if int(ep.get("model_calls") or 0) == before and self._budget_exhausted(cdb, ep, now):
                self._budget_blocked = True
                _add(res["closed"], self._close(cdb, ro, ep, "gave_up", BUDGET_TEXT, now))
            else:
                _add(res["closed"], self._close(cdb, ro, ep, "asked", "the model rung returned no plan", now))
            return
        lifecycle.append_rung(cdb, ep, MODEL_RUNG, plan.get("lane"))
        merge_ev(cdb, ep, model_diagnosis=plan.get("diagnosis"), model_rule=plan.get("rule"),
                 model_lane=plan.get("lane"), refused=refused or None,
                 found=plan.get("ask") or plan.get("diagnosis"))
        if not kept:
            _add(res["closed"], self._close(cdb, ro, ep, "asked", plan.get("ask") or plan.get("diagnosis") or
                                             "the model proposed nothing it may do", now))
            return
        self._act(cdb, ro, ep, MODEL_RUNG, kept, now, res)

    # ---------- step 7: daily ----------

    def _daily(self, now) -> None:
        try:
            self._digest(now)
        except Exception:  # noqa: BLE001
            log.exception("digest failed", **_x())
        try:
            self._budget_tg(now)
        except Exception:  # noqa: BLE001
            log.exception("budget tg failed", **_x())

    def _digest(self, now) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        lt = time.localtime(now)
        if self.cdb.cursor_get("digest_day", "") == day or lt.tm_hour * 60 + lt.tm_min < _minutes(
                self.settings.digest_at):
            return
        self.cdb.cursor_set("digest_day", day)
        if self.digest_fn is not None:
            url = self.digest_fn(cdb=self.cdb, now=now, settings=self.settings, run=self.run)
        else:
            url = daily_digest(self.cdb, now=now, settings=self.settings, run=self.run)
        log.info("digest %s", url or "not published", **_x())

    def _budget_tg(self, now) -> None:
        if not self._budget_blocked or self.cdb.budget_today(now)["tg_sent"]:
            return
        b = self.cdb.budget_today(now)
        text = (f"steward-coordinator: model budget exhausted today (${b['usd']:.2f}, {b['codex_calls']} Codex "
                f"calls); stuck jobs go straight to the owner until tomorrow")
        send = self.tg_send
        if send is None:
            from .. import notify

            send = notify.tg_send_text
        if send(text):
            self.cdb.note_tg(now)
            self._budget_blocked = False


def _minutes(hhmm) -> int:
    """`HH:MM` (or `H:MM`) -> minutes after midnight; unparsable -> 07:00."""
    try:
        h, m = str(hhmm).strip().split(":", 1)
        return max(0, min(23, int(h))) * 60 + max(0, min(59, int(m)))
    except ValueError:
        return 7 * 60


def daily_digest(cdb, *, now, settings, run):
    """render + publish through Task 13's `digest` module; None (logged) while it is absent."""
    try:
        from . import digest
    except ImportError:
        log.info("digest module not present", **_x())
        return None
    from . import rules

    html = digest.render(cdb, since=now - 86400, until=now,
                         metrics=digest.zero_touch_metrics(settings, since_iso=digest.since_iso(now), run=run),
                         absorbed_fn=lambda tid: rules.absorbed(tid, run=run))
    return digest.publish(html, slug=settings.digest_slug, run=run)
