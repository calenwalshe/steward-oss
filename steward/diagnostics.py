"""Scoped, read-only diagnostics for the tech lead (continuous job ownership Plan 2; spec §6, §2 "the lead inherits
the job's authorized read-only diagnostic access, including named remote hosts and services, without broader write
rights or secret exposure").

A read names a target (`local`, or a host the owner granted with `remote_read`) and one command line. The line is
validated piece by piece (`validate_read`): every simple command must be a known read, compound lines may only chain
reads, and redirects, substitutions, wrappers, shells/interpreters and credential paths are refused. SSH is only the
transport for a validated line to a granted alias; it is never a shell grant. Authority is revalidated immediately
before running. Output is sanitized, bounded and recorded with its target, operation, authority reference and
digest (`diagnostic_reads`), so evidence can be cited (`diag:<read_id>`) without retaining raw output.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from steward import authority, permissions, sanitize
from steward.actor import ActorRef
from steward.models import Judgment
from steward.store import Store

MAX_COMMAND_CHARS = 1000
MAX_SEGMENTS = 8
READ_TIMEOUT_S = 30.0
MAX_READS_PER_HOUR = 20
CAPTURE_CHARS = 64000
EXCERPT_CHARS = 2000
PACKET_READS = 5
PACKET_EXCERPT_CHARS = 1500
RETENTION_DAYS = 30
MAX_ROWS_PER_JOB = 500
SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
READ_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
LOCAL_READ_ROOTS = ("/var/log/", "/etc/", "/proc/", "/sys/", "/run/", "/tmp/")
_RATE_KEY = "diagnostic_reads_hour"

READ_ONLY = frozenset({"cat", "head", "tail", "grep", "egrep", "fgrep", "zgrep", "zcat", "wc", "ls", "stat", "file",
                       "du", "df", "free", "uptime", "uname", "hostname", "date", "whoami", "id", "ps", "pgrep",
                       "ss", "netstat", "readlink", "realpath", "sha256sum", "md5sum", "sort", "uniq", "cut", "tr",
                       "jq", "find", "journalctl", "curl", "ping", "lsof", "lsblk", "dmesg", "nproc", "getent",
                       "dig", "host", "nslookup", "true", "echo"})
SUBCOMMAND_READS = {
    "systemctl": frozenset({"status", "show", "is-active", "is-enabled", "is-failed", "list-units", "list-timers",
                            "cat", "list-unit-files"}),
    "git": frozenset({"log", "show", "status", "diff", "rev-parse", "branch", "ls-files", "describe", "remote"}),
    "docker": frozenset({"ps", "logs", "inspect", "images", "stats", "version", "info"}),
    "ip": frozenset({"addr", "a", "route", "r", "link", "-br"}),
    "nft": frozenset({"list"}),
    "caddy": frozenset({"version", "validate", "list-modules"}),
}
_BAD_FLAGS = {
    "find": ("-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"),
    "journalctl": ("--rotate", "--vacuum", "--flush", "--sync", "--relinquish-var", "--setup-keys",
                   "--update-catalog"),
    "curl": ("-o", "-O", "--output", "--remote-name", "--remote-name-all", "-K", "--config", "-T", "--upload-file"),
    "git": ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--set-upstream-to", "-u"),
    "docker": ("--rm",),
}
_REFUSED_TOKENS = (("\n", "newline"), ("`", "substitution"), ("$(", "substitution"), ("${", "substitution"),
                   ("<(", "process substitution"), (">(", "process substitution"), ("<<", "heredoc"))
_HARMLESS_REDIRECTS = re.compile(r"(?<=\s)2>&1(?=\s|$)|(?<=\s)2>/dev/null(?=\s|$)")


@dataclass(frozen=True)
class ReadCheck:
    ok: bool
    reason: str
    argvs: tuple = ()


@dataclass(frozen=True)
class DiagnosticResult:
    read_id: str
    job_id: str
    target: str
    operation: str
    outcome: str
    exit_code: int | None
    excerpt: str
    output_digest: str
    truncated: bool
    authority_rev: int
    grant_id: str | None
    refused_reason: str | None


def _no(reason: str) -> ReadCheck:
    return ReadCheck(False, reason)


def _paths_in(argv: list[str]) -> list[str]:
    return [w for w in argv[1:] if w.startswith(("/", "~"))]


def validate_read(command: str, *, services: tuple = (), paths: tuple = ()) -> ReadCheck:
    line = str(command or "").strip()
    if not line:
        return _no("empty command")
    if len(line) > MAX_COMMAND_CHARS:
        return _no(f"too long ({len(line)} > {MAX_COMMAND_CHARS} chars)")
    for tok, why in _REFUSED_TOKENS:
        if tok in line:
            return _no(f"{why} is not a read ({tok!r})")
    if sanitize.SECRET_READ_RE.search(line):
        return _no("reads a credential or the environment")
    probe = _HARMLESS_REDIRECTS.sub(" ", " " + line)
    if re.search(r"[<>]", probe):
        return _no("redirects are not reads")
    if re.search(r"(?<![&|])&(?![&])", probe):
        return _no("background execution is not a read")
    segments = permissions._segments(probe.strip())  # harmless `2>&1`/`2>/dev/null` already removed
    if not segments:
        return _no("could not parse the command")
    if len(segments) > MAX_SEGMENTS:
        return _no(f"too many commands in one line ({len(segments)} > {MAX_SEGMENTS} segments)")
    argvs = []
    for words in segments:
        if not words:
            continue
        head = words[0]
        if "=" in head and not head.startswith("-"):
            return _no(f"environment assignment {head!r} is not allowed")
        exe = Path(head).name
        if exe in SUBCOMMAND_READS:
            sub = next((w for w in words[1:] if not w.startswith("-") or exe == "ip"), None)
            if exe == "git":
                rest = words[1:]
                while rest and rest[0] == "-C" and len(rest) >= 2:  # `git -C DIR <sub>` names a repo, not a write
                    rest = rest[2:]
                sub = rest[0] if rest else None
                flags = rest[1:]
            else:
                flags = words[1:]
            if sub not in SUBCOMMAND_READS[exe]:
                return _no(f"{exe} {sub or ''} is not a read".strip())
            bad = next((f for f in flags if any(f == b or f.startswith(b + "=") for b in _BAD_FLAGS.get(exe, ()))
                        and not (exe == "git" and sub != "branch")), None)
            if bad:
                return _no(f"{exe} {sub} {bad} is not a read")
            if exe == "systemctl" and services:
                units = [w for w in words[2:] if not w.startswith("-")]
                if any(u.removesuffix(".service") not in services for u in units):
                    return _no(f"unit outside the granted services {list(services)}")
        elif exe in READ_ONLY:
            bad = next((f for f in words[1:] for b in _BAD_FLAGS.get(exe, ())
                        if f == b or f.startswith(b + "=") or (b.startswith("--") and f.startswith(b))), None)
            if bad:
                return _no(f"{exe} {bad} is not a read")
            if exe == "curl" and permissions.is_remote_write(" ".join(words)):
                return _no("curl with a write method or body is not a read")
            if exe == "ping":
                m = re.search(r"-c\s*(\d+)", " ".join(words))
                if not m or int(m.group(1)) > 5:
                    return _no("ping needs -c N with N <= 5")
            if exe == "journalctl" and services:
                units = [words[i + 1] for i, w in enumerate(words[:-1]) if w in ("-u", "--unit")]
                if not units or any(u.removesuffix(".service") not in services for u in units):
                    return _no(f"journal unit outside the granted services {list(services)}")
        else:
            return _no(f"{exe} is not a known read-only command")
        if paths:
            for p in _paths_in(words):
                if not any(p == q.rstrip("/") or p.startswith(q if q.endswith("/") else q + "/") for q in paths):
                    return _no(f"path {p} is outside the granted paths {list(paths)}")
        argvs.append(tuple(words))
    return ReadCheck(True, "read-only", tuple(argvs))


def _local_scope(store: Store, job_id: str, check: ReadCheck) -> ReadCheck:
    home = Path(store.home).resolve()
    wt = (home / "worktrees" / job_id).resolve()
    forbidden = [Path.home() / ".local" / "state" / "steward", Path.home() / ".config" / "steward", home]
    for argv in check.argvs:
        for raw in _paths_in(list(argv)):
            p = Path(raw).expanduser().resolve()
            if p == wt or wt in p.parents:
                continue
            if any(p == f.resolve() or f.resolve() in p.parents for f in forbidden):
                return _no(f"{raw} is the live Steward store/config or another job's worktree")
            if not any(str(p).startswith(r) or str(p) + "/" == r for r in LOCAL_READ_ROOTS):
                return _no(f"{raw} is outside the local diagnostic roots {list(LOCAL_READ_ROOTS)}")
    return check


def authorize(store: Store, job_id: str, target: str, command: str) -> tuple[ReadCheck, authority.Verdict]:
    auth = authority.effective(store, job_id)
    if target == "local":
        check = validate_read(command)
        if check.ok:
            check = _local_scope(store, job_id, check)
        v = (authority.Verdict(False, "job_stopped", f"job is {auth.state}", auth.rev) if auth.stopped else
             authority.Verdict(True, "local_read", "local read-only diagnostics (job read scope)", auth.rev))
        return check, v
    v = authority.remote_read_authorized(store, job_id, target, auth=auth)
    g = authority.covering_grant(auth, "remote_read", {"host": target})
    scope = g.scope if g else {}
    check = validate_read(command, services=tuple(scope.get("services") or ()), paths=tuple(scope.get("paths") or ()))
    return check, v


def _rate_ok(store: Store, job_id: str, now: float) -> bool:
    recent = [t for t in (store.kv_get(job_id, _RATE_KEY) or []) if now - float(t) < 3600.0]
    if len(recent) >= MAX_READS_PER_HOUR:
        store.kv_set(job_id, _RATE_KEY, recent)
        return False
    store.kv_set(job_id, _RATE_KEY, recent + [now])
    return True


def _record(store: Store, job_id: str, *, read_id, target, command, purpose, requested_by: ActorRef, rev, grant_id,
            outcome, exit_code=None, refused_reason=None, excerpt="", digest="", truncated=False, duration=0.0,
            now: float) -> None:
    rec = sanitize.capture("diagnostic", {
        "target": target, "operation": command, "purpose": purpose, "excerpt": excerpt, "exit_code": exit_code,
        "outcome": outcome, "refused_reason": refused_reason, "output_digest": digest, "truncated": truncated,
        "duration_s": duration, "grant_id": grant_id, "authority_rev": rev, "requested_by": requested_by.label()})
    with store._conn:
        store._conn.execute(
            "INSERT INTO diagnostic_reads (read_id, job_id, target, operation, purpose, requested_by, authority_rev,"
            " grant_id, outcome, exit_code, refused_reason, excerpt, output_digest, truncated, duration_s,"
            " created_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (read_id, job_id, rec["target"], str(rec["operation"])[:MAX_COMMAND_CHARS], str(rec["purpose"])[:300],
             rec["requested_by"], int(rev), grant_id, outcome, exit_code, rec.get("refused_reason"),
             str(rec["excerpt"])[-EXCERPT_CHARS:], digest, int(bool(truncated)), float(duration), now))
    what = "diagnostic_read" if outcome != "refused" else "diagnostic_read_refused"
    store.append_event(job_id, what=what, evidence={
        "read_id": read_id, "target": rec["target"], "operation": str(rec["operation"])[:300], "outcome": outcome,
        "exit_code": exit_code, "refused_reason": rec.get("refused_reason"), "authority_rev": rev,
        "grant_id": grant_id, "requested_by": rec["requested_by"], "output_digest": digest},
        rule="scoped_diagnostic_read", judgment=Judgment.code)
    try:
        from steward import decision_trace

        decision_trace.record(store, job_id=job_id, actor=requested_by, stage="effect" if outcome != "refused"
                              else "discarded", decision=f"{what} {rec['target']}", now=now,
                              evidence_refs=[f"diag:{read_id}"], authority_rev=rev)
    except Exception:  # noqa: BLE001 - tracing never blocks the read record
        pass


def run_read(store: Store, job_id: str, *, target: str, command: str, purpose: str, requested_by: ActorRef,
             runner_exec, now: float, expected_rev: int | None = None) -> DiagnosticResult:
    read_id = "diag-" + uuid.uuid4().hex[:12]
    check, verdict = authorize(store, job_id, target, command)
    rev = verdict.rev
    refused = None
    if not check.ok:
        refused = f"invalid: {check.reason}"
    elif not verdict.allow:
        refused = f"{verdict.basis}: {verdict.reason}"
    elif expected_rev is not None:
        rv = authority.revalidate(store, job_id, expected_rev=expected_rev,
                                  check=lambda auth: authorize(store, job_id, target, command)[1])
        if not rv.allow:
            refused = f"{rv.basis}: {rv.reason}"
    if refused is None and not _rate_ok(store, job_id, now):
        refused = f"rate: more than {MAX_READS_PER_HOUR} diagnostic reads in the last hour; retry later"
    if refused is not None:
        _record(store, job_id, read_id=read_id, target=target, command=command, purpose=purpose,
                requested_by=requested_by, rev=rev, grant_id=verdict.grant_id, outcome="refused",
                refused_reason=refused, now=now)
        return DiagnosticResult(read_id, job_id, target, sanitize.text(command), "refused", None, "", "", False, rev,
                                verdict.grant_id, sanitize.text(refused))
    wt = Path(store.home) / "worktrees" / job_id
    if target == "local":
        argv = ["bash", "--noprofile", "--norc", "-c", command]
    else:
        g = authority.covering_grant(authority.effective(store, job_id), "remote_read", {"host": target})
        argv = ["ssh", *SSH_OPTS, g.scope["ssh"], "--", command]
    t0 = time.monotonic()
    try:
        res = runner_exec.run(argv, cwd=wt if wt.is_dir() else Path(store.home), timeout=READ_TIMEOUT_S,
                              env=dict(READ_ENV))
        raw = (res.stdout or "") + (f"\n[stderr]\n{res.stderr}" if res.stderr else "")
        outcome, code = ("done" if res.exit_code == 0 else "failed"), res.exit_code
    except subprocess.TimeoutExpired:
        raw, outcome, code = "", "timeout", None
    duration = time.monotonic() - t0
    over = len(raw) > CAPTURE_CHARS
    clean = sanitize.text(raw[-CAPTURE_CHARS:])
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]
    excerpt = clean[-EXCERPT_CHARS:]
    truncated = over or len(clean) > EXCERPT_CHARS
    _record(store, job_id, read_id=read_id, target=target, command=command, purpose=purpose,
            requested_by=requested_by, rev=rev, grant_id=verdict.grant_id, outcome=outcome, exit_code=code,
            excerpt=excerpt, digest=digest, truncated=truncated, duration=duration, now=now)
    return DiagnosticResult(read_id, job_id, target, sanitize.text(command), outcome, code, excerpt, digest, truncated,
                            rev, verdict.grant_id, None)


def reads_for(store: Store, job_id: str, *, limit: int = PACKET_READS) -> list[dict]:
    rows = store._conn.execute("SELECT * FROM diagnostic_reads WHERE job_id = ? ORDER BY created_epoch DESC, rowid DESC"
                               " LIMIT ?", (job_id, limit)).fetchall()
    return [{"read_id": r["read_id"], "target": r["target"], "operation": r["operation"], "outcome": r["outcome"],
             "exit_code": r["exit_code"], "refused_reason": r["refused_reason"],
             "excerpt": r["excerpt"][-PACKET_EXCERPT_CHARS:], "truncated": bool(r["truncated"]),
             "authority_rev": r["authority_rev"], "at": r["created_epoch"]} for r in rows]


def packet_section(store: Store, job_id: str) -> dict:
    auth = authority.effective(store, job_id)
    hosts = sorted({g.scope["host"] for g in auth.grants if g.operation == "remote_read"})
    return {"granted_hosts": hosts, "local": "read-only, under the worktree and " + ", ".join(LOCAL_READ_ROOTS),
            "how": "action diagnose {target: local|<granted host>, command: one read-only line, purpose}; "
                   "cite results as diag:<read_id>", "recent": reads_for(store, job_id)}


def purge(store: Store, *, now: float) -> int:
    from steward import decision_trace

    removed = 0
    cutoff = now - RETENTION_DAYS * 86400.0
    for (job_id,) in store._conn.execute("SELECT DISTINCT job_id FROM diagnostic_reads").fetchall():
        if decision_trace.preserved(store, job_id, now=now):
            continue
        with store._conn:
            removed += store._conn.execute("DELETE FROM diagnostic_reads WHERE job_id = ? AND created_epoch < ?",
                                           (job_id, cutoff)).rowcount
            removed += store._conn.execute(
                "DELETE FROM diagnostic_reads WHERE job_id = ? AND read_id NOT IN (SELECT read_id FROM diagnostic_reads"
                " WHERE job_id = ? ORDER BY created_epoch DESC LIMIT ?)", (job_id, job_id, MAX_ROWS_PER_JOB)).rowcount
    return removed
