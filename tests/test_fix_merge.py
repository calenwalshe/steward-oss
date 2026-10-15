"""`[ fix merge ]`: shown only for a conflicting accepted job with no fix-up; tap starts + links one; manual follow-ups link."""

import re

from steward import autopilot, cli, dashact, incorporate
from steward.models import JobState
from steward.store import Store
from test_autopilot import Followups, git, mkjob, runner, scratch, whats  # noqa: F401
from test_autopilot_fixup import FakeHandlers, _child_branch, _row, conflicted  # noqa: F401
from test_dash_polish import _render, world  # noqa: F401


def _conflict_on(scratch, name):
    (scratch / name).write_text("main\n")
    git(scratch, "add", "-A")
    git(scratch, "commit", "-qm", f"main adds {name}")


def test_needed_only_for_conflicting_accepted_job_without_fixup(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    clean = mkjob(store, scratch, tmp_path, {"clean.txt": "x\n"})
    assert not autopilot.fix_merge_needed(store, clean.job_id)
    job = conflicted(store, scratch, tmp_path)
    assert autopilot.fix_merge_needed(store, job.job_id)
    child = store.create_job("fix", str(scratch))
    store.append_event(job.job_id, what="auto_followup_started", evidence={"follow_up": child.job_id, "attempt": 1},
                       rule="autopilot_merge", judgment=autopilot.Judgment.code)
    assert not autopilot.fix_merge_needed(store, job.job_id)


def test_tap_starts_and_links_a_fixup(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    fu = Followups(store)
    job = conflicted(store, scratch, tmp_path)
    res = autopilot.start_fix_merge(store, job.job_id, runner(), start_followup=fu)
    assert res["accepted"] and res["follow_up"] == fu.calls[0][2]
    assert "into YOUR branch" in fu.calls[0][1] and "- a.txt:" in fu.calls[0][1]
    assert store.kv_get(res["follow_up"], "autopilot_followup") == job.job_id
    assert autopilot.followup_status(store, job.job_id) == {"kind": "running", "child": res["follow_up"], "n": 1, "max": 2} or \
        autopilot.followup_status(store, job.job_id)["kind"] in ("running", "not_started")
    again = autopilot.start_fix_merge(store, job.job_id, runner(), start_followup=fu)
    assert not again["accepted"] and len(fu.calls) == 1  # one at a time
    assert not autopilot.start_fix_merge(store, mkjob(store, scratch, tmp_path, {"ok.txt": "x\n"}).job_id, runner(),
                                         start_followup=fu)["accepted"]


def test_tap_uses_default_followup_and_launches(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    fake = FakeHandlers(store)
    monkeypatch.setattr(cli, "_build_control_handlers", fake)
    job = conflicted(store, scratch, tmp_path)
    res = autopilot.start_fix_merge(store, job.job_id, runner())
    assert res["accepted"] and [a["job_id"] for a in fake.launched] == [res["follow_up"]]
    assert autopilot.followup_status(store, job.job_id) == {"kind": "running", "child": res["follow_up"], "n": 1, "max": 2}


def test_parent_released_when_fixup_merges(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    parent = conflicted(store, scratch, tmp_path)
    child = _child_branch(store, scratch, tmp_path, parent)
    store.kv_set(child.job_id, "autopilot_followup", parent.job_id)
    res = autopilot.try_merge(store, child.job_id, runner(), checks=["pytest -q"])
    assert res["outcome"] == "merged"
    assert store.get_job(parent.job_id).state == JobState.RELEASED
    assert incorporate.is_incorporated(store, parent.job_id) == res["merge_commit"]


def test_manual_followup_of_unmerged_accepted_parent_links(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    parent = conflicted(store, scratch, tmp_path)
    res = cli._build_control_handlers(store)["new"]({"args": {"goal": "tidy", "follow_up": parent.job_id,
                                                              "repo": str(scratch), "no_baseline": True}})
    child = res["job_id"]
    assert store.kv_get(child, "autopilot_followup") == parent.job_id
    assert autopilot.followup_status(store, parent.job_id)["child"] == child
    assert whats(store, parent).count("auto_followup_started") == 1


def test_manual_followup_of_merged_parent_is_not_linked(steward_home, scratch, tmp_path, monkeypatch):
    store = Store(steward_home)
    parent = mkjob(store, scratch, tmp_path, {"m.txt": "x\n"})
    store.kv_set(parent.job_id, "incorporated_commit", "abc1234")
    res = cli._build_control_handlers(store)["new"]({"args": {"goal": "more", "follow_up": parent.job_id,
                                                              "repo": str(scratch), "no_baseline": True}})
    assert store.kv_get(res["job_id"], "autopilot_followup") is None


def test_pane_shows_button_row_and_panel(world, steward_home, tmp_path, scratch):
    store, _, done = world
    parent = done[2]
    row = _row(_render(steward_home, tmp_path, 120)[0], parent)
    assert "[ incorporate ]" in row and "fix merge" not in row  # no conflict yet
    _conflict_on(scratch, "f2.txt")
    lines, cmap = _render(steward_home, tmp_path, 120)
    row = _row(lines, parent)
    assert re.search(r"\[ fix merge \]  \[ release \]", row) and "[ incorporate ]" not in row
    assert any(re.match(rf"btn \d+ \d+ \d+ fix_merge {parent.job_id} -", c) for c in cmap)
    lines, cmap = _render(steward_home, tmp_path, 120, (f"inc:{parent.job_id}",))
    assert any("[ fix merge ]" in ln and "[ cancel ]" in ln for ln in lines) and not any("merge into" in ln for ln in lines)


def test_pane_row_text_once_fixup_started(world, steward_home, tmp_path, scratch):
    store, _, done = world
    parent = done[2]
    _conflict_on(scratch, "f2.txt")
    res = autopilot.start_fix_merge(store, parent.job_id, runner(), start_followup=_Launched(store))
    row = next(ln for ln in _render(steward_home, tmp_path, 120)[0] if re.match(rf"\S+ ✓ {parent.job_id[:8]} ", ln))
    assert f"merge blocked · fix-up {res['follow_up'][:8]} running (1/2)" in row and "fix merge" not in row


class _Launched(Followups):
    def __call__(self, store, parent_id, goal):
        res = super().__call__(store, parent_id, goal)
        store.kv_set(res["job_id"], "herdr_tab", "w1:t9")
        return res


def test_cli_and_action_wiring(steward_home, scratch, tmp_path, monkeypatch, capsys):
    assert dashact.argv_for("fix_merge", "J")[-2:] == ["fix-merge", "J"]
    store = Store(steward_home)
    monkeypatch.setattr(cli, "_build_control_handlers", FakeHandlers(store))
    job = conflicted(store, scratch, tmp_path)
    assert cli.main(["fix-merge", job.job_id[:8]]) == 0
    assert "fix-up" in capsys.readouterr().out
    assert cli.main(["fix-merge", job.job_id]) == 2  # already one in flight


def test_fix_merge_claim_blocks_duplicate_starts(steward_home):
    # Regression (live 2026-10-01): three [ fix merge ] taps plus autopilot started four fix-ups for 88ad60fb.
    from steward import autopilot
    from steward.store import Store
    store = Store(steward_home)
    jid = store.create_job("x", str(steward_home)).job_id
    assert autopilot._claim_fixup(store, jid) is True
    assert autopilot._claim_fixup(store, jid) is False and autopilot.fixup_claimed(store, jid)
    assert not autopilot.fixup_claimed(store, jid, now=__import__("time").time() + autopilot.FIXUP_CLAIM_TTL_S + 1)
