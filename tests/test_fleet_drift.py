"""Runtime qualification drift watch: fake version reader + fake qualify; no CLI, probe or message is ever real."""

from __future__ import annotations

from steward import fleet_drift as fd
from steward import fleet_registry as fr
from steward import fleet_runtime as rt
from steward.fleet_store import FleetStore
from steward.store import Store

ROSTER = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [
    {"id": "codex-worker", "provider": "openai", "runtime": "codex", "model": "gpt-x", "roles": ["worker"],
     "capabilities": ["edit", "shell"], "strengths": [], "enabled": True, "label": "Codex worker"}]})


class Env:
    def __init__(self, home, *, passing=True, fail_caps=("launch",)):
        self.store = Store(home)
        self.job = self.store.create_job("g", "/tmp/r").job_id
        self.version = "1.0"
        self.passing, self.fail_caps = passing, set(fail_caps)
        self.qualify_calls, self.notices, self.t = [], [], 1000.0
        self.fs = FleetStore(self.store, version_probe=lambda runtime: self.version)
        self.record("1.0", all_pass=True)
        self.watch = fd.DriftWatch(self.store, version_reader=lambda r: self.version, qualify_fn=self.fake_qualify,
                                   send_text=lambda t: self.notices.append(t) or True,
                                   roster_loader=lambda: ROSTER, clock=lambda: self.t)

    def record(self, version, *, all_pass):
        probes = {c: (lambda c=c: (all_pass or c not in self.fail_caps, "e")) for c in fr.RUNTIME_CAPABILITIES}
        return rt.qualify_runtime(self.fs, "codex", "1", probes, roster_capabilities=["edit", "shell"],
                                  cli_version=version)

    def fake_qualify(self, store, runtime, *, roster=None):
        self.qualify_calls.append((runtime, roster))
        results = self.record(self.version, all_pass=self.passing)
        return {"runtime": runtime, "results": results, "cli_version": self.version,
                "qualified": not fr.missing_capabilities({c for c, v in results.items() if v}, "start")}

    def events(self, what):
        return [e for e in self.store.events(self.job) if e.what == what]

    def tick(self, dt=700):
        self.t += dt
        return self.watch.check(self.job)

    def dispatchable(self):
        return self.fs.dispatchable(ROSTER.entries[0])


def test_drift_triggers_one_event_one_drift_notice_one_qualify(steward_home):
    e = Env(steward_home)
    e.version = "1.1"
    assert e.tick() == ["codex"]
    assert len(e.events("runtime_drift")) == 1
    assert len(e.qualify_calls) == 1 and e.qualify_calls[0][1] is ROSTER
    assert sum("1.0 -> 1.1" in n for n in e.notices) == 1
    for _ in range(3):  # never loops
        e.tick()
    assert len(e.events("runtime_drift")) == 1 and len(e.qualify_calls) == 1 and len(e.notices) == 2


def test_pass_makes_runtime_dispatchable_again_and_says_requalified(steward_home):
    e = Env(steward_home)
    assert e.dispatchable()
    e.version = "1.1"
    assert not e.dispatchable()  # stale until requalified
    e.tick()
    assert e.dispatchable()
    assert len(e.events("runtime_requalified")) == 1
    assert "requalified on 1.1" in e.notices[-1]


def test_fail_keeps_runtime_blocked_and_notices_once(steward_home):
    e = Env(steward_home, passing=False)
    e.version = "1.1"
    e.tick()
    assert not e.dispatchable()
    assert len(e.notices) == 2 and "launch" in e.notices[-1] and "failed requalification" in e.notices[-1]
    for _ in range(3):
        e.tick()
    assert not e.dispatchable() and len(e.qualify_calls) == 1 and len(e.notices) == 2
    e.version = "1.2"  # a NEW version earns one new attempt
    e.tick()
    assert len(e.qualify_calls) == 2


def test_no_drift_does_nothing(steward_home):
    e = Env(steward_home)
    assert e.tick() == []
    assert not e.qualify_calls and not e.notices and not e.events("runtime_drift")


def test_rate_limited(steward_home):
    e = Env(steward_home)
    e.tick()
    e.version = "1.1"
    assert e.tick(dt=30) == []  # inside the 10 min window: not even read
    assert e.tick(dt=700) == ["codex"]


def test_unreadable_version_notices_once_without_qualify(steward_home):
    e = Env(steward_home)
    e.watch._reader = lambda r: None
    e.tick()
    e.tick()
    e.tick()
    assert len(e.notices) == 1 and not e.qualify_calls and len(e.events("runtime_drift")) == 1


def test_qualify_error_is_noticed_once_and_not_retried(steward_home):
    e = Env(steward_home)

    def boom(store, runtime, *, roster=None):
        e.qualify_calls.append(runtime)
        raise RuntimeError("probe harness down")
    e.watch._qualify = boom
    e.version = "1.1"
    e.tick()
    e.tick()
    assert len(e.qualify_calls) == 1 and len(e.notices) == 2 and "did not run" in e.notices[-1]


def test_failed_a_then_failed_b_then_a_again_never_retries_a(steward_home):
    e = Env(steward_home, passing=False)
    for v in ("A", "B", "A", "B", "A"):
        e.version = v
        e.tick()
    assert [c[0] for c in e.qualify_calls] == ["codex", "codex"]  # A once, B once
    assert len(e.events("runtime_drift")) == 2 and len(e.notices) == 4


def test_unreadable_states_separated_by_a_readable_version_notice_once(steward_home):
    e = Env(steward_home)
    seq = [None, "1.0", None, "1.0", None]
    for v in seq:
        e.watch._reader = lambda r, v=v: v
        e.tick()
    assert len(e.notices) == 1 and not e.qualify_calls and len(e.events("runtime_drift")) == 1
    e.watch._reader = lambda r: "1.1"  # a readable new version is still attempted, once
    e.tick()
    e.watch._reader = lambda r: None
    e.tick()
    assert len(e.qualify_calls) == 1 and len(e.notices) == 3


def test_attempts_survive_watcher_recreation_and_old_string_state_migrates(steward_home):
    e = Env(steward_home, passing=False)
    e.version = "1.1"
    e.tick()
    e.version = "1.2"
    e.tick()
    for _ in range(2):  # fresh watcher over the same store: nothing re-attempted
        e.watch = fd.DriftWatch(e.store, version_reader=lambda r: e.version, qualify_fn=e.fake_qualify,
                                send_text=lambda t: e.notices.append(t) or True, roster_loader=lambda: ROSTER,
                                clock=lambda: e.t)
        e.version = "1.1"
        e.tick()
        e.version = "1.2"
        e.tick()
    assert len(e.qualify_calls) == 2
    # legacy state: a bare string latest attempt is kept, so that version is not retried
    e2 = Env(steward_home.parent / "h2")
    e2.store.kv_set(fd.KV_JOB, fd.KEY, {"last_check": 0, "attempted": {"codex": "1.1"}})
    e2.version = "1.1"
    e2.tick()
    assert not e2.qualify_calls and e2.store.kv_get(fd.KV_JOB, fd.KEY)["attempted"] == {"codex": ["1.1"]}
