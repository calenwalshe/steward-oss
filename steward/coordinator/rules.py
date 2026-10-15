"""Rule proposals (spec 5.15): a novel unblock files one tasks-hub task; never a self-launched Steward job."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile

log = logging.getLogger(__name__)
_EVIDENCE_LINES = 40
_ZT = re.compile(r"\bZT[-\s]?\d+\b", re.I)


def novel(cdb, rung: str, classification: str, now, window_s=30 * 86400) -> bool:
    """True when no episode closed `resolved` inside (now-window_s, now] recorded this (rung, classification)."""
    for ep in cdb.episodes_since(now - window_s):
        if ep.get("outcome") != "resolved":
            continue
        closed = ep.get("closed_at")
        if closed is None or not (now - window_s < closed <= now):
            continue
        try:
            items = json.loads(ep.get("rungs_json") or "[]")
        except (TypeError, ValueError):
            continue
        for it in items:
            if isinstance(it, dict) and it.get("rung") == rung and it.get("classification") == classification:
                return False
    return True


def _evidence(episode: dict) -> str:
    raw = episode.get("evidence_json", episode.get("evidence"))
    if raw in (None, "", {}):
        return "(none recorded)"
    if not isinstance(raw, str):
        raw = json.dumps(raw, indent=1, default=str)
    else:
        try:
            raw = json.dumps(json.loads(raw), indent=1, default=str)
        except ValueError:
            pass
    lines = raw.splitlines()
    if len(lines) > _EVIDENCE_LINES:
        lines = lines[:_EVIDENCE_LINES] + ["..."]
    return "\n".join(lines)


def _where(episode: dict) -> str:
    if episode.get("where"):
        return str(episode["where"])
    try:
        for it in json.loads(episode.get("rungs_json") or "[]"):
            if isinstance(it, dict) and it.get("where"):
                return str(it["where"])
    except (TypeError, ValueError):
        pass
    return "see rule text"


def _body(episode: dict, rule_text: str) -> str:
    job = str(episode.get("job_id") or "")[:8] or "-"
    zt = _ZT.search(rule_text)
    return "\n".join([
        f"Episode: {episode.get('episode_id', '-')}",
        f"Job: {job}",
        f"Trigger: {episode.get('trigger', '-')}",
        f"Classification: {episode.get('classification', '-')}",
        "",
        "Evidence:",
        _evidence(episode),
        "",
        f"Diagnosis: {episode.get('diagnosis') or '-'}",
        "",
        "Proposed rule:",
        rule_text,
        "",
        f"Where it belongs: {_where(episode)}",
        f"Overlaps ZT task: {zt.group(0) if zt else '—'}",
        "",
        'To mark absorbed: tasks note <id> "absorbed: <commit>"  (<id> = this task\'s id)',
        "",
    ])


def file(episode: dict, rule_text: str, *, run=subprocess.run) -> str | None:
    """File a rule-proposal task; return its id, or None on any error. Never raises."""
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            path = fh.name
            fh.write(_body(episode, rule_text))
        argv = ["tasks", "add", f"rule: {rule_text[:80]}", "--area", "steward",
                "--tag", "rule-proposal", "--tag", "steward-coordinator", "--body-file", path]
        r = run(argv, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log.warning("tasks add failed rc=%s: %s", r.returncode, (r.stderr or "")[:200])
            return None
        m = re.search(r"#(\d+)", r.stdout or "")
        if not m:
            log.warning("tasks add: no task id in output %r", (r.stdout or "")[:200])
            return None
        return m.group(1)
    except Exception as exc:  # noqa: BLE001 - never raises
        log.warning("rule proposal not filed: %s", exc)
        return None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def absorbed(task_id: str, *, run=subprocess.run) -> bool:
    """`tasks show <id>` has a line starting `absorbed:`."""
    try:
        r = run(["tasks", "show", str(task_id)], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return False
        return any(ln.strip().lower().startswith("absorbed:") for ln in (r.stdout or "").splitlines())
    except Exception as exc:  # noqa: BLE001
        log.warning("tasks show %s failed: %s", task_id, exc)
        return False
