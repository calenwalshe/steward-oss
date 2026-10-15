"""Revisioned operational grants with provenance (continuous job ownership, Plan 2; spec §6; audit RC-1/F-1).

A grant is the owner's recorded permission for one operation over one scope: `write_path` (paths outside the job
worktree the worker may write), `run_command` (command prefixes the worker may run although the default policy holds
them), `remote_read` (read-only diagnostics on a named host alias, optionally restricted to services/paths), `key`
(key names put in the worker's environment), `delivery` (incorporation/activation the owner already authorized; read
by Plan 4, never acceptance). A `prohibit` row uses the same grammar and always wins over an allow.

Every change bumps the job's authority revision (`job_authority.rev`) inside the same transaction and appends an
insert-only `authority_history` row, so a decision bound to an older revision can be revalidated (`authority`).
Only the owner, an owner relay (operator agent with a human origin) or the controller migrating a *recorded owner
source* may change authority; the tech lead, workers, Jev and model supervisors never can. Source excerpts are
sanitized and bounded. Scopes are normalised and refuse shell operators, relative paths and whole-filesystem
patterns, so a grant can never become a generic shell bypass.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import asdict, dataclass, field

from steward.actor import Actor, ActorRef
from steward.sanitize import text as redact
from steward.models import Judgment
from steward.store import Store

OPERATIONS = ("write_path", "run_command", "remote_read", "key", "delivery")
KINDS = ("allow", "prohibit")
SOURCE_KINDS = ("cli_flag", "grant_verb", "goal_text", "owner_steer", "owner_answer", "legacy_capabilities")
MIGRATION_SOURCES = frozenset({"goal_text", "owner_steer", "owner_answer", "legacy_capabilities"})
CHANGES = ("grant_added", "grant_revoked", "contract_changed", "stopped", "migrated")
MAX_GRANTS_PER_JOB = 200
SCOPE_MAX_ITEMS = 20
SCOPE_ITEM_MAX_CHARS = 300
SOURCE_EXCERPT_MAX = 300
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_SHELL_OPS = ("$(", "`", ">", "<", "|", ";", "&", "\n")
_WHOLE_FS = frozenset({"/", "/*", "/**", "/**/*"})


class GrantError(ValueError):
    """A refused authority change (wrong actor, malformed scope, already revoked, too many grants)."""


@dataclass(frozen=True)
class Source:
    kind: str
    ref: str
    excerpt: str = ""
    digest: str = ""
    relayed_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Grant:
    grant_id: str
    job_id: str
    kind: str
    operation: str
    scope: dict
    conditions: dict
    source: Source
    granted_by: str
    created_rev: int
    created_epoch: float
    revoked_rev: int | None = None
    revoked_by: str | None = None
    revoked_epoch: float | None = None
    revoke_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_rev is None

    def live(self, now: float) -> bool:
        until = self.conditions.get("until_epoch")
        return self.active and (until is None or now < float(until))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source"] = self.source.to_dict()
        return d


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _items(operation: str, scope: dict, key: str, *, required: bool = True) -> list[str]:
    raw = scope.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise GrantError(f"{operation}.{key} must be a list")
    vals = [" ".join(str(v).split()) for v in raw if str(v).strip()]
    if required and not vals:
        raise GrantError(f"{operation} needs {key}")
    if len(vals) > SCOPE_MAX_ITEMS or any(len(v) > SCOPE_ITEM_MAX_CHARS for v in vals):
        raise GrantError(f"{operation}.{key}: at most {SCOPE_MAX_ITEMS} items of {SCOPE_ITEM_MAX_CHARS} chars")
    return sorted(dict.fromkeys(vals))


def _norm_path(p: str) -> str:
    if not p.startswith("/"):
        raise GrantError(f"grant paths are absolute: {p!r}")
    if p in _WHOLE_FS:
        raise GrantError("a grant cannot cover the whole filesystem")
    if any(c in p for c in "*?["):
        return p
    return posixpath.normpath(p) + ("/" if p.endswith("/") else "")


def _host(value, what: str) -> str:
    v = str(value or "").strip()
    if not _HOST_RE.fullmatch(v) or v == "local":
        raise GrantError(f"{what} must be a host alias (letters, digits, . _ -), not {v!r}")
    return v


def normalize_scope(operation: str, scope: dict) -> dict:
    if operation not in OPERATIONS:
        raise GrantError(f"unknown operation {operation!r}; one of {OPERATIONS}")
    if not isinstance(scope, dict):
        raise GrantError("scope must be an object")
    if operation == "write_path":
        return {"paths": sorted(dict.fromkeys(_norm_path(p) for p in _items(operation, scope, "paths")))}
    if operation == "run_command":
        cmds = _items(operation, scope, "commands")
        for c in cmds:
            if any(op in c for op in _SHELL_OPS):
                raise GrantError(f"a command grant is a plain command prefix (no shell operators): {c!r}")
        return {"commands": cmds}
    if operation == "remote_read":
        out = {"host": _host(scope.get("host"), "remote_read.host"),
               "ssh": _host(scope.get("ssh") or scope.get("host"), "remote_read.ssh")}
        services = _items(operation, scope, "services", required=False)
        paths = [_norm_path(p) for p in _items(operation, scope, "paths", required=False)]
        if services:
            out["services"] = services
        if paths:
            out["paths"] = sorted(dict.fromkeys(paths))
        return out
    if operation == "key":
        names = _items(operation, scope, "names")
        bad = [n for n in names if not _KEY_RE.fullmatch(n)]
        if bad:
            raise GrantError(f"key names are upper-case identifiers: {bad}")
        return {"names": names}
    milestone = scope.get("milestone")
    if milestone not in ("incorporate", "activate"):
        raise GrantError("delivery.milestone is incorporate or activate (acceptance stays the owner's)")
    target = " ".join(str(scope.get("target") or "").split())
    if not target or len(target) > SCOPE_ITEM_MAX_CHARS or any(op in target for op in _SHELL_OPS):
        raise GrantError("delivery.target must name a branch or service")
    return {"milestone": milestone, "target": target}


def parse_spec(text: str) -> tuple[str, dict]:
    """`OP:VALUE` as typed on the CLI: `write_path:/etc/caddy/Caddyfile`, `run_command:sudo systemctl reload caddy`,
    `remote_read:HOST[=SSH_ALIAS][;services=a,b][;paths=/p,/q]`, `key:NAME`, `delivery:incorporate=BRANCH`."""
    op, sep, value = str(text).partition(":")
    op, value = op.strip(), value.strip()
    if not sep or not value:
        raise GrantError(f"grant spec is OP:VALUE, got {text!r}")
    if op == "write_path":
        return op, normalize_scope(op, {"paths": [value]})
    if op == "run_command":
        return op, normalize_scope(op, {"commands": [value]})
    if op == "key":
        return op, normalize_scope(op, {"names": [value]})
    if op == "remote_read":
        head, *opts = [p.strip() for p in value.split(";")]
        host, _, alias = head.partition("=")
        scope: dict = {"host": host, "ssh": alias or host}
        for o in opts:
            k, _, v = o.partition("=")
            if k not in ("services", "paths"):
                raise GrantError(f"remote_read option {k!r} (services|paths)")
            scope[k] = [x for x in v.split(",") if x.strip()]
        return op, normalize_scope(op, scope)
    if op == "delivery":
        milestone, _, target = value.partition("=")
        return op, normalize_scope(op, {"milestone": milestone.strip(), "target": target})
    raise GrantError(f"unknown operation {op!r}; one of {OPERATIONS}")


def _conditions(conditions: dict | None) -> dict:
    conditions = dict(conditions or {})
    if set(conditions) - {"until_epoch"}:
        raise GrantError("the only grant condition is until_epoch")
    if "until_epoch" in conditions:
        conditions["until_epoch"] = float(conditions["until_epoch"])
    return conditions


def _rights(actor: ActorRef, source: Source) -> None:
    if source.kind not in SOURCE_KINDS:
        raise GrantError(f"unknown source kind {source.kind!r}")
    if actor.kind in (Actor.OWNER, Actor.OPERATOR_AGENT):
        return
    if actor.kind == Actor.CONTROLLER and source.kind in MIGRATION_SOURCES and str(source.ref).strip():
        return
    raise GrantError(f"{actor.label()} cannot change job authority: only the owner (or a migration of the owner's "
                     "recorded words) can (spec §6)")


def _judgment(actor: ActorRef) -> Judgment:
    return Judgment.human if actor.kind in (Actor.OWNER, Actor.OPERATOR_AGENT) else Judgment.code


def _bump(tx, job_id: str, now: float, actor: ActorRef, change: str, grant_id: str | None, detail: dict) -> int:
    row = tx.conn.execute("SELECT rev FROM job_authority WHERE job_id = ?", (job_id,)).fetchone()
    rev = (int(row["rev"]) if row else 0) + 1
    tx.conn.execute("INSERT INTO job_authority (job_id, rev, updated_epoch) VALUES (?, ?, ?) ON CONFLICT(job_id) "
                    "DO UPDATE SET rev = excluded.rev, updated_epoch = excluded.updated_epoch", (job_id, rev, now))
    tx.conn.execute("INSERT INTO authority_history (job_id, rev, at_epoch, actor, change, grant_id, detail_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (job_id, rev, now, actor.label(), change, grant_id,
                                                     _canon(detail)))
    return rev


def _row(r) -> Grant:
    return Grant(grant_id=r["grant_id"], job_id=r["job_id"], kind=r["kind"], operation=r["operation"],
                 scope=json.loads(r["scope_json"]), conditions=json.loads(r["conditions_json"]),
                 source=Source(**json.loads(r["source_json"])), granted_by=r["granted_by"],
                 created_rev=int(r["created_rev"]), created_epoch=float(r["created_epoch"]),
                 revoked_rev=r["revoked_rev"], revoked_by=r["revoked_by"], revoked_epoch=r["revoked_epoch"],
                 revoke_reason=r["revoke_reason"])


def current_rev(store: Store, job_id: str) -> int:
    row = store._conn.execute("SELECT rev FROM job_authority WHERE job_id = ?", (job_id,)).fetchone()
    return int(row["rev"]) if row else 0


def add(store: Store, job_id: str, *, operation: str, scope: dict, source: Source, granted_by: ActorRef, now: float,
        kind: str = "allow", conditions: dict | None = None) -> tuple[Grant, bool]:
    if kind not in KINDS:
        raise GrantError(f"grant kind is allow or prohibit, not {kind!r}")
    _rights(granted_by, source)
    scope = normalize_scope(operation, scope)
    conditions = _conditions(conditions)
    excerpt = redact(" ".join(str(source.excerpt or "").split()))[:SOURCE_EXCERPT_MAX]
    src = Source(kind=source.kind, ref=redact(str(source.ref))[:200], excerpt=excerpt,
                 digest=source.digest or _digest(f"{source.kind}|{source.ref}|{excerpt}"),
                 relayed_by=str(source.relayed_by or "")[:80])
    gid = "grt-" + hashlib.sha256(_canon([job_id, kind, operation, scope, src.digest]).encode()).hexdigest()[:12]
    with store.immediate() as tx:
        existing = tx.conn.execute("SELECT * FROM authority_grants WHERE grant_id = ?", (gid,)).fetchone()
        if existing is not None and existing["revoked_rev"] is None:
            return _row(existing), False
        if existing is not None:
            raise GrantError(f"grant {gid} was revoked; record a new owner source to grant it again")
        active = tx.conn.execute("SELECT COUNT(*) AS n FROM authority_grants WHERE job_id = ? AND revoked_rev IS NULL",
                                 (job_id,)).fetchone()["n"]
        if active >= MAX_GRANTS_PER_JOB:
            raise GrantError(f"job already has {active} active grants (max {MAX_GRANTS_PER_JOB}); revoke some first")
        rev = _bump(tx, job_id, now, granted_by, "grant_added", gid, {"kind": kind, "operation": operation})
        tx.conn.execute(
            "INSERT INTO authority_grants (grant_id, job_id, kind, operation, scope_json, conditions_json, source_json,"
            " granted_by, created_rev, created_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, job_id, kind, operation, _canon(scope), _canon(conditions), _canon(src.to_dict()),
             granted_by.label(), rev, now))
        tx.append_event(job_id, what="authority_grant_added",
                        evidence={"grant_id": gid, "rev": rev, "kind": kind, "operation": operation, "scope": scope,
                                  "source": {"kind": src.kind, "ref": src.ref}, "by": granted_by.label()},
                        rule="job_authority", judgment=_judgment(granted_by))
        row = tx.conn.execute("SELECT * FROM authority_grants WHERE grant_id = ?", (gid,)).fetchone()
    return _row(row), True


def revoke(store: Store, grant_id: str, *, by: ActorRef, reason: str, now: float) -> Grant:
    if by.kind not in (Actor.OWNER, Actor.OPERATOR_AGENT):
        raise GrantError(f"{by.label()} cannot revoke job authority (owner-only; spec §6)")
    with store.immediate() as tx:
        row = tx.conn.execute("SELECT * FROM authority_grants WHERE grant_id = ?", (grant_id,)).fetchone()
        if row is None:
            raise GrantError(f"unknown grant {grant_id}")
        if row["revoked_rev"] is not None:
            raise GrantError(f"grant {grant_id} is already revoked")
        rev = _bump(tx, row["job_id"], now, by, "grant_revoked", grant_id, {"reason": redact(reason)[:200]})
        tx.conn.execute("UPDATE authority_grants SET revoked_rev = ?, revoked_by = ?, revoked_epoch = ?,"
                        " revoke_reason = ? WHERE grant_id = ?", (rev, by.label(), now, redact(reason)[:200], grant_id))
        tx.append_event(row["job_id"], what="authority_grant_revoked",
                        evidence={"grant_id": grant_id, "rev": rev, "reason": redact(reason)[:200], "by": by.label()},
                        rule="job_authority", judgment=_judgment(by))
        row = tx.conn.execute("SELECT * FROM authority_grants WHERE grant_id = ?", (grant_id,)).fetchone()
    return _row(row)


def bump(store: Store, job_id: str, *, change: str, actor: ActorRef, now: float, detail: dict | None = None) -> int:
    """A non-grant authority change (contract boundaries/authority rewritten, job stopped, migration finished)."""
    if change not in CHANGES or change in ("grant_added", "grant_revoked"):
        raise GrantError(f"bump change is one of contract_changed|stopped|migrated, not {change!r}")
    with store.immediate() as tx:
        return _bump(tx, job_id, now, actor, change, None, detail or {})


def get(store: Store, grant_id: str) -> Grant | None:
    r = store._conn.execute("SELECT * FROM authority_grants WHERE grant_id = ?", (grant_id,)).fetchone()
    return None if r is None else _row(r)


def for_job(store: Store, job_id: str, *, include_revoked: bool = False) -> list[Grant]:
    q = "SELECT * FROM authority_grants WHERE job_id = ?" + ("" if include_revoked else " AND revoked_rev IS NULL")
    return [_row(r) for r in store._conn.execute(q + " ORDER BY created_rev, rowid", (job_id,)).fetchall()]


def history(store: Store, job_id: str) -> list[dict]:
    rows = store._conn.execute("SELECT * FROM authority_history WHERE job_id = ? ORDER BY seq", (job_id,)).fetchall()
    return [{"seq": r["seq"], "rev": r["rev"], "at": r["at_epoch"], "actor": r["actor"], "change": r["change"],
             "grant_id": r["grant_id"], "detail": json.loads(r["detail_json"])} for r in rows]


def effective(store: Store, job_id: str):
    """`authority.effective` under the name Plan 4's `delivery._grant` imports."""
    from steward import authority

    return authority.effective(store, job_id)
