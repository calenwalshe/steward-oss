"""The completed-jobs tab (steward/donelog.py): ordering, day grouping, filters, unfold detail, totals, keys/taps, and the
pinned tab found by its pane. Fakes only: no herdr, no CLI verb, no real cost scan."""
import re
from datetime import datetime, timedelta, timezone

from herdr_fake import FakeHerdr, live_layout
from steward import donelog, herdr_tabs
from steward.models import JobState, Judgment
from steward.store import Store

ANSI = re.compile(r"\033\[[0-9;]*[A-Za-z]")
NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)


def _done(store, goal, state, finished, *, repo="/r/alpha", started=None, commit=None, name=None):
    job = store.create_job(goal, repo)
    started = started or finished - timedelta(minutes=30)
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=?, created_at=?, updated_at=? WHERE job_id=?",
                            (state.value, started.isoformat(), finished.isoformat(), job.job_id))
    if commit:
        store.kv_set(job.job_id, "incorporated_commit", commit)
    if name:
        store.kv_set(job.job_id, "short_name", name)
    return job


def _fill(store):
    j = {}
    j["merged"] = _done(store, "Fix the widget", JobState.ACCEPTED, NOW - timedelta(hours=1), commit="abc1234ffff", name="widget")
    j["accepted"] = _done(store, "Add a gadget", JobState.ACCEPTED, NOW - timedelta(hours=2), repo="/r/beta", name="gadget")
    j["yday"] = _done(store, "Old ship", JobState.RELEASED, NOW - timedelta(days=1), name="old-ship")
    j["stopped"] = _done(store, "Bad idea", JobState.STOPPED, NOW - timedelta(days=5), repo="/r/beta", name="bad-idea")
    j["failed"] = _done(store, "Crashy", JobState.FAILED, NOW - timedelta(days=40), name="crashy")
    _done(store, "Still going", JobState.RUNNING, NOW, name="live")  # never listed
    return j


def _app(store, **kw):
    app = donelog.App(in_fd=0, out_fd=1, store_factory=lambda: store, usd_fn=lambda s, ids: {i: 1.5 for i in ids},
                      detail_fn=kw.pop("detail_fn", lambda s, e: ["goal:", "  " + e.goal]), cols=100, rows=30,
                      now_fn=lambda: NOW, run=kw.pop("run", lambda argv: (0, "ok")), **kw)
    app.load()
    return app


def _text(app):
    return [ANSI.sub("", ln) for ln in app.frame()]


def test_lists_only_finished_jobs_newest_first(steward_home):
    store = Store(steward_home)
    j = _fill(store)
    got = donelog.collect(store)
    assert [e.job_id for e in got] == [j[k].job_id for k in ("merged", "accepted", "yday", "stopped", "failed")]
    assert [e.outcome for e in got] == ["merged", "accepted", "released", "stopped", "failed"]
    assert got[0].commit == "abc1234ffff" and got[0].repo_name == "alpha" and got[0].duration_s == 1800


def test_merge_later_than_acceptance_counts_as_finish_time(steward_home):
    store = Store(steward_home)
    job = _done(store, "g", JobState.ACCEPTED, NOW - timedelta(hours=3), commit="deadbeef0")
    store.append_event(job.job_id, what="incorporated", evidence={}, rule="r", judgment=Judgment.code)
    assert donelog.collect(store)[0].finished_at > NOW - timedelta(hours=3)


def test_day_separators_today_yesterday_dates():
    f = lambda **kw: donelog.day_label(NOW - timedelta(**kw), NOW)
    assert f(hours=1) == "Today" and f(days=1) == "Yesterday"
    assert f(days=5).endswith("Sep")  # weekday is local-zone dependent
    assert "2025" in donelog.day_label(NOW - timedelta(days=400), NOW)


def test_frame_groups_rows_under_separators_with_glyphs_and_columns(steward_home):
    store = Store(steward_home)
    _fill(store)
    text = _text(_app(store))
    body = "\n".join(text)
    seps = [ln for ln in text if ln.startswith("── ")]
    assert seps[0].startswith("── Today") and seps[1].startswith("── Yesterday") and len(seps) >= 4
    row = next(ln for ln in text if "widget" in ln)
    assert "✓" in row and "alpha" in row and "30m" in row and "$1.50" in row and "abc1234" in row
    assert "✗" in next(ln for ln in text if "bad-idea" in ln)
    assert "✗" in next(ln for ln in text if "crashy" in ln)
    assert "live" not in body


def test_header_totals_follow_the_visible_range(steward_home):
    store = Store(steward_home)
    _fill(store)
    app = _app(store)
    assert "5 jobs · 1 merged · $7.50 · 2.5h" in _text(app)[0]
    app.feed(b"a")  # hide released/stopped
    assert "3 jobs · 1 merged · $4.50 · 1.5h" in _text(app)[0]


def test_filters_search_repo_outcome_and_toggle(steward_home):
    store = Store(steward_home)
    _fill(store)
    app = _app(store)
    app.feed(b"/gadg")
    assert [e.name for e in app.visible()] == ["gadget"] and "search: gadg" in "\n".join(_text(app))
    app.feed(b"\r")
    assert app.filters.query == "gadg" and not app.searching
    app.feed(b"/\x1b")  # Esc clears the query
    assert app.filters.query == "" and len(app.visible()) == 5
    app.feed(b"/Crashy\r")  # goal text also matches
    assert [e.name for e in app.visible()] == ["crashy"]
    app.feed(b"/\x7f\x7f\x7f\x7f\x7f\x7f\x7f\r")
    app.feed(b"r")
    assert app.filters.repo == "alpha" and len(app.visible()) == 3
    app.feed(b"r")
    assert app.filters.repo == "beta" and len(app.visible()) == 2
    app.feed(b"r")
    assert app.filters.repo is None
    app.feed(b"o")
    assert app.filters.outcome == "merged" and [e.name for e in app.visible()] == ["widget"]
    for _ in range(5):
        app.feed(b"o")
    assert app.filters.outcome == "all"
    app.feed(b"a")
    assert {e.outcome for e in app.visible()} == {"merged", "accepted", "failed"}
    assert "hiding released/stopped" in _text(app)[1]


def test_cursor_keys_paging_and_ends(steward_home):
    store = Store(steward_home)
    for i in range(40):
        _done(store, f"job {i}", JobState.ACCEPTED, NOW - timedelta(minutes=i), name=f"j{i:02d}")
    app = _app(store)
    app.feed(b"j")
    app.feed(b"\x1b[B")
    assert app.cursor == 2
    app.feed(b"k")
    assert app.cursor == 1
    app.feed(b"\x1b[6~")
    assert app.cursor == 1 + (30 - 4)
    app.feed(b"\x1b[5~")
    assert app.cursor == 1
    app.feed(b"G")
    assert app.cursor == 39 and any("j39" in ln for ln in _text(app))  # scrolled so the cursor row shows
    app.feed(b"g")
    assert app.cursor == 0 and any("j00" in ln for ln in _text(app))


def test_enter_unfolds_detail_in_place_and_again_folds(steward_home):
    store = Store(steward_home)
    j = _fill(store)
    app = _app(store)
    app.feed(b"\r")
    app.details[j["merged"].job_id] = ["goal:", "  Fix the widget fully", "commits:", "  abc1234 fix"]  # thread result
    text = _text(app)
    i = next(n for n, ln in enumerate(text) if "widget" in ln and "abc1234" in ln)
    assert "Fix the widget fully" in text[i + 2] and "[ export ]  [ explain ]  [ transcript ]" in text[i + 5]
    assert "gadget" in "\n".join(text[i + 6:])  # the next job is pushed down, not covered
    app.feed(b"\r")
    assert not any("Fix the widget fully" in ln for ln in _text(app))


def test_tap_on_a_row_unfolds_and_tap_on_a_link_runs_the_verb(steward_home):
    store = Store(steward_home)
    j = _fill(store)
    calls = []
    app = _app(store, run=lambda argv: calls.append(argv) or (0, "exported → w9"))
    app.frame()
    row = next(r for r, m in app.map.items() if m == ("row", 1))
    app.feed(f"\x1b[<0;10;{row}M".encode())
    app.details[j["accepted"].job_id] = ["goal:"]
    app.frame()
    link_row, spans = next((r, m[1]) for r, m in app.map.items() if m[0] == "link")
    c0, c1, label = spans[0]
    app.feed(f"\x1b[<0;{c0 + 2};{link_row}M".encode())
    import time
    for _ in range(100):
        app.events.empty() or app.events.get_nowait()()
        if calls:
            break
        time.sleep(0.01)
    assert label == "export" and calls and calls[0][-2:] == ["export", j["accepted"].job_id]
    assert app.cursor == 1 and j["accepted"].job_id in app.open


def test_explain_output_shows_in_a_closable_overlay(steward_home):
    store = Store(steward_home)
    _fill(store)
    app = _app(store, run=lambda argv: (0, "line one\nline two"))
    app.link("explain", "x" * 8)
    import time
    for _ in range(100):
        if not app.events.empty():
            app.events.get_nowait()()
            break
        time.sleep(0.01)
    assert "line two" in "\n".join(_text(app))
    app.feed(b"q")
    assert app.overlay is None and "widget" in "\n".join(_text(app))


def test_load_detail_collects_every_section_from_the_job(steward_home, tmp_path):
    store = Store(steward_home)
    job = _done(store, "Do it. FINISH LINE: pytest green", JobState.ACCEPTED, NOW, commit="c0ffee1", name="doit")
    entry = donelog.collect(store)[0]

    class Git:
        def run(self, argv, cwd, **kw):
            from steward.runner_exec import CommandResult
            out = {"diff": " a.py | 2 +-\n 1 file changed, 1 insertion(+)\n", "log": "c0ffee1 the commit\n"}.get(argv[1], "")
            return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0)

    lines = donelog.load_detail(store, entry, runner=Git(), projects_dir=tmp_path)
    text = "\n".join(lines)
    for head in ("goal:", "finish line:", "worker's final message:", "commits:", "files changed:", "questions answered:", "cost:"):
        assert head in text, head
    assert "Do it." in text and "pytest green" in text and "c0ffee1 the commit" in text and "a.py | 2 +-" in text
    assert "1 file changed" not in text.split("files changed:")[1].split("questions")[0]  # the summary line is dropped


def test_refresh_is_event_driven_and_never_faster_than_five_seconds(steward_home):
    store = Store(steward_home)
    _fill(store)
    app = _app(store, poll_s=0.01)
    assert app.poll_s >= 5.0
    n = app.loads
    v = app.version
    app.load()
    assert app.version == v  # unchanged store: same data_version
    other = Store(steward_home)  # a job finishing is a commit from another connection
    _done(other, "new one", JobState.ACCEPTED, NOW, name="fresh")
    assert app.data_version() != v
    app.load()
    assert app.loads == n + 2 and app.visible()[0].name == "fresh"


def test_state_is_never_written(steward_home):
    store = Store(steward_home)
    _fill(store)
    before = store._conn.total_changes
    app = _app(store)
    app.feed(b"\rjj/x\r\x1bra")
    app.frame()
    assert store._conn.total_changes == before


# ---- the pinned tab ------------------------------------------------------------------------------------------------


def test_done_tab_is_found_by_its_pane_not_its_label():
    h = live_layout()
    assert "done" not in herdr_tabs.pinned_tabs(h.tabs, h.panes)
    h.tabs.append({"tab_id": "w4Q:tX", "label": "renamed by the owner"})
    h.panes.append({"pane_id": "w4Q:pX", "tab_id": "w4Q:tX", "label": "✓ done"})
    assert herdr_tabs.pinned_tabs(h.tabs, h.panes)["done"] == "w4Q:tX"


def test_open_done_tab_creates_once_runs_the_command_then_focuses(steward_home):
    h = live_layout()
    rc, msg = donelog.open_done_tab(h, "w4Q")
    assert rc == 0 and msg.startswith("opened ✓ done tab")
    created = [c for c in h.calls if c[:3] == ["herdr", "tab", "create"]]
    assert len(created) == 1 and "✓ done" in created[0]
    assert any(c[:3] == ["herdr", "pane", "run"] and c[-1].endswith("done-log") for c in h.calls)
    rc, msg = donelog.open_done_tab(h, "w4Q")
    assert rc == 0 and msg.startswith("focused existing") and len([c for c in h.calls if c[:3] == ["herdr", "tab", "create"]]) == 1
    assert donelog.open_done_tab(h, "w4Q", dry_run=True)[0] == 0


def test_tidy_creates_the_done_tab_and_layout_places_it_before_phone(steward_home):
    store = Store(steward_home)
    h = live_layout()
    lines = []
    herdr_tabs.tidy(store, h, h, say=lines.append)
    assert any("create the '✓ done' tab" in ln for ln in lines)
    kinds = [s.kind for s in herdr_tabs.layout(store, h.tabs, h.panes)]
    assert kinds[-2:] == ["done", "phone"] and kinds.count("done") == 1
    again = []
    herdr_tabs.tidy(store, h, h, say=again.append)
    assert not any("create the" in ln for ln in again)


def test_tidy_dry_run_only_says_it_would_create(steward_home):
    store = Store(steward_home)
    h = live_layout()
    herdr_tabs.tidy(store, h, h, dry_run=True, say=lambda _: None)
    assert not [c for c in h.calls if c[:3] == ["herdr", "tab", "create"]]


def test_cli_done_log_open_dry_run(steward_home, capsys, monkeypatch):
    from steward import cli

    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w4Q")
    assert cli.main(["done-log", "--open", "--dry-run"]) == 0
    assert "herdr tab create" in capsys.readouterr().out
