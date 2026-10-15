"""High-information phone events with Herdr deep links (MOSHI-02, MOSHI-03, SP-25).

Only 6 kinds ever reach the phone — `RUNNING`, `NEEDS_YOU`, `RECOVERY`,
`CHECKS_FAILED`, `READY`, `BUDGET` — each capped to a short title/body and
carrying a `moshi://herdr?...` deep link so tapping it opens the same Herdr
workspace/tab/pane Moshi's own terminal feature would (see
`docs/moshi-integration.md`). `emit` dedupes on `(job, kind, revision)` so an
unchanged job sitting in the same state across many heartbeat ticks never
re-notifies, and a notifier failure is always recorded as an event
(`judgment=code`) and swallowed — it can never change job state or propagate
up into the controller loop.

Per the read-only Moshi spike (MOSHI-01, `docs/moshi-integration.md`), no
supported general-purpose event-injection path was found, so `MoshiNotifier`
stays a stub: constructing it is harmless, but `send()` always raises so a
misconfigured `STEWARD_NOTIFIER=moshi` fails loudly through `emit`'s
catch-and-record path rather than silently doing nothing. `TelegramNotifier`
is the real notifier for this phase.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from steward import exceptions
from steward.models import Judgment
from steward.runner_exec import Runner as CommandRunner
from steward.store import Store

_KINDS = frozenset({"RUNNING", "NEEDS_YOU", "RECOVERY", "CHECKS_FAILED", "READY", "BUDGET"})
# v2.2: a deadline notice reaches a notifier ONLY through `send_approved`, called by `steward.deadline_outbox.deliver`
# for one individually approved, leased attempt. `emit` never sends it.
_DEADLINE_KIND = "DEADLINE"
_ALL_KINDS = _KINDS | {_DEADLINE_KIND}

_TITLE_CAP = 40
_BODY_CAP = 140
OWNERSHIP_EXEMPT_REVISIONS = ("blocker:", "questions:", "review_outage", "coordinator:")


@dataclass(frozen=True)
class PhoneEvent:
    kind: str
    job_id: str
    title: str
    body: str
    deep_link: str | None
    revision: int | str

    def __post_init__(self):
        if self.kind not in _ALL_KINDS:
            raise ValueError(f"unknown phone event kind: {self.kind}")
        if len(self.title) > _TITLE_CAP:
            raise ValueError(f"title exceeds {_TITLE_CAP} chars: {self.title!r}")
        if len(self.body) > _BODY_CAP:
            raise ValueError(f"body exceeds {_BODY_CAP} chars: {self.body!r}")


def deep_link(workspace: str | None, tab: str | None = None, pane: str | None = None) -> str | None:
    """`moshi://herdr?workspace=<ws>&tab=<tab>&pane=<pane>`, omitting missing values.

    A deep link with no workspace is not a link anywhere useful, so when the
    job's herdr workspace is unknown this returns `None` (rather than the
    bare `moshi://herdr`) — callers omit the link entirely in that case.
    """
    if not workspace:
        return None
    params = [
        (name, value)
        for name, value in (("workspace", workspace), ("tab", tab), ("pane", pane))
        if value
    ]
    query = "&".join(f"{name}={urllib.parse.quote(str(value), safe='')}" for name, value in params)
    return f"moshi://herdr?{query}"


class Notifier(Protocol):
    def send(self, event: PhoneEvent) -> bool: ...


class NullNotifier:
    """Default notifier: records nothing, sends nothing, always succeeds."""

    def send(self, event: PhoneEvent) -> bool:
        return True


class TelegramNotifier:
    """Real notifier for this phase: `tg send "<title>\\n<body>\\n<link>"`.

    Goes through the injected command runner like every other subprocess in
    this codebase (SP-1); tests only ever pass `FakeRunner` and never
    invoke a real `tg`.
    """

    def __init__(self, runner: CommandRunner):
        self.runner = runner

    def send(self, event: PhoneEvent) -> bool:
        lines = [event.title, event.body]
        if event.deep_link:
            lines.append(event.deep_link)
        return self.send_text("\n".join(lines))

    def send_text(self, text: str) -> bool:
        """`tg send "<text>"`: exit 0 = delivered, 1 = not (never swallowed)."""
        result = self.runner.run(["tg", "send", text], cwd=Path("."))
        return result.exit_code == 0


class MoshiNotifier:
    """Stub: the read-only spike (MOSHI-01) found no supported event path.

    See `docs/moshi-integration.md` for what was probed and why. Safe to
    construct; `send` always raises so misconfiguration is visible through
    `emit`'s failure-recording path instead of silently doing nothing.
    """

    def send(self, event: PhoneEvent) -> bool:
        raise NotImplementedError(
            "MoshiNotifier has no supported event-injection path yet; "
            "see docs/moshi-integration.md. Use STEWARD_NOTIFIER=telegram or unset."
        )


def tg_send_text(text: str, *, runner: CommandRunner | None = None) -> bool:
    """`tg send "<text>"` to the Notifications bot (operational notices, e.g. the v2.6 Jev-outage alert).
    Exit 0 = delivered, anything else = not (never swallowed). `runner` is injectable; tests pass a fake."""
    if runner is None:
        from steward.runner_exec import SubprocessRunner

        runner = SubprocessRunner()
    try:
        return TelegramNotifier(runner).send_text(text)
    except OSError:
        return False


def notifier_from_env(runner: CommandRunner) -> Notifier:
    choice = os.environ.get("STEWARD_NOTIFIER", "null").strip().lower()
    if choice == "telegram":
        return TelegramNotifier(runner)
    if choice == "moshi":
        return MoshiNotifier()
    return NullNotifier()


def _deadline_local(store: Store, event: PhoneEvent) -> bool:
    """DL-17: from a job's first deadline commitment on (lifetime marker, inherited by follow-ups), an
    automatic notice is recorded locally once per `(kind, revision)` and never reaches a notifier."""
    from steward import deadline

    if not deadline.notices_local(store, event.job_id):
        return False
    key = f"deadline_local:{event.kind}:{event.revision}"
    if not store.kv_get(event.job_id, key, default=False):
        store.kv_set(event.job_id, key, True)
        store.append_event(
            event.job_id,
            what="deadline_notice_local",
            evidence={"kind": event.kind, "revision": event.revision, "title": event.title, "body": event.body,
                      "reason": "deadline job: outbound needs the exact attempt's human approval"},
            rule="deadline_notice_floor",
            judgment=Judgment.code,
        )
    return True


def emit(store: Store, notifier: Notifier, event: PhoneEvent) -> bool:
    """Send `event` at most once per `(job, kind, revision)`.

    Returns whether the phone was actually notified this call (`False` for a
    dedupe hit, a notifier that reported failure, or one that raised) —
    never raises, and never touches job state; only an event may record a
    failure.

    `phone-notify` is a registered non-Herdr exception (OPX-04, SP-34):
    checked here (not inside a `Notifier`, so `NullNotifier` — which never
    actually leaves Steward — never needs a registry entry at all) right
    before a real notifier's `send()` is called. An unregistered/removed
    `phone-notify` entry is treated exactly like any other notifier failure
    (`exceptions.use` itself records the violation event) — never raised
    out of `emit`.
    """
    if event.kind == _DEADLINE_KIND:
        return False  # deadline notices go only through an approved attempt (`send_approved`)
    dedupe_key = f"notified:{event.kind}:{event.revision}"
    if store.kv_get(event.job_id, dedupe_key, default=False):
        return False
    if not str(event.revision).startswith(OWNERSHIP_EXEMPT_REVISIONS):
        try:
            from steward import ownership

            allowed, why = ownership.owner_notice_allowed(store, event.job_id, event.kind, notifier=notifier)
        except Exception:  # noqa: BLE001 - the gate fails open
            allowed, why = True, "gate_error"
        if not allowed:
            held_key = f"notice_held:{event.kind}:{event.revision}"
            if not store.kv_get(event.job_id, held_key, default=False):
                store.kv_set(event.job_id, held_key, True)
                store.append_event(event.job_id, what="owner_notice_held",
                                   evidence={"kind": event.kind, "revision": event.revision, "why": why,
                                             "body": event.body[:140]},
                                   rule="ownership_notice_gate", judgment=Judgment.code)
            return False
    try:
        if _deadline_local(store, event):
            return False
    except Exception:  # noqa: BLE001 - unsure means local: never send
        return False

    if not isinstance(notifier, NullNotifier):
        try:
            exceptions.use(store, event.job_id, "phone-notify", {"kind": event.kind})
        except exceptions.UnregisteredPath:
            return False

    try:
        ok = notifier.send(event)
    except Exception as exc:  # noqa: BLE001 - a notifier failure must never propagate
        store.append_event(
            event.job_id,
            what="notify_failed",
            evidence={"kind": event.kind, "revision": event.revision, "error": str(exc)},
            rule="notifier_error",
            judgment=Judgment.code,
        )
        return False

    if ok:
        store.kv_set(event.job_id, dedupe_key, True)
    else:
        store.append_event(
            event.job_id,
            what="notify_failed",
            evidence={
                "kind": event.kind,
                "revision": event.revision,
                "detail": "notifier reported failure",
            },
            rule="notifier_error",
            judgment=Judgment.code,
        )
    return ok


def send_approved(store: Store, notifier: Notifier, event: PhoneEvent) -> bool:
    """One notifier invocation for one approved, already-consumed deadline attempt (DL-11, DL-17). The caller
    (`steward.deadline_outbox.deliver`) has frozen the exact bytes, leased the attempt and consumed its approval;
    this only checks the registered `phone-notify` exception and calls `send` once. A raise propagates so the
    caller can record the attempt as unknown; there is no dedupe key and no retry here."""
    if event.kind != _DEADLINE_KIND:
        raise ValueError("send_approved carries deadline notices only")
    if not isinstance(notifier, NullNotifier):
        exceptions.use(store, event.job_id, "phone-notify", {"kind": event.kind, "attempt": str(event.revision)})
    return bool(notifier.send(event))


def emit_questions(
    store: Store, notifier: Notifier, job_id: str, *, waiting: int, starred: int, goal: str, qhash: str,
    link: str | None,
) -> bool:
    """One NEEDS_YOU per waiting question queue (Phase 17-03, G8): `N questions (M ★) · <goal>` with the phone-tab
    deep link. The revision is `questions:<hash of the waiting qids>`, so `emit`'s own dedupe key makes an unchanged
    queue silent and a new batch send once. Counts and the job goal only - no question text is pushed (T-17-16)."""
    noun = "question" if waiting == 1 else "questions"
    body = f"{waiting} {noun} ({starred} \u2605) \u00b7 {' '.join((goal or '').split())}".rstrip(" \u00b7")
    if len(body) > _BODY_CAP:
        body = body[: _BODY_CAP - 1].rstrip() + "\u2026"
    event = PhoneEvent(
        kind="NEEDS_YOU", job_id=job_id, title=f"Steward: questions ({job_id[:8]})"[:_TITLE_CAP], body=body, deep_link=link,
        revision=f"questions:{qhash}",
    )
    return emit(store, notifier, event)
