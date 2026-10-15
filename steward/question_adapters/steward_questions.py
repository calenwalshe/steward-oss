"""The `steward_questions` adapter (Phase 17, G1/G13/G14/G15): the generic structured format, designed for the job.

A worker that is not a GSD job asks the owner a question by committing either a `STEWARD-QUESTIONS.json` (an object with a
`questions` list, or a bare array) or a fenced ```steward-questions block (the same JSON) inside any markdown file.
Fields per question, all optional except `question`: id, title, question, options (objects {key,label,detail} or plain
strings), recommended (a key or a list of keys), rationale, evidence (list of strings), blocking (bool) and
blocking_reason. Unknown keys are ignored; bad types degrade to defaults; nothing is guessed (G15).

Steward writes answers to a sibling `STEWARD-ANSWERS.json` (plan 17-03); an id listed there carries a marker.
"""

from __future__ import annotations

import json
import re
from typing import Any

from steward import owner
from steward.question_adapters.base import (
    Adapter, Change, Edit, FileReader, Found, basename, dedupe_anchors, digest, dirname, make_edit, one_line,
)

QUESTIONS_FILE = "STEWARD-QUESTIONS.json"
ANSWERS_FILE = "STEWARD-ANSWERS.json"
INFO_STRING = "steward-questions"

_OPEN_FENCE = re.compile(r"^\s{0,3}(`{3,})\s*steward-questions\s*$")
_ANY_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_CLOSE_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*$")
_MAX_OPTIONS = 26


def sibling_answers_path(path: str) -> str:
    d = dirname(path)
    return f"{d}/{ANSWERS_FILE}" if d else ANSWERS_FILE


def _s(value: Any, cap: int) -> str:
    return one_line(value, cap) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


class StewardQuestions(Adapter):
    name = "steward_questions"

    def wants(self, path: str) -> bool:
        return basename(path) == QUESTIONS_FILE or path.lower().endswith(".md")

    def scan(self, diff: list[Change], files: FileReader) -> list[Found]:
        live = [c.path for c in diff if c.status != "D" and self.wants(c.path)]
        json_paths = [p for p in live if basename(p) == QUESTIONS_FILE]
        md_paths = files.grep_l(INFO_STRING, [p for p in live if p not in json_paths])
        out: list[Found] = []
        for path in json_paths + md_paths:
            text = files.read(path)
            if text is None:
                continue
            found = self.parse_json_file(path, text) if path in json_paths else self.parse_markdown(path, text)
            dedupe_anchors(found)
            self._apply_answers(path, found, files)
            out.extend(found)
        return out

    def areas(self, path: str, text: str) -> list[Found]:
        found = self.parse_json_file(path, text) if basename(path) == QUESTIONS_FILE else self.parse_markdown(path, text)
        dedupe_anchors(found)
        return found

    def sibling_paths(self, path: str) -> list[str]:
        return [sibling_answers_path(path)]

    def patch(self, texts: dict[str, str], items: list[tuple[dict, dict]], date: str) -> list[Edit]:
        """Merge the answers of one questions file into its sibling STEWARD-ANSWERS.json (created when absent). Existing
        entries keep their place, the same id is replaced in place, new ones follow the questions' order."""
        if not items:
            return []
        path = items[0][0]["source"]["file"]
        answers_path = sibling_answers_path(path)
        old = texts.get(answers_path)
        doc: dict = {"answers": []}
        if old is not None:
            try:
                loaded = json.loads(old)
            except ValueError:
                return []  # never overwrite an answers file we cannot read
            doc = loaded if isinstance(loaded, dict) else {"answers": loaded}
            if not isinstance(doc.get("answers"), list):
                return []
        source = texts.get(path)
        order = [f.anchor for f in self.areas(path, source)] if source is not None else []
        rank = {a: n for n, a in enumerate(order)}
        rows = list(doc["answers"])
        index = {str(r.get("id")): n for n, r in enumerate(rows) if isinstance(r, dict) and r.get("id") is not None}
        fresh: list[tuple[int, dict]] = []
        for rec, ans in items:
            anchor = rec["source"]["anchor"]
            row = _answer_row(anchor, rec["qid"], ans, date)
            if anchor in index:
                rows[index[anchor]] = row
            else:
                fresh.append((rank.get(anchor, len(rank)), row))
        rows += [row for _, row in sorted(fresh, key=lambda t: t[0])]
        new_doc = {**doc, "answers": rows}
        if old is not None and json.loads(old) == (new_doc if isinstance(json.loads(old), dict) else new_doc["answers"]):
            return []
        return [make_edit(answers_path, old or "", json.dumps(new_doc, indent=2, ensure_ascii=False) + "\n")]

    # -- containers -----------------------------------------------------------------------

    def parse_json_file(self, path: str, text: str) -> list[Found]:
        return self._container(path, text, 1, "")

    def parse_markdown(self, path: str, text: str) -> list[Found]:
        found: list[Found] = []
        fence = ""
        block: list[str] | None = None
        block_line = 0
        n_block = 0
        for i, line in enumerate(text.splitlines(), start=1):
            if block is not None:
                cm = _CLOSE_FENCE.match(line)
                if cm and cm.group(1)[0] == "`" and len(cm.group(1)) >= len(fence):
                    n_block += 1
                    found.extend(self._container(path, "\n".join(block), block_line, f"b{n_block}-"))
                    block = None
                    fence = ""
                else:
                    block.append(line)
                continue
            if fence:
                cm = _CLOSE_FENCE.match(line)
                if cm and cm.group(1)[0] == fence[0] and len(cm.group(1)) >= len(fence):
                    fence = ""
                continue
            om = _OPEN_FENCE.match(line)
            if om:
                fence, block, block_line = om.group(1), [], i
                continue
            fm = _ANY_FENCE.match(line)
            if fm:
                fence = fm.group(1)
        return found

    def _container(self, path: str, text: str, line: int, tag: str) -> list[Found]:
        try:
            data = json.loads(text)
        except ValueError:
            return [self._unparsed(path, line, f"unparsed-{digest(text)[:8]}", digest(text))]
        if isinstance(data, dict):
            data = data.get("questions")
        if not isinstance(data, list):
            return [self._unparsed(path, line, f"unparsed-{digest(text)[:8]}", digest(text))]
        out: list[Found] = []
        for n, item in enumerate(data, start=1):
            out.append(self._item(path, line, tag, n, item))
        return out

    # -- one question ------------------------------------------------------------------------

    def _unparsed(self, path: str, line: int, anchor: str, hsh: str) -> Found:
        return Found(
            adapter=self.name, file=path, anchor=anchor, line=line, title="unparsed question",
            question=f"unparsed question in {path}:{line}", options=[], recommended=[], recommended_text="",
            starred=False, rationale="", evidence=[f"{path}:{line}"], hash=hsh, marker=None, unparsed=True,
        )

    def _item(self, path: str, line: int, tag: str, n: int, item: Any) -> Found:
        canon = json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        question = _s(item.get("question"), 600) if isinstance(item, dict) else ""
        if not question:
            return self._unparsed(path, line, f"unparsed-{tag}item{n}", digest(canon))
        options = _options(item.get("options"))
        keys = {o["key"] for o in options}
        rec = item.get("recommended")
        rec_list = [rec] if isinstance(rec, str) else [r for r in rec if isinstance(r, str)] if isinstance(rec, list) else []
        recommended = [k for k in (r.strip() for r in rec_list) if k in keys]
        labels = {o["key"]: o["label"] for o in options}
        ident = _s(item.get("id"), 60) or f"q{n}"
        title = _s(item.get("title"), 60) or one_line(" ".join(question.split()[:8]), 60)
        evidence_raw = item.get("evidence")
        evidence = [e for e in (_s(x, 120) for x in evidence_raw) if e][:8] if isinstance(evidence_raw, list) else []
        return Found(
            adapter=self.name, file=path, anchor=ident, line=line, title=title, question=question,
            options=options, recommended=recommended,
            recommended_text=" + ".join(labels[k] for k in recommended), starred=bool(recommended),
            rationale=_s(item.get("rationale"), 200), evidence=evidence, blocking=item.get("blocking") is True,
            blocking_reason=_s(item.get("blocking_reason"), 200), hash=digest(canon), marker=None, unparsed=False,
        )

    # -- sibling answers ------------------------------------------------------------------------

    def _apply_answers(self, path: str, found: list[Found], files: FileReader) -> None:
        if not found or all(f.unparsed for f in found):
            return
        raw = files.read(sibling_answers_path(path))
        if raw is None:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        rows = data.get("answers") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return
        by_id = {str(r.get("id")): r for r in rows if isinstance(r, dict) and r.get("id") is not None}
        for f in found:
            row = by_id.get(f.anchor)
            if f.unparsed or row is None or row.get("status") not in ("decided", "deferred"):
                continue
            opt = str(row.get("option") or "")
            text = _s(row.get("other"), 200) or _s(row.get("label"), 200) or opt
            if row.get("recommended") is True or opt == "recommended":
                text = "recommended"  # new rows carry the concrete label plus recommended:true; old ones say "recommended"
            f.marker = {"state": row["status"], "who": _s(row.get("by"), 60) or "unknown",
                        "date": _s(row.get("date"), 20), "text": text}


def _answer_row(anchor: str, qid: str, ans: dict, date: str) -> dict:
    defer = ans.get("option") == "defer"
    if defer:
        label = ""
    else:
        label = ans.get("label") or ans.get("other") or ""
    row = {"id": anchor, "qid": qid, "status": "deferred" if defer else "decided", "option": ans.get("option", ""),
            "label": label, "other": ans.get("other", ""), "note": ans.get("note", ""), "by": owner.name(),
            "date": ans.get("date") or date}
    if ans.get("recommended") and not defer:
        row["recommended"] = True
    return row


def _options(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    options: list[dict] = []
    for i, o in enumerate(raw[:_MAX_OPTIONS]):
        auto = chr(ord("a") + i)
        if isinstance(o, str):
            label = one_line(o, 200)
            if label:
                options.append({"key": auto, "label": label, "detail": ""})
        elif isinstance(o, dict):
            label = _s(o.get("label"), 200)
            if not label:
                continue
            options.append({"key": _s(o.get("key"), 20) or auto, "label": label, "detail": _s(o.get("detail"), 300)})
    seen: set[str] = set()
    for o in options:  # keys are unique: a repeated key falls back to the next free letter
        if o["key"] in seen:
            o["key"] = next(chr(ord("a") + j) for j in range(_MAX_OPTIONS) if chr(ord("a") + j) not in seen)
        seen.add(o["key"])
    return options
