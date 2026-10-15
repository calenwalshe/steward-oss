#!/usr/bin/env python3
"""Trim a job's Claude transcripts into a content-free replay fixture (phase 16-07, G15, T-16-23/24).

Reads `DIR/*.jsonl` and `DIR/*/subagents/*.jsonl` (+ `.meta.json`) READ-ONLY and writes exactly one file, `--out`:
a gzip of sorted-key compact JSON with gzip mtime 0, so two runs are byte-identical.

The output is an allow-list schema of numbers, ids, models, stop reasons, effort, timestamps, verbatim meta.json
fields, tool names, sha1[:10] command/path keys and character counts. No prompt, text, code, tool input value
or tool output ever enters it; the watcher-run marker keeps only the fixed opening words and the watcher name.
Any string longer than 200 characters aborts the run.

`expected` comes from this script's OWN loop over the raw files (not tokens._parse_file): dedupe by message id with
the worker file first (a streamed id keeps its largest output), priced with steward.tokens.price_usd.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from steward import tokens  # noqa: E402

META_KEYS = ("agentType", "description", "toolUseId", "spawnDepth", "requestShape", "requestNonInteractive")
MAX_STR = 200
_USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


class TrimError(SystemExit):
    pass


def _fail(msg: str) -> None:
    print(f"trim_efficiency_fixture: {msg}", file=sys.stderr)
    raise TrimError(2)


def _marker(text: str) -> str:
    """The fixed opening words the parser classifies a transcript by, and nothing after them."""
    if text.startswith(tokens._WATCHER_MARKER):
        m = tokens._WATCHER_NAME_RE.match(text[len(tokens._WATCHER_MARKER) - len("named "):])
        if m:
            end = len(tokens._WATCHER_MARKER) + len(m.group(1))
            return text[:end] + (", " if text[end:end + 2] == ", " else "")
        return tokens._WATCHER_MARKER
    if text.startswith(tokens._ASTRA_MARKER):
        return tokens._ASTRA_MARKER
    if text.startswith(tokens.REVIEW_MARKER):
        return tokens.REVIEW_MARKER
    if tokens._SOL_MARKER in text:
        return tokens._SOL_MARKER
    return ""


def _tool_row(b: dict) -> list:
    """[tool_use_id, name, cmd_key, path_key, write_chars, whole_read] via the parser's own definitions."""
    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
    name = b.get("name") or ""
    cmd = tokens._key10(str(inp.get("command") or "")) if name == "Bash" else ""
    path = ""
    if name in tokens._PATH_TOOLS:
        path = tokens._key10(str(inp.get("file_path") or inp.get("notebook_path") or ""))
    wchars = len(str(inp.get("content") or "")) if name == "Write" else 0
    whole = name == "Read" and "offset" not in inp and "limit" not in inp
    return [b.get("id") or "", name, cmd, path, wchars, whole]


def _read_lines(path: Path):
    """(parsed line objects with a timestamp) in file order; unparsable lines are skipped."""
    out = []
    with open(path, "rb") as fh:
        for raw in fh:
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _meta_of(path: Path) -> dict:
    try:
        raw = json.loads(path.with_suffix(".meta.json").read_text())
    except (OSError, ValueError):
        return {}
    return {k: raw[k] for k in META_KEYS if isinstance(raw, dict) and k in raw}


def _record(obj: dict, t: float) -> dict:
    typ = obj.get("type")
    if typ == "assistant":
        msg = obj.get("message") or {}
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        return {"t": t, "k": "a", "id": msg.get("id") or "", "m": msg.get("model") or "",
                "u": [int(usage.get(k) or 0) for k in _USAGE], "v": tokens.visible_chars(blocks),
                "s": msg.get("stop_reason") or "", "e": obj.get("effort") or "",
                "tu": [_tool_row(b) for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]}
    if typ == "user":
        content = (obj.get("message") or {}).get("content")
        res = []
        if isinstance(content, list):
            res = [[b.get("tool_use_id") or "", tokens._result_chars(b.get("content"))]
                   for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        return {"t": t, "k": "u", "r": res}
    return {"t": t, "k": "o", "y": str(typ or "")}


def _guard(node, where: str = "") -> None:
    if isinstance(node, str):
        if len(node) > MAX_STR:
            _fail(f"refusing: a string value of {len(node)} chars (> {MAX_STR}) at {where or 'root'}")
    elif isinstance(node, dict):
        for k, v in node.items():
            _guard(k, f"{where}/key")
            _guard(v, f"{where}/{k}")
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            _guard(v, f"{where}[{i}]")


def _iso(t: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def trim(project_dir: Path, worker_session: str, job: str, until: float | None) -> dict:
    files = []
    paths = sorted(project_dir.glob("*.jsonl")) + sorted(project_dir.glob("*/subagents/*.jsonl"))
    for p in paths:
        rel = p.relative_to(project_dir).as_posix()
        is_sub = p.parent.name == "subagents"
        kind = "subagent" if is_sub else "worker" if p.stem == worker_session else "toplevel"
        objs = _read_lines(p)
        marker, first_user, lines = "", True, []
        for obj in objs:
            if obj.get("type") == "user" and first_user:
                first_user = False
                marker = _marker(tokens._text_of(obj.get("message") or {}))
            ts = tokens._epoch(obj.get("timestamp", "")) if isinstance(obj.get("timestamp"), str) else 0.0
            if not ts:
                if obj.get("type") == "assistant":
                    _fail(f"{rel}: an assistant line without a timestamp")
                continue
            if until is not None and ts > until:
                continue
            lines.append(_record(obj, round(ts, 3)))
        files.append({"rel": rel, "kind": kind, "meta": _meta_of(p) if is_sub else {}, "marker": marker,
                      "lines": lines, "_objs": objs})
    return {"files": files}


def _expected(project_dir: Path, worker_session: str, files: list[dict], until: float | None) -> dict:
    """An independent recompute from the raw transcripts (worker file first, each id once, largest output kept)."""
    def last_t(f):
        ts = [r["t"] for r in f["lines"]]
        return max(ts) if ts else 0.0

    def order(f):
        if f["kind"] == "worker":
            return (0, 0.0, f["rel"])
        sess = f["rel"].split("/", 1)[0]
        if f["kind"] == "subagent" and sess == worker_session:
            return (1, last_t(f), f["rel"])
        return (2 if f["kind"] == "toplevel" else 3, last_t(f), f["rel"])

    seen: set[str] = set()
    by_source: dict[str, float] = {}
    calls_by_rel: dict[str, int] = {}
    total = usd_worker = usd_sub = 0.0
    latest = 0.0
    for f in sorted(files, key=order):
        best: dict[str, tuple] = {}  # id -> (output, model, vals)
        for obj in f["_objs"]:
            if obj.get("type") != "assistant":
                continue
            ts = tokens._epoch(obj.get("timestamp", "")) if isinstance(obj.get("timestamp"), str) else 0.0
            if until is not None and ts > until:
                continue
            msg = obj.get("message") or {}
            usage, mid = msg.get("usage"), msg.get("id")
            if not isinstance(usage, dict) or not mid:
                continue
            vals = [int(usage.get(k) or 0) for k in _USAGE]
            if mid not in best or vals[1] > best[mid][0]:
                best[mid] = (vals[1], msg.get("model"), vals)
        if f["kind"] == "subagent":
            src = f"sub:{f['meta'].get('agentType') or 'unknown'}"
        elif f["marker"].startswith(tokens._WATCHER_MARKER):
            src = "watcher:" + f["marker"][len(tokens._WATCHER_MARKER):].rstrip(", ")
        elif f["marker"] == tokens.REVIEW_MARKER:
            src = "efficiency"
        elif f["kind"] == "worker":
            src = "worker"
        else:
            src = "sol" if f["marker"] == tokens._SOL_MARKER else "chats"
        n = 0
        for mid, (_o, model, vals) in best.items():
            if mid in seen:
                continue
            seen.add(mid)
            n += 1
            cost = tokens.price_usd(model, vals)
            total += cost
            by_source[src] = by_source.get(src, 0.0) + cost
            if src == "worker":
                usd_worker += cost
            elif src.startswith("sub:"):
                usd_sub += cost
        calls_by_rel[f["rel"]] = n
        latest = max(latest, last_t(f))
    return {"usd_total": total, "usd_worker": usd_worker, "usd_sub": usd_sub, "usd_by_source": by_source,
            "calls_by_rel": calls_by_rel, "generated_until": _iso(until if until is not None else latest)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--worker-session", required=True)
    ap.add_argument("--job", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--until", default=None, help="ISO timestamp: keep only lines at or before it")
    args = ap.parse_args(argv)
    project_dir = Path(args.project_dir).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if not project_dir.is_dir():
        _fail(f"not a directory: {project_dir}")
    if out == project_dir or project_dir in out.parents:
        _fail("refusing to write inside the source project dir")
    until = None
    if args.until:
        until = tokens._epoch(args.until)
        if not until:
            _fail(f"bad --until: {args.until}")
    data = trim(project_dir, args.worker_session, args.job, until)
    files = data["files"]
    expected = _expected(project_dir, args.worker_session, files, until)
    for f in files:
        del f["_objs"]
    doc = {"job": args.job, "worker_session": args.worker_session, "files": sorted(files, key=lambda f: f["rel"]),
           "expected": expected}
    _guard(doc)
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
        gz.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
