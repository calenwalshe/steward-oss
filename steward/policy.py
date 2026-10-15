"""Instruction classification + hard-constraint checking (SP-9, SP-10).

`classify_instruction` maps a steering verb + its text to `(kind,
enforcement)` — the same shape stored on every instruction revision.
`violations` is the deterministic diff-inspection code path that turns an
`avoid <glob>` instruction into an enforceable boundary: a hard-enforcement
constraint blocks closeout on its own, no model asked whether it minds.

An `avoid` instruction is only `hard` when its text actually contains at
least one path/glob-shaped token (SP-10): a prose sentence like "Don't touch
the db." has no filesystem path in it, so labelling it `hard` would claim an
enforcement that `violations()` can never perform (a hard label with nothing
to match against is worse than no label — it looks enforced but is not). An
`avoid` instruction with no extractable path token is downgraded to
`review`/`preference` like `focus`/`nudge` prose. A dedicated `authority`
verb exists for boundaries whose enforcement is already handled elsewhere
(discovery's check/unclassified command split) rather than by diffing the
worktree — e.g. "no deploy/publish/network" is not a path and was never
meant to be diff-matched.
"""

from __future__ import annotations

import fnmatch
import re

_REVIEW_KEYWORDS = ("api", "interface", "compatib")

# Trailing punctuation/quotes that can hang off a token when it's the last
# word of a sentence, e.g. "migrations/**." or '"db/**",'.
_TRAILING_PUNCT = ".,;:!?)]}\"'"
_LEADING_PUNCT = "([{\"'"

# A token has a recognizable file extension if it ends in a dot followed by
# a short run of alphanumerics, e.g. "store.py", "001.sql" — but not a
# sentence-ending period on an ordinary word, which `_TRAILING_PUNCT`
# stripping already removes before this check runs.
_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def classify_instruction(verb: str, text: str) -> tuple[str, str]:
    """Map a steering verb + text to `(kind, enforcement)`.

    `kind` in `{intent, execution, policy}`; `enforcement` in
    `{hard, preference, review, authority}`.
    """
    verb = verb.lower()
    lowered = text.lower()

    if verb == "avoid":
        if parse_avoid_globs(text):
            return "policy", "hard"
        enforcement = "review" if _mentions_review_keyword(lowered) else "preference"
        return "policy", enforcement
    if verb == "authority":
        # Enforced by discovery's check/unclassified classification, not by
        # diffing the worktree (SP-10) — never diff-matched, never blocks
        # closeout on its own.
        return "policy", "authority"
    if verb == "goal":
        # A goal change always requires review — it creates a new contract
        # revision marker (SP-7), which is not something code can decide
        # alone (steering.Steering.record handles the marker).
        return "intent", "review"
    if verb == "priority":
        return "policy", "preference"
    if verb in ("focus", "nudge"):
        enforcement = "review" if _mentions_review_keyword(lowered) else "preference"
        return "execution", enforcement
    raise ValueError(f"unknown steering verb: {verb}")


def _mentions_review_keyword(lowered: str) -> bool:
    return any(keyword in lowered for keyword in _REVIEW_KEYWORDS)


def _is_path_token(token: str) -> bool:
    """Is `token` shaped like a filesystem path or glob, not a prose word?

    A bare "/" is not enough on its own — "deploy/publish/network" is a
    slash-separated list of alternatives, not a path — so a token only
    counts when it has a glob wildcard, a recognizable file extension
    (on the whole token or on a '/'-separated segment), or a trailing
    slash (a directory boundary like "migrations/").
    """
    if "*" in token:
        return True
    if token.endswith("/"):
        return True
    if "/" in token:
        return any(_EXTENSION_RE.search(segment) for segment in token.split("/"))
    return bool(_EXTENSION_RE.search(token))


_VERB = r"(?:edit(?:ing)?|touch(?:ing)?|modify(?:ing)?|chang(?:e|ing)|alter(?:ing)?|rewrit(?:e|ing)|delet(?:e|ing)|remov(?:e|ing)|overwrit(?:e|ing)|writ(?:e|ing)(?:\s+to)?)"
# "do not edit X", "never change X", "avoid touching X", bare "avoid X".
_PROHIBITION_RE = re.compile(
    rf"\b(?:do\s+not|don't|dont|never|must\s+not|mustn't|should\s+not|shouldn't)\s+{_VERB}\b"
    rf"|\bavoid(?:\s+{_VERB})?\b",
    re.IGNORECASE,
)
_LEAVE_ALONE_RE = re.compile(r"\bleave\b(?P<t>[^;\n]*?)\balone\b", re.IGNORECASE)
_READ_ONLY_RE = re.compile(r"(?P<t>[^;\n]*?)\b(?:is|are)\s+read[- ]only\b", re.IGNORECASE)
_CLAUSE_END_RE = re.compile(r";|\n|\bbut\b|\.\s", re.IGNORECASE)


def _tokens(chunk: str) -> list[str]:
    """Path/glob-shaped tokens in `chunk`; a 'A.ext/B.ext' pair becomes A.ext and B.ext."""
    out: list[str] = []
    for piece in (p.strip() for c in chunk.split(",") for p in c.split()):
        cleaned = piece.lstrip(_LEADING_PUNCT + "`").rstrip(_TRAILING_PUNCT + "`")
        if not cleaned:
            continue
        segments = cleaned.split("/")
        if "*" not in cleaned and len(segments) > 1 and any(
            _EXTENSION_RE.search(seg) and not seg.startswith(".") for seg in segments[:-1]
        ):
            # "A.json/B.json": a file is never a directory — it is a list of files.
            out.extend(seg for seg in segments if seg and _is_path_token(seg))
        elif _is_path_token(cleaned):
            out.append(cleaned)
    return out


def _prohibited_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    for m in _PROHIBITION_RE.finditer(text):
        rest = text[m.end():]
        end = _CLAUSE_END_RE.search(rest)
        chunks.append(rest[: end.start()] if end else rest)
    for m in _LEAVE_ALONE_RE.finditer(text):
        chunks.append(m.group("t"))
    for m in _READ_ONLY_RE.finditer(text):
        # only the subject of this clause, not earlier sentences
        chunks.append(re.split(r";|\.\s", m.group("t"))[-1])
    return chunks


def parse_avoid_globs(text: str) -> list[str]:
    """Parse an `avoid` instruction's text into a list of path globs.

    Only paths inside an explicit prohibition ("do not edit/touch/modify X",
    "never change X", "avoid X", "leave X alone", "X is read-only") are
    protected; paths merely mentioned, or listed as work to do, are not. A
    text with no prohibition phrase counts only when it is a bare glob list
    (`"steward/store.py, tests/*"`, the CLI `avoid` form); prose without a
    prohibition yields nothing. Trailing punctuation/quotes are stripped, so
    "migrations/**." still yields "migrations/**"; leading dots are kept.
    """
    chunks = _prohibited_chunks(text)
    if not chunks:
        words = [w for c in text.split(",") for w in c.split()]
        if not words or not all(_tokens(w) for w in words):
            return []
        chunks = [text]
    seen: list[str] = []
    for chunk in chunks:
        for t in _tokens(chunk):
            if t not in seen:
                seen.append(t)
    return seen


def violations(diff_paths: list[str], active_hard_globs: list[str]) -> list[str]:
    """Return every path in `diff_paths` that matches an active hard glob."""
    if not active_hard_globs:
        return []
    hits = []
    for path in diff_paths:
        for pattern in active_hard_globs:
            if fnmatch.fnmatch(path, pattern):
                hits.append(path)
                break
    return hits
