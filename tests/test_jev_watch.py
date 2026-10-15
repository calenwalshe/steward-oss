"""Jev as the always-on 10-second triage layer (steward.jev_watch + runner wiring)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from steward import approvals as approvals_module
from steward import cli, custom_watchers, jev_observer, jev_watch, masking, supervisors
from steward.jev_observer import FakeTransport, JevObserver, TransportResponse
from steward.jev_watch import JevWatch, build_context
from steward.models import JobState
from steward.runner import Runner
from steward.steering import Steering
from steward.store import Store
from steward.worker import WorkerObservation
from test_runner import _contracted_job, _Factory, _fake_diagnoser, _git_runner

FAKE_KEY = "fake-typesafe-key-000000"


def _resp(choice="ok", conf=0.9):
    payload = {"answers": {"triage": {"choice": choice, "confidence": conf,
                                      "probabilities": {choice: conf}}}}
    return TransportResponse(status_code=200, body=json.dumps(payload).encode())


class ScriptedTransport:
    """Returns the next scripted response per post(); records the clock at each call."""

    def __init__(self, responses, now=None):
        self.responses, self.now, self.calls, self.times = list(responses), now, [], []

    def post(self, url, body, headers, timeout):
        self.calls.append(json.loads(body))
        self.times.append(self.now[0] if self.now else None)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


class FakeActions:
    def __init__(self):
        self.calls = []

    def doctor(self, job_id):
        self.calls.append(("doctor", job_id))
        return "doc"

    def permission(self, job_id):
        self.calls.append(("permission", job_id))
        return "perm"

    def watcher(self, job_id, choice):
        self.calls.append(("watcher", choice))
        return "watch"

    def needs_you(self, job_id, text):
        self.calls.append(("needs_you", text))
        return "notified"


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", FAKE_KEY)


def _running(store, goal="fix the parser"):
    job = _contracted_job(store, goal)
    store.set_state(job.job_id, JobState.RUNNING, what="t", evidence={}, rule="t", judgment=jev_observer.Judgment.code)
    return job


def _sig(**kw):
    from steward.heartbeat import Signals

    base = dict(lifecycle="working", pid_alive=True, head_sha="a", diff_fingerprint="", last_check_exit=None,
                error_fingerprint="", same_error_count=0, permission_prompt=False, running_process=None,
                since_last_change_s=42.0)
    return Signals(**{**base, **kw})


def _iso(now, ago):
    return datetime.fromtimestamp(now - ago, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _watch(store, responses, mode="shadow", actions=None, now=None, activity=None):
    now = now if now is not None else [1000.0]
    store.kv_set(store.list_jobs()[0].job_id, "supervisor_jev", mode)
    t = ScriptedTransport(responses, now)
    obs = JevObserver(transport=t, enabled=True, api_key_fn=lambda: FAKE_KEY, clock=lambda: now[0])
    actions = actions or FakeActions()
    w = JevWatch(store, obs, actions, lambda: now[0], activity_fn=activity or (lambda s, j: {"turns": []}))
    return w, t, actions, now


# -- context builder ------------------------------------------------------------------


def test_context_has_approvals_running_tool_turns_steers_verdicts(steward_home):
    store = Store(steward_home)
    job = _running(store)
    now = datetime.now(timezone.utc).timestamp()
    store.kv_set(job.job_id, "last_class", "waiting")
    pend = approvals_module.open_approval(store, job.job_id, "worker permission: Bash: rm -rf build", kind="permission")
    stale = approvals_module.open_approval(store, job.job_id, "worker permission: Edit x.py", kind="permission")
    approvals_module.mark_stale(store, job.job_id, stale["id"], reason="x")
    Steering(store).record(job.job_id, "nudge", "focus on parser", "r1")
    store.append_event(job.job_id, what="watcher_verdict", evidence={"name": "drift", "status": "ok"},
                       rule="r", judgment=jev_observer.Judgment.model)
    turns = [
        {"i": 0, "ts": _iso(now, 300), "kind": "said", "text": "starting"},
        {"i": 1, "ts": _iso(now, 200), "kind": "tool", "text": "Bash: pytest"},
        {"i": 2, "ts": _iso(now, 190), "kind": "result", "text": "3 passed"},
        {"i": 3, "ts": _iso(now, 95), "kind": "tool", "text": "Bash: sleep 900"},
    ]
    ctx = build_context(store, job.job_id, _sig(), now, activity_fn=lambda s, j: {"turns": turns})
    assert ctx["goal"] == "fix the parser" and ctx["state"] == "RUNNING" and ctx["hb"] == "waiting"
    assert ctx["idle_s"] == 42
    assert ctx["act_s"] == 95 and ctx["res_s"] == 190
    assert ctx["tool"] == "Bash: sleep 900" and ctx["tool_s"] == 95
    assert {p["st"] for p in ctx["perms"]} == {"pending", "stale"} and ctx["perm_stale"] == 1
    assert [t[0] for t in ctx["turns"]] == ["said", "tool", "result", "tool"]
    assert ctx["steers"][0]["rev"] >= 1 and ctx["steers"][0]["st"] == "received"
    assert ctx["verdicts"][0]["w"] == "drift" and ctx["verdicts"][0]["s"] == "ok"
    assert pend["id"]  # keep flake8 quiet about the unused pending record


def test_context_redacts_secrets(steward_home):
    store = Store(steward_home)
    job = _running(store)
    now = 2000.0
    masking.register(["literal-secret-value-123"])
    turns = [{"i": 0, "ts": _iso(now, 5), "kind": "said",
              "text": "export API_KEY=hunter2hunter2 and Bearer abcdef.ghi and sk-abcdefghijklmnopqr literal-secret-value-123"}]
    ctx = build_context(store, job.job_id, _sig(), now, activity_fn=lambda s, j: {"turns": turns})
    blob = json.dumps(ctx)
    for leaked in ("hunter2hunter2", "abcdef.ghi", "sk-abcdefghijklmnopqr", "literal-secret-value-123"):
        assert leaked not in blob


def test_context_is_size_capped(steward_home):
    store = Store(steward_home)
    job = _running(store, goal="g" * 500)
    now = 2000.0
    turns = [{"i": i, "ts": _iso(now, 100 - i), "kind": "said", "text": "word " * 200} for i in range(40)]
    for i in range(8):
        approvals_module.open_approval(store, job.job_id, "worker permission: " + "x" * 300, kind="permission")
    ctx = build_context(store, job.job_id, _sig(), now, activity_fn=lambda s, j: {"turns": turns})
    assert len(json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))) <= jev_observer.MAX_STATE_CHARS
    assert ctx["state"] == "RUNNING"


# -- dedupe + stats -----------------------------------------------------------------------


def test_unavailable_and_ok_events_only_on_change_and_stats_roll(steward_home, keyed):
    store = Store(steward_home)
    job = _running(store)
    t = ScriptedTransport([TransportResponse(status_code=500, body=b"")] * 3 + [_resp("ok")] * 3 + [_resp("stalled", 0.9)])
    obs = JevObserver(transport=t, enabled=True, clock=lambda: 5000.0)
    for _ in range(7):
        obs.observe(store, job.job_id, {}, "")
    evs = [(e.what, e.evidence.get("code") or e.evidence.get("choice")) for e in store.events(job.job_id)
           if e.what in ("jev_unavailable", "jev_shadow")]
    assert evs == [("jev_unavailable", "http_error_500"), ("jev_shadow", "ok"), ("jev_shadow", "stalled")]
    st = jev_observer.get_stats(store, job.job_id)
    assert st["calls"] == 7 and st["ok"] == 4 and st["unavailable"] == 3
    assert st["flags"] == {"stalled": 1} and st["last_ts"] == 5000.0 and st["last_ok_ts"] == 5000.0


def test_missing_key_one_event_per_state_change_not_per_tick(steward_home, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    store = Store(steward_home)
    job = _running(store)
    obs = JevObserver(transport=FakeTransport(_resp()), enabled=True)
    for _ in range(5):
        obs.observe(store, job.job_id, {}, "")
    assert [e.evidence["code"] for e in store.events(job.job_id) if e.what == "jev_unavailable"] == ["no_api_key"]


def test_supervisors_row_shows_mode_checks_flags_last_ok(steward_home):
    store = Store(steward_home)
    job = _running(store)
    store.kv_set(job.job_id, "jev_stats", {"calls": 140, "ok": 132, "unavailable": 8, "flags": {"stalled": 2, "looping": 1},
                                           "last_ts": 0, "last_ok_ts": datetime(2026, 9, 29, 12, 4).timestamp()})
    store.kv_set(job.job_id, "jev_status", "ok:ok")
    row = [r for r in supervisors.supervisor_rows(store, job.job_id) if r["role"] == "jev"][0]
    assert row["display"] == "[shadow▸] 10s · 132 checks · 3 flags · last ok 12:04"
    assert row["next"] == "active" and row["switch"] == "shadow▸"


# -- cadence -----------------------------------------------------------------------------


def _runner(store, job, responses, now, notifier=None):
    obs_ = WorkerObservation(lifecycle="working", output_tail="", pid_alive=True, ts="t")
    t = ScriptedTransport(responses, now)
    jev = JevObserver(transport=t, enabled=True, api_key_fn=lambda: FAKE_KEY, clock=lambda: now[0])
    kw = {"notifier": notifier} if notifier else {}
    runner = Runner(store, _Factory(scripts=[[obs_] * 50]), _git_runner(), _fake_diagnoser(), clock=lambda: now[0],
                    jev_observer=jev, **kw)
    runner.jev_watch.activity_fn = lambda s, j: {"turns": []}
    runner.start(job.job_id)
    return runner, t


def test_jev_runs_every_10s_independent_of_the_15s_tick(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    now = [1000.0]
    runner, transport = _runner(store, job, [_resp("ok")], now)
    ticks = []
    real_tick = runner.tick

    def tick(j):
        ticks.append(now[0])
        return real_tick(j)

    runner.tick = tick

    def sleep(d):
        now[0] += d

    runner.run_forever(job.job_id, interval_s=15, sleep_fn=sleep, max_ticks=5)
    assert ticks == [1000.0, 1015.0, 1030.0, 1045.0, 1060.0]
    times = transport.times
    assert len(times) >= 6 and all(round(b - a, 3) == 10.0 for a, b in zip(times, times[1:]))


def test_only_running_jobs_are_checked(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    now = [1000.0]
    runner, transport = _runner(store, job, [_resp("ok")], now)
    store.set_state(job.job_id, JobState.PAUSED, what="t", evidence={}, rule="t", judgment=jev_observer.Judgment.human)
    runner.jev_step(job.job_id)
    assert transport.calls == []


def test_mode_off_makes_no_calls_and_default_is_shadow(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    assert supervisors.jev_mode(store, job.job_id) == "shadow"
    store.kv_set(job.job_id, "supervisor_jev", "off")
    now = [1000.0]
    runner, transport = _runner(store, job, [_resp("ok")], now)
    runner.tick(job.job_id)
    assert transport.calls == []


def test_global_default_from_steward_toml(steward_home, tmp_path):
    store = Store(steward_home)
    job = _contracted_job(store)
    cfg = os.environ["STEWARD_CONFIG_DIR"]
    open(os.path.join(cfg, "steward.toml"), "w").write('[jev]\nmode = "active"\n')
    assert supervisors.jev_mode(store, job.job_id) == "active"
    store.kv_set(job.job_id, "supervisor_jev", "on")  # legacy alias wins per job
    assert supervisors.jev_mode(store, job.job_id) == "shadow"


# -- the gate: shadow vs active, 2 consecutive, confidence, cooldown ------------------------


def test_shadow_records_would_escalate_and_never_acts(steward_home):
    store = Store(steward_home)
    job = _running(store)
    w, _, actions, now = _watch(store, [_resp("stalled", 0.9)], "shadow")
    w.step(job.job_id, _sig(), force=True)
    assert [e for e in store.events(job.job_id) if e.what == "jev_flag"] == []  # one check is not enough
    w.step(job.job_id, _sig(), force=True)
    flags = [e.evidence for e in store.events(job.job_id) if e.what == "jev_flag"]
    assert len(flags) == 1 and flags[0]["choice"] == "stalled" and flags[0]["would_escalate_to"] == "doctor"
    assert actions.calls == []
    assert [e for e in store.events(job.job_id) if e.what == "jev_escalated"] == []


def test_gate_needs_two_consecutive_same_choice_and_confidence(steward_home):
    store = Store(steward_home)
    job = _running(store)
    w, _, actions, now = _watch(store, [_resp("stalled", 0.9), _resp("looping", 0.9), _resp("looping", 0.5),
                                         _resp("looping", 0.9), _resp("ok", 0.9), _resp("stalled", 0.95)], "active")
    for _ in range(6):
        w.step(job.job_id, _sig(), force=True)
    assert actions.calls == []  # never two consecutive confident same choices
    w2, _, actions2, _ = _watch(store, [_resp("stalled", 0.9), _resp("stalled", 0.95)], "active")
    w2.step(job.job_id, _sig(), force=True)
    w2.step(job.job_id, _sig(), force=True)
    assert actions2.calls == [("doctor", job.job_id)]


def test_cooldown_five_minutes_per_choice(steward_home):
    store = Store(steward_home)
    job = _running(store)
    w, _, actions, now = _watch(store, [_resp("stalled", 0.9)], "active")
    for _ in range(4):
        w.step(job.job_id, _sig(), force=True)
    assert actions.calls == [("doctor", job.job_id)]  # 2nd pair is inside the cooldown
    now[0] += 301
    for _ in range(2):
        w.step(job.job_id, _sig(), force=True)
    assert actions.calls == [("doctor", job.job_id)] * 2


@pytest.mark.parametrize("choice,expected", [
    ("stalled", [("doctor", "J")]),
    ("hung_call", [("permission", "J"), ("doctor", "J")]),
    ("looping", [("watcher", "looping")]),
    ("off_track", [("watcher", "off_track")]),
])
def test_active_routes(steward_home, choice, expected):
    store = Store(steward_home)
    job = _running(store)
    # hung_call routes only on a measured age (job d3477a2a): here a tool call running 31 min.
    tool = (lambda s, j: {"turns": [{"kind": "tool", "ts": _iso(1000.0, 31 * 60), "text": "Bash: ./long.sh"}]}) \
        if choice == "hung_call" else None
    w, _, actions, _ = _watch(store, [_resp(choice, 0.9)], "active", activity=tool)
    for _ in range(2):
        w.step(job.job_id, _sig(), force=True)
    assert actions.calls == [(a, job.job_id if b == "J" else b) for a, b in expected]
    esc = [e.evidence for e in store.events(job.job_id) if e.what == "jev_escalated"]
    assert len(esc) == 1 and esc[0]["choice"] == choice


def test_active_risky_action_needs_you(steward_home):
    store = Store(steward_home)
    job = _running(store)
    w, _, actions, _ = _watch(store, [_resp("risky_action", 0.9)], "active")
    for _ in range(2):
        w.step(job.job_id, _sig(), force=True)
    assert [c[0] for c in actions.calls] == ["needs_you"]


# -- real escalation routes through the runner ---------------------------------------------


class _Recorder:
    def __init__(self):
        self.events = []

    def send(self, event):
        self.events.append(event)
        return True


def test_route_doctor_uses_existing_stall_path_and_budget(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    now = [1000.0]
    runner, _ = _runner(store, job, [_resp("ok")], now)
    diag = runner.diagnoser
    from steward.runner import _JevActions

    runner._signals[job.job_id] = _sig()
    out = _JevActions(runner).doctor(job.job_id)
    assert "guide" in out and len(diag.contexts) == 1
    assert any(e.what == "stall diagnosis produced" for e in store.events(job.job_id))
    again = _JevActions(runner).doctor(job.job_id)  # same episode: the doctor's own cap holds
    assert "already diagnosed" in again and len(diag.contexts) == 1
    store.kv_set(job.job_id, "supervisor_sol", "off")
    assert "doctor is off" in _JevActions(runner).doctor(job.job_id)


def test_route_permission_reopens_stale_and_rerelays(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    now = [1000.0]
    rec = _Recorder()
    runner, _ = _runner(store, job, [_resp("ok")], now, notifier=rec)
    from steward.runner import _JevActions

    runner._signals[job.job_id] = _sig(permission_prompt=True, lifecycle="blocked")
    # stale + worker still blocked -> reopened as pending, human re-notified
    a = approvals_module.open_approval(store, job.job_id, "worker permission: Bash: x", kind="permission")
    approvals_module.mark_stale(store, job.job_id, a["id"], reason="gone")
    assert "reopened" in _JevActions(runner).permission(job.job_id)
    assert approvals_module.get_approval(store, job.job_id, a["id"])["status"] == "pending"
    assert [e.kind for e in rec.events if e.kind == "NEEDS_YOU"] == ["NEEDS_YOU"]
    # decided but never relayed -> relayed now
    b = approvals_module.open_approval(store, job.job_id, "worker permission: Bash: y", kind="permission")
    a2 = approvals_module._load(store, job.job_id)
    for r in a2:
        if r["id"] == a["id"]:
            r["status"] = "denied"
        if r["id"] == b["id"]:
            r["status"] = "approved"
    approvals_module._save(store, job.job_id, a2)
    worker = runner._workers[job.job_id]
    assert "relayed a decided" in _JevActions(runner).permission(job.job_id)
    assert worker.answered == [False, True]  # both decided answers went out
    # relayed >=60s ago and the worker is STILL blocked -> re-relayed
    recs = approvals_module._load(store, job.job_id)
    for r in recs:
        if r["id"] == b["id"]:
            r["relayed_at"] = "2000-01-01T00:00:00+00:00"
    approvals_module._save(store, job.job_id, recs)
    assert "re-relayed" in _JevActions(runner).permission(job.job_id)
    assert worker.answered == [False, True, True]


def test_route_watcher_runs_most_relevant_now_else_advisor(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    now = [1000.0]
    runner, _ = _runner(store, job, [_resp("ok")], now)
    from steward.runner import _JevActions

    class Adv:
        def __init__(self):
            self.calls = []

        def request_review_now(self, j, reason):
            self.calls.append(reason)
            return "review requested"

    runner.trajectory = Adv()
    assert "advisor: review requested" == _JevActions(runner).watcher(job.job_id, "looping")
    custom_watchers.add_watcher(store, job.job_id, custom_watchers.make_spec("style", "is the style consistent?"))
    custom_watchers.add_watcher(store, job.job_id, custom_watchers.make_spec("goalcheck", "is it still on track for the goal?"))
    assert "goalcheck" in _JevActions(runner).watcher(job.job_id, "off_track")
    assert store.kv_get(job.job_id, "watcher_run_now:goalcheck") is True
    assert store.kv_get(job.job_id, "watcher_run_now:style") is None


def test_route_needs_you_notifies(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    rec = _Recorder()
    runner, _ = _runner(store, job, [_resp("ok")], [1000.0], notifier=rec)
    from steward.runner import _JevActions

    _JevActions(runner).needs_you(job.job_id, "risky")
    assert [(e.kind, e.body) for e in rec.events if e.kind == "NEEDS_YOU"] == [("NEEDS_YOU", "risky · tap to answer")]


def test_active_end_to_end_never_steers_the_worker(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store)
    store.kv_set(job.job_id, "supervisor_jev", "active")
    now = [1000.0]
    runner, _ = _runner(store, job, [_resp("stalled", 0.9)], now)
    runner.tick(job.job_id)  # first check (streak 1) rides the tick
    now[0] += 10
    runner.jev_step(job.job_id)  # second consecutive confident check -> active escalation
    assert any(e.what == "jev_escalated" for e in store.events(job.job_id))
    assert not any(i.get("origin") == "jev" for i in Steering(store).all_instructions(job.job_id))


# -- scoring -------------------------------------------------------------------------------


def test_score_agree_disagree_pending(steward_home):
    store = Store(steward_home)
    job = _running(store)
    from steward.models import Judgment

    def flag(choice):
        store.append_event(job.job_id, what="jev_flag", evidence={"choice": choice, "confidence": 0.9, "mode": "shadow"},
                           rule="r", judgment=Judgment.model)

    flag("looping")
    store.append_event(job.job_id, what="watcher_verdict", evidence={"name": "w", "status": "off_track"},
                       rule="r", judgment=Judgment.model)
    flag("stalled")
    t0 = datetime.now(timezone.utc).timestamp()
    s = jev_watch.score(store, job.job_id, now=t0 + 5)
    assert (s["agree"], s["pending"], s["disagree"]) == (1, 1, 0)  # stalled has nothing after it yet, still in window
    s = jev_watch.score(store, job.job_id, now=t0 + 700)
    assert (s["agree"], s["pending"], s["disagree"]) == (1, 0, 1) and s["precision"] == 0.5
    assert s["by_choice"]["looping"]["agree"] == 1 and s["by_choice"]["stalled"]["disagree"] == 1


def test_cli_observer_stats(steward_home, capsys):
    store = Store(steward_home)
    job = _running(store)
    store.kv_set(job.job_id, "jev_stats", {"calls": 3, "ok": 3, "flags": {"looping": 1}, "unavailable": 0})
    assert cli.main(["observer", "stats", job.job_id]) == 0
    assert "3 checks" in capsys.readouterr().out
    assert cli.main(["observer", "stats", "--all", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total"]["checks"] == 3 and len(out["jobs"]) == 1
    assert cli.main(["observer", "stats"]) == 1


# -- key handling ----------------------------------------------------------------------------


def test_key_loaded_in_memory_only_never_into_environ_or_worker_env(steward_home, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    keys = os.environ["STEWARD_API_KEYS_FILE"]
    open(keys, "w").write(f"export TYPESAFE_API_KEY={FAKE_KEY}\nOTHER=1\n")
    assert jev_observer.load_api_key() == FAKE_KEY
    assert "TYPESAFE_API_KEY" not in os.environ
    store = Store(steward_home)
    job = _contracted_job(store)
    from pathlib import Path

    env = cli._worker_spawn_env(store, job.job_id, Path("/x"))
    assert "TYPESAFE_API_KEY" not in env and FAKE_KEY not in json.dumps(env)
    # and it is masked if it ever reaches an event
    store.append_event(job.job_id, what="x", evidence={"t": FAKE_KEY}, rule="r", judgment=jev_observer.Judgment.code)
    assert FAKE_KEY not in json.dumps([e.evidence for e in store.events(job.job_id)])


def test_observer_uses_key_from_keys_file_for_the_request(steward_home, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    open(os.environ["STEWARD_API_KEYS_FILE"], "w").write(f"TYPESAFE_API_KEY={FAKE_KEY}\n")
    store = Store(steward_home)
    job = _running(store)
    t = FakeTransport(_resp("ok"))
    JevObserver(transport=t, enabled=True).observe(store, job.job_id, {}, "")
    assert t.calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"


# -- aliases --------------------------------------------------------------------------------


def test_observer_alias_and_modes_via_cli(steward_home):
    store = Store(steward_home)
    job = _running(store)
    for word, expect in (("shadow", "shadow"), ("active", "active"), ("off", "off"), ("on", "shadow")):
        assert cli.main(["supervisor", job.job_id, "observer", word]) == 0
        assert supervisors.jev_mode(store, job.job_id) == expect
    assert cli.main(["supervisor", job.job_id, "observer", "toggle"]) == 0
    assert supervisors.jev_mode(store, job.job_id) == "active"


def test_feed_lines_for_flags_and_escalations_not_ok(steward_home):
    from steward import watchers
    from steward.models import Judgment

    store = Store(steward_home)
    job = _running(store)
    for what, ev in (("jev_shadow", {"choice": "ok", "confidence": 0.9}),
                     ("jev_flag", {"choice": "stalled", "confidence": 0.9, "would_escalate_to": "doctor"}),
                     ("jev_flag", {"choice": "looping", "confidence": 0.8, "escalated_to": "watcher"}),
                     ("jev_escalated", {"choice": "looping", "route": "watcher", "details": ["watcher: ran x"]})):
        store.append_event(job.job_id, what=what, evidence=ev, rule="r", judgment=Judgment.model)
    lines = [text for _, _, text in watchers.collect(store.events(job.job_id))]
    assert not any("ok" == t for t in lines)
    assert any(t.startswith("flag stalled") for t in lines)
    assert any(t.startswith("FLAG looping") for t in lines)
    assert any(t.startswith("escalated looping") for t in lines)
    assert len([t for t in lines if "observes" in t]) == 0


# -- audit rec 9: precision against independent ground truth, longer timeout, backoff ----------


def _flag_and_escalation(store, job, *, then):
    from steward.models import Judgment as J

    store.append_event(job.job_id, what="jev_flag", evidence={"choice": "hung_call", "confidence": 0.87, "mode": "active"},
                       rule="r", judgment=J.model)
    store.append_event(job.job_id, what="jev_escalated", evidence={"choice": "hung_call", "route": "doctor+permission"},
                       rule="r", judgment=J.code)
    for what, ev in then:
        store.append_event(job.job_id, what=what, evidence=ev, rule="r", judgment=J.model)


def test_a_flag_that_called_the_doctor_never_agrees_with_what_the_doctor_found(steward_home):
    """d3477a2a: the false hung_call escalated to the doctor, the doctor's diagnosis and recovery then
    'corroborated' the flag. Everything downstream of an escalating flag is not independent evidence."""
    store = Store(steward_home)
    job = _running(store)
    _flag_and_escalation(store, job, then=[
        ("stall diagnosis produced", {}), ("recovery escalated to human", {}),
        ("watcher_verdict", {"name": "w", "status": "off_track"}),
        ("trajectory_advice_received", {"verdict": {"trajectory": "off_track"}}),
    ])
    s = jev_watch.score(store, job.job_id, now=datetime.now(timezone.utc).timestamp() + 700)
    assert (s["agree"], s["disagree"]) == (0, 1) and s["precision"] == 0.0


def test_a_human_steer_is_still_independent_ground_truth_for_an_escalating_flag(steward_home):
    store = Store(steward_home)
    job = _running(store)
    _flag_and_escalation(store, job, then=[("focus instruction recorded", {"origin": "cli"})])
    s = jev_watch.score(store, job.job_id, now=datetime.now(timezone.utc).timestamp() + 700)
    assert (s["agree"], s["disagree"]) == (1, 0)


def test_a_shadow_flag_is_still_corroborated_by_an_independent_diagnosis(steward_home):
    from steward.models import Judgment as J

    store = Store(steward_home)
    job = _running(store)
    store.append_event(job.job_id, what="jev_flag", evidence={"choice": "stalled", "confidence": 0.9, "mode": "shadow"},
                       rule="r", judgment=J.model)
    store.append_event(job.job_id, what="stall diagnosis produced", evidence={}, rule="r", judgment=J.model)
    s = jev_watch.score(store, job.job_id, now=datetime.now(timezone.utc).timestamp() + 700)
    assert (s["agree"], s["disagree"]) == (1, 0)


def test_request_timeout_is_30s_by_default_and_configurable(steward_home, monkeypatch):
    store = Store(steward_home)
    job = _running(store)
    t = FakeTransport(_resp("ok"))
    JevObserver(transport=t, enabled=True, api_key_fn=lambda: FAKE_KEY).observe(store, job.job_id, {}, "")
    assert t.calls[0]["timeout"] == 30.0
    monkeypatch.setenv("STEWARD_JEV_TIMEOUT", "45")
    JevObserver(transport=t, enabled=True, api_key_fn=lambda: FAKE_KEY).observe(store, job.job_id, {}, "")
    assert t.calls[1]["timeout"] == 45.0


def test_repeated_timeouts_back_off_instead_of_blocking_every_tick(steward_home):
    store = Store(steward_home)
    job = _running(store)
    now = [1000.0]
    t = ScriptedTransport([TimeoutError()], now)
    jev = JevObserver(transport=t, enabled=True, api_key_fn=lambda: FAKE_KEY, clock=lambda: now[0])
    watch = JevWatch(store, jev, FakeActions(), clock=lambda: now[0], activity_fn=lambda s, j: {"turns": []})
    for _ in range(jev_watch.BACKOFF_AFTER):
        assert watch.due(job.job_id)
        watch.step(job.job_id, _sig())
        now[0] += jev_watch.INTERVAL_S
    assert not watch.due(job.job_id)  # held back after the third failure
    assert watch.until_due(job.job_id) > 0
    now[0] += jev_watch.BACKOFF_MAX_S
    assert watch.due(job.job_id)
