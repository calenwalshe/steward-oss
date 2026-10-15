"""The single effective-authority function (continuous job ownership Plan 2; spec §6; audit RC-2/F-2).

Every decider — the runner's permission seam, worker preauthorization, the fleet lane relay, the resolver, the
"why held" text, the watcher signal, the tech-lead packet and action gate, and Plans 3–4 — asks this module what the
job may do. It combines the contract's AUTHORITY/BOUNDARIES, the job's live grants and prohibitions (`grants`), its
granted keys and its state. Precedence, always in this order: a stopped job allows nothing grant-based; a prohibition
covering any piece of the call denies; a call the default policy (`permissions.decide`) allows is allowed; otherwise
every default-held piece (each held simple command and each outside-worktree write target) must be covered by a live
allow grant, else the call is held exactly as before. Credential reads and unverifiable targets are never covered.

`authority_rev` is the revision decisions bind to: grant revision × 1 000 000 + the job's steering/contract revision,
monotonic in both. `revalidate` runs immediately before an action executes: unchanged ⇒ the check; changed ⇒ the
check again on current authority, refusing with `authority_changed` when it no longer passes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from steward import grants, permissions
from steward.grants import Grant
from steward.models import JobState
from steward.store import Store

FLOOR_SCOPE = ("The floor binds YOUR actions as tech lead, not the worker's. The worker's limits are BOUNDARIES and "
               "PROHIBITIONS; its permissions are AUTHORITY plus GRANTS (owner-recorded, revisioned). A pending ask for "
               "a granted operation is yours to clear with `unblock`; to check a claim on a granted host use "
               "`diagnose` (read-only). Steward revalidates authority before anything you propose executes.")
PACKET_GRANTS_MAX = 40
RESOLVER_GRANT_LINES_MAX = 40
EXCERPTS_MAX = 3
EXCERPT_MAX_CHARS = 400
REV_STRIDE = 1_000_000
STOPPED_STATES = (JobState.STOPPING, JobState.STOPPED, JobState.RELEASED, JobState.FAILED)


@dataclass(frozen=True)
class EffectiveAuthority:
    job_id: str
    rev: int
    grants_rev: int
    state: str
    stopped: bool
    authority: tuple[str, ...]
    boundaries: tuple[str, ...]
    grants: tuple[Grant, ...]
    prohibitions: tuple[Grant, ...]
    keys: tuple[str, ...]
    excerpts: tuple[str, ...]
    digest: str

    @property
    def revision(self) -> int:  # Plan 4's name
        return self.rev

    def permits(self, operation: str, resource: str) -> "GrantDecision":
        """Plan 4's lookup: `incorporate`/`activate` on a resource (`<repo>#<branch>` or a target name) map to
        `delivery` grants (matched on the branch part or the whole resource); grant operations map directly."""
        if operation in ("incorporate", "activate"):
            op = "delivery"
            targets = [{"milestone": operation, "target": t}
                       for t in dict.fromkeys([resource, resource.split("#", 1)[-1]])]
        else:
            key = {"write_path": "path", "run_command": "command", "remote_read": "host", "key": "name"}.get(operation)
            if key is None:
                return GrantDecision(False, False, "", f"unknown operation {operation!r}")
            op, targets = operation, [{key: resource}]
        if self.stopped:
            return GrantDecision(False, False, "job_state", f"job is {self.state}")
        for t in targets:
            p = prohibiting_grant(self, op, t)
            if p:
                return GrantDecision(False, True, f"{p.grant_id} ({p.source.kind})", f"owner prohibition {p.grant_id}")
        for t in targets:
            g = covering_grant(self, op, t)
            if g:
                return GrantDecision(True, False, f"{g.grant_id} ({g.source.kind})", f"owner grant {g.grant_id}")
        return GrantDecision(False, False, "", f"no owner grant for {operation} {resource}")


@dataclass(frozen=True)
class GrantDecision:
    allowed: bool
    prohibited: bool
    source: str
    reason: str


@dataclass(frozen=True)
class Verdict:
    allow: bool
    basis: str
    reason: str
    rev: int
    grant_id: str | None = None

    def to_dict(self) -> dict:
        return {"allow": self.allow, "basis": self.basis, "reason": self.reason[:300], "rev": self.rev,
                "grant_id": self.grant_id}


def _ownership_on() -> bool:
    try:
        from steward import ownership

        return ownership.enabled()
    except Exception:  # noqa: BLE001 - without Plan 1 there is no ownership layer: legacy behaviour
        return False


def authority_rev(store: Store, job_id: str) -> int:
    return grants.current_rev(store, job_id) * REV_STRIDE + int(store.get_revision(job_id))


def effective(store: Store, job_id: str, *, now: float | None = None) -> EffectiveAuthority:
    from steward import research
    from steward.contract import load_contract

    now = time.time() if now is None else now
    job = store.get_job(job_id)
    contract = load_contract(store, job_id)
    live = [g for g in grants.for_job(store, job_id) if g.live(now)]
    allow = tuple(g for g in live if g.kind == "allow")
    prohibit = tuple(g for g in live if g.kind == "prohibit")
    try:
        cap_keys = research.granted_keys(store, job_id)
    except Exception:  # noqa: BLE001 - unreadable capabilities mean none recorded there
        cap_keys = []
    keys = tuple(sorted(set(getattr(contract, "keys", None) or []) | set(cap_keys)
                        | {n for g in allow if g.operation == "key" for n in g.scope["names"]}))
    excerpts = tuple(dict.fromkeys(g.source.excerpt[:EXCERPT_MAX_CHARS] for g in (*allow, *prohibit)
                                   if g.source.excerpt))[:EXCERPTS_MAX]
    state = job.state.value if job is not None else "?"
    stopped = job is not None and job.state in STOPPED_STATES
    a = tuple(contract.authority) if contract else ()
    b = tuple(contract.boundaries) if contract else ()
    rev = authority_rev(store, job_id)
    digest = hashlib.sha256(json.dumps([state, stopped, a, b, [g.grant_id for g in allow],
                                        [g.grant_id for g in prohibit], keys]).encode()).hexdigest()[:16]
    return EffectiveAuthority(job_id=job_id, rev=rev, grants_rev=grants.current_rev(store, job_id), state=state,
                              stopped=stopped, authority=a, boundaries=b, grants=allow, prohibitions=prohibit,
                              keys=keys, excerpts=excerpts, digest=digest)


# -- matching ------------------------------------------------------------------------------------------------------

def _path_match(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path == pattern.rstrip("/") or path.startswith(pattern)
    return path == pattern or fnmatch.fnmatchcase(path, pattern)


def _prefix(head: str, prefix: str) -> bool:
    return head == prefix or head.startswith(prefix + " ")


def _subsequence(head: str, needle: str) -> bool:
    words, it = head.split(), iter(head.split())
    return bool(words) and all(any(w == n for w in it) for n in needle.split())


def _matches(g: Grant, operation: str, target: dict, *, prohibit: bool) -> bool:
    if g.operation != operation:
        return False
    if operation == "write_path":
        return any(_path_match(str(target.get("path", "")), p) for p in g.scope["paths"])
    if operation == "run_command":
        head = str(target.get("command", ""))
        return any(_prefix(head, c) or (prohibit and _subsequence(head, c)) for c in g.scope["commands"])
    if operation == "remote_read":
        return str(target.get("host", "")) in (g.scope["host"], g.scope["ssh"])
    if operation == "key":
        return str(target.get("name", "")) in g.scope["names"]
    if operation == "delivery":
        return (g.scope["milestone"] == target.get("milestone")
                and fnmatch.fnmatchcase(str(target.get("target", "")), g.scope["target"]))
    return False


def covering_grant(auth: EffectiveAuthority, operation: str, target: dict, *, now: float | None = None) -> Grant | None:
    now = time.time() if now is None else now
    return next((g for g in auth.grants if g.live(now) and _matches(g, operation, target, prohibit=False)), None)


def prohibiting_grant(auth: EffectiveAuthority, operation: str, target: dict, *,
                      now: float | None = None) -> Grant | None:
    now = time.time() if now is None else now
    return next((g for g in auth.prohibitions if g.live(now) and _matches(g, operation, target, prohibit=True)), None)


# -- worker decisions ----------------------------------------------------------------------------------------------

_SSH_SAFE_OPTS = {"-q", "-T", "-n"}
_SSH_SAFE_O = ("BatchMode=yes", "ConnectTimeout=")


def _ssh_parts(argv: list[str]) -> tuple[str, str] | None:
    """Parse the small SSH read transport; reject arbitrary SSH options and commands."""
    i, n = 1, len(argv)
    while i < n and argv[i].startswith("-"):
        a = argv[i]
        if a in _SSH_SAFE_OPTS:
            i += 1
        elif a == "-p" and i + 1 < n and argv[i + 1].isdigit():
            i += 2
        elif a == "-o" and i + 1 < n and argv[i + 1].startswith(_SSH_SAFE_O):
            i += 2
        else:
            return None
    if i >= n:
        return None
    host, rest = argv[i], argv[i + 1:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    return (host, " ".join(rest)) if rest else None


def _ssh_read_grant(auth: EffectiveAuthority, argv: list[str]) -> Grant | None:
    from steward import diagnostics

    parts = _ssh_parts(list(argv))
    if parts is None:
        return None
    host, remote = parts
    g = covering_grant(auth, "remote_read", {"host": host})
    if g is None or prohibiting_grant(auth, "remote_read", {"host": host}):
        return None
    chk = diagnostics.validate_read(remote, services=tuple(g.scope.get("services") or ()),
                                    paths=tuple(g.scope.get("paths") or ()))
    return g if chk.ok else None


def _bash(store, job_id, command: str, worktree: Path, auth: EffectiveAuthority) -> Verdict:
    heads = permissions.command_words(command)
    writes = permissions.write_targets(command, worktree)
    for h in heads:
        p = prohibiting_grant(auth, "run_command", {"command": h})
        if p:
            return Verdict(False, f"prohibited:{p.grant_id}", f"owner prohibition {p.grant_id}: {h[:120]}", auth.rev,
                           p.grant_id)
    for w in writes:
        p = prohibiting_grant(auth, "write_path", {"path": w})
        if p:
            return Verdict(False, f"prohibited:{p.grant_id}", f"owner prohibition {p.grant_id}: write {w}", auth.rev,
                           p.grant_id)
    base = permissions.decide("Bash", command, worktree)
    if base.allow:
        return Verdict(True, "default_policy", base.reason, auth.rev)
    if permissions.sensitive_path_in(permissions._commands(command)):
        return Verdict(False, "not_granted", base.reason, auth.rev)
    used: list[str] = []
    uncovered: list[str] = []
    covered_heads: list[str] = []
    ssh_reads = {" ".join(a): a for a in permissions._commands(command) if a and Path(a[0]).name == "ssh"}
    for h in heads:
        if permissions.command_hold_reason(h) is None:
            continue
        if h in ssh_reads:
            g = _ssh_read_grant(auth, ssh_reads[h])
            if g:
                used.append(g.grant_id)
                covered_heads.append(h)
                continue
        g = covering_grant(auth, "run_command", {"command": h})
        if g:
            used.append(g.grant_id)
            covered_heads.append(h)
        elif not any(c != h and c.endswith(" " + h) for c in covered_heads):
            uncovered.append(h)
    for w in writes:
        g = covering_grant(auth, "write_path", {"path": w}) if w != permissions.UNVERIFIABLE else None
        if g:
            used.append(g.grant_id)
        else:
            uncovered.append(f"write {w}")
    if uncovered or not used:
        return Verdict(False, "not_granted", f"{base.reason}; not covered by any owner grant: "
                                             f"{', '.join(u[:80] for u in uncovered[:4]) or 'held for another reason'}",
                       auth.rev)
    ids = list(dict.fromkeys(used))
    return Verdict(True, f"grant:{ids[0]}", f"owner grant(s) {', '.join(ids)}", auth.rev, ids[0])


def decide_worker(store: Store, job_id: str, tool: str, command_or_input: Any, worktree, *,
                  auth: EffectiveAuthority | None = None) -> Verdict:
    worktree = Path(worktree).resolve()
    auth = auth or effective(store, job_id)
    if not _ownership_on():  # kill switch: grants are recorded but not consulted
        d = permissions.decide(tool, command_or_input, worktree)
        return Verdict(d.allow, "default_policy", d.reason, auth.rev)
    if auth.stopped:
        return Verdict(False, "job_stopped", f"job is {auth.state}: no grant applies after a stop", auth.rev)
    if tool == "Bash":
        command = command_or_input if isinstance(command_or_input, str) else (command_or_input or {}).get("command", "")
        return _bash(store, job_id, command or "", worktree, auth)
    if tool in ("Write", "Edit", "NotebookEdit"):
        raw = permissions._extract_path(command_or_input)
        target = permissions._resolve(raw, worktree) if raw else None
        if target is not None:
            p = prohibiting_grant(auth, "write_path", {"path": str(target)})
            if p:
                return Verdict(False, f"prohibited:{p.grant_id}", f"owner prohibition {p.grant_id}: {target}",
                               auth.rev, p.grant_id)
        d = permissions.decide(tool, command_or_input, worktree)
        if d.allow or target is None:
            return Verdict(d.allow, "default_policy", d.reason, auth.rev)
        g = covering_grant(auth, "write_path", {"path": str(target)})
        if g:
            return Verdict(True, f"grant:{g.grant_id}", f"owner grant {g.grant_id}", auth.rev, g.grant_id)
        return Verdict(False, "not_granted", d.reason, auth.rev)
    d = permissions.decide(tool, command_or_input, worktree)
    return Verdict(d.allow, "default_policy", d.reason, auth.rev)


def remote_read_authorized(store: Store, job_id: str, host: str, *, auth: EffectiveAuthority | None = None) -> Verdict:
    auth = auth or effective(store, job_id)
    if auth.stopped:
        return Verdict(False, "job_stopped", f"job is {auth.state}", auth.rev)
    p = prohibiting_grant(auth, "remote_read", {"host": host})
    if p:
        return Verdict(False, f"prohibited:{p.grant_id}", f"owner prohibition {p.grant_id}: {host}", auth.rev, p.grant_id)
    g = covering_grant(auth, "remote_read", {"host": host})
    if g:
        return Verdict(True, f"grant:{g.grant_id}", f"owner grant {g.grant_id}: read-only diagnostics on {host}",
                       auth.rev, g.grant_id)
    return Verdict(False, "not_granted", f"no owner grant for read-only diagnostics on {host!r}", auth.rev)


def delivery_authorized(store: Store, job_id: str, milestone: str, target: str, *,
                        auth: EffectiveAuthority | None = None) -> Verdict:
    auth = auth or effective(store, job_id)
    if auth.stopped:
        return Verdict(False, "job_stopped", f"job is {auth.state}", auth.rev)
    t = {"milestone": milestone, "target": target}
    p = prohibiting_grant(auth, "delivery", t)
    if p:
        return Verdict(False, f"prohibited:{p.grant_id}", f"owner prohibition {p.grant_id}", auth.rev, p.grant_id)
    g = covering_grant(auth, "delivery", t)
    if g:
        return Verdict(True, f"grant:{g.grant_id}", f"owner grant {g.grant_id}: {milestone} {target}", auth.rev,
                       g.grant_id)
    return Verdict(False, "not_granted", f"no owner grant to {milestone} {target}", auth.rev)


def revalidate(store: Store, job_id: str, *, expected_rev: int,
               check: Callable[[EffectiveAuthority], Verdict] | None = None) -> Verdict:
    auth = effective(store, job_id)
    if auth.stopped:
        return Verdict(False, "job_stopped", f"job is {auth.state}", auth.rev)
    if auth.rev == expected_rev:
        return check(auth) if check else Verdict(True, "unchanged", "authority unchanged", auth.rev)
    changed = f"authority revision {expected_rev} -> {auth.rev}"
    if check is None:
        return Verdict(False, "authority_changed", changed, auth.rev)
    v = check(auth)
    if v.allow:
        return replace(v, basis="revalidated:" + v.basis, reason=f"{changed}; still allowed: {v.reason}")
    return Verdict(False, "authority_changed", f"{changed}; {v.reason}", auth.rev, v.grant_id)


# -- rendering -----------------------------------------------------------------------------------------------------

def _scope_text(g: Grant) -> str:
    s = g.scope
    if g.operation == "write_path":
        return " ".join(s["paths"])
    if g.operation == "run_command":
        return " | ".join(s["commands"])
    if g.operation == "remote_read":
        extra = "".join(f" {k}={','.join(s[k])}" for k in ("services", "paths") if s.get(k))
        return f"{s['host']} (ssh {s['ssh']}){extra}"
    if g.operation == "key":
        return " ".join(s["names"])
    return f"{s['milestone']} {s['target']}"


def render_lines(auth: EffectiveAuthority, *, max_lines: int | None = None) -> list[str]:
    allow = [f"GRANT {g.grant_id} {g.operation} {_scope_text(g)} (owner source: {g.source.kind}, by {g.granted_by}, "
             f"rev {g.created_rev})" for g in auth.grants]
    proh = [f"PROHIBITED {g.grant_id} {g.operation} {_scope_text(g)} (by {g.granted_by})" for g in auth.prohibitions]
    lines = proh + allow  # prohibitions are never the ones cut
    if max_lines is not None and len(lines) > max_lines:
        cut = len(lines) - max_lines
        lines = lines[:max_lines] + [f"... {cut} more grant(s) not shown; Steward validates every action against all "
                                     f"of them (steward grants {auth.job_id[:8]})"]
    return lines


def packet_grants(store: Store, job_id: str) -> list[dict]:
    """Complete grant list for independent reviewers, with prohibitions first."""
    auth = effective(store, job_id)
    return [{"id": g.grant_id, "kind": g.kind, "op": g.operation, "scope": g.scope,
             "source": g.source.kind, "by": g.granted_by} for g in (*auth.prohibitions, *auth.grants)]


def packet_section(store: Store, job_id: str, *, max_grants: int | None = None) -> dict:
    max_grants = PACKET_GRANTS_MAX if max_grants is None else max_grants
    auth = effective(store, job_id)
    rows = [{"id": g.grant_id, "op": g.operation, "scope": g.scope, "source": g.source.kind, "by": g.granted_by}
            for g in auth.grants]
    out = {"rev": auth.rev, "grants_rev": auth.grants_rev, "digest": auth.digest, "stopped": auth.stopped,
           "authority": list(auth.authority), "boundaries": list(auth.boundaries), "grants": rows[:max_grants],
           "prohibitions": [{"id": g.grant_id, "op": g.operation, "scope": g.scope} for g in auth.prohibitions],
           "keys": list(auth.keys), "owner_words": list(auth.excerpts)}
    if len(rows) > max_grants:
        out["omitted"] = len(rows) - max_grants
        out["lookup"] = f"steward grants {job_id[:8]}"
    return out


def preauthorized_run_lines(auth: EffectiveAuthority) -> list[str]:
    out = []
    for line in auth.authority:
        if not line.startswith("run: "):
            continue
        cmd = line[len("run: "):].strip()
        if cmd and prohibiting_grant(auth, "run_command", {"command": cmd}) is None:
            out.append(line)
    return out


# -- Plan 1 seams --------------------------------------------------------------------------------------------------

def _approval_refs(b) -> list[str]:
    return [r.split(":", 1)[1] for r in b.evidence if str(r).startswith("approval:")]


def _cheap_grant_covers(store: Store, job_id: str, b) -> dict | None:
    """Clears a blocker before any paid review when every permission ask it cites is now covered by a live grant
    (or already approved), or when its clearing params are covered. Never clears on anything else."""
    params = (b.clearing or {}).get("params") or {}
    if (b.clearing or {}).get("predicate") == "grant_covers" and params:
        if _predicate_grant_covers(store, job_id, b, params) == "cleared":
            return {"reason": "covered by a current owner grant", "evidence": [f"authority_rev:{authority_rev(store, job_id)}"]}
        return None
    ids = _approval_refs(b)
    if not ids:
        return None
    from steward import approvals, resolver

    auth = effective(store, job_id)
    used = []
    for aid in ids:
        rec = approvals.get_approval(store, job_id, aid)
        if rec is None:
            return None
        if rec["status"] == "approved":
            continue
        if rec["status"] != "pending" or rec.get("kind") != "permission":
            return None
        hold = resolver.hold_for(store, rec)
        cmd = hold.get("input") if hold.get("tool") != "Bash" else (hold.get("input") or {}).get("command", "")
        v = decide_worker(store, job_id, hold.get("tool", ""), cmd, store.home / "worktrees" / job_id, auth=auth)
        if not (v.allow and v.grant_id):
            return None
        used.append(f"grant:{v.grant_id}")
    return {"reason": "every cited permission ask is covered by a current owner grant",
            "evidence": used + [f"authority_rev:{auth.rev}"]}


def _predicate_grant_covers(store: Store, job_id: str, b, params: dict) -> str:
    op, target = params.get("operation"), params.get("target")
    if op not in grants.OPERATIONS or not isinstance(target, dict):
        return "uncertain"
    auth = effective(store, job_id)
    if auth.stopped or prohibiting_grant(auth, op, target):
        return "holds"
    return "cleared" if covering_grant(auth, op, target) else "holds"


_INSTALLED = False


def install() -> None:
    """Idempotent. Installs grant revisions, the `authorization_missing` kind and the `grant_covers` check into the
    Plan 1 ownership layer. The ownership kill switch is honoured per call (`decide_worker`, `ownership.enabled`)."""
    global _INSTALLED
    if _INSTALLED:
        return
    from steward import blockers, ownership

    ownership.set_authority_rev_fn(authority_rev)
    blockers.register_kind("authorization_missing", blockers.KindSpec(
        False, 3, ("progress", "completion"), "grant_covers", "tech_lead"))
    ownership.register_cheap_check("grant_covers", _cheap_grant_covers)
    ownership.register_predicate("grant_covers", _predicate_grant_covers)
    _INSTALLED = True


def settle_pending_asks(store: Store, job_id: str, *, now: float) -> list[str]:
    """Approve pending permission asks covered by a newly recorded live grant."""
    from steward import approvals, resolver
    from steward.models import Judgment

    if not _ownership_on():
        return []
    auth = effective(store, job_id, now=now)
    if auth.stopped or not auth.grants:
        return []
    done = []
    for rec in approvals.list_approvals(store, job_id, status="pending"):
        if rec.get("kind") != "permission":
            continue
        hold = resolver.hold_for(store, rec)
        tool = hold.get("tool", "")
        inp = (hold.get("input") or {}).get("command", "") if tool == "Bash" else hold.get("input")
        v = decide_worker(store, job_id, tool, inp, store.home / "worktrees" / job_id, auth=auth)
        if v.allow and v.grant_id:
            approvals.decide(store, job_id, rec["id"], "approve", decided_by="code")
            store.append_event(job_id, what="owner-granted permission auto-approved",
                               evidence={"approval_id": rec["id"], "tool": tool, "grant_id": v.grant_id,
                                         "authority_rev": v.rev, "basis": v.basis, "late": True},
                               rule="owner_grant_auto_approve", judgment=Judgment.code)
            done.append(rec["id"])
    return done
