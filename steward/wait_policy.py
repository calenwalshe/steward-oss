"""Bounded foreground waits (latency #3) and the strict pure-wait rule (#7).

docs/diagnostics/2026-10-02-latency: workers spent 16.7 h in foreground wait/poll calls (typically
`until [ -f X ] || [ $SECONDS -ge $end ]; do sleep 20; done`, capped just under the 600 s Bash timeout and
re-issued), and no steer can reach the model while one is in flight. Every Bash call already passes through
Steward's permission policy, so the cap is enforced there: a `sleep N` / `until|while ... sleep` / `timeout N
<wait>` form whose bound is above `cap_s()` is denied with `replacement()` as the reason. The cap was
10 s (2026-10-02) and made workers poll every ~11 s at a full model turn each (~$25/h, 2026-10-05); since 2026-10-06
it is 600 s, the Bash max, and `auto_interrupt` delivers queued steers into a pure wait. The worker brief carries the same text.

Fail OPEN: anything this parser does not understand (unbalanced quotes, a loop that does more than wait, a
backgrounded group) is allowed. A false deny costs a model turn; a false allow costs only latency.

`is_pure_wait(tool, input)` is the strict rule the code-initiated interrupt (#7) relies on: a call is a pure
wait only when EVERY simple command in it is a sleep, a read-only observer, or a loop made only of those, it
writes nothing (no redirection), starts nothing in the background, and has at least one wait in it.
Unknown means protected.
"""

from __future__ import annotations

import math
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

RULE_VERSION = "wait-rule-v1"
DEFAULT_CAP_S = 600.0
CAP_ENV = "STEWARD_WAIT_CAP_S"
MODE_ENV = "STEWARD_WAIT_POLICY"  # enforce (default) | shadow | off
DEFAULT_BASH_TIMEOUT_S = 120.0  # Claude Code's Bash default when the call names no timeout
MAX_BASH_TIMEOUT_S = 600.0
WAIT_TOOLS = frozenset({"Monitor", "BashOutput", "TaskOutput"})  # observers of a background task by definition

# Read-only commands a pure wait may contain (anything else is unknown -> not a pure wait).
_OBSERVERS = frozenset({
    "test", "[", "[[", "true", "false", ":", "echo", "printf", "cat", "head", "tail", "grep", "egrep", "fgrep",
    "rg", "wc", "ls", "stat", "date", "pgrep", "ps", "pidof", "sleep", "jobs", "break", "continue", "exit",
    "inotifywait", "basename", "dirname", "realpath", "readlink", "du", "df", "nproc", "uptime", "free", "id",
    "whoami", "hostname", "pwd", "seq", "cd", "__SUB__",
})
_GIT_READS = frozenset({"log", "status", "diff", "show", "rev-parse", "ls-files", "cat-file", "rev-list", "describe"})
_KEYWORDS = frozenset({"do", "then", "else", "elif", "if", "!", "{", "(", "time"})
_CLOSERS = frozenset({"done", "fi", "}", ")", "esac"})
_REDIR_OK = re.compile(r"^(\d?>>?/dev/null|\d?>&\d|&>/dev/null|<.*)$")
_DURATION = re.compile(r"^(\d+(?:\.\d+)?|\.\d+)([smhd]?)$")
_UNIT = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_DEADLINE_ASSIGN = re.compile(r"\b([A-Za-z_]\w*)=\$\(\(\s*(?:\$?SECONDS|__SUB__)\s*\+\s*(\d+)\s*\)\)")
_SUB = "__SUB__"  # a `$(...)` whose inner command is a read-only observer (date, stat, cat ...)
_SECONDS_CMP = re.compile(r"\$\{?SECONDS\}?\s+-(?:lt|le|ge|gt)\s+(\d+)")


def cap_s(env=None) -> float:
    raw = (os.environ if env is None else env).get(CAP_ENV, "")
    try:
        value = float(raw) if raw.strip() else DEFAULT_CAP_S
    except ValueError:
        value = DEFAULT_CAP_S
    return value if value > 0 else DEFAULT_CAP_S


def mode(env=None) -> str:
    value = (os.environ if env is None else env).get(MODE_ENV, "enforce").strip().lower()
    if value in ("0", "off", "false", "no"):
        return "off"
    return value if value in ("enforce", "shadow") else "enforce"


def replacement(cap: float | None = None) -> str:
    """The replacement instruction: the deny reason the worker sees, and the brief line (same text)."""
    c = _fmt(cap_s() if cap is None else cap)
    return (f"Every tool call costs a full model turn, so wait in as few calls as possible. Start long-running "
            f"work with run_in_background, then wait for it with one call of up to {c} s (the Monitor tool, or one "
            f"`until <finished>; do sleep 30; done` loop), and repeat only while it is still running - one wait per "
            f"tool batch, never several in parallel. Never poll in short sleep-and-check calls. Do not end your "
            f"turn while your own background work is still running.")


def brief_line(cap: float | None = None) -> str:
    """The worker brief's wait contract: the check verbs first (Phase 42), then the same replacement text the deny
    carries as the temporary fallback."""
    c = cap_s() if cap is None else cap
    return (f"WAITING: Steward owns long processes; never run the full test suite in your own turn. Submit it with "
            f"`steward check run <cmd>` (Steward runs it on your committed tree and prints a run id), then either end "
            f"your turn with `steward check await <id>` (preferred: you are resumed with the result and no model "
            f"turn is spent waiting) or block on `steward check wait <id>` (zero model cost, returns the result). "
            f"Each check verb must be a single simple command: no pipes, no `&&` or `;`, no redirects, no `cd` "
            f"first (it already runs in your worktree); a chained one is not a check. "
            f"Temporary fallback for any other wait: {replacement(c)} Steward denies a foreground wait bounded above "
            f"{_fmt(c)} s. Run subagents in the foreground: a background subagent is denied.")


CHECK_VERBS = ("run", "await", "wait")
CHECK_UNSAFE = re.compile(r"[;&|<>`\n]|\$\(")


def parse_check_verb(tool: str, tool_input: dict | None) -> tuple[str, list[str]] | None:
    """`(verb, args)` when the Bash call is exactly one `steward check run|await|wait ...` simple command, else None.
    A chained, redirected or substituted command is never a verb call (it stays an ordinary Bash call)."""
    if tool != "Bash":
        return None
    command = str((tool_input or {}).get("command") or "").strip()
    if not command or CHECK_UNSAFE.search(command):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) < 3 or Path(words[0]).name != "steward" or words[1] != "check" or words[2] not in CHECK_VERBS:
        return None
    return words[2], words[3:]


AGENT_TOOLS = frozenset({"Agent", "Task"})
BACKGROUND_AGENT_DENIAL = (
    "Background subagents are disabled for Steward workers: Steward cannot see them, so it cannot tell when your "
    "work is finished. Run the subagent in the foreground (omit run_in_background).")


def background_agent_denial(tool: str, tool_input: dict | None) -> str | None:
    """S1 (2026-10-06 decision record D5): the deny reason for a background subagent, else None. Interim until
    the shim tracks background task ids (S2); foreground subagents are untouched."""
    if tool in AGENT_TOOLS and (tool_input or {}).get("run_in_background") is True:
        return BACKGROUND_AGENT_DENIAL
    return None


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


@dataclass(frozen=True)
class Verdict:
    deny: bool
    bound_s: float | None = None  # effective foreground bound of the wait (inf = until the tool timeout)
    form: str = ""                # sleep | loop | timeout_wait | follow
    reason: str = ""
    rule_version: str = RULE_VERSION


@dataclass
class _Analysis:
    ok: bool = True               # parsed and understood
    waits: list[str] = field(default_factory=list)  # wait forms seen (foreground)
    bound: float = 0.0            # summed foreground wait bound
    pure: bool = True             # every simple command is an observer / sleep / pure loop
    background: bool = False      # a `&`, nohup, setsid or disown anywhere


def check(tool: str, tool_input: dict | None, *, cap: float | None = None) -> Verdict:
    """Permission-time cap: deny a foreground wait bounded above the cap. Never raises; unsure -> allow."""
    try:
        return _check(tool, tool_input or {}, cap_s() if cap is None else cap)
    except Exception:  # noqa: BLE001 - fail open
        return Verdict(deny=False, reason="unparsed")


def _check(tool: str, tool_input: dict, cap: float) -> Verdict:
    if tool != "Bash" or tool_input.get("run_in_background"):
        return Verdict(deny=False)
    command = str(tool_input.get("command") or "")
    if not command.strip():
        return Verdict(deny=False)
    a = analyze(command)
    if not a.ok or not a.waits:
        return Verdict(deny=False)
    effective = min(a.bound, _tool_timeout(tool_input))
    form = a.waits[0]
    if effective <= cap:
        return Verdict(deny=False, bound_s=effective, form=form)
    return Verdict(deny=True, bound_s=effective, form=form, reason=replacement(cap))


def is_pure_wait(tool: str, tool_input: dict | None) -> bool:
    """The strict rule (#7): True only for a call that does nothing but wait. Never raises; unsure -> False."""
    try:
        if tool in WAIT_TOOLS:
            return True
        tool_input = tool_input or {}
        if tool != "Bash" or tool_input.get("run_in_background"):
            return False
        a = analyze(str(tool_input.get("command") or ""))
        return a.ok and a.pure and not a.background and bool(a.waits)
    except Exception:  # noqa: BLE001 - unknown means protected
        return False


def _tool_timeout(tool_input: dict) -> float:
    try:
        ms = float(tool_input.get("timeout"))
    except (TypeError, ValueError):
        return DEFAULT_BASH_TIMEOUT_S
    return max(0.0, min(ms / 1000.0, MAX_BASH_TIMEOUT_S))


# -- shell analysis ----------------------------------------------------------------------------------------


def _strip_heredocs(command: str) -> tuple[str, bool]:
    """Drop heredoc bodies; the bool says one was present (its command feeds data somewhere: not pure)."""
    from steward.permissions import _strip_heredocs as strip

    stripped = strip(command)
    return stripped, stripped != command


def _observer_substitutions(text: str, depth: int) -> str:
    """Replace each `$(cmd)` whose `cmd` is made only of read-only observers with `__SUB__`; others stay (and make
    the command impure). Arithmetic `$((...))` is kept, but substitutions inside it are replaced too."""
    out, i = [], 0
    while i < len(text):
        if text.startswith("$((", i):
            out.append("$((")
            i += 3
            continue
        if text.startswith("$(", i):
            level, j = 1, i + 2
            while j < len(text) and level:
                level += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            inner = text[i + 2:j - 1]
            if level == 0 and depth < 2 and not inner.lstrip().startswith("seq"):  # `for i in $(seq N)` is a count
                sub = analyze(inner, depth + 1)
                if sub.ok and sub.pure and not sub.waits and not sub.background:
                    out.append(_SUB)
                    i = j
                    continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _tokens(command: str) -> list[str] | None:
    text = re.sub(r">\s+/dev/null", ">/dev/null", command)
    text = re.sub(r"\d?>&\d|&>", lambda m: " >/dev/null " if m.group(0) == "&>" else " ", text)
    text = text.replace("\\\n", " ").replace("\n", " ; ")
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return None


def _segments(tokens: list[str]) -> list[tuple[list[str], str]]:
    """(words, separator after) for each simple command."""
    out, cur = [], []
    for tok in tokens:
        if tok and set(tok) <= set(";&|"):
            out.append((cur, tok))
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append((cur, ""))
    return [(w, sep) for w, sep in out if w or sep]


def _duration(word: str) -> float | None:
    if word == "infinity":
        return math.inf
    m = _DURATION.match(word)
    return float(m.group(1)) * _UNIT[m.group(2)] if m else None


def analyze(command: str, depth: int = 0) -> _Analysis:
    a = _Analysis()
    body, had_heredoc = _strip_heredocs(command)
    if had_heredoc:
        a.pure = False
    body = _observer_substitutions(body, depth)
    command = body
    tokens = _tokens(body)
    if tokens is None:
        return _Analysis(ok=False, pure=False)
    segs = _segments(tokens)
    if any(sep == "&" for _, sep in segs):
        a.background = True
        a.pure = False
        if any(w and (w[0] in ("(", "{") or w[0].startswith("(")) for w, _ in segs):
            return _Analysis(ok=False, pure=False, background=True)  # a backgrounded group: fail open
    i = 0
    while i < len(segs):
        words, sep = segs[i]
        head = _strip_keywords(words)
        if head and head[0] in ("while", "until", "for"):
            end = _loop_end(segs, i)
            if end is None:
                return _Analysis(ok=False, pure=False)
            _loop(a, segs[i:end + 1], command, sep_after=segs[end][1])
            i = end + 1
            continue
        if sep != "&":
            _simple(a, head, depth)
        i += 1
    return a


def _strip_keywords(words: list[str]) -> list[str]:
    out = list(words)
    while out and (out[0] in _KEYWORDS or out[0] in _CLOSERS):
        out = out[1:]
    while out and out[-1] in _CLOSERS:
        out = out[:-1]
    return out


def _loop_end(segs, start: int) -> int | None:
    depth = 0
    for j in range(start, len(segs)):
        words = segs[j][0]
        for w in words:
            if w in ("while", "until", "for") and (words.index(w) == 0 or words[words.index(w) - 1] in _KEYWORDS):
                depth += 1
            if w == "done":
                depth -= 1
                if depth == 0:
                    return j
    return None


def _loop(a: _Analysis, segs, command: str, sep_after: str) -> None:
    """A while/until/for loop. A loop made only of observers + sleep is a wait; its bound comes from a deadline,
    a counter or `for ... in` count, else it runs until the tool timeout."""
    inner = _Analysis()
    words_all: list[str] = []
    for words, _ in segs:
        words_all += words
        head = _strip_keywords(words)
        if head and head[0] in ("while", "until"):
            head = head[1:]
        if head and head[0] == "for":
            continue  # the for header: `for x in ...`
        _simple(inner, _strip_keywords(head), 1)
    sleeps = [d for d in _sleep_args(words_all) if d is not None]
    if sep_after == "&" or not inner.ok:
        a.pure = a.pure and inner.pure
        return
    if not inner.pure:
        a.pure = False  # a loop that also does work is not a wait (fail open for the cap)
        return
    if not sleeps and not inner.waits:
        return  # a pure loop with no sleep (e.g. a for over files with echo): not a wait
    per_iter = sum(sleeps) or 0.0
    text = " ".join(words_all)
    bound = _loop_bound(text, command, per_iter)
    a.waits.append("loop")
    a.bound += bound


def _sleep_args(words: list[str]) -> list[float | None]:
    out = []
    for k, w in enumerate(words):
        if w == "sleep" and k + 1 < len(words):
            out.append(_duration(words[k + 1]))
    return out


def _loop_bound(text: str, command: str, per_iter: float) -> float:
    m = _SECONDS_CMP.search(text)
    if m:
        return float(m.group(1))
    for var, n in _DEADLINE_ASSIGN.findall(command):
        if re.search(r"\$\{?" + re.escape(var) + r"\b", text):
            return float(n)
    count = _iterations(text)
    if count is not None and per_iter > 0:
        return count * per_iter
    return math.inf


def _iterations(text: str) -> int | None:
    m = re.search(r"\bfor\s+\w+\s+in\s+\$\(\s*seq\s+(?:(\d+)\s+)?(\d+)\s*\)", text)
    if m:
        lo = int(m.group(1) or 1)
        return max(0, int(m.group(2)) - lo + 1)
    m = re.search(r"\bfor\s+\w+\s+in\s+\{(\d+)\.\.(\d+)\}", text)
    if m:
        return max(0, int(m.group(2)) - int(m.group(1)) + 1)
    m = re.search(r"\bfor\s+\w+\s+in\s+((?:[^;]+?))\s*;?\s*do\b", text)
    if m and "$" not in m.group(1) and "*" not in m.group(1):
        return len(m.group(1).split())
    m = re.search(r"\$\{?(\w+)\}?\s+-(?:lt|le)\s+(\d+)", text)
    if m and re.search(r"\b" + m.group(1) + r"=\$\(\(\s*\$?" + m.group(1) + r"\s*\+\s*1\s*\)\)|\(\(\s*" + m.group(1)
                       + r"\+\+\s*\)\)", text):
        return int(m.group(2))
    return None


def _simple(a: _Analysis, words: list[str], depth: int) -> None:
    """One simple command (already split, keywords stripped)."""
    words = [w for w in words if w]
    while words and re.match(r"^[A-Za-z_]\w*=", words[0]):
        if "$(" in words[0].replace("$((", "") or "`" in words[0]:
            a.pure = False
        words = words[1:]  # VAR=value prefix / plain assignment
    if not words:
        return
    if any(">" in w and not _REDIR_OK.match(w) for w in words):
        a.pure = False  # writes a file
    if any(("$(" in w.replace("$((", "")) or "`" in w for w in words):
        a.pure = False  # a substitution runs something we did not classify
    name = Path(words[0]).name
    if name in ("nohup", "setsid", "disown"):
        a.background = True
        a.pure = False
        return
    if name == "timeout":
        args = [w for w in words[1:] if not w.startswith("-")]
        limit = _duration(args[0]) if args else None
        rest = words[words.index(args[0]) + 1:] if args else []
        if limit is None or not rest:
            a.pure = False
            return
        sub = _Analysis()
        _simple(sub, rest, depth)
        a.pure = a.pure and sub.pure
        a.background = a.background or sub.background
        a.ok = a.ok and sub.ok
        if sub.waits:
            a.waits.append("timeout_wait")
            a.bound += min(limit, sub.bound)
        return
    if name in ("bash", "sh", "zsh") and len(words) >= 3 and words[1] == "-c":
        if depth >= 2:
            a.pure = False
            return
        sub = analyze(words[2], depth + 1)
        a.ok = a.ok and sub.ok
        a.pure = a.pure and sub.pure
        a.background = a.background or sub.background
        a.waits += sub.waits
        a.bound += sub.bound
        return
    if name == "sleep":
        total = 0.0
        for w in words[1:]:
            d = _duration(w)
            if d is None:
                a.ok = False
                return
            total += d
        a.waits.append("sleep")
        a.bound += total
        return
    if name == "tail" and any(w in ("-f", "-F", "--follow") or w.startswith("--follow") for w in words[1:]):
        a.waits.append("follow")
        a.bound += math.inf
        return
    if name == "inotifywait":
        t = next((words[k + 1] for k, w in enumerate(words[:-1]) if w in ("-t", "--timeout")), None)
        a.waits.append("follow")
        a.bound += _duration(t) if t and _duration(t) is not None else math.inf
        return
    if name == "steward" and words[1:3] == ["check", "wait"]:
        a.waits.append("check_wait")  # bound 0: it ends by itself (its own --timeout, under the Bash cap)
        return
    if name == "kill" and len(words) >= 2 and words[1] == "-0":
        return  # liveness probe
    if name == "git" and len(words) >= 2 and words[1] in _GIT_READS:
        return
    if name == "wait" or name not in _OBSERVERS:
        a.pure = False


# -- in-flight call labels (latency #6/#7) -------------------------------------------------------------------

_BUILD = re.compile(
    r"(?:^|[;&|(]\s*|\b(?:then|do|timeout\s+\d+[smh]?)\s+)(?:\S*/)?(?:pytest|python3?\s+-m\s+(?:pytest|unittest|build)|"
    r"npm\s+(?:test|run|ci|install)|pnpm\s+\w+|yarn\s+\w+|make\b|cargo\s+\w+|go\s+(?:test|build)|tsc\b|vitest|jest|"
    r"\S*gradlew?\s+\w+|mvn\s+\w+|pip\s+install|uv\s+(?:sync|pip|run))", re.M)
_GIT_WRITE = re.compile(r"\bgit\s+(?:commit|merge|rebase|cherry-pick|am|push|checkout|reset|stash|apply|switch)\b")


def classify_call(tool: str, tool_input: dict | None) -> str:
    """pure_wait | build_test | git_write | subagent | network | other. Only `pure_wait` is ever interruptible."""
    tool_input = tool_input or {}
    if is_pure_wait(tool, tool_input):
        return "pure_wait"
    if tool in ("Task", "Agent"):
        return "subagent"
    if tool in ("WebFetch", "WebSearch"):
        return "network"
    if tool == "Bash":
        command = str(tool_input.get("command") or "")
        if _GIT_WRITE.search(command):
            return "git_write"
        if _BUILD.search(command):
            return "build_test"
    return "other"
