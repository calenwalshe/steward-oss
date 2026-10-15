"""STEWARD status pane polish: fit() never cuts a word, no line exceeds the pane, columns align,
finished jobs collapse into one `done` line."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from steward import custom_watchers as cw, dashact, jobview
from steward.jobview import fit, short_model, vlen, wrap
from steward.models import JobState
from steward.store import Store
from test_incorporate import mkjob, scratch  # noqa: F401  (scratch is a fixture)

_REPO = Path(__file__).resolve().parent.parent
ANSI = re.compile(r"\033\[[0-9;]*m")


def test_fit_never_splits_a_word():
    text = "Upgrade Steward itself and build the flagship worker for release"
    for w in range(len(text.split()[0]) + 1, len(text) + 3):  # a lone over-long word is hard-cut, the one exception
        out = fit(text, w)
        assert vlen(out) <= w
        body = out.rstrip("…")
        if out != text:
            assert out.endswith("…")
            words = text.split()
            assert body.split() == words[:len(body.split())], (w, out)  # only whole words survive
    assert fit(text, 200) == text
    assert fit("anything", 0) == ""


def test_fit_is_ansi_aware():
    text = "\033[2mdim words that go on and on\033[0m"
    out = fit(text, 14)
    assert vlen(out) <= 14 and out.endswith("…\033[0m") and ANSI.sub("", out) == "dim words…"
    assert vlen(fit("{SPIN} 20 actions · last 1m ago", 14)) <= 14
    assert vlen("\033[32m{SPIN}\033[0m ab") == 4


def test_wrap_two_lines_ellipsized():
    lines = wrap("one two three four five six seven eight nine ten eleven", 16, 2)
    assert len(lines) == 2 and all(vlen(x) <= 16 for x in lines) and lines[0] == "one two three"
    assert lines[1].endswith("…")
    assert wrap("short goal", 40) == ["short goal"]


def test_short_model_names():
    assert [short_model(m) for m in ("jev-latest", "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5", "mcp")] == \
        ["jev", "sonnet", "opus", "haiku", "mcp"]


def _running(store, goal="Ship the widget and then keep going for a good long while with more words after that to force wrapping"):
    job = store.create_job(goal, "/tmp/somerepo")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    store.kv_set(job.job_id, "herdr_workspace", "w1")
    store.kv_set(job.job_id, "herdr_tab", "w1:t2")
    store.kv_set(job.job_id, "worker_model", "claude-sonnet-5-5")
    return job


def _render(steward_home, tmp_path, cols, expanded=()):
    m = tmp_path / f"map{cols}"
    dashact.save(dashact.state_path(str(m)), {"expanded": {k: True for k in expanded}, "armed": {}})
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_DASH_COLS": str(cols),
           "STEWARD_DASH_MAP": str(m), "PYTHONPATH": str(_REPO)}
    res = subprocess.run([sys.executable, str(_REPO / "deploy/cockpit/steward-dash-jobs.py")],
                         capture_output=True, text=True, env=env, cwd=_REPO)
    lines = [ANSI.sub("", ln).replace("{SPIN}", "x") for ln in res.stdout.splitlines()]
    return lines, m.read_text().splitlines()


@pytest.fixture
def world(steward_home, scratch, tmp_path):
    store = Store(steward_home)
    job = _running(store)
    cw.add_watcher(store, job.job_id, cw.make_spec("verylongname", "are tests green?"))
    done = [mkjob(store, scratch, tmp_path, {f"f{i}.txt": "x\n"}, goal=f"Finished job number {i} with a fairly descriptive goal text")
            for i in range(3)]
    for i, d in enumerate(done):
        store.kv_set(d.job_id, "herdr_workspace", "w1")
        store.kv_set(d.job_id, "herdr_tab", f"w1:t{i + 5}")
    return store, job, done


@pytest.mark.parametrize("cols", [60, 80, 100, 120])
def test_no_line_exceeds_cols_any_mode(world, steward_home, tmp_path, cols):
    for expanded in ((), ("done:",)):
        lines, _ = _render(steward_home, tmp_path, cols, expanded)
        assert lines and all(len(ln) <= cols for ln in lines), [ln for ln in lines if len(ln) > cols]
        assert not any(re.search(r"\w…\w", ln) for ln in lines)


def test_supervisor_columns_align(world, steward_home, tmp_path):
    lines, _ = _render(steward_home, tmp_path, 100)
    rows = [ln for ln in lines if ln.startswith("    ├ ") or ln.startswith("    └ ")]
    assert len(rows) >= 5
    assert all(ln[6:15].strip() and ln[15:22].strip() for ln in rows)
    assert all(ln[14] == " " and ln[21] == " " for ln in rows)  # label 9 / model 7 columns
    assert {ln[15:22].strip() for ln in rows} >= {"sonnet", "mcp"} or "sonnet" in rows[0]
    worker = next(ln for ln in rows if ln[6:14].strip() == "worker")
    assert not re.search(r"actions.*\$", worker)  # the tokens column carries the cost; no "· $x" repeat
    assert "sonnet" in worker[15:22]


def test_done_group_collapsed_and_expanded(world, steward_home, tmp_path):
    store, job, done = world
    for d in done[:2]:
        store.kv_set(d.job_id, "incorporated_commit", "abc1234def")  # merged: folds into "done"
    lines, cmap = _render(steward_home, tmp_path, 88)
    # accepted but NOT merged stays in view with its merge button (regression: it was folded away)
    waiting = [ln for ln in lines if done[2].job_id[:8] in ln]
    assert len(waiting) == 1 and re.search(r"not merged.* +\[ incorporate \]  \[ release \]  \[ export \]$", waiting[0])
    assert any("done (2)" in ln and "tap to show" in ln for ln in lines)
    assert not any(d.job_id[:8] in ln for d in done[:2] for ln in lines)
    idx = next(i for i, ln in enumerate(lines) if "done (2)" in ln)
    assert f"expand {idx} done:" in cmap and lines.index(next(ln for ln in lines if "RUNNING" in ln)) < idx
    assert lines.index(waiting[0]) < idx
    lines, cmap = _render(steward_home, tmp_path, 88, ("done:",))
    idx = next(i for i, ln in enumerate(lines) if "done (2)" in ln)
    assert "tap to hide" in lines[idx]
    rows = lines[idx + 1:idx + 3]
    assert all(re.match(r"\[\d\] ✓ [0-9a-f]{8} .*merged abc1234.* +\[ release \]  \[ export \]$", r) for r in rows), rows
    for k, r in enumerate(rows):
        assert len(r) == 88
        for c in (m.split() for m in cmap if m.startswith(f"btn {idx + 1 + k} ")):
            assert r[int(c[2]) - 1:int(c[3])] == {"incorporate": "[ incorporate ]", "release": "[ release ]", "export": "[ export ]"}[c[4]]
    assert sum(1 for m in cmap if m.startswith("key ")) == 4  # running job + waiting + 2 done rows when expanded


def test_frame_fits_pane_height_and_click_map_follows(tmp_path, monkeypatch):
    # Regression: a 54-line frame in a 43-row pane scrolled, so every tap landed 11 rows off.
    import re, subprocess, sys, os
    repo = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(repo), "STEWARD_DASH_COLS": "80", "STEWARD_DASH_ROWS": "20",
           "STEWARD_DASH_MAP": str(tmp_path / "m")}
    res = subprocess.run([sys.executable, str(repo / "deploy/cockpit/steward-dash-jobs.py")],
                         capture_output=True, text=True, env=env, cwd=repo)
    lines = res.stdout.splitlines()
    assert len(lines) <= 20 - 9
    for entry in (tmp_path / "m").read_text().splitlines():
        kind, *rest = entry.split(" ")
        if kind in ("row", "toggle", "btn", "expand") and rest and rest[0].isdigit():
            assert int(rest[0]) < len(lines), entry


def test_second_release_is_a_noop_and_released_jobs_stay_in_done_history(world, steward_home, tmp_path):
    # Regression: a second tap on [ release ] before the pane redrew raised "RELEASED -> RELEASED is not a legal transition".
    from steward import closeout
    from steward.machine import JobState
    store, job, done = world
    closeout.release(store, done[0].job_id)
    assert closeout.release(store, done[0].job_id) == JobState.RELEASED
    lines, _ = _render(steward_home, tmp_path, 88, ("done:",))
    row = [ln for ln in lines if done[0].job_id[:8] in ln]  # recently finished jobs stay findable (and exportable), release is gone
    assert len(row) == 1 and "[ export ]" in row[0] and "[ release ]" not in row[0]


def test_tapping_the_job_number_folds_it_to_two_lines(world, steward_home, tmp_path):
    store, job, done = world
    lines, cmap = _render(steward_home, tmp_path, 88)
    head = next(i for i, ln in enumerate(lines) if job.job_id[:8] in ln and "RUNNING" in ln)
    assert f"btn {head} 1 3 fold {job.job_id} -" in cmap  # the [n] cell is the fold button
    full = [ln for ln in lines if ln.startswith("    ")]
    from steward import dashact
    lines2, _ = _render(steward_home, tmp_path, 88, (dashact.FOLD_PREFIX + job.job_id,))
    head2 = next(i for i, ln in enumerate(lines2) if job.job_id[:8] in ln and "RUNNING" in ln)
    block = [ln for ln in lines2[head2 + 1:] if ln.startswith("    ")]
    nxt = next((k for k, ln in enumerate(lines2[head2 + 1:]) if not ln.startswith("    ")), len(lines2) - head2 - 1)
    assert nxt == 2 and len(full) > 3  # folded: header + the [ explain ] row + exactly one summary line
    assert dashact.argv_for("fold", "J1")[-1] == dashact.FOLD_PREFIX + "J1"


def test_folded_job_still_shows_its_blockers(world, steward_home, tmp_path):
    _needs_git_history(_REPO)
    import subprocess as sp
    from steward import dashact, stability
    store, job, done = world
    # The parent of the newest commit touching steward/ is always "outdated"; HEAD~N is not once docs-only
    # commits sit on top (a 3-commit docs pack made HEAD~3 current and this test fail).
    last = sp.run(["git", "log", "-1", "--format=%H", "--", "steward/"], capture_output=True, text=True,
                  cwd=_REPO).stdout.strip()
    old = sp.run(["git", "rev-parse", f"{last}^"], capture_output=True, text=True, cwd=_REPO).stdout.strip()
    store.kv_set(job.job_id, stability.RUNNER_CODE_KEY, {"head": old, "pid": 1})
    lines, cmap = _render(steward_home, tmp_path, 100, (dashact.FOLD_PREFIX + job.job_id,))
    head = next(i for i, ln in enumerate(lines) if job.job_id[:8] in ln and "RUNNING" in ln)
    block = []
    for ln in lines[head + 1:]:
        if not ln.startswith("    "):
            break
        block.append(ln)
    assert any("runner outdated" in ln and "[ restart runner ]" in ln for ln in block), block
    assert not any("spend" in ln or "tokens" in ln for ln in block)  # advice stays hidden when folded


# ---- [n] is the tab's position in the bar (tab design 2026-09-30) ----------------------------------------------------


def _two_jobs(store):
    """Launched B first in the table but created later than A: the shared order must put A before B."""
    from steward.models import Judgment

    jobs = []
    for goal, tab, created in (('Build Phase 19 "Installable package"', "w1:tB", "2026-09-30T11:00:00+00:00"),
                               ('Implement Phase 18 "ETA"', "w1:tA", "2026-09-30T10:00:00+00:00")):
        job = store.create_job(goal, "/tmp/somerepo")
        store._conn.execute("UPDATE jobs SET state=?, created_at=? WHERE job_id=?", (JobState.RUNNING.value, created, job.job_id))
        store._conn.commit()
        store.kv_set(job.job_id, "herdr_workspace", "w1")
        store.kv_set(job.job_id, "herdr_tab", tab)
        store.append_event(job.job_id, what="heartbeat", evidence={}, rule="r", judgment=Judgment.code)
        jobs.append(job)
    return jobs


def test_status_numbers_equal_the_tab_bar_positions(steward_home, monkeypatch):
    from herdr_fake import FakeHerdr
    from steward import dashrender, herdr_tabs

    monkeypatch.setenv("STEWARD_HEALTH", "0")
    store = Store(steward_home)
    jb, ja = _two_jobs(store)
    bar = FakeHerdr(
        [{"tab_id": "w1:t1", "label": "x"}, {"tab_id": "w1:t2", "label": "y"}, {"tab_id": "w1:tB", "label": "b"},
         {"tab_id": "w1:tA", "label": "a"}],
        [{"pane_id": "p1", "tab_id": "w1:t1", "label": "steward status"},
         {"pane_id": "p2", "tab_id": "w1:t2", "label": "c", "tokens": {"steward_role": "control"}},
         {"pane_id": "pA", "tab_id": "w1:tA", "label": "runner"}, {"pane_id": "pB", "tab_id": "w1:tB", "label": "runner"}])
    monkeypatch.setattr(dashrender.Pane, "fetch_pinned", lambda self: herdr_tabs.pinned_tabs(bar.tabs, bar.panes))

    lines, cmap = dashrender.Pane(cols=88, store=store).render(refresh=True)

    slots = {s.job_id: s.position for s in herdr_tabs.layout(store, bar.tabs, bar.panes) if s.job_id}
    text = [ANSI.sub("", ln).replace("{SPIN}", "x") for ln in lines]
    for job, name in ((ja, "P18 ETA"), (jb, "P19 Installable")):
        head = next(ln for ln in text if ln.startswith(f"[{slots[job.job_id]}]"))
        assert name in head and job.job_id[:8] in head  # same number and name as the tab; the id moved to the dim detail
    assert slots[ja.job_id] == 3 and slots[jb.job_id] == 4
    assert {"key 1 w1:t1", "key 2 w1:t2", "key 3 w1:tA", "key 4 w1:tB"} <= set(cmap)


def test_status_has_no_pinned_keys_outside_herdr(steward_home, monkeypatch):
    from steward import dashrender

    monkeypatch.setenv("STEWARD_HEALTH", "0")
    store = Store(steward_home)
    _two_jobs(store)
    _, cmap = dashrender.Pane(cols=88, store=store).render(refresh=True)
    assert not [c for c in cmap if c.startswith(("key 1 ", "key 2 "))]


def test_jobs_past_the_ninth_position_get_no_number(steward_home, monkeypatch):
    from steward import dashrender

    monkeypatch.setenv("STEWARD_HEALTH", "0")
    store = Store(steward_home)
    for i in range(9):
        job = _running(store, f"job number {i}")
        store.kv_set(job.job_id, "herdr_tab", f"w1:t{i}")
    lines, cmap = dashrender.Pane(cols=88, store=store).render(refresh=True)
    keys = sorted(int(c.split()[1]) for c in cmap if c.startswith("key "))
    assert keys == [3, 4, 5, 6, 7, 8, 9]
def test_several_tips_collapse_to_one_tappable_line(monkeypatch):
    # Sam 2026-09-30: four spend/efficiency lines per job were clutter. They fold into one line; tap to see them.
    from steward import dashrender, stability
    r = dashrender.Pane.__new__(dashrender.Pane)
    tips = ["spend $5.57/h", "gsd-planner#18 78 calls × 272k ctx → respawn (≈-$3.29)",
            "gsd-planner thinking 79% → step effort down (≈-$1.00)"]
    monkeypatch.setattr(stability, "spend_warnings", lambda tj: tips)
    r.cols, r.out, r.prio, r.cmap, r.dash_state = 100, [], [], [], {"expanded": {}, "armed": {}}
    r.emit = lambda text, action=None, toggle=None, p=5: (r.out.append(text), r.prio.append(p))
    r.safe = lambda key, fn, default, ttl=None: []
    r.warn_lines("J1", "RUNNING", {}, blockers_only=False)
    plain = [dashrender.ANSI.sub("", ln) for ln in r.out]
    assert len(plain) == 1 and "3 tips" in plain[0] and "$5.57/h" in plain[0] and "save ≈$4.29" in plain[0]
    assert any(c.startswith("expand 0 tips:J1") for c in r.cmap)


def test_opened_tips_survive_pane_height_pruning(monkeypatch):
    # Regression: "show doesn't show anything" -- opened tips were emitted at the lowest priority and pruned away.
    from steward import dashrender, stability
    r = dashrender.Pane.__new__(dashrender.Pane)
    monkeypatch.setattr(stability, "spend_warnings", lambda tj: ["spend $5/h", "tip a (≈-$1.00)", "tip b"])
    r.cols, r.out, r.prio, r.cmap = 100, [], [], []
    r.dash_state = {"expanded": {"tips:J1": True}, "armed": {}}
    r.emit = lambda text, action=None, toggle=None, p=5: (r.out.append(text), r.prio.append(p))
    r.safe = lambda key, fn, default, ttl=None: []
    r.warn_lines("J1", "RUNNING", {}, blockers_only=False)
    assert len(r.out) == 4 and min(r.prio[1:]) >= 5  # the opened tips outrank the supervisor rows (p 2-3)


# -- tap to expand a line cut with "…" (Sam 2026-10-01) -------------------------------------------------------------

LONG_GOAL = ("Rework the status pane tap model so a line cut with an ellipsis can be opened in full by one tap and "
             "closed again by another, keeping every word of the goal available no matter how narrow the pane is, "
             "including the very last words zebra-crossing")


def test_a_cut_goal_is_tappable_and_expands_to_its_full_text(steward_home, tmp_path):
    store = Store(steward_home)
    job = _running(store, LONG_GOAL)
    lines, cmap = _render(steward_home, tmp_path, 60)
    key = f"{job.job_id}:goal"
    idx = [int(c.split()[1]) for c in cmap if c.startswith("more ") and c.endswith(" " + key)]
    assert idx and any(lines[i].endswith("…") for i in idx)
    assert "zebra-crossing" not in " ".join(lines)
    lines2, cmap2 = _render(steward_home, tmp_path, 60, ("more:" + key,))
    assert "zebra-crossing" in " ".join(lines2) and not any(ln.endswith("…") and "Rework" in ln for ln in lines2)
    assert sum(1 for c in cmap2 if c.startswith("more ") and c.endswith(" " + key)) > len(idx)  # every wrapped line collapses it
    assert all(len(ln) <= 60 for ln in lines2)


def test_an_opened_cut_line_outranks_pruning(steward_home, tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HEALTH", "0")
    store = Store(steward_home)
    job = _running(store, LONG_GOAL)
    dashact.save(dashact.state_path(str(tmp_path / "m")), {"expanded": {f"more:{job.job_id}:goal": True}, "armed": {}})
    from steward import dashrender
    pane = dashrender.Pane(cols=60, rows=24, state_path=dashact.state_path(str(tmp_path / "m")), store=store)
    out, cmap = pane.render(refresh=True)
    assert "zebra-crossing" in " ".join(dashrender.ANSI.sub("", ln) for ln in out)
    assert pane.fulltext[f"{job.job_id}:goal"].endswith("zebra-crossing")  # the full text is kept in the render data


def test_header_detail_and_supervisor_rows_expand_too(steward_home, tmp_path):
    store = Store(steward_home)
    job = _running(store, "short goal")
    lines, cmap = _render(steward_home, tmp_path, 60)
    keys = {c.split()[2] for c in cmap if c.startswith("more ")}
    assert f"{job.job_id}:header" in keys, keys


def _needs_git_history(repo) -> None:
    """These checks measure real commits behind HEAD; a one-commit snapshot (the public export) has none."""
    import subprocess as _sp
    n = _sp.run(["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    if not n.isdigit() or int(n) < 3:
        pytest.skip("needs the repository's git history (a snapshot export has one commit)")
