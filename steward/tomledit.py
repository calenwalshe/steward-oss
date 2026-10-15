"""A small line-based TOML editor (Phase 19, G15).

No third-party TOML writer exists and none may be added, so config edits are made on the text itself: a key is
replaced in place (its trailing comment kept), a missing key is appended inside its table, a missing table is
appended at the end, and every other line is byte-identical. The result is always parsed with `tomllib`; an edit
that would break the file raises `TomlEditError` and nothing is returned.

`table` is `""` for the top level, else the header name as written (`jev`, `alerts.extra`).
"""

from __future__ import annotations

import json
import re
import tomllib

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")
_HEADER = re.compile(r"^\s*\[(?!\[)\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
_ARRAY_HEADER = re.compile(r"^\s*\[\[")
_KEY_LINE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*?)(\r?\n?)$", re.S)


class TomlEditError(ValueError):
    """The requested edit cannot be made safely."""


def _literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            raise TomlEditError("non-finite number")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)) and all(isinstance(v, (str, int, float, bool)) for v in value):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    raise TomlEditError(f"unsupported value type: {type(value).__name__}")


def _check_names(table: str, *keys: str) -> None:
    for key in keys:
        if not _BARE.match(key or ""):
            raise TomlEditError(f"unsupported key name: {key!r}")
    if table and not all(_BARE.match(part) for part in table.split(".")):
        raise TomlEditError(f"unsupported table name: {table!r}")


def _split_comment(rest: str) -> tuple[str, str]:
    """`(value text, comment including its leading spaces)` for the text after `=`."""
    quote = ""
    i = 0
    while i < len(rest):
        ch = rest[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            value = rest[:i]
            stripped = value.rstrip()
            return stripped, value[len(stripped):] + rest[i:]
        i += 1
    return rest.rstrip(), rest[len(rest.rstrip()):]


def _sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """`(table, first line, end line)` per section; the top level is `("", 0, first header)`."""
    heads = []
    for i, line in enumerate(lines):
        if _ARRAY_HEADER.match(line):
            heads.append(("[[array]]", i))
            continue
        m = _HEADER.match(line.rstrip("\r\n"))
        if m:
            heads.append((re.sub(r"\s*\.\s*", ".", m.group(1)), i))
    out = []
    first = heads[0][1] if heads else len(lines)
    out.append(("", 0, first))
    for n, (name, i) in enumerate(heads):
        end = heads[n + 1][1] if n + 1 < len(heads) else len(lines)
        out.append((name, i + 1, end))
    return out


def _find(lines: list[str], table: str, key: str) -> int | None:
    for name, start, end in _sections(lines):
        if name != table:
            continue
        for i in range(start, end):
            m = _KEY_LINE.match(lines[i])
            if m and m.group(2) == key and not lines[i].lstrip().startswith("#"):
                return i
    return None


def _parse(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except ValueError as exc:
        raise TomlEditError(f"the edit would break the file: {exc}") from exc


def get(text: str, table: str, key: str, default=None):
    """The parsed value of `key` in `table` (`""` = top level), else `default`."""
    node = _parse(text)
    for part in (table.split(".") if table else []):
        node = node.get(part) if isinstance(node, dict) else None
    return node.get(key, default) if isinstance(node, dict) else default


def set_key(text: str, table: str, key: str, value: object) -> str:
    """Replace (keeping the trailing comment) or add `key = value` in `table`; returns the new text."""
    _check_names(table, key)
    literal = _literal(value)
    lines = text.splitlines(keepends=True)
    hit = _find(lines, table, key)
    if hit is not None:
        m = _KEY_LINE.match(lines[hit])
        _value, comment = _split_comment(m.group(4))
        lines[hit] = f"{m.group(1)}{key}{m.group(3)}{literal}{comment}{m.group(5) or chr(10)}"
        new = "".join(lines)
    else:
        sections = [s for s in _sections(lines) if s[0] == table]
        if sections:
            _name, start, end = sections[0]
            last = start
            for i in range(start, end):
                if lines[i].strip():
                    last = i + 1
            if last > 0 and last <= len(lines) and lines and not lines[last - 1].endswith("\n"):
                lines[last - 1] += "\n"
            add = f"{key} = {literal}\n"
            if not table and last == 0 and lines:
                add += "\n"  # the file starts with a table header: keep it separated
            lines.insert(last, add)
            new = "".join(lines)
        else:
            base = text if not text or text.endswith("\n") else text + "\n"
            sep = "\n" if base.strip() else ""
            new = f"{base}{sep}[{table}]\n{key} = {literal}\n" if table else f"{key} = {literal}\n{base}"
    parsed = get(new, table, key, default=object())
    if parsed != (list(value) if isinstance(value, tuple) else value):
        raise TomlEditError(f"the edit did not produce {key} = {literal}")
    return new


def rename_key(text: str, table: str, old: str, new_name: str) -> str:
    """Rename `old` to `new_name` in `table`, keeping value and comments. A missing `old` changes nothing."""
    _check_names(table, old, new_name)
    lines = text.splitlines(keepends=True)
    hit = _find(lines, table, old)
    if hit is None:
        return text
    if _find(lines, table, new_name) is not None:
        raise TomlEditError(f"{new_name} already exists in [{table}]")
    m = _KEY_LINE.match(lines[hit])
    lines[hit] = f"{m.group(1)}{new_name}{m.group(3)}{m.group(4)}{m.group(5)}"
    result = "".join(lines)
    _parse(result)
    return result
