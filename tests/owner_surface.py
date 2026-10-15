"""What the owner actually sees (Plan 4 qualification harness; spec §11).

Records every owner-facing channel for jobs in an isolated Store — phone notices (`notify.emit` through `notes`, and
the runner's `notify_fn`), pane pushes (`push.notify_creator`), Telegram alerts (`alerts.check`, forced on, with a
recording notifier) — and renders the status surfaces from the same helpers the dashboard, phone screen and
`steward jobs` use. Never sends anything real: the pane runner is a FakeRunner and `tg` is never invoked."""
from __future__ import annotations

from steward import alerts, autopilot, jobview, ownership, push, stability
from steward.models import JobState
from steward.runner_exec import CommandResult, FakeRunner
from ownership_fixtures import Notes

OWNER_KINDS = ("NEEDS_YOU", "READY")
_PANE_OWNER_EVENTS = ("auto_escalated", "restart_needed")
_UNFINISHED_STATES = {s.value for s in (JobState.CREATED, JobState.CONTRACTED, JobState.RUNNING, JobState.PAUSED,
                                        JobState.WORKER_FINISHED, JobState.CHECKS_PASSED, JobState.NEEDS_HUMAN)}


def _delivery_summary(store, job_id):
    try:
        from steward import delivery  # Plan 4 Task 7; absent before it lands
    except ImportError:
        return None
    return delivery.summary(store, job_id)


class OwnerSurface:
    def __init__(self, store, monkeypatch):
        self.store = store
        self.notes = Notes()
        self.tg = Notes()
        self.runner_notices: list[dict] = []
        self.pushes: list[dict] = []
        self.pane_runner = FakeRunner(lambda argv, cwd: CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout="",
                                                                      stderr="", duration_s=0.0))
        real = push.notify_creator

        def recording(store_, job_id, event, runner_exec, facts=None, clock=None, **kw):
            self.pushes.append({"job_id": job_id, "event": event, "facts": dict(facts or {})})
            args = {"facts": facts} if clock is None else {"facts": facts, "clock": clock}
            return real(store_, job_id, event, self.pane_runner, **args)

        monkeypatch.setattr(push, "notify_creator", recording)

    def notify_fn(self, job_id, kind, title, body):
        self.runner_notices.append({"job_id": job_id, "kind": kind, "title": title, "body": body})

    def alerts(self, job_id) -> list[str]:
        return alerts.check(self.store, job_id, self.pane_runner, notifier=self.tg, enabled=True, link="")

    def snapshot(self, job_id) -> dict:
        job = self.store.get_job(job_id)
        events = jobview.event_dicts(self.store.events(job_id))
        delivery = _delivery_summary(self.store, job_id)
        view = ownership.owner_view(self.store, job_id)
        try:
            live = jobview.liveness(job.state.value, events, delivery=delivery)
        except TypeError:  # before Task 7 adds the delivery keyword
            live = jobview.liveness(job.state.value, events)
        return {"state": job.state.value,
                "owner_view": {"status": view.status, "text": view.text, "next_actor": view.next_actor},
                "alert_reasons": alerts.reasons(self.store, job_id), "liveness": live,
                "explain": stability.explain_lines(self.store, job_id, count_fn=lambda _h: 0),
                "label": autopilot.label(self.store, job_id), "delivery": delivery}

    def owner_requests(self, job_id: str | None = None) -> list[dict]:
        out = []
        for e in self.notes.events:
            if e.kind in OWNER_KINDS and job_id in (None, e.job_id):
                out.append({"channel": "phone", "job_id": e.job_id, "kind": e.kind, "text": e.body})
        for n in self.runner_notices:
            if n["kind"] in OWNER_KINDS and job_id in (None, n["job_id"]):
                out.append({"channel": "runner", "job_id": n["job_id"], "kind": n["kind"], "text": n["body"]})
        for t in self.tg.texts:
            if job_id is None or job_id[:8] in t:
                out.append({"channel": "telegram", "job_id": job_id, "kind": "alert", "text": t})
        for p in self.pushes:
            if p["event"] in _PANE_OWNER_EVENTS and job_id in (None, p["job_id"]):
                out.append({"channel": "pane", "job_id": p["job_id"], "kind": p["event"],
                            "text": str(p["facts"].get("reason", ""))})
        return out

    def concealed(self, job_id: str) -> bool:
        """True when the job is unfinished (unfinished state, open blocker, or undelivered acceptance) but the
        row/status surfaces read it as done or say nothing — spec §11 s18: no display may conceal it."""
        snap = self.snapshot(job_id)
        delivery = snap["delivery"] or {}
        unfinished = (snap["state"] in _UNFINISHED_STATES or snap["owner_view"]["status"] != "none"
                      or (snap["state"] == "ACCEPTED" and delivery and not delivery.get("delivered")))
        shown_done = snap["liveness"][0] == "done"
        silent = snap["owner_view"]["status"] == "none" and not snap["alert_reasons"] and snap["state"] == "NEEDS_HUMAN"
        return bool(unfinished and (shown_done or silent))
