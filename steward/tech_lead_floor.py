"""The tech lead's hard floor (v2.6, PRD D-04, SUP-11): hard-coded, checked before EVERY tech-lead action.

No policy value can open it: `check()` takes an optional policy only so callers can pass one uniformly; it never reads
a policy field, and `steward.tech_lead_policy` rejects any key that names a floor class. The six classes:

  1. outside_world      no push, publish, deploy, or contacting anyone outside
  2. final_decision     no accept, merge, incorporate or release
  3. spend              no spending past the job's owner hard money caps (advisory limits never refuse)
  4. secrets            no exposing secrets or granting keys the job was not given
  5. outside_worktree   no deleting or rewriting outside the job's worktree; never the live Steward store or services
  6. test_weakening     no weakening, skipping or deleting tests to get green

A refused action is refused, logged and escalated by the caller (`steward.tech_lead_actions`). An action is a dict:
`{"type", "params", "commands": [shell lines a takeover would run], "paths": [files it would touch],
"cost_usd": float}`; text fields in `params` are scanned too (an instruction to the worker to push is a push), except
where the instruction is negated ("never git push", "do not publish").
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

CLASSES = ("outside_world", "final_decision", "spend", "secrets", "outside_worktree", "test_weakening")
SUMMARY = ("Hard floor (hard-coded, never openable by policy): 1 outside_world - no push, publish, deploy or contacting "
           "anyone outside; 2 final_decision - no accept, merge, incorporate or release; 3 spend - nothing past the "
           "job's money or rate caps; 4 secrets - no exposing secrets or granting keys the job was not given; "
           "5 outside_worktree - no writes or deletes outside the job's worktree, never the live Steward store or "
           "services; 6 test_weakening - no weakening, skipping or deleting tests to get green.")
SUMMARY_KEYS = CLASSES

_OUTSIDE_TYPES = frozenset({"push", "publish", "deploy", "notify", "send", "email", "message", "post", "contact",
                            "tweet", "upload"})
_FINAL_TYPES = frozenset({"accept", "merge", "incorporate", "release", "approve_acceptance"})
_SPEND_TYPES = frozenset({"set_limit", "raise_cap", "set_budget", "spend"})

_OUTSIDE_RE = re.compile(
    r"\bgit\s+push\b|\bnpm\s+publish\b|\b(?:yarn|pnpm)\s+publish\b|\btwine\s+upload\b|\bdocker\s+push\b"
    r"|\bgh\s+(?:release|pr\s+create|gist|api)\b|\btg\s+send\b|\bsendmail\b|\bmail\s+-s\b"
    r"|\bcurl\b[^\n]*\s-X\s*(?:POST|PUT|PATCH|DELETE)\b|\bcurl\b[^\n]*\s(?:-d|--data)\b|\bwget\s+--post"
    r"|\b(?:ssh|scp)\s|\brsync\b[^\n]*\S+:|\bdeploy(?:_self)?\b|\bsteward\s+deploy-self\b|\bpublish\b", re.I)
_FINAL_RE = re.compile(r"\bsteward\s+(?:accept|incorporate|release|merge)\b|\bgit\s+merge\b|\bgh\s+pr\s+merge\b"
                       r"|\bgit\s+tag\b|\bnpm\s+version\b", re.I)
_SECRET_VALUE_RE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}"
                              r"|xox[abp]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY)")
_SECRET_CMD_RE = re.compile(r"\.api-keys\b|\bprintenv\b|^\s*env\s*$|\bset\s*$|/proc/\d+/environ|\.netrc\b"
                            r"|\bsecret[s]?\.(?:toml|json|ya?ml)\b|\bMETA_API_KEY\b", re.I | re.M)
_SECRET_PATH_RE = re.compile(r"(^|/)(\.env(\..*)?|\.api-keys|\.netrc|id_rsa|id_ed25519|credentials(\.json)?)$")
_SERVICE_RE = re.compile(r"\bsystemctl\b|\bservice\s+\S+\s+(?:start|stop|restart)\b|\bkill(?:all)?\b|\bpkill\b"
                         r"|\bherdr\s+(?:pane|tab|workspace)\b|\bsteward\s+(?:control|run|stop|pause|resume)\b"
                         r"|\bcrontab\b|\bdocker(?:-compose)?\s+(?:restart|stop|rm|kill)\b", re.I)
_RM_ABS_RE = re.compile(r"\b(?:rm|rmdir|mv|cp|tee|truncate|chmod|chown|ln)\b[^\n;|&]*?\s(/|~)\S*", re.I)
_WEAKEN_RE = re.compile(r"@pytest\.mark\.(?:skip|xfail)|pytest\.skip\(|--deselect\b|\s-k\s+['\"]?not\b"
                        r"|\bgit\s+rm\b[^\n;&|]*\btests?/|\brm\b[^\n;&|]*\btests?/|--no-verify\b|\bunittest\.skip"
                        r"|\bdelete (?:the )?tests?\b|\bskip (?:the )?tests?\b|\bweaken\b", re.I)
_WEAKEN_PARAMS = ("skip_tests", "delete_tests", "weaken_tests", "deselect")
_NEGATION_RE = re.compile(r"\b(?:never|not|no|don'?t|do not|without|avoid|must not|mustn'?t)\b[^.;:\n]{0,24}$",
                          re.I)
# a negated coordinated verb list reaching the match: "Do not skip, deselect, delete or weaken any tests" (item 13b)
_NEGATED_LIST_RE = re.compile(r"\b(?:never|not|no|don'?t|do not|without|avoid|must not|mustn'?t)\s+"
                              r"(?:[\w-]+,\s+){0,8}[\w-]+,?\s+(?:or|nor)\s+$", re.I)


@dataclass(frozen=True)
class FloorContext:
    worktree: Path
    granted_keys: frozenset = frozenset()
    money_cap_usd: float | None = None
    spent_usd: float = 0.0
    rate_usd_h: float | None = None
    rate_cap_usd_h: float | None = None
    protected: tuple = ()  # the live Steward store/home and config dir (never touched by a tech lead)
    granted_paths: frozenset = frozenset()
    granted_commands: frozenset = frozenset()


@dataclass(frozen=True)
class FloorVerdict:
    ok: bool
    klass: str | None = None
    reason: str = ""
    evidence: dict = field(default_factory=dict)


OK = FloorVerdict(True)


def _refuse(klass: str, reason: str, **evidence) -> FloorVerdict:
    return FloorVerdict(False, klass, reason, evidence)


def _texts(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [t for v in obj.values() for t in _texts(v)]
    if isinstance(obj, (list, tuple)):
        return [t for v in obj for t in _texts(v)]
    return []


def _hits(rx: re.Pattern, text: str, *, allow_negated: bool) -> str | None:
    for m in rx.finditer(text):
        if allow_negated and (_NEGATION_RE.search(text[max(0, m.start() - 40):m.start()])
                              or _NEGATED_LIST_RE.search(text[max(0, m.start() - 120):m.start()])):
            continue
        return m.group(0)
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def check(action: dict, ctx: FloorContext, *, policy=None) -> FloorVerdict:  # noqa: ARG001 - policy never consulted
    """The floor verdict for one proposed tech-lead action. Pure; never reads a policy value."""
    kind = str(action.get("type") or "").lower()
    params = action.get("params") or {}
    commands = [str(c) for c in action.get("commands") or []]
    paths = [str(p) for p in action.get("paths") or []]
    prose = _texts(params)  # instructions to the worker: negated mentions are allowed

    # 2. final decisions (checked first: "release" is both outside and final; final is the more specific class)
    if kind in _FINAL_TYPES:
        return _refuse("final_decision", f"`{kind}` is a final decision: only the owner accepts, merges, incorporates "
                                         "or releases")
    if str(params.get("approval_kind") or "").lower() == "acceptance":
        return _refuse("final_decision", "deciding the acceptance approval is the owner's final decision")
    for c in commands:
        hit = _hits(_FINAL_RE, c, allow_negated=False)
        if hit:
            return _refuse("final_decision", f"command `{c[:120]}` is a final decision ({hit})", command=c)
    # 1. outside world
    if kind in _OUTSIDE_TYPES:
        return _refuse("outside_world", f"`{kind}` reaches outside the job: never by the tech lead")
    for c in commands:
        hit = _hits(_OUTSIDE_RE, c, allow_negated=False)
        if hit:
            return _refuse("outside_world", f"command `{c[:120]}` reaches outside the job ({hit})", command=c)
    for t in prose:
        hit = _hits(_OUTSIDE_RE, t, allow_negated=True)
        if hit and any(gc in t for gc in ctx.granted_commands):
            continue
        if hit and hit.lower() not in ("deploy", "publish"):
            return _refuse("outside_world", f"instruction tells the worker to reach outside ({hit})", text=t[:200])
        if hit and re.search(rf"\b(?:then|now|and|please|to)\s+{re.escape(hit)}\b", t, re.I):
            return _refuse("outside_world", f"instruction tells the worker to {hit}", text=t[:200])
    # 3. spend
    if kind in _SPEND_TYPES:
        return _refuse("spend", f"`{kind}` changes the job's money or rate caps")
    cost = float(action.get("cost_usd") or 0.0)
    if ctx.money_cap_usd is not None and cost and ctx.spent_usd + cost > ctx.money_cap_usd:
        return _refuse("spend", f"would spend ${ctx.spent_usd + cost:.2f}, past the job's ${ctx.money_cap_usd:.2f} cap",
                       spent=ctx.spent_usd, cost=cost, cap=ctx.money_cap_usd)
    if ctx.rate_cap_usd_h is not None and ctx.rate_usd_h is not None and ctx.rate_usd_h > ctx.rate_cap_usd_h and cost:
        return _refuse("spend", f"job already at ${ctx.rate_usd_h:.2f}/h, past its ${ctx.rate_cap_usd_h:.2f}/h rate cap")
    # 4. secrets and keys
    for k in params.get("grant_keys") or []:
        if str(k) not in ctx.granted_keys:
            return _refuse("secrets", f"key {k} was not granted to this job", key=str(k))
    for t in [*commands, *prose]:
        if _SECRET_VALUE_RE.search(t):
            return _refuse("secrets", "the action carries a secret-looking value")
    for c in commands:
        if _SECRET_CMD_RE.search(c):
            return _refuse("secrets", f"command `{c[:120]}` reads secrets or the environment", command=c)
    for p in paths:
        if _SECRET_PATH_RE.search(p.replace(os.sep, "/")):
            return _refuse("secrets", f"path {p} holds secrets", path=p)
    # 5. outside the worktree, the live store, services
    for p in paths:
        cand = Path(os.path.expanduser(p))
        cand = cand if cand.is_absolute() else ctx.worktree / cand
        if any(_inside(cand, Path(x)) for x in ctx.protected):
            return _refuse("outside_worktree", f"path {p} is the live Steward store/config", path=p)
        if not _inside(cand, ctx.worktree):
            return _refuse("outside_worktree", f"path {p} is outside the job's worktree", path=p)
    for c in commands:
        if _SERVICE_RE.search(c):
            return _refuse("outside_worktree", f"command `{c[:120]}` touches services, processes or panes", command=c)
        m = _RM_ABS_RE.search(c)
        if m:
            target = Path(os.path.expanduser(c[m.start(1):].split()[0]))
            if not _inside(target, ctx.worktree):
                return _refuse("outside_worktree", f"command `{c[:120]}` writes outside the worktree", command=c)
    # 6. test weakening
    for name in _WEAKEN_PARAMS:
        if params.get(name):
            return _refuse("test_weakening", f"`{name}` weakens the tests")
    for t in commands:
        if _hits(_WEAKEN_RE, t, allow_negated=False):
            return _refuse("test_weakening", f"command `{t[:120]}` weakens, skips or deletes tests", command=t)
    for t in prose:
        if _hits(_WEAKEN_RE, t, allow_negated=True):
            return _refuse("test_weakening", "instruction tells the worker to weaken, skip or delete tests",
                           text=t[:200])
    return OK


def check_diff(findings) -> FloorVerdict:
    """A takeover's committed diff, through the existing weakening detector (`steward.weakening.detect`)."""
    if findings:
        kinds = sorted({getattr(f, "kind", str(f)) for f in findings})
        return _refuse("test_weakening", f"the takeover diff weakens tests: {kinds}", findings=kinds)
    return OK


def context_for(store, job_id: str, worktree: Path) -> FloorContext:
    """The floor context of a job: its worktree, granted keys, money cap and spend, and the protected live paths."""
    granted: frozenset = frozenset()
    g_paths: frozenset = frozenset()
    g_cmds: frozenset = frozenset()
    cap = None
    spent = 0.0
    try:
        from steward import authority

        auth = authority.effective(store, job_id)
        granted = frozenset(auth.keys)
        g_paths = frozenset(p for g in auth.grants if g.operation == "write_path" for p in g.scope["paths"])
        g_cmds = frozenset(c for g in auth.grants if g.operation == "run_command" for c in g.scope["commands"])
    except Exception:  # noqa: BLE001 - unknown grants mean none granted
        pass
    try:
        from steward import spend_policy

        if spend_policy.enabled():
            best = None
            for lim in spend_policy.hard_limits(store, job_id):
                if lim.scope == "job_total":
                    used = spend_policy.report(store, job_id).known_usd
                else:
                    used = spend_policy.known_spend(store, job_id, spend_policy.SCOPE_SOURCES.get(lim.scope, ()))[0]
                if best is None or lim.usd - used < best[0].usd - best[1]:
                    best = (lim, used)
            if best is not None:
                cap, spent = best[0].usd, best[1]
        else:
            from steward import research

            cap = research.get_caps(store, job_id).get("cap_usd")
            spent = spend_policy.known_spend(store, job_id, ("tech_lead",))[0]
        from steward import research as _research

        granted = granted | frozenset(_research.get_caps(store, job_id).get("keys") or [])
    except Exception:  # noqa: BLE001
        cap = None
    protected = []
    live = Path.home() / ".local" / "state" / "steward"
    for p in (live, Path.home() / ".config" / "steward"):
        if not _inside(Path(worktree), p):
            protected.append(p)
    if not _inside(Path(worktree), Path(store.home)):
        protected.append(Path(store.home))
    else:  # the job's own store home holds its worktree: protect the store files, not the worktrees dir
        protected.extend(p for p in Path(store.home).glob("*.db*"))
    return FloorContext(worktree=Path(worktree), granted_keys=granted, money_cap_usd=cap, spent_usd=spent,
                        protected=tuple(protected), granted_paths=g_paths, granted_commands=g_cmds)
