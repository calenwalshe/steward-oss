"""Golden builders for Phase 19-02 (G7): every deterministic text builder that used to name the owner.

`python3 tests/owner_golden.py --write` renders the three builders below under the pinned legacy name and writes
tests/fixtures/owner/{mcp_tools,chat_text,misc_text}.golden.json. The files were captured from the UNMODIFIED
code before the sweep began; the comparison tests in test_owner_text.py then hold the swept code byte-equal under
that name. A failing golden is fixed in the code, never in the file.

Each builder runs in a throwaway STEWARD_HOME/STEWARD_CONFIG_DIR with a fixed job id and a frozen clock; the
temp home and ISO timestamps are normalised out of the output.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

FIXTURES = _HERE / "fixtures" / "owner"
GOLDEN_FILES = {
    "mcp_tools": FIXTURES / "mcp_tools.golden.json",
    "chat_text": FIXTURES / "chat_text.golden.json",
    "misc_text": FIXTURES / "misc_text.golden.json",
}
JOB = "job-fixed-0001"
_TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:\+00:00|Z)?")
_CLOCK = re.compile(r"\b\d\d:\d\d:\d\d\b")
_AGO = re.compile(r"\b\d+s ago\b")

_ENV_KEYS = ("STEWARD_HOME", "STEWARD_CONFIG_DIR", "STEWARD_OWNER_NAME", "STEWARD_HEALTH", "COLUMNS")


@contextlib.contextmanager
def sandbox(name: str | None = "Sam"):
    """Throwaway home + config dir; the owner name pinned to `name` (None leaves the environment alone)."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["STEWARD_HOME"] = str(Path(tmp) / "home")
        os.environ["STEWARD_CONFIG_DIR"] = str(Path(tmp) / "config")
        os.environ["STEWARD_HEALTH"] = "0"
        os.environ["COLUMNS"] = "100"
        if name is not None:
            os.environ["STEWARD_OWNER_NAME"] = name
        try:
            yield Path(tmp)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _norm(obj, tmp: Path):
    """Replace the temp dir and ISO timestamps in every string of a JSON-able structure."""
    if isinstance(obj, str):
        return _AGO.sub("<N>s ago", _CLOCK.sub("<T>", _TS.sub("<TS>", obj.replace(str(tmp), "<TMP>").replace(str(Path(__file__).resolve().parents[1]), "<REPO>"))))
    if isinstance(obj, dict):
        return {k: _norm(v, tmp) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_norm(v, tmp) for v in obj]
    return obj


def _now(store) -> float:
    """A frozen clock one hour after the job was created."""
    from datetime import datetime

    return datetime.fromisoformat(store.get_job(JOB).created_at).timestamp() + 3600


def _store(state: str = "RUNNING"):
    from steward.store import Store

    store = Store()
    job = store.create_job("ship the widget. Done when: tests pass", "/tmp/somerepo", job_id=JOB)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state, JOB))
    store.kv_set(JOB, "worker_model", "sonnet")
    store.kv_set(JOB, "worker_session_id", "sess-1")
    return store


# -- MCP ---------------------------------------------------------------------------------------------------------


def mcp_tools() -> dict:
    from questions_helpers import make_record, seed

    from steward import mcp_server
    from steward.api import StewardAPI
    from steward.runner_exec import FakeRunner

    with sandbox(None) as tmp:
        store = _store(state="NEEDS_HUMAN")
        api = StewardAPI(store, runner_exec=FakeRunner(), relay=None)
        tools = mcp_server.build_tools(api)
        out = {
            "tools": [{"name": t.name, "description": t.description, "input_schema": t.input_schema,
                       "annotations": t.annotations, "required_scope": t.required_scope} for t in tools],
        }
        by_name = {t.name: t for t in tools}
        seed(store, JOB, [make_record("gsd_context:17-CONTEXT.md:G1")])
        out["payloads"] = {
            "get_decision": by_name["get_decision"].handler({}, None),
            "get_questions": by_name["get_questions"].handler({}, None),
        }
        return _norm(json.loads(json.dumps(out, sort_keys=True, default=str)), tmp)


# -- chat --------------------------------------------------------------------------------------------------------


def chat_text() -> dict:
    from questions_helpers import make_record, seed

    from steward import approvals as approvals_module
    from steward import chat, custom_watchers

    with sandbox(None) as tmp:
        store = _store(state="NEEDS_HUMAN")
        seed(store, JOB, [make_record("gsd_context:17-CONTEXT.md:G1", blocking=True),
                          make_record("gsd_context:17-CONTEXT.md:G2", starred=False, recommended=())])
        approvals_module.open_approval(store, JOB, "worker permission: Bash: git status", approval_id="appr-1",
                                       kind="permission")
        custom_watchers.add_watcher(store, JOB, custom_watchers.make_spec("tests", "are tests green?"))
        out: dict = {
            "allowed_tools": chat.job_allowed_tools("JOB123"),
            "primer_astra": chat.build_primer(store, JOB, "astra", now=_now(store)),
            "primer_sol": chat.build_primer(store, JOB, "sol", now=_now(store)),
            "primer_why": chat.build_why_primer(store, JOB),
            "primer_job": chat.build_job_primer(store, JOB, projects_dir=tmp / "projects", now=_now(store)),
            "questions_block": chat._questions_block(store, JOB),
            "watcher_primer": custom_watchers.build_chat_primer(store, JOB, custom_watchers.list_watchers(store, JOB)[0], _now(store)),
            "commands": {},
        }
        for label, kwargs in (("worker", {"role": "worker"}), ("astra", {"role": "astra"}), ("sol", {"role": "sol"}),
                              ("why", {"role": "why"}), ("job", {"role": "job"}),
                              ("job_send_back", {"role": "job", "send_back": True}), ("watcher", {"role": "tests"})):
            plan = chat.build_chat(store, JOB, projects_dir=tmp / "projects", now=_now(store), **kwargs)
            out["commands"][label] = {"role": plan.role, "model": plan.model, "command": plan.command}
        return _norm(out, tmp)


# -- everything else ---------------------------------------------------------------------------------------------


def _help(argv: list[str]) -> str:
    from steward import cli

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
        cli.main(argv)
    return buf.getvalue()


def misc_text() -> dict:
    from steward import alerts, closeout, contract, custom_watchers, efficiency_detect, efficiency_review  # noqa: F401
    from steward import approvals as approvals_module
    from steward import jobview, resolver, session
    from steward.discovery import Discovery

    with sandbox(None) as tmp:
        store = _store(state="CREATED")
        c = contract.propose_contract("Make it reliable. Don't touch the db.", Discovery(commands=[], instruction_files=[], languages=[]), None)
        contract.store_contract(store, JOB, c)
        with store._conn:
            store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", ("NEEDS_HUMAN", JOB))
        approvals_module.open_approval(store, JOB, approvals_module.ACCEPTANCE_QUESTION, kind="acceptance")
        events = jobview.event_dicts(store.events(JOB))
        approvals = approvals_module.list_approvals(store, JOB)
        cfg = dict(efficiency_detect.DEFAULTS)
        findings = [efficiency_detect._finding(cfg, "idle_wait", "s", "ev", ("idle_s", 1), rid)
                    for rid in ("escalate_idle", "escalate_projection", "escalate_remove_watcher")]
        metrics = {"totals": {"usd": 1.0, "calls": 2, "peak_ctx": 3}, "stages": {}, "units": []}
        result = session.build_result(store, JOB, projects_dir=tmp / "projects")
        w = custom_watchers.add_watcher(store, JOB, custom_watchers.make_spec("tests", "are tests green?"))
        hold = {"tool": "Bash", "input": {"command": "git push"}, "reason": "outbound"}
        out = {
            "contract_boundaries": c.boundaries,
            "contract_render": contract.render(c),
            "jobview_reason": jobview.reason(approvals, "NEEDS_HUMAN", events),
            "efficiency_findings": [{k: f[k] for k in ("remedy", "remedy_text", "remedy_short", "actions")} for f in findings],
            "efficiency_prompt": efficiency_review.build_prompt(store, JOB, metrics, findings, 1),
            "resolver_brief": resolver.build_brief(store, approvals[0], "floor", "outbound contact", hold, tmp / "wt",
                                                   activity_fn=lambda s, j: {"turns": []}),
            "watcher_prompt": custom_watchers.build_prompt(store, JOB, w, _now(store)),
            "result_keys": sorted(result),
            "result_render": session.render_result(result),
            "help_main": _help(["--help"]),
            "help_watcher": _help(["watcher", "--help"]),
        }
        return _norm(out, tmp)


BUILDERS = {"mcp_tools": mcp_tools, "chat_text": chat_text, "misc_text": misc_text}


def render(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    if "--write" not in argv:
        print("usage: python3 tests/owner_golden.py --write", file=sys.stderr)
        return 2
    import hashlib

    FIXTURES.mkdir(parents=True, exist_ok=True)
    for key, build in BUILDERS.items():
        os.environ["STEWARD_OWNER_NAME"] = "Sam"  # the pin the suite runs under
        text = render(build())
        GOLDEN_FILES[key].write_text(text)
        print(key, hashlib.sha256(text.encode()).hexdigest(), GOLDEN_FILES[key])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
