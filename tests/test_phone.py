"""`steward phone`: rendering at phone widths, the key/mouse state machine, the
`📱 phone` tab (--open/--link/--dry-run), Telegram alert gating + dedupe, origin moshi."""

from __future__ import annotations

import json

import pytest

from steward import alerts, approvals, cli, notify, phone
from steward.models import JobState, Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store


def _job(store, goal, state=JobState.RUNNING, tab=None):
    job = store.create_job(goal, "/tmp/somerepo")
    with store._conn:  # tests only: skip the transition graph
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))
    store.kv_set(job.job_id, "worker_model", "sonnet")
    if tab:
        store.kv_set(job.job_id, "herdr_workspace", "w1")
        store.kv_set(job.job_id, "herdr_tab", tab)
        store.kv_set(job.job_id, "herdr_pane", tab.replace("t", "p"))
    store.append_event(job.job_id, what="heartbeat class changed", evidence={"to": "working"}, rule="r", judgment=Judgment.code)
    return job


@pytest.fixture
def two(steward_home):
    store = Store()
    a = _job(store, "Refactor the login flow and add the retry logic to it", tab="w1:t2")
    b = _job(store, "Fix flaky checkout tests", state=JobState.NEEDS_HUMAN, tab="w1:t3")
    approvals.open_approval(store, a.job_id, "worker permission: Bash: rm -rf build", kind="permission")
    store.append_event(b.job_id, what="checks passed; acceptance unresolved", evidence={"reasons": ["Sam accepts"]},
                       rule="r", judgment=Judgment.code)
    store.append_event(a.job_id, what="watcher_verdict", evidence={"name": "tests", "status": "ok", "note": "fine"},
                       rule="r", judgment=Judgment.code)
    return store, a, b


def _phone(store, calls=None):
    calls = calls if calls is not None else []

    def run_cli(argv):
        calls.append(argv)
        return 0, "ok"

    return phone.Phone(store, run_cli=run_cli, tokens_fn=lambda s: None), calls


def _text(lines):
    return [phone.plain(l) for l in lines]


# -- rendering -------------------------------------------------------------------


@pytest.mark.parametrize("width", [40, 80])
def test_home_render_fits_and_has_cards(two, width):
    store, a, b = two
    p, _ = _phone(store)
    lines = p.lines(width, 30)
    text = _text(lines)
    assert all(len(t) <= width for t in text)
    assert all(phone.vwidth(l.row) <= width for l in lines)
    assert text[0].startswith("STEWARD  2 jobs · $0.00 · 1h $0.00")
    assert any(t.startswith("[1] ") and "Refactor" in t for t in text)
    assert any(t.startswith("[2] ") for t in text)
    assert any(t.startswith("    ◆ ") and "accept or reject?" in t for t in text)  # needs-you line
    assert "1-9 open" in text[-1] or "1-9 open" in " ".join(text[-2:])


@pytest.mark.parametrize("width", [40, 80])
def test_job_render_fits_and_shows_supervisors_feed_approvals(two, width):
    store, a, b = two
    p, _ = _phone(store)
    p.handle("1")
    text = _text(p.lines(width, 40))
    assert all(len(t) <= width for t in text)
    joined = "\n".join(text)
    assert "RUNNING" in joined and "Refactor the login" in joined
    for role in ("worker", "advisor", "doctor", "observer"):
        assert any(t.startswith(role) for t in text)
    assert "watcher tests ok" in joined
    assert "approvals" in joined and "[1] Bash: rm -rf build" in joined
    assert any("s steer" in t for t in text)


def test_goal_wraps_to_three_lines_max(steward_home):
    store = Store()
    _job(store, "word " * 200)
    p, _ = _phone(store)
    p.handle("1")
    text = _text(p.lines(40, 40))
    goal = [t for t in text if t.startswith("word")]
    assert len(goal) == 3 and goal[-1].endswith("…")


def test_wide_and_narrow_terminals_never_overflow(two):
    store, *_ = two
    p, _ = _phone(store)
    for w in (10, 20, 33, 40, 60, 120):
        assert all(len(t) <= w for t in _text(p.lines(w, 30)))


# -- key state machine -----------------------------------------------------------


def test_open_job_and_back(two):
    store, a, b = two
    p, _ = _phone(store)
    p.handle("1")
    assert (p.screen, p.job_id) == ("job", a.job_id)
    p.handle("b")
    assert p.screen == "home"
    p.handle("9")
    assert p.screen == "home" and p.note == "no job 9"
    assert p.handle("q").quit


def test_steer_prompt_sends_nudge_with_moshi_origin(two):
    store, a, _ = two
    p, calls = _phone(store)
    p.handle("1")
    p.handle("s")
    for ch in "helo":
        p.handle(ch)
    p.handle("backspace")
    for ch in "lo there":
        p.handle(ch)
    p.handle("enter")
    assert calls == [["steer", a.job_id, "hello there", "--verb", "nudge", "--origin", "moshi"]]
    assert p.mode is None


def test_empty_steer_cancels(two):
    store, a, _ = two
    p, calls = _phone(store)
    p.handle("1"); p.handle("s"); p.handle("enter")
    assert calls == [] and p.mode is None and p.note == "cancelled"
    p.handle("s"); p.handle("x"); p.handle("esc")
    assert calls == []


def test_urgent_steer_needs_confirm(two):
    store, a, _ = two
    p, calls = _phone(store)
    p.handle("1"); p.handle("u")
    for ch in "stop it":
        p.handle(ch)
    p.handle("enter")
    assert calls == [] and p.mode == "confirm"
    p.handle("n")
    assert calls == []
    p.handle("u"); [p.handle(c) for c in "now"]; p.handle("enter"); p.handle("y")
    assert calls == [["steer", a.job_id, "now", "--verb", "nudge", "--urgent", "--origin", "moshi"]]


def test_focus_steer(two):
    store, a, _ = two
    p, calls = _phone(store)
    p.handle("1"); p.handle("f"); [p.handle(c) for c in "auth"]; p.handle("enter")
    assert calls == [["steer", a.job_id, "auth", "--verb", "focus", "--origin", "moshi"]]


def test_approve_single_from_home_and_job(two):
    store, a, b = two
    approval = approvals.list_approvals(store, a.job_id)[0]
    # home 'a': two pending across jobs? b has none (only an event), so exactly one
    p, calls = _phone(store)
    p.handle("a")
    assert calls == [["approve", a.job_id, approval["id"], "--origin", "moshi"]]
    p2, calls2 = _phone(store)
    p2.handle("1"); p2.handle("d")
    assert calls2 == [["deny", a.job_id, approval["id"], "--origin", "moshi"]]


def test_approve_pick_when_several(two):
    store, a, b = two
    second = approvals.open_approval(store, b.job_id, "worker permission: Edit x.py", kind="permission")
    p, calls = _phone(store)
    p.handle("a")
    assert p.mode == "pick" and len(p.items) == 2 and calls == []
    assert any("[2]" in t for t in _text(p.lines(40, 30)))
    p.handle("2")
    assert calls == [["approve", b.job_id, second["id"], "--origin", "moshi"]]


def test_approve_nothing_pending(steward_home):
    store = Store()
    _job(store, "x")
    p, calls = _phone(store)
    p.handle("a")
    assert calls == [] and p.note == "nothing pending"


def test_toggle_pick(two):
    store, a, _ = two
    p, calls = _phone(store)
    p.handle("1"); p.handle("t")
    assert p.mode == "pick"
    labels = [l for l, _ in p.items]
    assert any(l.startswith("advisor") for l in labels)
    idx = next(i for i, l in enumerate(labels, 1) if l.startswith("doctor"))
    p.handle(str(idx))
    assert calls == [["supervisor", a.job_id, "sol", "toggle", "--origin", "moshi"]]


def test_stop_confirm_and_pause(two):
    store, a, _ = two
    p, calls = _phone(store)
    p.handle("1"); p.handle("x")
    assert p.mode == "confirm" and calls == []
    p.handle("n")
    assert calls == []
    p.handle("x"); p.handle("y")
    assert calls == [["stop", a.job_id, "--origin", "moshi"]]
    p.handle("p")
    assert calls[-1] == ["pause", a.job_id, "--origin", "moshi"]


def test_chat_runs_cli_and_names_tab(two):
    store, a, _ = two
    p, calls = _phone(store)
    p.handle("1"); p.handle("c")
    assert calls == [["chat", a.job_id, "worker"]]
    assert f"tab ⚙ {a.job_id[:8]}" in p.note


def test_feed_screen_scrolls(steward_home):
    store = Store()
    j = _job(store, "busy")
    for i in range(30):
        store.append_event(j.job_id, what="watcher_verdict", evidence={"name": "w", "status": "ok", "note": f"n{i}"},
                           rule="r", judgment=Judgment.code)
    p, _ = _phone(store)
    p.handle("1"); p.handle("w")
    assert p.screen == "feed"
    before = _text(p.lines(40, 14))
    p.handle("k"); p.handle("k")
    after = _text(p.lines(40, 14))
    assert before != after and all(len(t) <= 40 for t in after)
    p.handle("j"); p.handle("j")
    assert _text(p.lines(40, 14)) == before
    p.handle("b")
    assert p.screen == "job"


# -- mouse / input parsing ---------------------------------------------------------


def test_tap_card_opens_job_and_hint_presses_key(two):
    store, a, b = two
    p, calls = _phone(store)
    lines = p.lines(40, 30)
    row = next(i for i, l in enumerate(lines, 1) if phone.plain(l).startswith("[2]"))
    p.tap(3, row, lines)
    assert p.screen == "job" and p.job_id == b.job_id
    lines = p.lines(40, 30)
    for i, l in enumerate(lines, 1):
        for c0, c1, key in l.hits:
            if key == "b":
                p.tap(c0, i, lines)
    assert p.screen == "home"


def test_tap_hint_word_x_asks_confirm(two):
    store, a, _ = two
    p, _ = _phone(store)
    p.handle("1")
    lines = p.lines(40, 30)
    hit = next((i, h) for i, l in enumerate(lines, 1) for h in l.hits if h[2] == "x")
    p.tap(hit[1][0], hit[0], lines)
    assert p.mode == "confirm"


def test_parser_mouse_arrows_bursts_and_esc():
    ip = phone.InputParser()
    assert ip.feed(b"\x1b[<0;5;7M") == [("mouse", 5, 7)]
    assert ip.feed(b"\x1b[<0;5;7m") == []  # release ignored
    assert ip.feed(b"\x1b[<2;5;7M") == []  # other button ignored
    assert ip.feed(b"\x1b[A\x1b[B") == [("key", "up"), ("key", "down")]
    assert ip.feed(b"hi\r") == [("key", "h"), ("key", "i"), ("key", "enter")]
    assert ip.feed(b"\x7f") == [("key", "backspace")]
    assert ip.feed(b"\x1b") == [("key", "esc")]
    assert ip.feed(b"\x1b[<0;1") == [] and ip.feed(b";2M") == [("mouse", 1, 2)]
    assert ip.feed("é".encode()[:1]) == [] and ip.feed("é".encode()[1:]) == [("key", "é")]


def test_dispatch_burst_text_until_enter(two):
    store, a, _ = two
    p, calls = _phone(store)
    p.handle("1")
    ip = phone.InputParser()
    quit_ = phone.dispatch(p, ip.feed(b"s"), [])
    quit_ = phone.dispatch(p, ip.feed(b"fix the bug\x7f\x7fg\r"), [])
    assert not quit_ and calls[0][2] == "fix the bg"


# -- CLI: origin moshi ---------------------------------------------------------------


def test_origin_moshi_recorded_in_feed(steward_home, capsys):
    store = Store()
    j = _job(store, "goal")
    rc = cli.main(["steer", j.job_id, "hello", "--verb", "nudge", "--origin", "moshi"])
    assert rc == 0
    ev = [e for e in store.events(j.job_id) if e.what == "nudge instruction recorded"][-1]
    assert ev.evidence["origin"] == "moshi"
    from steward import watchers

    items = watchers.collect(store.events(j.job_id))
    assert any("[moshi]" in text for _, _, text in items)


def test_origin_defaults_to_cli(steward_home):
    store = Store()
    j = _job(store, "goal")
    cli.main(["steer", j.job_id, "hello"])
    ev = [e for e in store.events(j.job_id) if e.what == "nudge instruction recorded"][-1]
    assert ev.evidence["origin"] == "cli"


def test_origin_rejects_unknown(steward_home):
    with pytest.raises(SystemExit):
        cli.main(["steer", "x", "hello", "--origin", "watcher:x"])


# -- the phone tab -----------------------------------------------------------------------


def _res(argv, out="", code=0):
    return CommandResult(argv=argv, cwd="", exit_code=code, stdout=out, stderr="", duration_s=0)


def _herdr(existing=None):
    tabs = [{"label": "other", "tab_id": "w4Q:t1", "workspace_id": "w4Q"}]
    panes = [{"pane_id": "w4Q:p1", "tab_id": "w4Q:t1"}]
    if existing:
        # the live tab was renamed by hand: only its PANE still says what it is
        tabs.append({"label": "phone-jobs-list", "tab_id": "w4Q:t9", "workspace_id": "w4Q"})
        panes.append({"pane_id": "w4Q:p9", "tab_id": "w4Q:t9", "label": phone.PHONE_LABEL})

    def respond(argv, cwd):
        key = " ".join(argv[:3])
        if key == "herdr tab list":
            return _res(argv, json.dumps({"result": {"tabs": tabs}}))
        if key == "herdr pane list":
            return _res(argv, json.dumps({"result": {"panes": panes}}))
        if key == "herdr tab create":
            return _res(argv, json.dumps({"result": {"tab": {"tab_id": "w4Q:t7"}, "root_pane": {"pane_id": "w4Q:p7"}}}))
        return _res(argv)

    return FakeRunner(respond)


def test_open_creates_tab_runs_phone_and_prints_link():
    r = _herdr()
    rc, msg = phone.open_phone_tab(r, "w4Q")
    assert rc == 0
    assert msg.splitlines()[-1] == "link: moshi://herdr?workspace=w4Q&tab=w4Q%3At7&pane=w4Q%3Ap7"
    argvs = [c["argv"] for c in r.calls]
    create = next(a for a in argvs if a[:3] == ["herdr", "tab", "create"])
    assert "--workspace" in create and phone.PHONE_LABEL in create
    run = next(a for a in argvs if a[:3] == ["herdr", "pane", "run"])
    assert run[3] == "w4Q:p7" and run[4].endswith("-m steward.cli phone") and "cd " in run[4]


def test_open_focuses_existing_tab():
    r = _herdr(existing=True)
    rc, msg = phone.open_phone_tab(r, "w4Q")
    argvs = [c["argv"] for c in r.calls]
    assert rc == 0 and ["herdr", "tab", "focus", "w4Q:t9"] in argvs
    assert not any(a[:3] == ["herdr", "tab", "create"] for a in argvs)
    assert "tab=w4Q%3At9&pane=w4Q%3Ap9" in msg


def test_dry_run_touches_no_herdr():
    r = _herdr()
    rc, msg = phone.open_phone_tab(r, "w4Q", dry_run=True)
    assert rc == 0 and r.calls == [] and "herdr tab create" in msg


def test_link_existing_and_none():
    assert phone.link_message(_herdr(existing=True), "w4Q") == (0, "moshi://herdr?workspace=w4Q&tab=w4Q%3At9&pane=w4Q%3Ap9")
    rc, msg = phone.link_message(_herdr(), "w4Q")
    assert rc == 1 and "no 📱 phone tab" in msg


def test_open_needs_workspace():
    assert phone.open_phone_tab(_herdr(), None)[0] == 2


def test_cli_open_dry_run(steward_home, capsys, monkeypatch):
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w4Q")
    assert cli.main(["phone", "--open", "--dry-run"]) == 0
    assert "herdr tab create" in capsys.readouterr().out


# -- alerts ------------------------------------------------------------------------------


class FakeTg:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def send_text(self, text):
        self.sent.append(text)
        return self.ok


def _enable(tmp_config, on=True):
    (tmp_config / "steward.toml").write_text(f"[alerts]\ntelegram = {'true' if on else 'false'}\n")


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(d))
    return d


def test_alerts_off_by_default(two, cfg):
    store, a, b = two
    tg = FakeTg()
    assert alerts.telegram_enabled() is False
    assert alerts.check(store, b.job_id, FakeRunner(), notifier=tg, link="moshi://x") == []
    assert tg.sent == []
    _enable(cfg, on=False)
    assert alerts.telegram_enabled() is False


@pytest.mark.legacy_ownership_off  # Plan 1 G30/G33: unclassified parks are reviewed before owner alerts
def test_alerts_on_via_config_sends_once_with_link(two, cfg):
    store, a, b = two
    _enable(cfg)
    assert alerts.telegram_enabled() is True
    tg = FakeTg()
    sent = alerts.check(store, b.job_id, FakeRunner(), notifier=tg, link="moshi://herdr?workspace=w1&tab=t")
    assert sent == ["needs_you"] and len(tg.sent) == 1
    assert "accept or reject?" in tg.sent[0] and "moshi://herdr?workspace=w1&tab=t" in tg.sent[0]
    assert [e.what for e in store.events(b.job_id)].count("alert_sent") == 1
    # dedupe: a second look sends nothing
    assert alerts.check(store, b.job_id, FakeRunner(), notifier=tg, link="l") == []
    assert len(tg.sent) == 1


def test_alert_per_reason_terminal_and_escalation(two, cfg):
    store, a, b = two
    _enable(cfg)
    tg = FakeTg()
    store.append_event(a.job_id, what="watcher_escalated", evidence={"name": "tests"}, rule="r", judgment=Judgment.code)
    assert sorted(alerts.check(store, a.job_id, FakeRunner(), notifier=tg, link=None)) == ["escalated", "needs_you"]
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = 'FAILED' WHERE job_id = ?", (a.job_id,))
    assert alerts.check(store, a.job_id, FakeRunner(), notifier=tg, link=None) == ["terminal:FAILED"]
    assert alerts.check(store, a.job_id, FakeRunner(), notifier=tg, link=None) == []


@pytest.mark.legacy_ownership_off  # Plan 1 G30/G33: unclassified parks are reviewed before owner alerts
def test_alert_failure_recorded_and_capped(two, cfg):
    store, a, b = two
    _enable(cfg)
    tg = FakeTg(ok=False)
    for _ in range(5):
        alerts.check(store, b.job_id, FakeRunner(), notifier=tg, link=None)
    whats = [e.what for e in store.events(b.job_id)]
    assert whats.count("alert_failed") == alerts.MAX_ATTEMPTS == len(tg.sent)
    assert "alert_sent" not in whats


def test_telegram_notifier_shells_out_to_tg_only_via_runner():
    r = FakeRunner()
    assert notify.TelegramNotifier(r).send_text("hi there") is True
    assert r.calls[0]["argv"] == ["tg", "send", "hi there"]
    bad = FakeRunner(lambda argv, cwd: _res(argv, code=1))
    assert notify.TelegramNotifier(bad).send_text("x") is False


@pytest.mark.legacy_ownership_off  # Plan 1 G30/G33: unclassified parks are reviewed before owner alerts
def test_alert_uses_real_notifier_through_fake_runner(two, cfg):
    store, a, b = two
    _enable(cfg)
    r = FakeRunner()
    alerts.check(store, b.job_id, r, link="moshi://l")
    tgs = [c["argv"] for c in r.calls if c["argv"][0] == "tg"]
    assert len(tgs) == 1 and tgs[0][:2] == ["tg", "send"] and "moshi://l" in tgs[0][2]


# -- the ETA (phase 18-06, G17) ---------------------------------------------------------------------------


def _eta_seed(store, job, stored):
    from steward import efficiency_watch as ew
    store.kv_set(job.job_id, ew.STATE_KEY, {"mode": "shadow", "eta": stored})


def test_phone_home_hint_and_job_page(steward_home):
    import sys
    from pathlib import Path
    import time
    sys.path.insert(0, str(Path(__file__).parent))
    from test_eta_display import mk
    from steward import eta as eta_module

    store = Store()
    job = _job(store, "Refactor the login flow", tab="w1:t2")
    now = time.time()
    u = mk(now, mid=100, half=0).to_dict()
    u["shown"].update(p10_end=now + 80 * 60, p90_end=now + 130 * 60)
    _eta_seed(store, job, u)
    snap = phone.gather(store)
    j = snap.jobs[0]
    assert j.hint == f"{j.hint_base} · ~1h40 left (1h20–2h10)" and j.hint_base.startswith("working")
    assert j.eta_line == "eta ~1h40 (1h20–2h10) · units 3/7 · from cycle times (no Jev)"
    p, _ = _phone(store)
    home = _text(p.lines(80, 30))
    assert any(t.startswith("[1] ") and "· ~1h40 left" in t for t in home)
    p.handle("1")
    page = _text(p.lines(80, 40))
    assert page[2] == j.eta_line                                        # right after the liveness line
    u["jev_state"] = "ok"
    _eta_seed(store, job, u)
    assert phone.gather(store).jobs[0].eta_line == "eta ~1h40 (1h20–2h10) · units 3/7 · from cycle times"
    prior = mk(now, source="prior", mid=168, half=90, done=0, total=None, elapsed_min=13.0).to_dict()
    _eta_seed(store, job, prior)
    assert phone.gather(store).jobs[0].hint.endswith(" · usually 2–5 h")
    assert eta_module.render(prior, now)["header"] == "usually 2–5 h"
    for width in (40, 60):                                              # narrow: the ETA is dropped first, nothing overflows
        for t in _text(phone.Phone(store, run_cli=lambda a: (0, "ok"), tokens_fn=lambda s: None).lines(width, 30)):
            assert len(t) <= width


def test_phone_narrow_row_drops_the_eta(steward_home):
    import time
    store = Store()
    job = _job(store, "Refactor the login flow", tab="w1:t2")
    now = time.time()
    from test_eta_display import mk
    _eta_seed(store, job, mk(now, mid=100, half=20).to_dict())
    info = phone.gather(store).jobs[0]
    row = _text(phone.home_lines(phone.Snapshot([info], None), 34))[2]
    assert "left" not in row and info.hint_base in row
    wide = _text(phone.home_lines(phone.Snapshot([info], None), 80))[2]
    assert "left (" in wide


def test_phone_no_eta_when_waiting_or_off(steward_home):
    from steward import efficiency_watch as ew
    import time
    from test_eta_display import mk
    store = Store()
    waiting = _job(store, "Waiting job", state=JobState.NEEDS_HUMAN)
    off = _job(store, "Switched off job")
    for j in (waiting, off):
        _eta_seed(store, j, mk(time.time(), mid=100).to_dict())
    store.kv_set(off.job_id, ew.SWITCH_KEY, "off")
    assert all(j.eta_part is None and j.eta_line is None and " · ~" not in j.hint for j in phone.gather(store).jobs)


def test_a_renamed_phone_tab_is_still_found_so_no_duplicate_is_created():
    r = _herdr(existing=True)
    assert phone.find_phone_tab(r, "w4Q") == {"workspace": "w4Q", "tab": "w4Q:t9", "pane": "w4Q:p9"}
    assert phone.find_phone_tab(_herdr(), "w4Q") is None
