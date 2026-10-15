"""Reviewer outage service (continuous job ownership §5 "Reviewer outage behavior"; Plan 1).

The tech-lead reviewers (the roster's tech_lead entries) are shared services: when every eligible lead fails a
review - timeout, malformed answer, unavailable CLI, auth/billing, provider error - that is a FAILED INVOCATION, never
a verdict and never evidence the owner must solve the engineering problem. The affected work stays pending (blockers
marked `unavailable` with the next retry; a running job's facts kept in its tech-lead state), availability is retried
with backoff and one probe per retry slot, and the owner gets ONE operational notice (the Notifications bot), updates
only on material change at most hourly, a remedy hint only after repeated auth/billing failures, and one recovery
notice. None of these is an owner-action request. Separate from Jev's outage record (`jev_watch.OutageAlert`).
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

from steward import blockers
from steward.models import Judgment
from steward.store import Store

KV_JOB = "_review"
KEY = "outage"
BACKOFF_S = (60.0, 120.0, 300.0, 600.0, 1200.0, 1800.0)
UPDATE_MIN_S = 3600.0
RETRY_UNDELIVERED_S = 300.0
REMEDY_AFTER = 3
PROBE_LEASE_S = 360.0  # review timeout (300 s) + 60 s
SERVICE_CLASSES = ("timeout", "malformed", "unavailable", "auth", "provider_error")
_AUTH_WORDS = ("401", "402", "403", "unauthor", "forbidden", "auth", "login", "credential", "billing", "credit",
               "quota", "subscription")


def classify_failure(reason: str) -> str:
    r = (reason or "").lower()
    if r.startswith("self_review"):
        return "self_review"
    if r.startswith("repeated_failed_strategy"):
        return "strategy"
    if r.startswith("malformed"):
        return "malformed"
    if r.startswith("timeout"):
        return "timeout"
    if any(w in r for w in _AUTH_WORDS):
        return "auth"
    if "exit " in r or "error" in r:
        return "provider_error"
    return "unavailable"


def _hhmm(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def outage_status(store: Store) -> dict | None:
    rec = store.kv_get(KV_JOB, KEY)
    return rec if isinstance(rec, dict) and rec.get("state") == "down" else None


class ReviewOutage:
    def __init__(self, store: Store, send_text: Callable[[str], bool], *, clock: Callable[[], float] = time.time):
        self.store, self.send_text, self.clock = store, send_text, clock

    # -- reads -----------------------------------------------------------------------------------------------
    def status(self) -> dict:
        return self.store.kv_get(KV_JOB, KEY) or {}

    def is_down(self) -> bool:
        return self.status().get("state") == "down"

    def next_retry_at(self) -> float | None:
        rec = self.status()
        return float(rec["next_retry_at"]) if rec.get("state") == "down" and rec.get("next_retry_at") else None

    # -- internals -------------------------------------------------------------------------------------------
    def _save(self, rec: dict) -> None:
        self.store.kv_set(KV_JOB, KEY, rec)

    def _send(self, text: str) -> bool:
        try:
            return bool(self.send_text(text))
        except Exception:  # noqa: BLE001 - recorded as undelivered, retried, never fatal
            return False

    @staticmethod
    def _alert_text(rec: dict) -> str:
        who = ", ".join(f"{k}: {v}" for k, v in sorted((rec.get("classes") or {}).items()))
        jobs = rec.get("affected") or []
        return (f"Steward: technical review unavailable since {_hhmm(rec.get('since'))} ({who}). "
                f"{len(jobs)} job(s) affected ({', '.join(j[:8] for j in jobs[:5])}); their review is kept and "
                f"retried at {_hhmm(rec.get('next_retry_at'))}; other work continues. No action needed.")

    @staticmethod
    def _remedy_text(rec: dict) -> str:
        return (f"Steward: the tech-lead reviewers failed on auth/billing {rec.get('auth_streak')} times in a row "
                f"({', '.join(sorted(rec.get('classes') or {}))}). If an account needs re-login or credit, that is "
                "the one thing only you can do; nothing else is waiting on you.")

    def _event(self, job_id: str, what: str, **evidence) -> None:
        self.store.append_event(job_id, what=what, evidence=evidence, rule="review_outage", judgment=Judgment.code)

    def _retry_undelivered(self, rec: dict, now: float) -> dict:
        if rec.get("state") == "down" and not rec.get("alert_delivered") \
                and now - float(rec.get("last_attempt") or 0) >= RETRY_UNDELIVERED_S:
            rec = {**rec, "last_attempt": now, "alert_delivered": self._send(self._alert_text(rec))}
            self._save(rec)
        return rec

    # -- writes ----------------------------------------------------------------------------------------------
    def record_failure(self, job_id: str, failures: list[dict]) -> dict:
        """Every eligible lead failed this review. Returns the record (`next_retry_at`) or {} when the failures
        were not service failures (self-review/strategy rejections are handled by the caller)."""
        now = self.clock()
        classes = {str(f.get("id")): classify_failure(str(f.get("reason") or "")) for f in failures}
        if not classes or not set(classes.values()) <= set(SERVICE_CLASSES):
            return {}
        all_auth = set(classes.values()) == {"auth"}
        rec = self.status()
        if rec.get("state") != "down":
            rec = {"state": "down", "since": now, "failures": 1, "classes": classes, "affected": [job_id],
                   "next_retry_at": now + BACKOFF_S[0], "probe_job": None, "auth_streak": 1 if all_auth else 0,
                   "last_attempt": now, "last_notice_at": now, "notified": sorted(f"{k}:{v}" for k, v in classes.items()),
                   "notified_affected": [job_id], "remedy_sent": False}
            rec["alert_delivered"] = self._send(self._alert_text(rec))
            self._save(rec)
            self._event(job_id, "review_outage_alert", classes=classes, delivered=rec["alert_delivered"],
                        next_retry_at=rec["next_retry_at"])
            return rec
        n = int(rec.get("failures") or 0) + 1
        affected = list(dict.fromkeys(list(rec.get("affected") or []) + [job_id]))[-50:]
        rec = {**rec, "failures": n, "classes": classes, "affected": affected, "probe_job": None,
               "next_retry_at": now + BACKOFF_S[min(n - 1, len(BACKOFF_S) - 1)],
               "auth_streak": int(rec.get("auth_streak") or 0) + 1 if all_auth else 0}
        sig = sorted(f"{k}:{v}" for k, v in classes.items())
        material = sig != rec.get("notified") or bool(set(affected) - set(rec.get("notified_affected") or []))
        if not rec.get("alert_delivered") and now - float(rec.get("last_attempt") or 0) >= RETRY_UNDELIVERED_S:
            rec.update(last_attempt=now, alert_delivered=self._send(self._alert_text(rec)), last_notice_at=now,
                       notified=sig, notified_affected=affected)
        elif material and now - float(rec.get("last_notice_at") or 0) >= UPDATE_MIN_S:
            self._send(self._alert_text(rec))
            rec.update(last_notice_at=now, notified=sig, notified_affected=affected)
        if rec["auth_streak"] >= REMEDY_AFTER and not rec.get("remedy_sent"):
            rec["remedy_sent"] = self._send(self._remedy_text(rec))
        self._save(rec)
        return rec

    def record_success(self, job_id: str, lead_id: str) -> bool:
        """A lead answered: one recovery notice; every affected job's pending review becomes due now."""
        rec = self.status()
        if rec.get("state") != "down":
            return False
        now = self.clock()
        delivered = self._send(f"Steward: technical review is back ({lead_id} answered; unavailable since "
                               f"{_hhmm(rec.get('since'))}). {len(rec.get('affected') or [])} affected job(s) resume "
                               "review. No action needed.")
        self._save({**rec, "state": "up", "recovered_at": now, "recovery_delivered": delivered, "probe_job": None})
        self._event(job_id, "review_outage_recovered", since=rec.get("since"), lead=lead_id, delivered=delivered)
        for jid in rec.get("affected") or []:
            for b in blockers.open_blockers(self.store, jid):
                if b.review_state == "unavailable":
                    blockers.set_review_state(self.store, b.blocker_id, "queued", now=now, next_review_epoch=now)
        return True

    def may_attempt(self, job_id: str, now: float) -> bool:
        """While down: one probe per retry slot (no changed evidence needed for an availability retry)."""
        rec = self.status()
        if rec.get("state") != "down":
            return True
        rec = self._retry_undelivered(rec, now)
        if now < float(rec.get("next_retry_at") or 0):
            return False
        probe = rec.get("probe_job")
        if probe and probe != job_id and now - float(rec.get("probe_at") or 0) < PROBE_LEASE_S:
            return False
        self._save({**rec, "probe_job": job_id, "probe_at": now})
        return True
