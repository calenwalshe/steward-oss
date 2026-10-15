"""Phone alerts (default OFF): Telegram pings when a job needs the owner.

Enabled ONLY by `[alerts] telegram = true` in `steward.toml` (config dir as
`store.resolve_config_dir`). When on, `check` looks at one job and sends at
most ONE alert per job per reason through `notify.TelegramNotifier`
(`tg send "<text>"`, exit 0 = delivered), recording `alert_sent` /
`alert_failed` events. Reasons: `needs_you` (NEEDS_HUMAN or a pending worker
permission ask), `escalated` (a watcher or Astra escalated to the human), `decision` (an
owner question carried forward, unanswered, past its job's ACCEPTED/RELEASED),
and `terminal:<STATE>` (ACCEPTED/RELEASED/STOPPED/FAILED). Each text carries
the phone-screen deep link when a `📱 phone` tab exists (else the job's own).

A failed send is retried on later checks, at most `MAX_ATTEMPTS` times per
reason. Nothing here ever raises into the runner or changes job state.
"""

from __future__ import annotations

import tomllib
from datetime import datetime
from pathlib import Path

from steward import jobview, notify
from steward.models import Judgment
from steward.runner_exec import Runner as CommandRunner
from steward.store import Store, resolve_config_dir

MAX_ATTEMPTS = 3
TERMINAL = ("ACCEPTED", "RELEASED", "STOPPED", "FAILED")


def telegram_enabled(config_dir: Path | str | None = None) -> bool:
    path = resolve_config_dir(config_dir) / "steward.toml"
    try:
        alerts = tomllib.loads(path.read_text()).get("alerts", {})
    except (OSError, ValueError):
        return False
    return isinstance(alerts, dict) and alerts.get("telegram") is True


def reasons(store: Store, job_id: str) -> list[tuple[str, str]]:
    """`(reason key, one-line text)` for everything about this job that warrants an alert now."""
    from steward import approvals as approvals_module
    from steward import blockers as blockers_module

    job = store.get_job(job_id)
    if job is None:
        return []
    state = job.state.value if hasattr(job.state, "value") else str(job.state)
    events = jobview.event_dicts(store.events(job_id))
    out: list[tuple[str, str]] = []
    import time as _time

    from steward import resolver as resolver_module

    def _resolving(a: dict) -> bool:  # a resolver is still looking; it pings the owner itself if it escalates
        if not a.get("resolver_pending"):
            return False
        try:
            age = _time.time() - datetime.fromisoformat(a["created_at"]).timestamp()
        except (KeyError, ValueError):
            return False
        return age < resolver_module.ORPHAN_S

    from steward import ownership

    try:
        ownership.ensure_classified(store, job_id)
        proj = ownership.projection(store, job_id)
    except Exception:  # noqa: BLE001 - ownership failure never silences an alert
        proj = None
    why = jobview.reason([a for a in approvals_module.list_approvals(store, job_id) if not _resolving(a)], state,
                         events, ownership=proj)
    delivery_waiting = False
    try:
        carried = ownership.carried_decisions(store, [job_id]).get(job_id) or []
    except Exception:  # noqa: BLE001 - ownership failure never silences an alert
        carried = []
    if state == "ACCEPTED":
        view = ownership.owner_view(store, job_id)
        carried_ids = {d["blocker_id"] for d in carried}
        delivery_waiting = view.status == "needs_owner" and any(
            b.disposition == "confirmed_human" and b.blocker_id not in carried_ids
            for b in blockers_module.open_blockers(store, job_id))
        if delivery_waiting:
            out.append(("delivery", view.text))
    if carried:
        more = f" (+{len(carried) - 1} more)" if len(carried) > 1 else ""
        out.append(("decision", carried[0]["text"][:200] + more))
    if why and not delivery_waiting and not why.startswith(jobview.CARRIED_PREFIX):  # sent as `decision` above
        out.append(("needs_you", why))
    esc = next((e for e in reversed(events) if e["what"] in ("watcher_escalated", "trajectory_escalated")), None)
    if esc:
        who = esc["evidence"].get("name") or "astra"
        out.append(("escalated", f"{who} escalated to you"))
    if state in TERMINAL and not (state == "ACCEPTED" and any(k == "delivery" for k, _ in out)):
        out.append((f"terminal:{state}", f"job {state.lower()}"))
    return out


def check(
    store: Store, job_id: str, runner_exec: CommandRunner, *, notifier: notify.TelegramNotifier | None = None,
    enabled: bool | None = None, link: str | None = None,
) -> list[str]:
    """Send any not-yet-sent alerts for `job_id`; returns the reasons sent this call.
    A no-op unless alerts are enabled in config (or `enabled=True` is forced by a test)."""
    try:
        if not (telegram_enabled() if enabled is None else enabled):
            return []
        from steward import deadline

        if deadline.notices_local(store, job_id):  # DL-17: deadline-job alerts stay local
            return []
        todo = [(k, t) for k, t in reasons(store, job_id) if not store.kv_get(job_id, f"alert_sent:{k}", default=False)
                and int(store.kv_get(job_id, f"alert_tries:{k}", default=0) or 0) < MAX_ATTEMPTS]
        if not todo:
            return []
        notifier = notifier or notify.TelegramNotifier(runner_exec)
        if link is None:
            from steward import phone

            link = phone.phone_link(runner_exec, store, job_id)
        job = store.get_job(job_id)
        sent = []
        for key, text in todo:
            body = f"Steward {job_id[:8]}: {text}\n{(job.goal or '')[:80]}" + (f"\n{link}" if link else "")
            try:
                ok = notifier.send_text(body)
            except Exception as exc:  # noqa: BLE001 - an alert failure never propagates
                ok = False
                detail = str(exc)
            else:
                detail = "tg exit != 0"
            if ok:
                store.kv_set(job_id, f"alert_sent:{key}", True)
                store.append_event(job_id, what="alert_sent", evidence={"reason": key, "channel": "telegram"},
                                   rule="alerts", judgment=Judgment.code)
                sent.append(key)
            else:
                tries = int(store.kv_get(job_id, f"alert_tries:{key}", default=0) or 0) + 1
                store.kv_set(job_id, f"alert_tries:{key}", tries)
                store.append_event(job_id, what="alert_failed",
                                   evidence={"reason": key, "channel": "telegram", "detail": detail, "attempt": tries},
                                   rule="alerts", judgment=Judgment.code)
        return sent
    except Exception:  # noqa: BLE001
        return []


def system_alert(text: str, runner_exec: CommandRunner, *, notifier: notify.TelegramNotifier | None = None,
                 enabled: bool | None = None) -> bool:
    """A job-less alert (system health) through the same gating as `check`: sends NOTHING unless
    `[alerts] telegram = true` (or a test forces `enabled=True`). Returns whether it was delivered."""
    try:
        if not (telegram_enabled() if enabled is None else enabled):
            return False
        return bool((notifier or notify.TelegramNotifier(runner_exec)).send_text(text))
    except Exception:  # noqa: BLE001
        return False
