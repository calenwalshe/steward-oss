"""Quick task 20261002: every status-pane row carries a stable [n]; a numbered series of staged jobs nests under one
group row. Fakes only."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from test_dash_polish import _running  # noqa: F401
from test_play_staged import _plain, _staged
from test_status_pane_features import _pane

from steward import dashrender
from steward.models import JobState
from steward.store import Store


def _titled(store, state, title, minutes):
    job = _staged(store, state, age=timedelta(minutes=minutes), goal=title)
    store.kv_set(job.job_id, "name", title)
    return job


def _v3(store):
    """The V3 shape: one running (M6), one needing the owner (M7), the rest staged, created in one burst."""
    jobs = {}
    for i, t in enumerate(["M6", "M7", "N1", "N2", "G1", "G2"]):
        state = {"M6": JobState.RUNNING, "M7": JobState.NEEDS_HUMAN}.get(t, JobState.CONTRACTED)
        jobs[t] = _titled(store, state, f"V3 {t} do the {t} thing", 60 - i)
    return jobs


def _key(line):
    m = re.match(r"\s*\[(\d+)\]", line)
    return int(m.group(1)) if m else None


def test_series_key():
    assert dashrender._series_key("V3 M6 recipe pages") == ("V3", "M6")
    assert dashrender._series_key("Recipe book Job 2") == ("Recipe book", "2")
    assert dashrender._series_key("Add 3 tests for parser") is None
    assert dashrender._series_key("Fix the login bug") is None


def test_every_row_is_numbered_even_without_a_tab_or_in_staged(steward_home, tmp_path):
    store = Store(steward_home)
    other = _running(store, goal="Other workspace job")
    store.kv_delete(other.job_id, "herdr_tab")  # lives in another workspace: no tab here
    staged = _staged(store, JobState.CONTRACTED, goal="Lone staged")
    out, cmap = _pane(steward_home, tmp_path, 100)
    plain = _plain(out)
    rows = [ln for ln in plain if ("RUNNING" in ln or "staged · ready" in ln)]
    assert len(rows) == 2 and all(_key(r) is not None for r in rows)
    nums = [_key(r) for r in rows]
    assert nums == sorted(set(nums)) and min(nums) >= 3  # 1 and 2 stay with the pinned ops / steward tabs
    # no tab: the digit unfolds / explains the row instead of focusing a tab
    assert f"key {nums[0]} @explain_more:{staged.job_id}" in cmap  # the staged row comes first
    assert f"key {nums[1]} @fold:{other.job_id}" in cmap
    assert any(c.startswith("num ") and c.endswith(f"@explain_more:{staged.job_id}") for c in cmap)


def test_numbers_above_nine_allowed_and_only_digits_bind(steward_home, tmp_path):
    store = Store(steward_home)
    for i in range(10):
        _staged(store, JobState.CONTRACTED, goal=f"Lone {chr(97 + i)} thing")
    out, cmap = _pane(steward_home, tmp_path, 100)
    nums = [_key(ln) for ln in _plain(out) if _key(ln)]
    assert max(nums) == 12 and nums == list(range(3, 13))
    assert sorted(int(c.split()[1]) for c in cmap if c.startswith("key ")) == list(range(3, 10))


def test_implicit_prefix_group_from_v3_shape(steward_home, tmp_path):
    store = Store(steward_home)
    _v3(store)
    out, cmap = _pane(steward_home, tmp_path, 120)
    plain = _plain(out)
    head = next(ln for ln in plain if "V3 · 6 jobs" in ln)
    assert "1 running (M6)" in head and "1 needs you (M7)" in head and "4 staged" in head and _key(head) is not None
    assert not any(ln.startswith("staged (") for ln in plain)  # nothing left for the flat staged block
    assert not any("N1 do the N1 thing" in ln for ln in plain)  # folded by default above two members
    assert any(c.startswith("expand ") and "grp:imp:" in c for c in cmap)


def test_implicit_group_unfolds_in_creation_order_with_play(steward_home, tmp_path):
    store = Store(steward_home)
    jobs = _v3(store)
    root = "grp:imp:" + jobs["M6"].job_id
    out, cmap = _pane(steward_home, tmp_path, 120, expanded=[root])
    plain = _plain(out)
    order = [next(i for i, ln in enumerate(plain) if f"v3 {t.lower()}" in ln.lower()) for t in ("M6", "M7", "N1", "N2", "G1", "G2")]
    assert order == sorted(order)
    staged_rows = [ln for ln in plain if "staged · ready" in ln]
    assert len(staged_rows) == 4 and all(ln.startswith("  [") for ln in staged_rows)  # indented under the group
    plays = [c for c in cmap if c.startswith("btn") and " play " in c]
    assert len(plays) == 4 and {c.split()[5] for c in plays} == {jobs[t].job_id for t in ("N1", "N2", "G1", "G2")}
    nums = [_key(ln) for ln in plain if _key(ln)]
    assert nums == sorted(set(nums))


def test_group_stands_where_the_running_member_would(steward_home, tmp_path):
    store = Store(steward_home)
    _v3(store)
    lone = _running(store, goal="Unrelated later job")
    with store._conn:
        store._conn.execute("UPDATE jobs SET created_at = ? WHERE job_id = ?",
                            ((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), lone.job_id))
    out, _ = _pane(steward_home, tmp_path, 120)
    plain = _plain(out)
    g = next(i for i, ln in enumerate(plain) if "V3 · 6 jobs" in ln)
    u = next(i for i, ln in enumerate(plain) if "unrelated" in ln.lower() and "RUNNING" in ln)
    assert g < u


def test_member_blocker_rolls_up_on_the_folded_group(steward_home, tmp_path):
    from steward import approvals

    store = Store(steward_home)
    jobs = _v3(store)
    approvals.open_approval(store, jobs["M6"].job_id, "worker permission: Bash {'command': 'rm -rf /x'}", kind="permission")
    out, _ = _pane(steward_home, tmp_path, 120)
    plain = _plain(out)
    g = next(i for i, ln in enumerate(plain) if "V3 · 6 jobs" in ln)
    assert any(ln.lstrip().startswith("→ ") and "m6" in ln.lower() for ln in plain[g + 1:g + 4])


def test_burst_and_distinct_ids_keep_unrelated_jobs_flat(steward_home, tmp_path):
    store = Store(steward_home)
    _titled(store, JobState.CONTRACTED, "Add 3 tests for parser", 5)
    _titled(store, JobState.CONTRACTED, "Add 3 tests for lexer", 4)
    out, _ = _pane(steward_home, tmp_path, 100)
    plain = _plain(out)
    assert any(ln.startswith("staged (2)") for ln in plain) and not any("jobs ·" in ln for ln in plain)


def test_staged_block_shrinks_to_ungrouped(steward_home, tmp_path):
    store = Store(steward_home)
    _v3(store)
    _staged(store, JobState.CONTRACTED, goal="Lone staged thing")
    out, _ = _pane(steward_home, tmp_path, 120)
    assert any(ln.startswith("staged (1)") for ln in _plain(out))


def test_group_digit_key_action_and_tabless_goto():
    from steward import dashact

    assert dashact.argv_for("group", "imp:abc")[-1] == "grp:imp:abc"
    assert dashact.argv_for("explain_more", "J1")[-2:] == ["J1", "--chat"]
