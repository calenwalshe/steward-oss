"""Sanctioned research access for workers: the `research` MCP server, named API
keys, and a per-job spend cap.

Workers run with `--setting-sources project` (no user MCP servers, no readable
`~/.api-keys`, no readable `keys.env`). A job can be shaped with capabilities instead:

* `research` on  -> the worker is launched with `--mcp-config <jobs/<job>/mcp.json>
  --strict-mcp-config`; that file holds ONLY the `research` server entry copied
  from `~/.claude.json` (0600; any auth headers travel with it, never printed).
* `grant_keys` names -> the shim (Steward, not the worker model) loads exactly
  those names (looked up through `steward.keys`: process env, `$STEWARD_API_KEYS_FILE`,
  `<config dir>/keys.env`, then `~/.api-keys`) into the worker's process environment. Values
  are never written to disk, events, frames or transcripts (`steward.masking`).
  A granted key does NOT lift the worker's ban on reading `~/.api-keys` or `keys.env`.
* a spend cap (default $5): each `mcp__research__*` call is counted at an
  estimated cost; crossing the cap escalates (NEEDS_YOU) and denies further
  calls until `steward research-cap <JOB> <usd>`.

Test/ops overrides: `$STEWARD_CLAUDE_JSON` (default `~/.claude.json`),
`$STEWARD_API_KEYS_FILE`, `$STEWARD_LEGACY_KEYS_FILE` (default `~/.api-keys`), and an optional
`research_costs.json` in the config dir overriding `COST_TABLE`.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from steward import keys as keys_module
from steward.store import Store, resolve_config_dir, resolve_home

TOOL_PREFIX = "mcp__research__"
SERVER_NAME = "research"
DEFAULT_CAP_USD = 5.0
# Estimated USD per call when the MCP result carries no cost info.
COST_TABLE: dict[str, float] = {"research_quick": 0.02, "research_deep": 0.50}
DEFAULT_CALL_USD = 0.02
KEY_NAME_RE = keys_module.KEY_NAME_RE
_KV_CAPS = "capabilities"
_KV_STATE = "research_state"


class ResearchError(ValueError):
    pass


# -- capabilities -------------------------------------------------------------

def get_caps(store: Store, job_id: str) -> dict:
    caps = store.kv_get(job_id, _KV_CAPS) or {}
    return {"research": bool(caps.get("research")), "keys": list(caps.get("keys") or []),
            "cap_usd": float(caps.get("cap_usd", DEFAULT_CAP_USD))}


def research_enabled(store: Store, job_id: str) -> bool:
    return get_caps(store, job_id)["research"]


def granted_keys(store: Store, job_id: str) -> list[str]:
    return get_caps(store, job_id)["keys"]


def normalize_key_name(name: str, available: list[str] | None = None) -> str:
    """`ScrapeCreators` -> `SCRAPECREATORS_API_KEY` when that (unique) name exists in the keys file."""
    name = name.strip()
    if not KEY_NAME_RE.match(name):
        raise ResearchError(f"bad key name {name!r}")
    available = key_names() if available is None else available
    if name in available:
        return name
    up = name.upper()
    if up in available:
        return up
    matches = [k for k in available if k.upper().startswith(up + "_")]
    if len(matches) == 1:
        return matches[0]
    raise ResearchError(f"no unique key named {name!r} in the keys file")


def set_caps(store: Store, job_id: str, *, research: bool, keys: list[str], cap_usd: float | None = None) -> dict:
    """Store the job's capabilities and record `capability_granted` events (names only)."""
    from steward.models import Judgment

    cap = DEFAULT_CAP_USD if cap_usd is None else float(cap_usd)
    if cap <= 0:
        raise ResearchError("research cap must be positive")
    keys = list(dict.fromkeys(keys))
    store.kv_set(job_id, _KV_CAPS, {"research": bool(research), "keys": keys, "cap_usd": cap})
    if research:
        store.append_event(job_id, what="capability_granted",
                           evidence={"capability": "research", "cap_usd": cap,
                                     "cap_source": "owner_explicit" if cap_usd is not None else "system_default"},
                           rule="job_capability", judgment=Judgment.human)
        from steward import spend_policy

        spend_policy.set_limit(store, job_id, "research", cap,
                               binding="hard" if cap_usd is not None else "advisory",
                               provenance="owner_explicit" if cap_usd is not None else "system_default",
                               source={"via": "steward new" if cap_usd is not None else "default"}, now=time.time())
    for name in keys:
        store.append_event(job_id, what="capability_granted", evidence={"capability": "key", "name": name},
                           rule="job_capability", judgment=Judgment.human)
    return get_caps(store, job_id)


# -- ~/.claude.json research server -> per-job mcp.json -------------------------

def claude_json_path() -> Path:
    return Path(os.environ.get("STEWARD_CLAUDE_JSON") or Path.home() / ".claude.json")


def mcp_config_path(job_id: str) -> Path:
    return resolve_home() / "jobs" / job_id / "mcp.json"


def write_mcp_config(job_id: str) -> Path:
    """Write `jobs/<job>/mcp.json` (0600) holding ONLY the `research` server entry."""
    try:
        servers = json.loads(claude_json_path().read_text()).get("mcpServers") or {}
    except (OSError, ValueError) as exc:
        raise ResearchError(f"cannot read the research MCP config: {type(exc).__name__}") from exc
    entry = servers.get(SERVER_NAME)
    if not isinstance(entry, dict):
        raise ResearchError("no `research` MCP server configured in ~/.claude.json")
    path = mcp_config_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"mcpServers": {SERVER_NAME: entry}}, fh)
    os.chmod(path, 0o600)
    return path


def worker_mcp_args(store: Store, job_id: str) -> list[str]:
    if not research_enabled(store, job_id):
        return []
    return ["--mcp-config", str(write_mcp_config(job_id)), "--strict-mcp-config"]


# -- keys -----------------------------------------------------------------------

def keys_file_path() -> Path:
    """One keys file for callers that want a single path: `$STEWARD_API_KEYS_FILE`, else the legacy file."""
    return keys_module.api_keys_file_path() or keys_module.legacy_path()


_parse_line = keys_module.parse_line  # kept for callers that import it from here


def key_names(path: Path | None = None) -> list[str]:
    """Names only (never values): every file of the chain, or exactly `path` when one is given."""
    if path is None:
        return keys_module.key_names()
    return list(keys_module.read_file(path))


def load_keys(names, path: Path | None = None) -> dict[str, str]:
    """`{name: value}` for exactly the requested names (missing ones are omitted). Without `path` each name is
    looked up through the chain (`steward.keys`); an explicit `path` reads exactly that one file."""
    wanted = list(dict.fromkeys(names))
    if not wanted:
        return {}
    if path is None:
        return keys_module.load(wanted)
    found = keys_module.read_file(path)
    return {n: found[n] for n in wanted if found.get(n)}


# -- spend accounting -----------------------------------------------------------

def cost_table() -> dict[str, float]:
    table = dict(COST_TABLE)
    try:
        override = json.loads((resolve_config_dir() / "research_costs.json").read_text())
        table.update({str(k): float(v) for k, v in override.items()})
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return table


def short_tool(tool: str) -> str:
    return tool[len(TOOL_PREFIX):] if tool.startswith(TOOL_PREFIX) else tool


def estimate_usd(tool: str, tool_input: dict | None = None) -> float:
    """Cost the research MCP reports (`cost_usd`/`cost` in the input is never
    populated by callers — results arrive after the permission request), else the table."""
    return cost_table().get(short_tool(tool), DEFAULT_CALL_USD)


def query_of(tool_input) -> str:
    if isinstance(tool_input, dict):
        for key in ("query", "q", "question", "topic", "prompt"):
            if tool_input.get(key):
                return str(tool_input[key])
        return json.dumps(tool_input, ensure_ascii=False)
    return str(tool_input or "")


def state(store: Store, job_id: str) -> dict:
    s = store.kv_get(job_id, _KV_STATE) or {}
    return {"calls": int(s.get("calls", 0)), "spent_usd": float(s.get("spent_usd", 0.0)),
            "tripped": bool(s.get("tripped", False))}


def summary(store: Store, job_id: str) -> dict | None:
    """`{calls, spent_usd, cap_usd, tripped}` for a research-enabled job, else None."""
    caps = get_caps(store, job_id)
    if not caps["research"]:
        return None
    return {**state(store, job_id), "cap_usd": caps["cap_usd"]}


def charge(store: Store, job_id: str, tool: str, tool_input) -> tuple[bool, dict]:
    """Account one research call. Returns `(allowed, info)`; `info["trip"]` is True
    exactly once, on the call that crosses (or first meets an already-crossed) cap."""
    caps = get_caps(store, job_id)
    st = state(store, job_id)
    cap = caps["cap_usd"]
    info = {"tool": short_tool(tool), "cap_usd": cap, "calls": st["calls"], "spent_usd": st["spent_usd"], "trip": False}
    if not caps["research"]:
        info["reason"] = "research is not enabled for this job"
        return False, info
    from steward import ownership, spend_policy

    is_owned = ownership.enabled()
    if st["spent_usd"] >= cap and not is_owned:
        info["reason"] = "research spend cap reached"
        if not st["tripped"]:
            info["trip"] = True
            store.kv_set(job_id, _KV_STATE, {**st, "tripped": True})
        return False, info
    est = estimate_usd(tool, tool_input)
    if is_owned:
        spend_policy.migrate_legacy(store, job_id, now=time.time())
        lim = spend_policy.effective(store, job_id, "research")
        binding = lim.binding if lim else "advisory"
        chk = spend_policy.check_hard(store, job_id, source="research", bound_usd=est, now=time.time())
        if not chk.ok:
            info.update(binding=binding, over=True, trip=not st["tripped"], reason=chk.reason, est_usd=est)
            if not st["tripped"]:
                store.kv_set(job_id, _KV_STATE, {**st, "tripped": True})
            return False, info
        st["calls"] += 1
        st["spent_usd"] = round(st["spent_usd"] + est, 6)
        info.update(calls=st["calls"], spent_usd=st["spent_usd"], est_usd=est, binding=binding,
                    over=bool(lim and st["spent_usd"] > lim.usd), trip=False)
        if binding == "hard" and st["spent_usd"] >= cap and not st["tripped"]:
            info["trip"] = True
            st["tripped"] = True
        store.kv_set(job_id, _KV_STATE, st)
        return True, info
    st["calls"] += 1
    st["spent_usd"] = round(st["spent_usd"] + est, 6)
    info.update(calls=st["calls"], spent_usd=st["spent_usd"], est_usd=est)
    if st["spent_usd"] >= cap and not st["tripped"]:
        st["tripped"] = True
        info["trip"] = True
    store.kv_set(job_id, _KV_STATE, st)
    return True, info


def raise_cap(store: Store, job_id: str, cap_usd: float) -> dict:
    caps = get_caps(store, job_id)
    if not caps["research"]:
        raise ResearchError("research is not enabled for this job")
    if cap_usd <= 0:
        raise ResearchError("research cap must be positive")
    store.kv_set(job_id, _KV_CAPS, {"research": True, "keys": caps["keys"], "cap_usd": float(cap_usd)})
    from steward import spend_policy

    spend_policy.set_limit(store, job_id, "research", float(cap_usd), binding="hard", provenance="owner_explicit",
                           source={"via": "research_cap_raised"}, now=time.time())
    st = state(store, job_id)
    if st["spent_usd"] < cap_usd:
        st["tripped"] = False
    store.kv_set(job_id, _KV_STATE, st)
    return {**st, "cap_usd": float(cap_usd), "previous_cap_usd": caps["cap_usd"]}
