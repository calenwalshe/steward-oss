"""Job tab titles: derivation, glyph precedence, info priority, the 24-cell cap, adopting a manual rename."""

import pytest

from steward import tabnames
from steward.jobview import vlen
from steward.store import Store

GOALS = [
    ('Build Steward Phase 18 "ETA" so every job shows a finish time', "P18 ETA"),
    ('Start Phase 19 "Installable package" for Steward', "P19 Installable"),
    ("Use gsd-quick to add a QUICK STEER button to the Steward status pane", "Quick steer"),
    ("make Steward job runners restart themselves on crash", "job runners"),
    ("Use gsd-quick to fix the lost-approval hang found on job 8a6", "lost-approval"),
    ('Rename the "Tab Tidy" thing', "Tab Tidy"),
    ("", "job"),
]


@pytest.mark.parametrize("goal,want", GOALS)
def test_derive_name(goal, want):
    assert tabnames.derive_name(goal) == want


def test_derive_name_follow_up_uses_the_parent_name():
    assert tabnames.derive_name("Follow-up of 5217a365: redo it", "P18 ETA") == "↳ P18 ETA"
    assert tabnames.derive_name("Follow-up of 5217a365: redo it") == "↳ 5217a365"


def test_derived_names_never_exceed_the_cap():
    assert vlen(tabnames.derive_name("Implementing extraordinarilylongwordwithoutanybreaksatall now")) <= tabnames.NAME_CAP


def test_job_name_is_stored_and_collisions_get_a_number(steward_home):
    store = Store(steward_home)
    a = store.create_job('Build Phase 18 "ETA"', "/r")
    b = store.create_job('Again Phase 18 "ETA"', "/r")
    assert tabnames.job_name(store, a) == "P18 ETA"
    assert store.kv_get(a.job_id, "short_name") == "P18 ETA"
    assert tabnames.job_name(store, b) == "P18 ETA 2"
    assert tabnames.job_name(store, a) == "P18 ETA"  # stable once stored


def test_job_name_persist_false_writes_nothing(steward_home):
    store = Store(steward_home)
    a = store.create_job('Build Phase 18 "ETA"', "/r")
    assert tabnames.job_name(store, a, persist=False) == "P18 ETA"
    assert store.kv_get(a.job_id, "short_name") is None


def test_job_name_follow_up_inherits_parent(steward_home):
    store = Store(steward_home)
    parent = store.create_job('Build Phase 18 "ETA"', "/r")
    tabnames.job_name(store, parent)
    child = store.create_job(f"Follow-up of {parent.job_id[:8]}: more", "/r")
    assert tabnames.job_name(store, child) == "↳ P18 ETA"


@pytest.mark.parametrize("args,glyph", [
    (dict(state="NEEDS_HUMAN", kind="waiting", reason="accept or reject? x"), "◆"),
    (dict(state="RUNNING", kind="live", reason="answer 3 questions (1 ★)"), "◆"),      # questions beat running
    (dict(state="RUNNING", kind="quiet"), "⚠"),
    (dict(state="RUNNING", kind="live", eta={"overdue_min": 12}), "⚠"),                # overdue ETA is stuck
    (dict(state="RUNNING", kind="live"), "●"),
    (dict(state="PAUSED", kind="paused"), "⏸"),
    (dict(state="ACCEPTED", kind="done"), "✓"),
    (dict(state="FAILED", kind="bad"), "■"),
])
def test_glyph_precedence(args, glyph):
    assert tabnames.status_glyph(**args) == glyph


@pytest.mark.parametrize("kw,want", [
    (dict(waiting=3, reason="answer 3 questions", eta={"p50_min": 100}, kind="live"), "?3"),
    (dict(reason="accept or reject? checks passed"), "ok?"),
    (dict(reason="approve? Bash(ls)"), "ok?"),
    (dict(eta={"p50_min": 100}, kind="live"), "~1h40"),
    (dict(eta={"p50_min": 47}, kind="live"), "~45m"),
    (dict(eta={"low_h": 2, "high_h": 5}, kind="live"), "2–5h"),
    (dict(eta={"overdue_min": 12, "p50_min": 1}, kind="quiet"), "+12m"),
    (dict(kind="live", units="3/7"), "3/7"),
    (dict(kind="quiet", quiet_age="14m"), "14m"),
    (dict(kind="paused"), ""),
    (dict(kind="live"), ""),
])
def test_info_priority(kw, want):
    assert tabnames.info_text(**kw) == want


def test_label_is_capped_at_24_cells_and_drops_info_first():
    assert tabnames.tab_label("⚠", "Installable package", "~1h40") == "⚠ Installable… ~1h40"
    worst = tabnames.tab_label("◆", "x" * 40, "~1h40")
    assert vlen(worst) + 1 <= 24  # glyph budgeted as two cells
    assert tabnames.tab_label("●", "P18 ETA") == "● P18 ETA"
    assert tabnames.tab_label("◆", "P19 Installable", "~2h") == "◆ P19 Installable ~2h"  # 2+1+15+1+3 = 22
    assert tabnames.tab_label("◆", "Installable pkgs", "~1h40") == "◆ Installable pkgs"   # 25 cells: info dropped
    assert tabnames.tab_label("◆", "P19 Installable", "~1h40") == "◆ P19 Installable ~1h40"  # exactly 24: kept


@pytest.mark.parametrize("glyph,name,info", [
    ("●", "P18 ETA", ""), ("◆", "P18 ETA", "?3"), ("●", "P19 Installable", "~1h40"), ("⚠", "job runners", "14m"),
    ("◆", "Android fix", "ok?"), ("●", "QUICK STEER", "2–5h"), ("⚠", "P18 ETA", "+12m"), ("✓", "lost-approval", ""),
])
def test_parse_label_round_trips(glyph, name, info):
    assert tabnames.parse_label(tabnames.tab_label(glyph, name, info)) == name


def test_parse_label_adopts_a_hand_typed_name():
    assert tabnames.parse_label("my own name") == "my own name"
    assert tabnames.parse_label("● Android fix") == "Android fix"


def test_name_comes_from_the_first_sentence_only():
    # Regression (live tidy, 2026-09-30): names like 'FINISH LINE' and another job's quoted 'P18 ETA' came from later text.
    from steward.tabnames import derive_name
    assert derive_name('Use gsd-quick to implement the new Steward Herdr tab design exactly as written in /x.md '
                       '(read it first). Title like "P18 ETA". FINISH LINE: done') == "Herdr tab design"
    assert derive_name("Use gsd-quick to fix the autopilot merge-block issues. FINISH LINE: all five") != "FINISH LINE"


@pytest.mark.parametrize("goal,want", [
    ("Extend the AI career-sites comparative brief", "Extend AI"),
    ("Extend the", "Extend"),
    ("Tidy the notes of the", "Tidy notes"),
    ("Refactor the parser for", "Refactor parser"),
])
def test_derived_name_never_ends_on_a_stopword(goal, want):
    got = tabnames.derive_name(goal)
    assert got == want and vlen(got) <= tabnames.NAME_CAP
    assert got.split()[-1].lower() not in tabnames._STOPWORDS
