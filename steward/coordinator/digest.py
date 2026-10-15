"""Daily digest (spec 5.17): self-contained HTML of the last day's coordinator work, published to pixel."""
from __future__ import annotations

import html as _html
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from .. import coordinator_link

log = logging.getLogger(__name__)

OUTCOMES = ("resolved", "asked", "gave_up", "preempted", "noted")
PRECISION_WINDOW_S = 14 * 86400

_STYLE = ("body{font:14px/1.4 system-ui,sans-serif;margin:2em;color:#222}table{border-collapse:collapse}"
          "td,th{border:1px solid #ccc;padding:4px 8px;text-align:left}th{background:#f3f3f3}h2{margin-top:1.5em}")


def _e(v) -> str:
    return _html.escape("" if v is None else str(v))


def _local(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "—"


def _json(raw, default):
    try:
        v = json.loads(raw) if isinstance(raw, str) and raw else default
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


def _unstick(ep) -> str:
    try:
        return f"{max(0, round((float(ep['unstuck_at']) - float(ep['opened_at'])) / 60))} min"
    except (KeyError, TypeError, ValueError):
        return "—"


def _classification(ep, rungs) -> str:
    found = [str(r["classification"]) for r in rungs if isinstance(r, dict) and r.get("classification")]
    if not found:
        ev = _json(ep.get("evidence_json"), {})
        if ev.get("classification"):
            found = [str(ev["classification"])]
    return ", ".join(dict.fromkeys(found)) or "—"


def _rule_names(rungs) -> str:
    names = [str(r.get("rung")) if isinstance(r, dict) else str(r) for r in rungs]
    return ", ".join(n for n in names if n and n != "None") or "—"


def _absorbed(fn, tid) -> bool:
    try:
        return bool(fn(tid)) if fn else False
    except Exception:  # noqa: BLE001
        return False


def _disabled(cdb) -> list[str]:
    try:
        from .lifecycle import KNOWN_RUNGS

        return [r for r in KNOWN_RUNGS if cdb.rung_disabled(r)]
    except Exception:  # noqa: BLE001
        return []


def since_iso(now: float, window_s: float = 86400) -> str:
    """UTC ISO-8601 (`...Z`) of `now - window_s`, the form `steward metrics zero-touch --since` takes."""
    return datetime.fromtimestamp(now - window_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def zero_touch_metrics(settings, *, since_iso: str, run=subprocess.run) -> dict | None:
    """`steward metrics zero-touch --since ISO --json` as a dict, or None on any failure (never raises)."""
    try:
        r = run([sys.executable, "-m", "steward.cli", "metrics", "zero-touch", "--since", since_iso, "--json"],
                cwd=settings.steward_repo, env={**os.environ, "STEWARD_HOME": str(settings.steward_home)},
                capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        out = json.loads(r.stdout)
    except Exception as exc:  # noqa: BLE001
        log.info("zero-touch metrics unavailable: %s", exc)
        return None
    return out if isinstance(out, dict) else None


def render(cdb, *, since: float, until: float, metrics: dict | None, absorbed_fn=None) -> str:
    eps = [e for e in cdb.episodes_since(since) if e.get("opened_at") is None or e["opened_at"] <= until]
    rows = []
    for ep in eps:
        rungs = _json(ep.get("rungs_json"), [])
        ev = _json(ep.get("evidence_json"), {})
        rows.append("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in (
            _local(ep.get("opened_at")), str(ep.get("job_id") or "")[:8], ep.get("trigger"), _rule_names(rungs),
            _classification(ep, rungs), ep.get("outcome") or "open", _unstick(ep), ev.get("delivery") or "—",
            str(ep.get("diagnosis") or "")[:200]))
            + "</tr>")
    head = "".join(f"<th>{h}</th>" for h in ("opened", "job", "trigger", "rungs", "classification", "outcome",
                                              "time to unstick", "ask delivery", "diagnosis"))
    table = f"<table><tr>{head}</tr>{''.join(rows)}</table>" if rows else "<p>no episodes</p>"
    counts = ", ".join(f"{o} {sum(1 for e in eps if e.get('outcome') == o)}" for o in OUTCOMES)
    over = cdb.overturns_in(PRECISION_WINDOW_S, until)
    acts = cdb.actions_in(PRECISION_WINDOW_S, until)
    precision = f"{round(100 * max(0.0, 1 - over / acts))}%" if acts else "n/a"
    proposed = [str(e["rule_proposal_id"]) for e in eps if e.get("rule_proposal_id")]
    absorbed = [t for t in proposed if _absorbed(absorbed_fn, t)]
    b = cdb.budget_today(until)
    if metrics is None:
        mhtml = "<p>metrics unavailable</p>"
    else:
        mhtml = "<ul>" + "".join(f"<li>{k}: {_e(metrics.get(k, '—'))}</li>" for k in (
            "touches_per_job", "coordinator_actions", "asks")) + "</ul>"
    disabled = _disabled(cdb)
    return (
        f"<!doctype html><html><head><meta charset=\"utf-8\"><title>Steward coordinator</title>"
        f"<style>{_STYLE}</style></head><body><h1>Steward coordinator — {_e(_local(until))}</h1>"
        f"<h2>Episodes</h2>{table}<h2>Outcomes</h2><p>{_e(counts)}</p>"
        f"<h2>Precision (14 d)</h2><p>overturned {over} of {acts} actions; precision {precision}</p>"
        f"<p>disabled rungs: {_e(', '.join(disabled) or 'none')}</p>"
        f"<h2>Rule proposals</h2><p>{len(proposed)} proposed, {len(absorbed)} absorbed"
        f"{' (' + _e(', '.join(proposed)) + ')' if proposed else ''}</p>"
        f"<h2>Budget today</h2><p>${float(b.get('usd') or 0):.2f}, {_e(b.get('codex_calls'))} Codex calls, "
        f"tg {_e(b.get('tg_sent'))}</p><h2>Zero-touch metrics</h2>{mhtml}</body></html>")


def publish(html: str, *, slug: str, run=subprocess.run) -> str | None:
    try:
        path = coordinator_link.state_dir() / "digest.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html)
        title = f"Steward coordinator — {time.strftime('%Y-%m-%d')}"
        r = run(["pixel", "update", slug, str(path), "--title", title], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            log.warning("pixel update failed rc=%s", r.returncode)
            return None
        for tok in (r.stdout or "").split():
            if tok.startswith("https://"):
                return tok
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("digest publish failed: %s", exc)
        return None
