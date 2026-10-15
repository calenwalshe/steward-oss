"""Phase 19-03 (G15): the line-based TOML editor."""

from __future__ import annotations

import tomllib

import pytest

from steward import tomledit as te

DOC = """# header comment
schema = 1   # the schema
acceptance = "human"

[owner]
name = "Sam"  # who

[jev]
mode = "shadow"
"""


def test_get_set_add():
    assert te.get(DOC, "", "schema") == 1
    assert te.get(DOC, "owner", "name") == "Sam"
    assert te.get(DOC, "owner", "missing", "dflt") == "dflt"
    assert te.get(DOC, "nope", "x") is None

    top = te.set_key(DOC, "", "schema", 2)
    assert top.splitlines()[1] == "schema = 2   # the schema"
    assert top.replace("schema = 2", "schema = 1") == DOC

    named = te.set_key(DOC, "owner", "name", "Priya")
    assert 'name = "Priya"  # who' in named and tomllib.loads(named)["owner"]["name"] == "Priya"

    added = te.set_key(DOC, "jev", "level", "high")
    assert added.endswith('[jev]\nmode = "shadow"\nlevel = "high"\n')
    assert added.startswith(DOC.rstrip("\n"))

    mid = te.set_key(DOC, "owner", "pronouns", "they")
    assert 'name = "Sam"  # who\npronouns = "they"\n\n[jev]' in mid

    table = te.set_key(DOC, "alerts", "telegram", True)
    assert table.endswith("\n[alerts]\ntelegram = true\n") and tomllib.loads(table)["alerts"]["telegram"] is True
    assert te.set_key(DOC, "", "flag", False).count("flag = false") == 1
    assert tomllib.loads(te.set_key(DOC, "", "flag", False))["flag"] is False
    assert tomllib.loads(te.set_key(DOC, "jev", "marks", [1, 2]))["jev"]["marks"] == [1, 2]
    assert te.set_key("", "a", "b", 1) == "[a]\nb = 1\n"
    assert te.set_key("[a]\nx = 1", "a", "y", 2) == "[a]\nx = 1\ny = 2\n"


def test_other_lines_are_byte_identical():
    out = te.set_key(DOC, "owner", "name", "Priya")
    before, after = DOC.splitlines(keepends=True), out.splitlines(keepends=True)
    assert len(before) == len(after)
    assert [a for a, b in zip(before, after) if a != b] == ['name = "Sam"  # who\n']


def test_comment_with_hash_in_a_string_is_kept_apart():
    text = 'label = "a # b"  # real comment\n'
    assert te.set_key(text, "", "label", "z") == 'label = "z"  # real comment\n'


def test_a_breaking_edit_raises_and_returns_nothing():
    with pytest.raises(te.TomlEditError):
        te.set_key("jev = 1\n", "jev", "mode", "x")  # [jev] would redefine the key
    with pytest.raises(te.TomlEditError):
        te.set_key(DOC, "owner", "bad key", 1)
    with pytest.raises(te.TomlEditError):
        te.set_key(DOC, "owner", "k", {"a": 1})
    with pytest.raises(te.TomlEditError):
        te.set_key(DOC, "bad table", "k", 1)
    with pytest.raises(te.TomlEditError):
        te.set_key(DOC, "", "k", float("nan"))


def test_rename_key_preserves_value_and_comments():
    out = te.rename_key(DOC, "owner", "name", "display")
    assert 'display = "Sam"  # who' in out and te.get(out, "owner", "name") is None
    assert te.get(out, "owner", "display") == "Sam"
    assert te.rename_key(DOC, "owner", "absent", "other") == DOC
    with pytest.raises(te.TomlEditError):
        te.rename_key("[t]\na = 1\nb = 2\n", "t", "a", "b")
