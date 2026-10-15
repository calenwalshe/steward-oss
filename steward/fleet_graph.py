"""Explicit task manifests and deterministic graph compilation (Phase 26, FLEET-04).

Only a committed, plan-identity-bound, schema-valid manifest opts a job into
multiple tasks. Prose-only, absent, malformed or stale metadata compiles to one
implicit task / one lane with a visible reason. Nothing here executes work.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

MANIFEST_VERSION = 1
IMPLICIT_ID = "implicit"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TASK_FIELDS = {"id", "objective", "done_when", "depends_on", "owns", "evidence", "required", "priority"}
_TOP_FIELDS = {"schema_version", "plan_blob_id", "tasks"}
_BLOB = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


@dataclass(frozen=True)
class Task:
    id: str
    objective: str
    done_when: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    owns: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    required: bool = True
    priority: int = 0
    parent: str | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "objective": self.objective, "done_when": list(self.done_when),
                "depends_on": list(self.depends_on), "owns": list(self.owns),
                "evidence": list(self.evidence), "required": self.required, "priority": self.priority}


@dataclass
class Graph:
    tasks: list[Task]
    plan_blob_id: str | None = None
    implicit: bool = False
    fallback_reason: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def graph_hash(self) -> str:
        body = {"plan_blob_id": self.plan_blob_id, "implicit": self.implicit,
                "tasks": [t.to_dict() for t in sorted(self.tasks, key=lambda t: t.id)]}
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def ready_tasks(self, done=frozenset(), active=frozenset()) -> list[Task]:
        """Tasks that may start now: dependencies done, not running, and claim-compatible with every
        active task and with each other (deterministic: priority, then id). An invalid graph has none."""
        if self.errors:
            return []
        by_id = {t.id: t for t in self.tasks}
        held = [by_id[a].owns for a in active if a in by_id]
        chosen: list[Task] = []
        for t in self.tasks:
            if t.id in done or t.id in active or not set(t.depends_on) <= set(done):
                continue
            if any(claims_conflict(t.owns, o) for o in held):
                continue
            chosen.append(t)
            held.append(t.owns)
        return chosen


class _Malformed(ValueError):
    pass


_GLOBAL_PARTS = frozenset({"migrations", "generated", "__generated__", "dist", "build", ".github"})
_GLOBAL_FILES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "package.json", "package-lock.json",
    "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
    "Makefile", "conftest.py",
})
_GLOB = re.compile(r"[*?\[]")


def _literal(claim: str) -> tuple[str, ...]:
    """Directory-literal segments of a claim: segments before the first glob."""
    out = []
    for seg in claim.split("/"):
        if _GLOB.search(seg):
            break
        out.append(seg)
    return tuple(out)


def is_global_claim(claim: str) -> bool:
    lit = _literal(claim)
    if not lit or lit == (".",):
        return True
    return bool(set(lit) & _GLOBAL_PARTS) or lit[-1] in _GLOBAL_FILES


def claims_conflict(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """True if two tasks must not write concurrently: empty ownership, global/generated/cross-cutting
    claims, or any overlapping path/directory (a glob counts as its literal directory prefix)."""
    if not a or not b:
        return True
    if any(is_global_claim(c) for c in a + b):
        return True
    for x in a:
        lx = _literal(x)
        for y in b:
            ly = _literal(y)
            n = min(len(lx), len(ly))
            if lx[:n] == ly[:n]:
                return True
    return False


def validate_graph(tasks: list[Task], prior: "Graph | None" = None) -> list[str]:
    errors: list[str] = []
    ids = [t.id for t in tasks]
    for i in sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(f"duplicate task id {i}")
    known = set(ids)
    for t in tasks:
        for d in t.depends_on:
            if d == t.id:
                errors.append(f"self dependency {t.id}")
            elif d not in known:
                errors.append(f"unknown dependency {d} for {t.id}")
    if not errors:
        graph = {t.id: set(t.depends_on) for t in tasks}
        state: dict[str, int] = {}

        def visit(n, path):
            state[n] = 1
            for m in sorted(graph[n]):
                if state.get(m) == 1:
                    errors.append("dependency cycle: " + " -> ".join(path + [n, m]))
                elif m not in state:
                    visit(m, path + [n])
            state[n] = 2

        for n in sorted(graph):
            if n not in state:
                visit(n, [])
    if prior is not None and not prior.implicit:
        now = {t.id: t for t in tasks}
        for t in prior.tasks:
            if t.required and (t.id not in now or not now[t.id].required):
                errors.append(f"required task {t.id} dropped or reclassified")
    return errors


def implicit_graph(goal: str, reason: str) -> Graph:
    t = Task(id=IMPLICIT_ID, objective=goal or "(job goal)", done_when=("parent finish line met",),
             required=True, priority=0)
    return Graph(tasks=[t], implicit=True, fallback_reason=reason)


def normalize_claim(p) -> str:
    if not isinstance(p, str) or not p.strip():
        raise _Malformed("empty ownership path")
    if "\\" in p or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise _Malformed(f"ownership path not repository-relative: {p}")
    n = str(PurePosixPath(p.strip()))
    parts = PurePosixPath(n).parts
    if ".." in parts:
        raise _Malformed(f"ownership path escapes repository: {p}")
    return n


def _str_list(v, what) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise _Malformed(f"{what} must be a list of non-empty strings")
    return tuple(x.strip() for x in v)


def _parse_task(t) -> Task:
    if not isinstance(t, dict):
        raise _Malformed("task is not a mapping")
    if set(t) - _TASK_FIELDS:
        raise _Malformed(f"unknown task field {sorted(set(t) - _TASK_FIELDS)}")
    tid = t.get("id")
    if not isinstance(tid, str) or not _ID.match(tid):
        raise _Malformed(f"bad task id {tid!r}")
    obj = t.get("objective")
    if not isinstance(obj, str) or not obj.strip():
        raise _Malformed(f"task {tid}: objective required")
    done = _str_list(t.get("done_when"), f"task {tid}: done_when")
    if not done:
        raise _Malformed(f"task {tid}: done_when required")
    req = t.get("required", True)
    pri = t.get("priority", 0)
    if not isinstance(req, bool) or not isinstance(pri, int) or isinstance(pri, bool):
        raise _Malformed(f"task {tid}: bad required/priority")
    owns = tuple(sorted({normalize_claim(p) for p in _str_list(t.get("owns", []), f"task {tid}: owns")}))
    return Task(id=tid, objective=obj.strip(), done_when=done,
                depends_on=tuple(sorted(set(_str_list(t.get("depends_on", []), f"task {tid}: depends_on")))),
                owns=owns, evidence=_str_list(t.get("evidence", []), f"task {tid}: evidence"),
                required=req, priority=pri)


def compile_graph(manifest, *, committed_plan_blob_id: str | None, goal: str = "", prior: Graph | None = None) -> Graph:
    """Compile manifest data (a dict, or None/other for 'no usable manifest')."""
    if manifest is None:
        return implicit_graph(goal, "absent: no task manifest; one implicit task")
    try:
        if not isinstance(manifest, dict):
            raise _Malformed("manifest is not a mapping")
        if set(manifest) - _TOP_FIELDS:
            raise _Malformed(f"unknown manifest field {sorted(set(manifest) - _TOP_FIELDS)}")
        if manifest.get("schema_version") != MANIFEST_VERSION or isinstance(manifest.get("schema_version"), bool):
            raise _Malformed(f"unsupported schema_version {manifest.get('schema_version')!r}")
        blob = manifest.get("plan_blob_id")
        if not isinstance(blob, str) or not _BLOB.match(blob):
            raise _Malformed("plan_blob_id missing or not a git blob id")
        raw = manifest.get("tasks")
        if not isinstance(raw, list) or not raw:
            raise _Malformed("tasks must be a non-empty list")
        tasks = [_parse_task(t) for t in raw]
    except _Malformed as exc:
        return implicit_graph(goal, f"malformed: {exc}; one implicit task")
    if committed_plan_blob_id is None or blob != committed_plan_blob_id:
        return implicit_graph(goal, f"stale: manifest plan_blob_id {blob[:12]} does not match committed plan "
                                    f"{(committed_plan_blob_id or 'none')[:12]}; one implicit task")
    tasks.sort(key=lambda t: (t.priority, t.id))
    return Graph(tasks=tasks, plan_blob_id=blob, errors=validate_graph(tasks, prior))


def load_manifest(path: Path | str):
    """Best-effort read of a companion manifest (.json or .toml). Unreadable -> None-like marker string."""
    p = Path(path)
    try:
        text = p.read_text()
    except OSError:
        return None
    try:
        return tomllib.loads(text) if p.suffix == ".toml" else json.loads(text)
    except (ValueError, tomllib.TOMLDecodeError):
        return "unparseable manifest"


def committed_blob_id(repo: str, plan_path: str, runner: Callable | None = None) -> str | None:
    """Git blob id of `plan_path` at HEAD (the committed source plan), via an injectable runner."""
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False))
    try:
        r = run(["git", "-C", repo, "rev-parse", f"HEAD:{plan_path}"])
    except (OSError, subprocess.SubprocessError):
        return None
    out = (getattr(r, "stdout", "") or "").strip()
    return out if getattr(r, "returncode", 1) == 0 and _BLOB.match(out) else None
