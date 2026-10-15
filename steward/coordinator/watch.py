"""Silence watch (spec 5.17): run by a systemd timer, outside the daemon. Tells the owner through `tg` when the
coordinator is silent or its unit is down, once per outage, and again when it is back. Never raises."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from .. import coordinator_link, notify

log = logging.getLogger(__name__)

ALERT_S = 600.0


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _outage(now: float, unit_active: bool) -> tuple[str, float] | None:
    """(reason, since) when the coordinator is down, else None."""
    hb = coordinator_link.read_heartbeat()
    ts = None
    if hb:
        try:
            ts = float(hb["ts"])
        except (KeyError, TypeError, ValueError):
            ts = None
    since = ts if ts is not None else now
    if not unit_active:
        return "unit inactive", since
    if ts is None:
        return "never started", since
    age = now - ts
    if age > ALERT_S:
        return f"no heartbeat for {int(age // 60)} min", since
    return None


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("alerted_at") is not None else None


def _write(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(body))
    os.replace(tmp, path)


def _clear(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _send(send, text: str) -> bool:
    try:
        return bool(send(text))
    except Exception as exc:  # noqa: BLE001
        log.warning("watch send failed: %s", exc)
        return False


def check(*, now: float, unit_active: bool, send=notify.tg_send_text, state_path: Path) -> str:
    try:
        state_path = Path(state_path)
        try:
            from . import config

            disabled = not config.load().enabled
        except Exception:  # noqa: BLE001
            disabled = False
        if coordinator_link.killed() or disabled:
            _clear(state_path)
            return "killed"
        out = _outage(now, unit_active)
        prior = _read(state_path)
        if out is None:
            if prior is None:
                return "ok"
            try:
                mins = max(0, int((now - float(prior["alerted_at"])) // 60))
            except (TypeError, ValueError):
                mins = 0
            if not _send(send, f"steward-coordinator: back after {mins} min"):
                return "tg_failed"
            _clear(state_path)
            return "recovered"
        if prior is not None:
            return "already_alerted"
        reason, since = out
        if not _send(send, f"steward-coordinator: silent since {_hhmm(since)} ({reason})"):
            return "tg_failed"
        _write(state_path, {"alerted_at": now, "reason": reason})
        return "alerted"
    except Exception as exc:  # noqa: BLE001
        log.warning("watch check failed: %s", exc)
        return "tg_failed"
