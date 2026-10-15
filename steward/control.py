"""Idempotent control plane.

Pause/resume/stop are handled purely in code — no model call, no dispatch
delay (SP-11). Duplicate control requests never re-execute; the stored
receipt is returned as-is with `duplicate=True` (SP-20).
"""

from __future__ import annotations

from dataclasses import replace

from steward import machine
from steward.models import ControlRequest, Job, JobState, Judgment, Receipt
from steward.store import Store, now_iso


class Controller:
    def __init__(self, store: Store):
        self.store = store

    def new_job(self, goal: str, repo: str, created_by: dict | None = None, job_id: str | None = None) -> Job:
        # `job_id`: a pre-assigned id (a reserved `new --deadline` request); its primary key makes a
        # concurrent duplicate fail with IntegrityError before any other write.
        job = self.store.create_job(goal, repo, job_id=job_id)
        self.store.append_event(
            job.job_id,
            what="job created",
            evidence={"goal": goal, "repo": repo, **({"created_by": created_by} if created_by else {})},
            rule="new_job",
            judgment=Judgment.human,
        )
        if created_by:
            self.store.kv_set(job.job_id, "created_by", created_by)
        return job

    def submit(self, req: ControlRequest) -> Receipt:
        existing = self.store.get_receipt(req.request_id)
        if existing is not None:
            return replace(existing, duplicate=True)

        handlers = {
            "pause": self._handle_pause,
            "resume": self._handle_resume,
            "stop": self._handle_stop,
        }
        handler = handlers.get(req.kind)
        ts = now_iso()

        if handler is None:
            receipt = Receipt(
                request_id=req.request_id,
                job_id=req.job_id,
                kind=req.kind,
                accepted=False,
                detail=f"unknown control kind: {req.kind}",
                ts=ts,
            )
            self.store.put_receipt(receipt)
            return receipt

        job = self.store.get_job(req.job_id)
        if job is None:
            receipt = Receipt(
                request_id=req.request_id,
                job_id=req.job_id,
                kind=req.kind,
                accepted=False,
                detail=f"unknown job: {req.job_id}",
                ts=ts,
            )
            self.store.put_receipt(receipt)
            return receipt

        accepted, detail = handler(job)
        receipt = Receipt(
            request_id=req.request_id,
            job_id=req.job_id,
            kind=req.kind,
            accepted=accepted,
            detail=detail,
            ts=ts,
        )
        self.store.put_receipt(receipt)
        return receipt

    def may_dispatch(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        if job is None:
            return False
        return machine.may_dispatch(job.state)

    def why(self, job_id: str) -> dict | None:
        events = self.store.events(job_id)
        if not events:
            return None
        last = events[-1]
        return {
            "seq": last.seq,
            "ts": last.ts,
            "what": last.what,
            "evidence": last.evidence,
            "rule": last.rule,
            "judgment": last.judgment.value,
        }

    # -- control kind handlers --------------------------------------------
    # Each returns (accepted: bool, detail: str). No model call, ever.

    def _handle_stop(self, job: Job) -> tuple[bool, str]:
        return self._stop(job, what="operator stop", rule="operator_stop", judgment=Judgment.human)

    def stop_by_code(self, job_id: str, *, what: str, rule: str, evidence: dict | None = None) -> tuple[bool, str]:
        """The same stop as `_handle_stop`, decided by code under a named rule (spec 2026-10-08 §5.3: e.g. a fix-up
        whose parent is done). An operator's stop stays on `submit` -> `_handle_stop` (Judgment.human)."""
        job = self.store.get_job(job_id)
        if job is None:
            return False, f"unknown job: {job_id}"
        return self._stop(job, what=what, rule=rule, judgment=Judgment.code, evidence=evidence)

    def _stop(self, job: Job, *, what: str, rule: str, judgment: Judgment,
              evidence: dict | None = None) -> tuple[bool, str]:
        ev = {"previous_state": job.state.value, **(evidence or {})}
        if job.state in (JobState.RUNNING, JobState.PAUSED):
            self.store.set_state(job.job_id, JobState.STOPPING, what=what, evidence=ev, rule=rule, judgment=judgment)
            return True, "dispatch blocked; worker cancellation pending"
        if job.state in (JobState.CREATED, JobState.CONTRACTED, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED):
            self.store.set_state(job.job_id, JobState.STOPPED, what=what, evidence=ev, rule=rule, judgment=judgment)
            if job.state in (JobState.WORKER_FINISHED, JobState.CHECKS_PASSED):
                # The branch and worktree are left alone; the tab is closed by the herdr_tabs sweep.
                return True, f"job stopped from {job.state.value}; its branch and worktree are kept"
            return True, "job stopped before it started running"
        if job.state == JobState.NEEDS_HUMAN:
            self.store.set_state(job.job_id, JobState.STOPPED, what=what, evidence=ev, rule=rule, judgment=judgment)
            return True, "job stopped instead of accepted"
        if job.state == JobState.STOPPING:
            return True, "already STOPPING; no action taken"
        return False, f"cannot stop: job is already {job.state.value}"

    def _handle_pause(self, job: Job) -> tuple[bool, str]:
        if job.state != JobState.RUNNING:
            return False, f"cannot pause from {job.state.value}"
        self.store.set_state(
            job.job_id,
            JobState.PAUSED,
            what="operator pause",
            evidence={"previous_state": job.state.value},
            rule="operator_pause",
            judgment=Judgment.human,
        )
        return True, "dispatch paused"

    def _handle_resume(self, job: Job) -> tuple[bool, str]:
        if job.state != JobState.PAUSED:
            return False, f"cannot resume from {job.state.value}"
        self.store.set_state(
            job.job_id,
            JobState.RUNNING,
            what="operator resume",
            evidence={"previous_state": job.state.value},
            rule="operator_resume",
            judgment=Judgment.human,
        )
        return True, "dispatch resumed"
