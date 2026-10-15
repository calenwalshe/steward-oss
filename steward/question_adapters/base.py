"""Shared types for the question adapters (Phase 17, G1/G14).

An adapter is a small class: `scan(diff, files) -> [Found]`. It only ever sees text the job COMMITTED, through a
`FileReader` that the scanner backs with `git show <head>:<path>` (T-17-04: no working-tree read, no path taken from
file text is opened). `patch` is the write-back half of G14; plan 17-02 implements it.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable

MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class Change:
    """One path from `git diff --raw`: status A (added), M (modified) or D (deleted); blob is the new object id."""

    path: str
    status: str
    blob: str = ""


@dataclass
class Found:
    """One question as an adapter saw it in one file at one commit."""

    qid: str = ""  # filled by the scanner, which sees every file at once
    adapter: str = ""
    file: str = ""
    anchor: str = ""
    line: int = 0
    title: str = ""
    question: str = ""
    options: list[dict] = field(default_factory=list)
    recommended: list[str] = field(default_factory=list)
    recommended_text: str = ""
    starred: bool = False
    rationale: str = ""
    evidence: list[str] = field(default_factory=list)
    blocking: bool = False
    blocking_reason: str = ""
    hash: str = ""
    marker: dict | None = None  # {state: decided|deferred, who, date, text}; None = still open
    unparsed: bool = False


@dataclass
class Edit:
    """One file's write-back: the committed text it was rendered from, the new text and the unified diff."""

    path: str
    old_text: str
    new_text: str
    diff: str = ""


def make_edit(path: str, old_text: str, new_text: str) -> Edit:
    diff = "".join(difflib.unified_diff(old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))
    return Edit(path=path, old_text=old_text, new_text=new_text, diff=diff)


class FileReader:
    """Committed file text for one scan, memoised. `read_fn(path) -> str | None`; `grep_fn(needle, paths) -> [path]`."""

    def __init__(self, read_fn: Callable[[str], str | None], grep_fn: Callable[[str, list[str]], list[str]] | None = None):
        self._read_fn = read_fn
        self._grep_fn = grep_fn
        self._cache: dict[str, str | None] = {}

    def read(self, path: str) -> str | None:
        if path not in self._cache:
            try:
                text = self._read_fn(path)
            except Exception:  # noqa: BLE001 - an unreadable file is simply absent
                text = None
            if text is not None and len(text.encode("utf-8", "replace")) > MAX_FILE_BYTES:
                text = None
            self._cache[path] = text
        return self._cache[path]

    def unreadable(self, path: str) -> bool:
        """True when a read of `path` was tried and came back empty (absent, unreadable or too big)."""
        return path in self._cache and self._cache[path] is None

    def grep_l(self, needle: str, paths: list[str]) -> list[str]:
        """The subset of `paths` whose committed text contains the fixed string `needle`."""
        if not paths:
            return []
        if self._grep_fn is None:
            return [p for p in paths if (self.read(p) or "").find(needle) >= 0]
        try:
            hits = set(self._grep_fn(needle, list(paths)))
        except Exception:  # noqa: BLE001
            hits = set()
        return [p for p in paths if p in hits]


class Adapter:
    """Base class. Subclasses set `name` and implement `wants` and `scan`."""

    name = ""

    def wants(self, path: str) -> bool:
        return False

    def scan(self, diff: list[Change], files: FileReader) -> list[Found]:
        raise NotImplementedError

    def areas(self, path: str, text: str) -> list[Found]:
        """The questions in one file's committed text, as `scan` would see them (used to check what an answer was
        given against before an edit is rendered)."""
        return []

    def sibling_paths(self, path: str) -> list[str]:
        """Other files `patch` reads next to `path` (a discussion log, an answers file), read from the same commit."""
        return []

    def patch(self, texts: dict[str, str], items: list[tuple[dict, dict]], date: str) -> list[Edit]:
        """Write-back (G5, G14): pure and deterministic. `texts` = committed text by path (the question file and its
        siblings), `items` = [(record, answer)] for ONE file, `date` = the batch date. Returns one Edit per changed
        file; rendering the result again yields none."""
        raise NotImplementedError


def one_line(text: str, cap: int) -> str:
    """Whitespace-collapsed, capped with an ellipsis."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > cap:
        text = text[: max(cap - 1, 0)].rstrip() + "…"
    return text


def digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def slug(text: str, cap: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:cap].strip("-") or "question"


def dedupe_anchors(found: list[Found]) -> None:
    """Two questions in one file must never share an anchor: the second gets -2, the third -3."""
    seen: dict[str, int] = {}
    for f in found:
        n = seen.get(f.anchor, 0) + 1
        seen[f.anchor] = n
        if n > 1:
            f.anchor = f"{f.anchor}-{n}"


def dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]
