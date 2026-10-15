"""The `gsd_context` adapter (Phase 17, G1/G2/G15): `### … — OPEN FOR <OWNER>` areas in a GSD `*-CONTEXT.md`.

Pure text parsing, no model. Options and the recommended answer are the job's own words; nothing is guessed (G15). A
marker the parser cannot turn into an area (a bullet reading `OPEN FOR <OWNER>: which DB?`) becomes one unparsed card.
"""

from __future__ import annotations

import re
import textwrap
from types import SimpleNamespace

from steward import owner
from steward.question_adapters.base import (
    Adapter, Change, Edit, FileReader, Found, basename, dedupe_anchors, digest, dirname, make_edit, one_line, slug,
)

_ANCHOR_TOKEN = re.compile(r"^[A-Za-z]{1,3}-?\d+[A-Za-z]?$")
_FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
_HEADING = re.compile(r"^#{1,6}\s")
_XML_TAG = re.compile(r"^\s*</?[A-Za-z][\w:-]*>\s*$")
_LIST_ITEM = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
_TOP_BULLET = re.compile(r"^[-*]\s+(.*)$")
_OPTION_MARK = re.compile(r"\(([a-z])\)\s+")
_NESTED_OPTION = re.compile(r"^[-*]\s+\([a-z]\)\s")
_EVIDENCE = re.compile(
    r"(?P<file>[\w./-]*[\w-]\.(?:py|md|json|toml|sh|ts|js|txt|yaml|yml):\d+)"
    r"|commit\s+(?P<sha>[0-9a-f]{7,40})\b"
    r"|(?P<url>https?://[^\s)>\]`]+)"
)


_RATIONALE = re.compile(r"^[-*]\s+\**Rationale:")
_NUM_ANCHOR = re.compile(r"^([A-Za-z]+-?)(\d+)$")
_DATE = r"\d{4}-\d{2}-\d{2}"
_LOG_SUFFIX = "CONTEXT.md"

_grammar_cache: dict = {}


def _alt(names) -> str:
    return "(?:" + "|".join(re.escape(n) for n in names) + ")"


def _grammar() -> SimpleNamespace:
    """The owner-dependent patterns (Phase 19, G7), built on first use and cached by the accepted names: the
    configured owner and the legacy spelling are both read; nothing is compiled at import time."""
    names, uppers = owner.parser_names(), owner.parser_uppers()
    key = (names, uppers)
    g = _grammar_cache.get(key)
    if g is None:
        who, up = _alt(names), _alt(uppers)
        g = SimpleNamespace(
            uppers=uppers,
            header=re.compile(
                r"^(?P<hashes>#{2,6})\s+(?P<head>.+?) — "
                rf"(?P<mk>OPEN FOR {up}"
                r"|(?P<state>DECIDED|DEFERRED) \((?P<who>[^,()]+),\s*(?P<date>\d{4}-\d{2}-\d{2})\)(?::\s*(?P<text>.*))?)\s*$"
            ),
            added_by=re.compile(rf"^(?:[-*]\s+)?\*\*Added by {who}"),
            note_line=re.compile(rf"^-\s+\*\*Added by {who}:\*\*"),
            head_a_done=re.compile(rf"decided defaults \({who}, {_DATE}\)"),
            head_b_done=re.compile(rf"\(decided, {who}, {_DATE}\)"),
            foot_done=re.compile(rf"\({who}, {_DATE}\)\*$"),
        )
        _grammar_cache.clear()
        _grammar_cache[key] = g
    return g


def log_path(path: str) -> str:
    """The sibling discussion log of a CONTEXT file: `16-CONTEXT.md` -> `16-DISCUSSION-LOG.md`."""
    d = dirname(path)
    name = basename(path)[: -len(_LOG_SUFFIX)] + "DISCUSSION-LOG.md"
    return f"{d}/{name}" if d else name


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _plain(text: str) -> str:
    return text.replace("**", "")


class GsdContext(Adapter):
    name = "gsd_context"

    def wants(self, path: str) -> bool:
        b = basename(path)
        return b.endswith("-CONTEXT.md") or b == "CONTEXT.md"

    def scan(self, diff: list[Change], files: FileReader) -> list[Found]:
        out: list[Found] = []
        for ch in diff:
            if ch.status == "D" or not self.wants(ch.path):
                continue
            text = files.read(ch.path)
            if text is None:
                continue
            out.extend(self.parse(ch.path, text))
        return out

    def areas(self, path: str, text: str) -> list[Found]:
        return self.parse(path, text)

    def sibling_paths(self, path: str) -> list[str]:
        return [log_path(path)]

    # -- write-back (G5) ------------------------------------------------------

    def patch(self, texts: dict[str, str], items: list[tuple[dict, dict]], date: str) -> list[Edit]:
        if not items:
            return []
        path = items[0][0]["source"]["file"]
        text = texts.get(path)
        if text is None:
            return []
        found = {f.anchor: f for f in self.parse(path, text)}
        lines = text.split("\n")
        batch: dict[str, dict] = {}
        placed: list[tuple[int, dict, dict]] = []
        for rec, ans in items:
            f = found.get(rec["source"]["anchor"])
            if f is None or not (0 < f.line <= len(lines)) or not _grammar().header.match(lines[f.line - 1]):
                continue
            placed.append((f.line - 1, rec, ans))
            batch[f.anchor] = ans
        for idx, rec, ans in sorted(placed, key=lambda t: -t[0]):  # bottom-up: earlier indices stay valid
            _rewrite_area(lines, idx, ans, date)
        new_text = _close(path, "\n".join(lines), batch, date, self)
        edits: list[Edit] = []
        if new_text != text:
            edits.append(make_edit(path, text, new_text))
        lp = log_path(path)
        if lp in texts:
            entries = _entries(self.parse(path, new_text), batch)
            new_log = _log_block(texts[lp], entries, date)
            if new_log != texts[lp]:
                edits.append(make_edit(lp, texts[lp], new_log))
        return edits

    # -- one file -----------------------------------------------------------

    def parse(self, path: str, text: str) -> list[Found]:
        lines = text.splitlines()
        found: list[Found] = []
        fence = ""
        grammar = _grammar()
        for i, line in enumerate(lines):
            fm = _FENCE.match(line)
            if fence:
                if fm and fm.group(1)[0] == fence[0] and len(fm.group(1)) >= len(fence) and line.strip() == fm.group(1):
                    fence = ""
                continue
            if fm:
                fence = fm.group(1)
                continue
            hm = grammar.header.match(line)
            if hm:
                found.append(self._area(path, i, lines, hm))
                continue
            um = _LIST_ITEM.match(line)
            if um and not _HEADING.match(line):
                item = um.group(1)
                marks = [f"OPEN FOR {u}" for u in grammar.uppers]
                if item.endswith(tuple(marks)) or item.startswith(
                        tuple(m + ":" for m in marks) + tuple("**" + m for m in marks)):
                    found.append(self._unparsed(path, i + 1, item))
        dedupe_anchors(found)
        return found

    def _unparsed(self, path: str, line_no: int, item: str) -> Found:
        d = digest(item)
        return Found(
            adapter=self.name, file=path, anchor=f"unparsed-{d[:8]}", line=line_no,
            title="unparsed question", question=f"unparsed question in {path}:{line_no}",
            options=[], recommended=[], recommended_text="", starred=False, rationale="",
            evidence=[f"{path}:{line_no}"], hash=d, marker=None, unparsed=True,
        )

    def _area(self, path: str, i: int, lines: list[str], hm: re.Match) -> Found:
        head = hm.group("head").strip()
        marker = None
        if hm.group("state"):
            marker = {"state": hm.group("state").lower(), "who": hm.group("who").strip(),
                      "date": hm.group("date"), "text": (hm.group("text") or "").strip()}
        body = self._body(lines, i + 1)
        first, _, rest = head.partition(" ")
        if rest and _ANCHOR_TOKEN.match(first):
            anchor, title_raw = first, rest.strip()
        else:
            title_raw = head
            anchor = slug(head, 40)
        title = one_line(title_raw, 60)
        options = _parse_options(body)
        keys, rec_text = _parse_recommended(body, {o["key"] for o in options})
        hashed = [head] + _hashed_body(body)
        while hashed and not hashed[-1].strip():
            hashed.pop()
        return Found(
            adapter=self.name, file=path, anchor=anchor, line=i + 1, title=title,
            question=_parse_question(body, title), options=options, recommended=keys,
            recommended_text=rec_text or "as written", starred=True,
            rationale=_parse_rationale(body), evidence=_evidence(body), blocking=False,
            hash=digest("\n".join(hashed)), marker=marker, unparsed=False,
        )

    @staticmethod
    def _body(lines: list[str], start: int) -> list[str]:
        body: list[str] = []
        fence = ""
        for line in lines[start:]:
            fm = _FENCE.match(line)
            if fence:
                if fm and fm.group(1)[0] == fence[0] and line.strip() == fm.group(1):
                    fence = ""
                body.append(line)
                continue
            if fm:
                fence = fm.group(1)
                body.append(line)
                continue
            if _HEADING.match(line) or line.strip() == "---" or _XML_TAG.match(line):
                break
            body.append(line)
        return body


# -- body parsers ---------------------------------------------------------------


def _hashed_body(body: list[str]) -> list[str]:
    """The body lines that count toward the area hash: the owner's own `**Added by <name>…**` note (the line and any
    lines wrapped under it) is Steward's write-back, so it must never make an answer stale."""
    added_by = _grammar().added_by
    out: list[str] = []
    skip_indent: int | None = None
    for line in body:
        if skip_indent is not None:
            if line.strip() and _indent(line) > skip_indent:
                continue
            skip_indent = None
        if added_by.match(line.strip()):
            skip_indent = _indent(line)
            continue
        out.append(line.rstrip())
    return out


def _top_bullet(body: list[str], prefix: re.Pattern) -> int | None:
    for idx, line in enumerate(body):
        if line and not line[0].isspace() and prefix.match(line):
            return idx
    return None


def _sub_block(body: list[str], idx: int) -> list[str]:
    """The lines indented under the top-level bullet at `idx` (blank lines skipped)."""
    block: list[str] = []
    for line in body[idx + 1:]:
        if not line.strip():
            continue
        if _indent(line) == 0:
            break
        block.append(line)
    return block


def _parse_options(body: list[str]) -> list[dict]:
    idx = _top_bullet(body, re.compile(r"^[-*]\s+\**Options:\**"))
    if idx is None:
        return []
    remainder = re.sub(r"^[-*]\s+\**Options:\**\s*", "", body[idx])
    text = remainder.strip()
    for raw in _sub_block(body, idx):
        s = raw.strip()
        if _NESTED_OPTION.match(s):
            s = re.sub(r"^[-*]\s+", "", s)
            text = (text.rstrip("; ") + "; " + s) if text else s
        else:
            text = (text + " " + s).strip()
    marks: list[re.Match] = []
    expected = "a"
    for m in _OPTION_MARK.finditer(text):
        if m.group(1) != expected:
            continue
        if not (m.start() == 0 or text[max(0, m.start() - 2):m.start()] == "; "):
            continue
        marks.append(m)
        expected = chr(ord(expected) + 1)
    options: list[dict] = []
    for n, m in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
        seg = re.sub(r"\s+", " ", text[m.end():end]).strip().rstrip(" ;.").strip()
        label, _, detail = seg.partition(" — ")
        options.append({"key": m.group(1), "label": label.strip(), "detail": detail.strip()})
    return options


def _parse_recommended(body: list[str], option_keys: set[str]) -> tuple[list[str], str]:
    idx = _top_bullet(body, re.compile(r"^[-*]\s+\**Recommended"))
    if idx is None:
        return [], ""
    text = _plain(re.sub(r"^[-*]\s+", "", body[idx]))
    text = re.sub(r"^Recommended\s*:?\s*", "", text)
    keys: list[str] = []
    pos = 0
    while True:
        m = re.compile(r"\(([a-z])\)").match(text, pos)
        if not m:
            break
        keys.append(m.group(1))
        pos = m.end()
        sep = re.compile(r"\s*(?:\+|,|/|\band\b)\s*(?=\([a-z]\))").match(text, pos)
        if not sep:
            break
        pos = sep.end()
    rest = text[pos:].lstrip(" .,;:").strip()
    if not rest:
        for raw in _sub_block(body, idx):
            sm = _TOP_BULLET.match(raw.strip())
            cand = _plain(sm.group(1) if sm else raw.strip()).strip()
            if cand:
                rest = cand
                break
    keys = [k for k in keys if k in option_keys]
    return keys, one_line(rest, 200)


def _parse_rationale(body: list[str]) -> str:
    idx = _top_bullet(body, re.compile(r"^[-*]\s+\**Rationale:"))
    if idx is None:
        return ""
    first = re.sub(r"^[-*]\s+\**Rationale:\**\s*", "", body[idx]).strip()
    if not first:
        for raw in _sub_block(body, idx):
            sm = _TOP_BULLET.match(raw.strip())
            first = _plain(sm.group(1) if sm else raw.strip()).strip()
            if first:
                break
    return one_line(first, 200)


def _parse_question(body: list[str], title: str) -> str:
    skip = re.compile(r"^[-*]\s+\**(?:Options:|Recommended|Rationale:)")
    for line in body:
        if not line.strip() or line[0].isspace() or skip.match(line):
            continue
        text = _plain(re.sub(r"^[-*]\s+", "", line)).strip()
        if text:
            return one_line(text, 300)
    return title


def _evidence(body: list[str]) -> list[str]:
    out: list[str] = []
    for m in _EVIDENCE.finditer("\n".join(body)):
        if m.group("file"):
            ref = m.group("file")
        elif m.group("sha"):
            ref = f"commit {m.group('sha')}"
        else:
            ref = m.group("url").rstrip(".,;:")
        ref = ref[:120]
        if ref not in out:
            out.append(ref)
        if len(out) >= 8:
            break
    return out


# -- write-back helpers -----------------------------------------------------------------------------------


def marker_text(ans: dict) -> str:
    """What the header says after `DECIDED (<name>, date): `."""
    if ans.get("recommended"):
        return "recommended"
    return one_line(ans.get("label") or ans.get("other") or ans.get("option") or "", 300)


def _marker(ans: dict, date: str) -> str:
    d = ans.get("date") or date
    if ans.get("option") == "defer":
        return f"DEFERRED ({owner.name()}, {d})"
    return f"DECIDED ({owner.name()}, {d}): {marker_text(ans)}"


def _rewrite_area(lines: list[str], idx: int, ans: dict, date: str) -> None:
    """Header marker first, then the Added-by bullet. `lines` is edited in place."""
    grammar = _grammar()
    hm = grammar.header.match(lines[idx])
    lines[idx] = lines[idx][: hm.start("mk")] + _marker(ans, date)
    body = GsdContext._body(lines, idx + 1)
    start, end = idx + 1, idx + 1 + len(body)
    for n in range(end - 1, start - 1, -1):  # an earlier one-line note of ours goes; a hand-written one stays
        if grammar.note_line.match(lines[n]):
            del lines[n]
            end -= 1
    note = one_line(ans.get("note") or "", 500)
    if not note:
        return
    bullet = f"- **Added by {owner.name()}:** {note}"
    at = next((n for n in range(start, end) if _RATIONALE.match(lines[n])), None)
    if at is None:
        at = end
        while at > start and not lines[at - 1].strip():
            at -= 1
    lines.insert(at, bullet)


def _entries(areas: list[Found], batch: dict[str, dict]) -> list[dict]:
    """The owner's decisions as the patched text now states them, in area order, with the notes of this batch."""
    out = []
    for f in areas:
        mk = f.marker
        if f.unparsed or not mk or not owner.is_owner_actor(mk.get("who")):
            continue
        text = mk.get("text", "")
        kind = "defer" if mk["state"] == "deferred" else "recommended" if text == "recommended" else "other"
        out.append({"anchor": f.anchor, "kind": kind, "text": text, "date": mk.get("date", ""),
                    "note": one_line((batch.get(f.anchor) or {}).get("note") or "", 500)})
    return out


def _numeric(anchor: str):
    m = _NUM_ANCHOR.match(anchor)
    return (m.group(1), int(m.group(2))) if m else None


def _groups(entries: list[dict], *, split_notes: bool = False) -> list[list[dict]]:
    """Runs of recommended entries with consecutive numbers under one prefix; every other entry is its own group."""
    groups: list[list[dict]] = []
    for e in entries:
        cur = groups[-1] if groups else None
        joinable = e["kind"] == "recommended" and not (split_notes and e["note"])
        if cur and joinable and cur[0]["kind"] == "recommended" and not (split_notes and cur[0]["note"]):
            a, b = _numeric(cur[-1]["anchor"]), _numeric(e["anchor"])
            if a and b and a[0] == b[0] and b[1] == a[1] + 1:
                cur.append(e)
                continue
        groups.append([e])
    return groups


def _span(group: list[dict]) -> str:
    return group[0]["anchor"] if len(group) == 1 else f"{group[0]['anchor']}–{group[-1]['anchor']}"


def _status_summary(entries: list[dict]) -> str:
    parts = []
    for g in _groups(entries):
        e = g[0]
        if e["kind"] == "recommended":
            parts.append(f"{g[0]['anchor']} accepts the recommended default exactly as written" if len(g) == 1
                         else f"{_span(g)} accept the recommended default exactly as written")
        elif e["kind"] == "defer":
            parts.append(f"{e['anchor']} deferred")
        else:
            parts.append(f'{e["anchor"]} is "{e["text"]}"')
    return "; ".join(parts)


def _footer_summary(entries: list[dict]) -> str:
    parts = []
    for g in _groups(entries):
        e = g[0]
        if e["kind"] == "recommended":
            parts.append(f"{_span(g)} decided: recommended")
        elif e["kind"] == "defer":
            parts.append(f"{e['anchor']} deferred")
        else:
            parts.append(f"{e['anchor']} decided: {e['text']}")
    return "; ".join(parts).replace(" · ", " / ")


def _close(path: str, text: str, batch: dict[str, dict], date: str, adapter: GsdContext) -> str:
    """Status paragraph, section heading and footer: only once no area of the file is still open."""
    areas = adapter.parse(path, text)
    if any(f.marker is None and not f.unparsed for f in areas):
        return text
    entries = _entries(areas, batch)
    if not entries:
        return text
    lines = text.split("\n")
    _status(lines, entries, date)
    _heading(lines, date)
    _footer(lines, entries, date)
    return "\n".join(lines)


def _status(lines: list[str], entries: list[dict], date: str) -> None:
    i = next((n for n, ln in enumerate(lines) if ln.startswith("**Status:**")), None)
    if i is None:
        return
    j = i + 1
    while j < len(lines) and lines[j].strip():
        j += 1
    original = " ".join(ln.strip() for ln in lines[i:j])
    lead = "**Status:** Discussed, no plans. " if original.startswith("**Status:** Discussed, no plans.") else "**Status:** "
    para = f"{lead}**Decided by {owner.name()} {date}:** {_status_summary(entries)}."
    lines[i:j] = textwrap.wrap(para, width=120, break_long_words=False, break_on_hyphens=False)


def _heading(lines: list[str], date: str) -> None:
    g = _grammar()
    name = owner.name()
    heads = [n for n, ln in enumerate(lines) if re.match(r"^#{2,6}\s", ln)]
    for n in heads:
        for up in g.uppers:
            head_a = f"recommended defaults (all OPEN FOR {up})"
            if head_a in lines[n]:
                lines[n] = lines[n].replace(head_a, f"decided defaults ({name}, {date})")
                return
        if g.head_a_done.search(lines[n]):
            lines[n] = g.head_a_done.sub(f"decided defaults ({name}, {date})", lines[n])
            return
    for n in heads:
        for up in g.uppers:
            head_b = f"(all OPEN FOR {up})"
            if head_b in lines[n]:
                lines[n] = lines[n].replace(head_b, f"(decided, {name}, {date})")
                break
        else:
            if g.head_b_done.search(lines[n]):
                lines[n] = g.head_b_done.sub(f"(decided, {name}, {date})", lines[n])


def _footer(lines: list[str], entries: list[dict], date: str) -> None:
    n = len(lines) - 1
    while n >= 0 and not lines[n].strip():
        n -= 1
    if n < 0:
        return
    line = lines[n].rstrip()
    if not (line.startswith("*") and line.endswith("*")):
        return
    g = _grammar()
    summary = f"{_footer_summary(entries)} ({owner.name()}, {date})"
    mark = next((f"OPEN FOR {u}" for u in g.uppers if f"OPEN FOR {u}" in line), None)
    if mark:
        cut = line.rfind(" · ", 0, line.index(mark))
        if cut < 0:
            return
        lines[n] = line[: cut + 3] + summary + "*"
    elif g.foot_done.search(line) and " · " in line:
        lines[n] = line[: line.rindex(" · ") + 3] + summary + "*"


def _log_bullets(entries: list[dict]) -> list[str]:
    out = []
    for g in _groups(entries, split_notes=True):
        e = g[0]
        note = f" {e['note']}" if e["note"] else ""
        if e["kind"] == "recommended" and not e["note"]:
            out.append(f"- **{_span(g)}:** accepted the recommended default exactly as written; markers now read "
                       f"`DECIDED ({owner.name()}, {e['date']}): recommended`.")
        elif e["kind"] == "recommended":
            out.append(f"- **{e['anchor']}:** accepted the recommended default exactly as written.{note}")
        elif e["kind"] == "defer":
            out.append(f"- **{e['anchor']}:** deferred.{note}")
        else:
            out.append(f"- **{e['anchor']}:** {e['text']}.{note}")
    return out


def _log_block(log: str, entries: list[dict], date: str) -> str:
    """Append (or replace, same date) `## <Name> answers (date)` in the discussion log; the rest is untouched."""
    todays = [e for e in entries if e["date"] == date]
    if not todays:
        return log
    heading = f"## {owner.cap()} answers ({date})"
    block = [heading, *_log_bullets(todays)]
    lines = log.split("\n")
    at = next((n for n, ln in enumerate(lines) if ln.strip() == heading), None)
    if at is None:
        base = log if log.endswith("\n") or not log else log + "\n"
        return base + "\n" + "\n".join(block) + "\n"
    end = at + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    tail = 0
    while end - tail - 1 > at and not lines[end - tail - 1].strip():
        tail += 1
    lines[at:end - tail] = block
    return "\n".join(lines)
