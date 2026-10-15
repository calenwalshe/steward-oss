"""Token accounting per job: every token that flowed through a job's Claude sessions.

Sources: the worker's transcript and any forked chat / Sol transcripts in the
worktree's Claude project dir (deduped by `message.id`, since forks copy the
parent's history and one message streams over several lines), plus Astra's
trajectory reviews: `claude -p` run from the advisor pane's folder (the Steward
repo), found by their opening prompt and matched to the job whose review request
is nearest in time. A review with no transcript falls back to the usage stored
in its `trajectory_advice:*` record.

Subagent transcripts (`<projectdir>/<session>/subagents/agent-<id>.jsonl`, with the
agent's type in the sibling `agent-<id>.meta.json`) are counted as `sub:<agentType>`
(phase 16, G1); `worker_usd` is the worker's corrected spend (worker + every sub:*).

Per-file parse results are cached on disk (`$STEWARD_HOME/tokens-cache.sqlite`, one row per transcript, keyed
by mtime+size) so a fresh process never reparses an unchanged transcript. A pass loads only the rows it reads and
writes only the rows it changed (TUI speed plan 11; it used to be one 17 MB JSON, loaded and rewritten whole).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from steward.chat import project_slug, worker_session_id, worktree_path

FIELDS = ("input", "output", "cache_read", "cache_creation")
_USAGE_KEYS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_creation": "cache_creation_input_tokens",
}
# Published list prices, USD per 1M tokens (Anthropic API, 2026-09): (input, output, cache read).
# Cache writes bill at 1.25x input for the 5-minute TTL and 2x for the 1-hour TTL (what Claude Code
# writes). Matched by model-id prefix.
PRICES = {
    "claude-fable": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus": (5.0, 25.0, 0.50),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet": (3.0, 15.0, 0.30),
    "claude-haiku": (1.0, 5.0, 0.10),
}
_CACHE_WRITE_X = 1.25
_CACHE_WRITE_1H_X = 2.0
# Bumped when the rates change: parsed transcript entries carry it, and an older (wrongly priced) one is re-parsed.
RATES_VERSION = 2
# "Effective tokens" = cost expressed in Sonnet 5.5 input tokens ($2 per 1M), so a
# cheap cache read counts for a tenth of a token and an Opus output token for ten.
EFFECTIVE_UNIT_USD = 2.0 / 1_000_000
_ALIASES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-4-5"}


def price_usd(model: str | None, vals, cc_1h: int = 0) -> float:
    """Cost of one message's usage [input, output, cache_read, cache_creation]. `cc_1h` is how many of the
    cache_creation tokens were 1-hour writes (2x input); the rest bill as 5-minute writes (1.25x)."""
    model = _ALIASES.get(model or "", model or "claude-sonnet-5-5")
    rate = next((r for k, r in PRICES.items() if model.startswith(k)), PRICES["claude-sonnet-5"])
    p_in, p_out, p_cr = rate
    i, o, cr, cc = vals
    cc_1h = min(max(int(cc_1h or 0), 0), cc)
    return (i * p_in + o * p_out + cr * p_cr
            + (cc - cc_1h) * p_in * _CACHE_WRITE_X + cc_1h * p_in * _CACHE_WRITE_1H_X) / 1_000_000


_SOL_MARKER = "A coding worker under a durable job runner appears stalled"
# Astra's reviews run `claude -p` from the advisor pane's folder (the Steward repo), not the
# job worktree; their transcripts start with this prompt and are matched to a job by the
# time the review was requested.
_ASTRA_MARKER = "You are a board member reviewing this project's trajectory"
_ASTRA_DIR = Path(__file__).resolve().parents[1]
_ASTRA_MAX_BYTES = 2_000_000  # review transcripts are small; skip big interactive sessions
_ASTRA_MATCH_S = (-5.0, 90.0)  # transcript start relative to trajectory_review_requested
# A custom watcher's `claude -p` run starts with this line, then the watcher's name.
_WATCHER_MARKER = "You are a Steward custom watcher named "
# The efficiency watcher's optional model review (16-06) starts with this line: supervision source "efficiency".
REVIEW_MARKER = "You are the Steward efficiency reviewer"
_WATCHER_NAME_RE = re.compile(r"named ([A-Za-z0-9_-]+)")
_CACHE_NAME = "tokens-cache.json"  # the pre-plan-11 single-file cache: imported once, then never written
_CACHE_DB = "tokens-cache.sqlite"
SOURCES = ("worker", "chats", "sol", "astra")


def _empty() -> dict:
    return {**{f: 0 for f in FIELDS}, "all": 0}


def _add(bucket: dict, vals: list[int]) -> None:
    for f, v in zip(FIELDS, vals):
        bucket[f] += v
    bucket["all"] += sum(vals)


def _epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _text_of(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _key10(s: str) -> str:
    """sha1[:10] of a command or path: the only form in which those are ever cached (T-16-04)."""
    return hashlib.sha1((s or "").encode("utf-8", "replace")).hexdigest()[:10]


def visible_chars(blocks) -> int:
    """Characters a message shows: text blocks plus tool_use inputs as compact JSON. Thinking is
    redacted in transcripts and counts 0. The one definition, shared with the 16-07 fixtures."""
    if isinstance(blocks, str):
        return len(blocks)
    n = 0
    for b in blocks or ():
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            n += len(b.get("text") or "")
        elif t == "tool_use":
            n += len(json.dumps(b.get("input"), separators=(",", ":")))
    return n


def _result_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return 0


_PATH_TOOLS = ("Read", "Write", "Edit", "NotebookEdit")


def _tool_row(b: dict, ts: float) -> list:
    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
    name = b.get("name") or ""
    cmd = _key10(str(inp.get("command") or "")) if name == "Bash" else ""
    path = ""
    if name in _PATH_TOOLS:
        path = _key10(str(inp.get("file_path") or inp.get("notebook_path") or ""))
    wchars = len(str(inp.get("content") or "")) if name == "Write" else 0
    whole = name == "Read" and "offset" not in inp and "limit" not in inp
    return [b.get("id") or "", name, ts, cmd, path, wchars, whole]


def _new_state() -> dict:
    return {"sol": False, "astra": False, "review": False, "watcher": None, "start": 0.0, "msgs": {}, "cost": {}, "cc1h": {},
            "detail": {"calls": {}, "tools": [], "results": [], "first_ts": None, "last_ts": None, "last_type": None}}


def _feed(st: dict, line: str, first_user: list) -> bool:
    """Apply one transcript line to the parse state; False if it is not valid JSON."""
    line = line.strip()
    if not line:
        return True
    try:
        obj = json.loads(line)
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return True
    d = st["detail"]
    typ = obj.get("type")
    ts = _epoch(obj.get("timestamp", ""))
    prev_ts = d["last_ts"]
    if ts:
        if d["first_ts"] is None:
            d["first_ts"] = ts
        d["last_ts"] = ts if prev_ts is None else max(prev_ts, ts)
    if typ == "user":
        msg = obj.get("message") or {}
        if first_user[0]:
            first_user[0] = False
            text = _text_of(msg)
            st["sol"] = _SOL_MARKER in text
            st["astra"] = text.startswith(_ASTRA_MARKER)
            st["review"] = text.startswith(REVIEW_MARKER)
            if text.startswith(_WATCHER_MARKER):
                m = _WATCHER_NAME_RE.match(text[len(_WATCHER_MARKER) - len("named "):])
                st["watcher"] = m.group(1) if m else None
            st["start"] = ts
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    d["results"].append([b.get("tool_use_id") or "", ts, _result_chars(b.get("content"))])
        d["last_type"] = "user"
        return True
    if typ != "assistant":
        return True
    d["last_type"] = "assistant"
    msg = obj.get("message") or {}
    content = msg.get("content")
    blocks = content if isinstance(content, list) else []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            d["tools"].append(_tool_row(b, ts))
    usage = msg.get("usage")
    mid = msg.get("id")
    if not isinstance(usage, dict) or not mid:
        return True
    vals = [int(usage.get(_USAGE_KEYS[f]) or 0) for f in FIELDS]
    split = usage.get("cache_creation")
    cc_1h = int(split.get("ephemeral_1h_input_tokens") or 0) if isinstance(split, dict) else 0
    prev = st["msgs"].get(mid)
    # Streamed repeats carry the same usage; keep the largest output seen.
    if prev is None or vals[1] > prev[3]:
        st["msgs"][mid] = [mid, ts, *vals]
        st["cost"][mid] = price_usd(msg.get("model"), vals, cc_1h)
        st["cc1h"][mid] = cc_1h
    has_tool = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)
    c = d["calls"].get(mid)
    if c is None:
        c = d["calls"][mid] = [prev_ts if prev_ts is not None else ts, ts, 0, "", None, "", False]
    c[1] = max(c[1], ts)
    c[2] += visible_chars(blocks)
    c[3] = msg.get("model") or c[3]
    c[4] = msg.get("stop_reason") or c[4]
    c[5] = obj.get("effort") or c[5]
    c[6] = c[6] or has_tool
    return True


def _finalize(st: dict, offset: int, first_user_done: bool) -> dict:
    return {"sol": st["sol"], "astra": st["astra"], "review": st.get("review", False), "watcher": st["watcher"],
            "start": st["start"],
            "msgs": list(st["msgs"].values()), "cost": st["cost"], "cc1h": st.get("cc1h", {}),
            "detail": st["detail"], "offset": offset, "fu": first_user_done, "rates": RATES_VERSION}


def _parse_file(path: Path, *, start: int = 0, state: dict | None = None) -> dict:
    """{"sol", "astra", "watcher", "start", "msgs": [[id, epoch, in, out, cr, cc], ...] (each id once),
    "cost": {id: usd}, "detail": {calls, tools, results, first_ts, last_ts, last_type},
    "offset": bytes consumed, "fu": first user line seen}.

    Resumes from byte `start` with `state` (a previous entry, extended in place) so a grown transcript is
    parsed once per byte. Only complete lines are consumed: a trailing line without its newline waits
    unless it already is a whole JSON object."""
    if state is None:
        st, first_user, start = _new_state(), [True], 0
    else:
        st = {k: state[k] for k in ("sol", "astra", "watcher", "start", "cost", "detail")}
        st["cc1h"] = state.get("cc1h", {})
        st["review"] = bool(state.get("review", False))
        st["msgs"] = {m[0]: m for m in state["msgs"]}
        first_user = [not state.get("fu", True)]
    pos = start
    with open(path, "rb") as fh:
        fh.seek(start)
        for raw in fh:
            ok = _feed(st, raw.decode("utf-8", "replace"), first_user)
            if raw.endswith(b"\n") or ok:
                pos += len(raw)
    return _finalize(st, pos, not first_user[0])


_DB_LOCK = threading.Lock()
_DBS: dict[str, sqlite3.Connection] = {}


def _db(home: Path) -> sqlite3.Connection | None:
    """The process's connection to `<home>/tokens-cache.sqlite` (created, and filled once from the old JSON cache)."""
    key = str(home)
    with _DB_LOCK:
        conn = _DBS.get(key)
        if conn is not None:
            return conn
        try:
            conn = sqlite3.connect(str(home / _CACHE_DB), timeout=5.0, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS entries (path TEXT PRIMARY KEY, sig TEXT, rates INTEGER,"
                         " astra INTEGER, start REAL, entry TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
            if conn.execute("SELECT 1 FROM meta WHERE k = 'imported'").fetchone() is None:
                _import_json(conn, home)
        except (OSError, sqlite3.Error):
            return None
        while len(_DBS) >= 4:  # one home per process in practice; tests and tools open many
            _DBS.pop(next(iter(_DBS))).close()
        _DBS[key] = conn
        return conn


def _row(path: str, ent: dict) -> tuple:
    return (path, json.dumps(ent.get("sig")), ent.get("rates"), 1 if ent.get("astra") else 0, ent.get("start"),
            json.dumps(ent))


def _import_json(conn: sqlite3.Connection, home: Path) -> None:
    try:
        data = json.loads((home / _CACHE_NAME).read_text())
    except (OSError, ValueError):
        data = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM meta WHERE k = 'imported'").fetchone() is None:
            conn.executemany("INSERT OR IGNORE INTO entries VALUES (?, ?, ?, ?, ?, ?)",
                             [_row(k, v) for k, v in data.items() if not k.startswith("__") and isinstance(v, dict)])
            conn.execute("INSERT INTO meta VALUES ('imported', ?)", (str(time.time()),))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


class _Cache(dict):
    """The parse cache of one pass (or a long-lived reader): a dict of the entries read so far, each loaded from the
    sqlite file on first `get`. `__`-prefixed keys are per-pass memos, never stored. `_save_cache` writes the entries
    set since the last save (an entry changed in place must be set again)."""

    def __init__(self, home: Path):
        super().__init__()
        self.home = home
        self.dirty: set[str] = set()
        self._missing: set[str] = set()

    def get(self, key, default=None):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        if key.startswith("__") or key in self._missing:
            return default
        conn = _db(self.home)
        row = None
        if conn is not None:
            try:
                row = conn.execute("SELECT entry FROM entries WHERE path = ?", (key,)).fetchone()
            except sqlite3.Error:
                row = None
        if row is None:
            self._missing.add(key)
            return default
        ent = json.loads(row[0])
        dict.__setitem__(self, key, ent)
        return ent

    def peek(self, key) -> tuple | None:
        """(sig, rates, astra, start) without decoding the entry; None if there is none."""
        if dict.__contains__(self, key):
            return _peek_entry(dict.__getitem__(self, key))
        if key in self._missing:
            return None
        conn = _db(self.home)
        try:
            row = conn.execute("SELECT sig, rates, astra, start, entry LIKE '%\"cost\"%' FROM entries WHERE path = ?",
                               (key,)).fetchone() if conn is not None else None
        except sqlite3.Error:
            row = None
        if row is None:
            self._missing.add(key)
            return None
        return (json.loads(row[0]) if row[0] else None, row[1], bool(row[2]), row[3], bool(row[4]))

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        self._missing.discard(key)
        if not key.startswith("__"):
            self.dirty.add(key)


def _peek_entry(ent) -> tuple | None:
    if not isinstance(ent, dict):
        return None
    return ent.get("sig"), ent.get("rates"), bool(ent.get("astra")), ent.get("start"), "cost" in ent


def _peek(cache: dict, key: str) -> tuple | None:
    """(sig, rates, astra, start, has_cost) of a cached entry without loading it where the cache allows."""
    peek = getattr(cache, "peek", None)
    return peek(key) if peek is not None else _peek_entry(cache.get(key))


def _load_cache(home: Path) -> dict:
    return _Cache(Path(home))


def new_pass(cache: dict) -> dict:
    """Start a counting pass on a cache that outlives one (efficiency_watch keeps one for the process): drop the
    "__"-prefixed entries, which are valid for one pass only (the advisor listing, the review-request snapshot,
    the transcript-to-job match, the dirty flag). A freshly loaded cache has none."""
    for k in [k for k in cache if k.startswith("__")]:
        del cache[k]
    return cache


def _save_cache(home: Path, cache: dict) -> None:
    """Write the entries this pass set (a plain dict: all of them). "__"-prefixed entries (e.g. the advisor folder
    listing) live for one pass only, never on disk."""
    keys = set(cache.dirty) if isinstance(cache, _Cache) else {k for k in cache if not k.startswith("__")}
    rows = [_row(k, dict.__getitem__(cache, k)) for k in keys if dict.__contains__(cache, k)]
    conn = _db(Path(home))
    if conn is None or not rows:
        return
    try:
        with _DB_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany("INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?, ?, ?)", rows)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    except sqlite3.Error:
        return
    if isinstance(cache, _Cache):
        cache.dirty -= keys


def cache_entries(home: Path) -> dict:
    """Every stored parse entry, path -> entry (tests and tooling)."""
    conn = _db(Path(home))
    if conn is None:
        return {}
    return {path: json.loads(entry) for path, entry in conn.execute("SELECT path, entry FROM entries")}


def _files_for(store, job_id: str, projects_dir: Path | None) -> tuple[str | None, list[Path], list[Path]]:
    """(worker session id, top-level transcripts, subagent transcripts: exactly `<session>/subagents/*.jsonl`)."""
    base = projects_dir if projects_dir is not None else Path.home() / ".claude" / "projects"
    d = base / project_slug(worktree_path(store, job_id))
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if d.is_dir() else []
    subs = list(d.glob("*/subagents/*.jsonl")) if d.is_dir() else []
    return worker_session_id(store, job_id, projects_dir=base), files, subs


def worker_usd(tokens: dict, *, hour: bool = False) -> float:
    """Corrected worker spend: source "worker" plus every "sub:*" source (subagents are worker spend)."""
    key = "usd_hour" if hour else "usd"
    total = 0.0
    for src, s in ((tokens or {}).get("sources") or {}).items():
        if src == "worker" or src.startswith("sub:"):
            try:
                total += float((s or {}).get(key) or 0.0)
            except (TypeError, ValueError):
                pass
    return total


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _meta(path: Path) -> tuple[str, str]:
    """(agentType, description) from the subagent's sibling meta.json; ("unknown", "") if absent."""
    try:
        meta = json.loads(path.with_suffix(".meta.json").read_text())
    except (OSError, ValueError):
        return "unknown", ""
    if not isinstance(meta, dict):
        return "unknown", ""
    t, desc = meta.get("agentType"), meta.get("description")
    return (t if isinstance(t, str) and t else "unknown"), (desc if isinstance(desc, str) else "")


def _transcripts(store, job_id: str, projects_dir: Path | None, cache: dict) -> tuple[list[dict], bool]:
    """The job's transcripts in counting order (worker top-level first: it owns shared ids, then its own
    subagents, other files, other sessions' subagents) with their parsed cache entries; (list, cache dirty)."""
    wsid, files, subs = _files_for(store, job_id, projects_dir)
    files, subs = sorted(files, key=_mtime), sorted(subs, key=_mtime)
    order = ([(p, False) for p in files if p.stem == wsid]
             + [(p, True) for p in subs if p.parent.parent.name == wsid]
             + [(p, False) for p in files if p.stem != wsid]
             + [(p, True) for p in subs if p.parent.parent.name != wsid])
    out, dirty = [], False
    for p, is_sub in order:
        try:
            st = p.stat()
        except OSError:
            continue
        sig = [st.st_mtime_ns, st.st_size]
        ent = cache.get(str(p))
        usable = (bool(ent) and all(k in ent for k in ("cost", "watcher", "detail", "offset", "fu"))
                  and ent.get("rates") == RATES_VERSION)
        if not usable or ent.get("sig") != sig:
            try:
                if usable and st.st_size >= ent["offset"]:  # grown: parse only the new bytes
                    keep = {"agent_type": ent["agent_type"]} if ent.get("agent_type") else {}
                    ent = {"sig": sig, **keep, **_parse_file(p, start=ent["offset"], state=ent)}
                else:  # new, shrunk, or a pre-offset cache entry: parse the whole file
                    ent = {"sig": sig, **_parse_file(p)}
            except OSError:
                continue
            cache[str(p)] = ent
            dirty = True
        atype = desc = ""
        if is_sub:
            atype, desc = _meta(p)
            if ent.get("agent_type") != atype and atype != "unknown":  # the meta may appear after the transcript
                ent["agent_type"] = atype
                cache[str(p)] = ent  # set again: the save writes what was set
                dirty = True
            elif atype == "unknown" and ent.get("agent_type"):
                atype = ent["agent_type"]
        if is_sub:
            source, session = f"sub:{atype}", p.parent.parent.name
        else:
            session = p.stem
            source = (f"watcher:{ent['watcher']}" if ent.get("watcher")  # even if the newest-file fallback picked it as "worker"
                      else "efficiency" if ent.get("review")
                      else "worker" if p.stem == wsid else "sol" if ent["sol"] else "chats")
        out.append({"path": p, "stem": p.stem, "session": session, "source": source, "agent_type": atype,
                    "description": desc, "entry": ent})
    return out, dirty


def job_transcripts(store, job_id: str, projects_dir: Path | None = None, _cache: dict | None = None) -> list[dict]:
    """[{path, stem, session, source, agent_type, description, entry}] in counting order: the one file walk
    and cache shared by job_tokens and the efficiency metrics."""
    cache = _load_cache(Path(store.home)) if _cache is None else _cache
    out, dirty = _transcripts(store, job_id, projects_dir, cache)
    if dirty and _cache is None:
        _save_cache(Path(store.home), cache)
    return out


def job_tokens(store, job_id: str, now: float | None = None, projects_dir: Path | None = None,
               _cache: dict | None = None) -> dict:
    now = time.time() if now is None else now
    cutoff = now - 3600
    total, last_hour = _empty(), _empty()
    by_source = {s: 0 for s in SOURCES}
    # Per source: all tokens, last-hour tokens, output tokens (the dashboard's per-model column).
    sources = {s: {"all": 0, "hour": 0, "out": 0, "usd": 0.0, "usd_hour": 0.0} for s in SOURCES}

    home = Path(store.home)
    cache = _load_cache(home) if _cache is None else _cache

    usd = {"total": 0.0, "hour": 0.0}

    def count(source: str, vals: list[int], ts: float, cost: float = 0.0) -> None:
        if source not in sources:  # dynamic source, e.g. "watcher:<name>"
            by_source[source] = 0
            sources[source] = {"all": 0, "hour": 0, "out": 0, "usd": 0.0, "usd_hour": 0.0}
        usd["total"] += cost
        sources[source]["usd"] += cost
        if ts >= cutoff:
            usd["hour"] += cost
            sources[source]["usd_hour"] += cost
        _add(total, vals)
        by_source[source] += sum(vals)
        sources[source]["all"] += sum(vals)
        sources[source]["out"] += vals[FIELDS.index("output")]
        if ts >= cutoff:
            _add(last_hour, vals)
            sources[source]["hour"] += sum(vals)

    listing, dirty = _transcripts(store, job_id, projects_dir, cache)
    if dirty:
        cache["__dirty__"] = True  # all_tokens rewrites the (large) cache file only when a pass changed it
    seen: set[str] = set()
    for tr in listing:
        ent = tr["entry"]
        for mid, ts, *vals in ent["msgs"]:
            if mid in seen:
                continue
            seen.add(mid)
            count(tr["source"], vals, ts, ent["cost"].get(mid, 0.0))
    if dirty and _cache is None:
        _save_cache(home, cache)

    matched = set()
    for rid, ts_req, ent in _astra_transcripts(store, job_id, projects_dir, cache):
        matched.add(rid)
        for mid, ts, *vals in ent["msgs"]:
            if mid not in seen:
                seen.add(mid)
                count("astra", vals, ts, ent["cost"].get(mid, 0.0))
    for key in _advice_keys(store, job_id):
        if key.split(":", 1)[1] in matched:
            continue
        advice = store.kv_get(job_id, key) or {}
        usage = advice.get("usage")
        if not isinstance(usage, dict):
            continue
        vals = [int(usage.get(f) or 0) for f in FIELDS]
        count("astra", vals, float(usage.get("ts") or advice.get("answered_at") or 0), price_usd("opus", vals))

    for b, key in ((total, "total"), (last_hour, "hour")):
        b["usd"] = usd[key]
        b["effective"] = int(usd[key] / EFFECTIVE_UNIT_USD)
    for src in sources.values():
        src["effective"] = int(src["usd"] / EFFECTIVE_UNIT_USD)
        src["effective_hour"] = int(src["usd_hour"] / EFFECTIVE_UNIT_USD)
    return {"total": total, "last_hour": last_hour, "by_source": by_source, "sources": sources}


def _astra_transcripts(store, job_id: str, projects_dir: Path | None, cache: dict):
    """(review_id, requested_at, parsed transcript) for each of this job's Astra reviews whose
    transcript is found in the advisor folder. Parses go through the shared file cache."""
    # Matching is global (each transcript goes to its nearest request), so do it once per pass for every job
    # instead of re-reading the advisor folder for each job. Checked first: the requests query below scans the
    # whole events table (no index on `what`), once per pass is enough.
    memo = cache.get("__astra_by_job__")
    if isinstance(memo, dict):
        return memo.get(job_id, [])
    # Every job's review requests: a transcript belongs to the NEAREST request, so two jobs
    # reviewed a minute apart never both claim the same transcript.
    requests = cache.get("__astra_requests__")
    if not isinstance(requests, list):
        rows = store._conn.execute(
            "SELECT job_id, ts, evidence FROM events WHERE what = 'trajectory_review_requested'"
        ).fetchall()
        requests = [(r["job_id"], json.loads(r["evidence"]).get("review_id"), _epoch(r["ts"])) for r in rows]
        cache["__astra_requests__"] = requests
    if not any(j == job_id for j, _, _ in requests):
        return []
    base = projects_dir if projects_dir is not None else Path.home() / ".claude" / "projects"
    d = base / project_slug(_ASTRA_DIR)
    if not d.is_dir():
        return []
    found = []
    # one directory listing per cache (i.e. per all_tokens pass), not one per job: the advisor folder holds thousands
    # of transcripts and re-globbing it for every job dominated the status pane's redraw time
    listing_key = f"__astra_listing__:{d}"
    listing = cache.get(listing_key)
    if not isinstance(listing, list):
        listing = sorted(str(p) for p in d.glob("*.jsonl"))
        cache[listing_key] = listing
    for p in map(Path, listing):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size > _ASTRA_MAX_BYTES:
            continue
        sig = [st.st_mtime_ns, st.st_size]
        # the signature, astra flag and start decide the match; the entry itself is loaded only for a match
        meta = _peek(cache, str(p))
        ent = None
        if not meta or meta[0] != sig or not meta[4] or meta[1] != RATES_VERSION:
            try:
                ent = {"sig": sig, **_parse_file(p)}
            except OSError:
                continue
            cache[str(p)] = ent
            cache["__dirty__"] = True
            meta = _peek_entry(ent)
        _sig, _rates, astra, start, _cost = meta
        if not astra:
            continue
        near = [(abs(start - req), j, rid, req) for j, rid, req in requests
                if rid and _ASTRA_MATCH_S[0] <= start - req <= _ASTRA_MATCH_S[1]]
        if near:
            _, j, rid, req = min(near)
            found.append((j, rid, req, ent if ent is not None else cache.get(str(p))))
    by_job: dict = {}
    for j, rid, req, ent in found:
        by_job.setdefault(j, []).append((rid, req, ent))
    cache["__astra_by_job__"] = by_job
    return by_job.get(job_id, [])


def _advice_keys(store, job_id: str) -> list[str]:
    rows = store._conn.execute(
        "SELECT key FROM kv WHERE job_id = ? AND key LIKE 'trajectory_advice:%'", (job_id,)
    ).fetchall()
    return [r["key"] for r in rows]


def all_tokens(store, job_ids: list[str], now: float | None = None, projects_dir: Path | None = None) -> dict:
    """{"jobs": {id: job_tokens}, "grand": {"total": bucket, "last_hour": bucket}}."""
    cache = _load_cache(Path(store.home))
    jobs = {j: job_tokens(store, j, now, projects_dir, _cache=cache) for j in job_ids}
    if jobs and cache.get("__dirty__"):
        _save_cache(Path(store.home), cache)
    grand = {k: {**_empty(), "usd": 0.0, "effective": 0} for k in ("total", "last_hour")}
    for r in jobs.values():
        for k in grand:
            for f in (*FIELDS, "all", "usd", "effective"):
                grand[k][f] = grand[k].get(f, 0) + r[k][f]
    return {"jobs": jobs, "grand": grand}


def humanize(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k" if n >= 10_000 else f"{n / 1_000:.1f}k"
    return str(n)
