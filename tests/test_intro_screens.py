"""Phase 19-07 (G8): the pure screens. Layout is data: every page is a list of lines for a given size."""

from __future__ import annotations

import itertools
import re

import pytest

from steward import cockpit, components, intro, prereq, scaffold
from steward.jobview import vlen
from test_scaffold import checks as required_checks
from test_scaffold import det

ANSI = re.compile(r"\x1b\[[0-9;]*m")
GEOMETRIES = [(80, 24), (40, 30)]
REQUIRED_IDS = [c.id for c in required_checks()]


def worst_optional() -> list[prereq.Check]:
    """Every optional item off, each with a fix: the most rows the page can ever be asked to draw."""
    out = []
    for id_, label, level, _fn in prereq.CHECKS:
        if level != prereq.OPTIONAL:
            continue
        fix = (prereq.Fix("local", "add the key to keys.env", "keys_env") if id_ == "jev"
               else prereq.Fix("command", f"install the thing for {id_}"))
        out.append(prereq.Check(id_, label, level, prereq.OFF, f"off (no {id_})", fix))
    return out


def all_checks(missing=()) -> list[prereq.Check]:
    return required_checks(missing=missing) + worst_optional()


def plain(lines) -> list[str]:
    return [ANSI.sub("", ln) for ln in lines]


def screen(missing=(), cols=80, rows=24, color=False, **kw):
    chk = all_checks(missing)
    d = det(checks=chk)
    actions = scaffold.plan_scaffold(d)
    return intro.welcome_screen(chk, actions, cols, rows, color, version="0.1.0", **kw)


def test_welcome_screen_contents():
    lines = screen()
    text = "\n".join(lines)
    assert lines[0] == "Steward 0.1.0"
    for what in intro.WHAT_IS:
        assert what in lines
    assert len(intro.WHAT_IS) == 4
    for c in required_checks():
        assert any(ln.startswith("✓ ") and c.label in ln for ln in lines), c.label
    assert "+8 optional off, press o" in lines
    assert "will create:" in lines
    plan = scaffold.render_plan(scaffold.plan_scaffold(det(checks=all_checks()))).splitlines()[1:]
    for step in plan:
        assert any(step[:30] in ln for ln in lines), step
    assert lines[-1] == "Enter scaffold  f fixes  o optional  q quit"
    assert "Sam" not in text


def test_geometry_80x24_and_40x30():
    for cols, rows in GEOMETRIES:
        for mask in itertools.product((False, True), repeat=len(REQUIRED_IDS)):
            missing = tuple(i for i, bad in zip(REQUIRED_IDS, mask) if bad)
            for color in (False, True):
                lines = screen(missing, cols, rows, color, offered=[("keys_env", "x")], login="claude" in missing)
                assert len(lines) <= rows, (cols, rows, missing, len(lines))
                assert all(vlen(ln) <= cols for ln in lines), (cols, missing, [ln for ln in lines if vlen(ln) > cols])
                assert "q quit" in plain(lines)[-1], (cols, missing, lines[-1])
                assert "\n" not in "".join(lines)


def test_geometry_holds_for_a_long_upgrade_plan():
    chk = all_checks()
    d = det(checks=chk, install={"version": "0.9.0"}, version="1.0.0")
    d.config_schema = 0
    d.dead = {"control": "p1", "advisor": "p2"}
    d.jobs_running = 2
    actions = scaffold.plan_scaffold(d)
    assert len(actions) >= 9
    for cols, rows in GEOMETRIES:
        lines = intro.welcome_screen(chk, actions, cols, rows, False, version="1.0.0",
                                     notes=["created keys.env", "something else to read"], offered=[("keys_env", "k")])
        assert len(lines) <= rows and all(vlen(ln) <= cols for ln in lines)
        assert "q quit" in lines[-1]


def test_optional_rows_collapse_when_rows_would_overflow():
    short = screen(cols=80, rows=24)
    assert sum(1 for ln in short if ln.startswith("– ")) == 0
    assert "+8 optional off, press o" in short
    roomy = screen(cols=80, rows=60)
    assert sum(1 for ln in roomy if ln.startswith("– ")) == 8
    assert not any("optional off" in ln for ln in roomy)


def test_marks_and_fixes_beside_each_row():
    lines = screen(missing=("claude", "git"), cols=80, rows=60)
    row = next(ln for ln in lines if "claude CLI" in ln)
    assert row.startswith("✗ ") and "→ install claude" in row
    jev = next(ln for ln in lines if "Jev" in ln)
    assert jev.startswith("– ") and "→ [f] add the key to keys.env" in jev
    cmd = next(ln for ln in lines if "codex" in ln)
    assert "→ install the thing for codex" in cmd and "[f]" not in cmd

    narrow = screen(missing=tuple(REQUIRED_IDS), cols=40, rows=60)
    rows = [ln for ln in narrow if ln.startswith("✗ ")]
    assert len(rows) == 5
    for ln in rows:
        assert vlen(ln) <= 40
        assert "→" in ln  # the fix survives at the phone width
    chk = [prereq.Check("claude", "claude CLI", prereq.REQUIRED, prereq.MISSING, "not logged in",
                        prereq.Fix("command", "claude auth login with a rather long explanation attached"))]
    lines = intro.welcome_screen(chk, [scaffold.Action("refuse", "refuse", "x", {"reason": "prerequisites"})], 40, 30,
                                 False)
    cut = next(ln for ln in lines if ln.startswith("✗ "))
    assert cut.endswith("…") and vlen(cut) <= 40
    body = cut[:-1].rstrip()
    assert body.split()[-1] in "✗ claude CLI → claude auth login with a rather long explanation attached".split()


def test_required_missing_says_fix_those_first():
    lines = screen(missing=("git",))
    assert intro.REQUIRED_FIRST in lines
    assert "will create:" not in lines


def test_keys_line_shrinks_and_shows_login_only_when_offered():
    assert intro.keys_line(80) == "Enter scaffold  f fixes  o optional  q quit"
    assert "l login" not in intro.keys_line(80)
    assert "l login" in intro.keys_line(80, login=True) and "r recheck" in intro.keys_line(80, login=True)
    for cols in (40, 30):
        for login in (False, True):
            line = intro.keys_line(cols, login)
            assert len(line) <= cols and line.endswith("q quit")


def test_done_screen():
    result = {"errors": [], "deferred": ["control"], "cockpit": {"operator_pane": "w1:p9"},
              "lines": ["a newer template was written beside your file: steward.toml.new (compare, then merge "
                        "what you want)", "control pane started: w1:p1"]}
    for cols, rows in GEOMETRIES:
        lines = intro.done_screen(result, "/home/sam/projects/acme", cols, rows, False)
        text = "\n".join(lines)
        assert len(lines) <= rows and all(vlen(ln) <= cols for ln in lines)
        assert "operator pane" in text and "Run Steward on acme: <goal>" in text
        assert "`cockpit` tab" in text
        assert "steward.toml.new" in text
        assert "restart deferred: control" in text
    noop = intro.done_screen({"errors": [], "deferred": [],
                              "cockpit": {"operator_note": "operator skipped (--no-operator)"}}, "/x/acme", 80, 24, False)
    assert any("operator skipped" in ln for ln in noop) and any("steward cockpit" in ln for ln in noop)
    failed = intro.done_screen({"errors": ["dirs: PermissionError"]}, "/x/acme", 40, 30, False)
    assert any("error: dirs" in ln for ln in failed) and any("nothing was recorded" in ln for ln in failed)


def test_no_color_mode():
    plain_lines = screen(color=False)
    assert not any("\x1b" in ln for ln in plain_lines)
    colored = screen(color=True)
    assert any("\x1b[" in ln for ln in colored)
    assert plain(colored) == plain_lines
    assert not any("\x1b" in ln for ln in intro.done_screen({"errors": [], "cockpit": {"operator_pane": "p"}}, "/x/y", 80,
                                                           24, False))


def test_optional_page_fits_both_geometries():
    rows_ = components.status(None)
    for cols, rows in GEOMETRIES:
        lines = intro.optional_screen(rows_, cols, rows, False, notes=["one", "two", "three", "four"])
        assert len(lines) <= rows and all(vlen(ln) <= cols for ln in lines)
        assert sum(1 for ln in lines if re.match(r"\d ", ln)) == 8
        assert "off (not enabled)" in lines[1] and lines[-1].startswith("1-8 select")
    on = intro.optional_screen([("mcp", True, "on (systemd)"), *rows_[1:]], 80, 24, False)
    assert on[1].startswith("1 ✓ mcp") and on[2].startswith("2 – tunnel")


def test_component_page_fits_both_geometries():
    plan_lines = [f"  writes    /a/very/long/path/number/{n}/that/goes/on/and/on/for/a/while" for n in range(40)]
    for cols, rows in GEOMETRIES:
        lines = intro.component_screen("tunnel", plan_lines, cols, rows, False)
        assert len(lines) <= rows and all(vlen(ln) <= cols for ln in lines)
        assert lines[-1].startswith("y enable") and any("more lines" in ln for ln in lines)
    blocked = intro.component_screen("android", ["BLOCKED: needs mcp"], 80, 24, False, can_enable=False)
    assert blocked[-1] == "b back  q quit"


def test_whatsnew_page_fits_both_geometries_and_falls_back():
    assert len(intro.WHATS_NEW[0]) == 2
    for version in ("0.3.0", "1.0.0", "7.2.1"):
        for cols, rows in GEOMETRIES:
            lines = intro.whatsnew_screen("0.1.0", version, cols, rows, False)
            assert len(lines) <= min(rows, 10 + 4) and all(vlen(ln) <= cols for ln in lines)
            assert lines[0] == f"What's new in Steward {version}" and lines[-1].startswith("Enter continue")
    generic = "\n".join(intro.whatsnew_screen("0.9.0", "1.0.0", 80, 24, False))
    assert "replaces 0.9.0" in generic
    assert "first packaged release" in "\n".join(intro.whatsnew_screen("0.1.0", "0.2.0", 80, 24, False))
    assert len(intro.whats_new_lines("0.9.0", "1.0.0")) <= intro.MAX_WHATS_NEW
