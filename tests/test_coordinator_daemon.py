"""The coordinator daemon (spec 5.16, 6.3, R15, R17, R18) end to end on a fixture Steward store.

The fake `run` records every argv and applies the relayed Steward verbs in-process the way the control pane would
(guard, actor binding, the real `coordinator_hold` / `coordinator_episode` / `approve` handlers); `steer` is recorded
only. No real herdr / tasks / codex / claude / systemctl is ever started.
"""
import json
import threading
import time
import uuid

import pytest

from steward import actor, approvals, cli, coordinator_guard, coordinator_link, deploy_pending
from steward.coordinator import daemon, db, reruns, triggers
from steward.coordinator import __main__ as coord_main
from steward.coordinator.config import Settings
from steward.models import JobState, Judgment
from steward.store import Store

H = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
B = "0badc0de11223344556677889900aabbccddeeff"
IDS = ["tests/test_health.py::test_a", "tests/test_launch.py::test_b"]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path / "coord"))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("HERDR_WORKSPACE_ID", raising=False)


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Fake:
    """Records argv; applies relays through the real control handlers."""

    APPLY = ("coordinator_hold", "coordinator_episode", "approve")

    def __init__(self, store, *, apply_approve=True, claude_plan=None, codex_rc=1):
        self.main_store, self.calls = store, []
        self.apply_approve, self.claude_plan, self.codex_rc = apply_approve, claude_plan, codex_rc
        self._local = threading.local()
        self._local.pair = (store, cli._build_control_handlers(store))

    @property
    def store(self):
        return self._pair()[0]

    def _pair(self):
        """The control pane's store is its own; a step on the worker thread gets a per-thread one (sqlite)."""
        if getattr(self._local, "pair", None) is None:
            st = Store()
            self._local.pair = (st, cli._build_control_handlers(st))
        return self._local.pair

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append(argv)
        if argv[1:3] == ["-m", "steward.cli"]:
            if argv[3:4] == ["health"]:
                return R(1, "", "no health read in tests")
            assert kw["env"]["STEWARD_FORCED_ORIGIN"] == "coordinator"
            if argv[3:4] == ["up"]:
                return R(0, "control started")
            return self._steward(argv[3:])
        if argv[:2] == ["tasks", "add"]:
            return R(0, "added task #42\n")
        if "codex" in argv:
            return R(self.codex_rc, "", "codex down")
        if "claude" in argv and self.claude_plan is not None:
            return R(0, json.dumps({"result": json.dumps(self.claude_plan), "total_cost_usd": 0.25}))
        return R(1, "", "not faked")

    def _steward(self, args):
        ns = cli._build_parser().parse_args(args)
        op, a = cli._op_and_args_for(ns)
        frame = {"op": op, "origin": "coordinator", "request_id": str(uuid.uuid4()), "args": a}
        store, handlers = self._pair()
        refused = coordinator_guard.guard(store, frame)
        if refused:
            return R(1, "", refused)
        if op in self.APPLY and (op != "approve" or self.apply_approve):
            with actor.bind(actor.label_for_cmd(frame)):
                res = handlers[op](frame)
            return R(0 if res.get("accepted") else 1, json.dumps(res))
        return R(0, "recorded")

    def verbs(self):
        return [c[3] if c[3] != "coordinator" else f"coordinator {c[4]}" for c in self.calls
                if c[1:3] == ["-m", "steward.cli"]]

    def relays(self, sub):
        return [c for c in self.calls if c[1:3] == ["-m", "steward.cli"] and c[3:5] == ["coordinator", sub]]


def _to(store, jid, state, what="t", rule="x", evidence=None):
    store.set_state(jid, state, what=what, evidence=evidence or {}, rule=rule, judgment=Judgment.code)


def _running(store, goal="ZT phases 1+2", repo="/tmp/repo"):
    j = store.create_job(goal, repo)
    _to(store, j.job_id, JobState.CONTRACTED, "contracted", "contract")
    _to(store, j.job_id, JobState.RUNNING, "launched", "launch")
    return j


def _five_b(store, *, rule="recovery_budget_exhausted", approval=True):
    """5b165868: RUNNING -> red verification at H -> last_ruled_sha -> recovery park + annotated approval."""
    j = _running(store)
    store.add_check(j.job_id, commit_sha=B, command="pytest", exit_code=0, results={}, label="baseline")
    store.append_event(j.job_id, what="verification recorded",
                       evidence={"commit_sha": H, "new_failures": IDS, "fixed": [], "preexisting_failures": []},
                       rule="verify_at_exact_commit", judgment=Judgment.code)
    store.kv_set(j.job_id, "last_ruled_sha", H)
    _to(store, j.job_id, JobState.NEEDS_HUMAN, "recovery escalated to human", rule)
    rec = None
    if approval:
        rec = approvals.open_approval(store, j.job_id, "recovery budget exhausted; worker exited")
        approvals.annotate(store, j.job_id, rec["id"], recovery_rule="recovery_budget_exhausted")
    return j, rec


def _rerun(outcome="env_dependent"):
    def fn(**kw):
        ok = {i: "passed" for i in kw["ids"]}
        return reruns.RerunResult(outcome, kw["head_sha"], kw["base_sha"], kw["ids"], list(kw["ids"]), [ok, ok], ok,
                                  [], ["pytest"], [1.0], f"{outcome}: fake")
    return fn


def _daemon(home, tmp_path, run, clock, *, settings=None, cdb=None, **kw):
    s = settings or Settings(steward_home=home, steward_repo=tmp_path)
    cdb = cdb or db.CoordDB(tmp_path / "c.db")
    kw.setdefault("runner_alive", lambda _j: False)
    kw.setdefault("health_fn", lambda: None)
    kw.setdefault("watchdog_active", lambda: True)
    kw.setdefault("workspace_fn", lambda: "w1")
    kw.setdefault("head_fn", lambda _root: "")
    kw.setdefault("rerun_fn", _rerun())
    kw.setdefault("digest_fn", lambda **_k: None)
    kw.setdefault("tg_send", lambda _text: True)
    kw.setdefault("inline", True)
    d = daemon.Daemon(s, ro_factory=lambda: triggers.RoStore(home / "steward.db", home), cdb=cdb, run=run,
                      clock=clock, **kw)
    return d, cdb


def _cursor_before_park(cdb, store, job_id):
    """The scan never replays history on its first run: start the cursor just before the job's last event."""
    park = [e for e in store.events(job_id) if e.rule in triggers.COORD_RULES][-1]
    cdb.cursor_set("events", park.seq - 1)


def _count_asks(monkeypatch):
    sent = []
    monkeypatch.setattr(coordinator_link, "deliver_ask", lambda *a, **k: sent.append(k.get("reason")) or "delivered")
    return sent


def _ep(cdb):
    return cdb.episodes_since(0)[-1]


# ---------- the 5b165868 shape ----------

def test_five_b_shape_resolves_without_a_human(steward_home, tmp_path, monkeypatch):
    sent = _count_asks(monkeypatch)
    store = Store()
    j, rec = _five_b(store)
    t = [time.time()]
    fake = Fake(store)
    d, cdb = _daemon(steward_home, tmp_path, fake, lambda: t[0])
    _cursor_before_park(cdb, store, j.job_id)
    out = d.tick()
    assert out["triggers"] == 1 and len(out["opened"]) == 1
    eid = out["opened"][0]
    # hold first, then approve + steer in order
    assert fake.verbs() == ["coordinator hold", "approve", "steer"]
    assert store.kv_get(j.job_id, "coordinator_hold")["state"] == "open"
    assert store.kv_get(j.job_id, "coordinator_hold")["episode_id"] == eid
    steer = fake.calls[-1]
    assert steer[3:5] == ["steer", j.job_id] and steer[-2:] == ["--verb", "nudge"]
    assert store.get_job(j.job_id).state is JobState.RUNNING
    ep = _ep(cdb)
    assert ep["acted_at"] == t[0] and ep["unstuck_at"] == t[0] and ep["closed_at"] is None
    assert json.loads(ep["rungs_json"]) == [{"rung": "false_red", "classification": "env_dependent"}]
    assert out["acted"] and out["acted"][0]["eid"] == eid
    # own approval is not an owner event: no preemption
    t[0] += 60
    assert d.tick()["closed"] == []
    t[0] += 600
    out = d.tick()
    assert out["closed"] == [{"eid": eid, "outcome": "resolved"}]
    ep = _ep(cdb)
    assert ep["outcome"] == "resolved" and ep["rule_proposal_id"] == "42"
    tasks = [c for c in fake.calls if c[:2] == ["tasks", "add"]]
    assert len(tasks) == 1 and "--tag" in tasks[0] and "rule-proposal" in tasks[0]
    relay = fake.relays("episode")
    assert len(relay) == 1
    argv = relay[0]
    assert argv[argv.index("--outcome") + 1] == "resolved" and argv[argv.index("--rule-task") + 1] == "42"
    # the relayed episode closes the hold `resolved`, and nothing went to the owner
    hold = store.kv_get(j.job_id, "coordinator_hold")
    assert hold["state"] == "closed" and hold["how"] == "resolved"
    assert sent == []
    closed_ev = json.loads(_ep(cdb)["evidence_json"])     # the hold's outcome copied after the relay (digest column)
    assert closed_ev["how"] == "resolved" and closed_ev.get("delivery") is None and closed_ev["held"] is True
    assert not [e for e in store.events(j.job_id)
                if e.what == "creator_notified" and e.evidence.get("event") == "needs_you"]
    assert store.get_job(j.job_id).state is JobState.RUNNING
    ev = store.events_of(j.job_id, ("coordinator_episode",))[-1]
    assert ev.evidence["outcome"] == "resolved" and ev.evidence["rule_proposal_id"] == "42"


def test_second_resolution_of_the_same_kind_files_no_rule(steward_home, tmp_path, monkeypatch):
    _count_asks(monkeypatch)
    store = Store()
    t = [time.time()]
    cdb = db.CoordDB(tmp_path / "c.db")
    e0 = cdb.open_episode("other", "needs_human:x", 1, now=t[0] - 7200)
    cdb.close(e0, "resolved", "d", ["nudge"], now=t[0] - 7000,
              rungs=[{"rung": "false_red", "classification": "env_dependent"}])
    j, _ = _five_b(store)
    fake = Fake(store)
    d, _ = _daemon(steward_home, tmp_path, fake, lambda: t[0], cdb=cdb)
    _cursor_before_park(cdb, store, j.job_id)
    d.tick()
    t[0] += 700
    d.tick()
    assert _ep(cdb)["outcome"] == "resolved" and _ep(cdb)["rule_proposal_id"] is None
    assert not [c for c in fake.calls if c[:2] == ["tasks", "add"]]
    assert "--rule-task" not in fake.relays("episode")[0]


# ---------- nothing applicable ----------

def test_nothing_applicable_closes_asked_with_exactly_one_line(steward_home, tmp_path, monkeypatch):
    sent = _count_asks(monkeypatch)
    store = Store()
    j = _running(store)
    _to(store, j.job_id, JobState.NEEDS_HUMAN, "diagnosis says a human", "diagnosis_verdict_needs_human")
    t = [time.time()]
    plan = {"diagnosis": "needs owner", "actions": [], "ask": "owner call: pick the schema", "rule": None}
    fake = Fake(store, claude_plan=plan)
    d, cdb = _daemon(steward_home, tmp_path, fake, lambda: t[0])
    _cursor_before_park(cdb, store, j.job_id)
    out = d.tick()
    eid = out["opened"][0]
    assert out["closed"] == [{"eid": eid, "outcome": "asked"}]
    ep = _ep(cdb)
    assert ep["outcome"] == "asked" and "owner call: pick the schema" in ep["diagnosis"]
    assert ep["model_calls"] == 2 and len(ep["ask"]) <= 160 and "tried" in ep["ask"]
    assert len(sent) == 1                                  # the relay handler delivered the one line
    relay = fake.relays("episode")
    assert len(relay) == 1 and "--model-used" in relay[0]
    assert relay[0][relay[0].index("--outcome") + 1] == "asked"
    hold = store.kv_get(j.job_id, "coordinator_hold")
    assert hold["state"] == "delivered" and hold["how"] == "asked"
    closed_ev = json.loads(_ep(cdb)["evidence_json"])
    assert closed_ev["delivery"] == "delivered" and closed_ev["how"] == "asked"
    assert not [c for c in fake.calls if c[:2] == ["tasks", "add"]]
    # nothing else runs on later ticks
    n = len(fake.calls)
    t[0] += 60
    d.tick()
    assert len(fake.calls) == n and len(sent) == 1


def test_budget_exhausted_gives_up_and_tgs_once(steward_home, tmp_path, monkeypatch):
    sent = _count_asks(monkeypatch)
    store = Store()
    t = [time.time()]
    tgs = []
    s = Settings(steward_home=steward_home, steward_repo=tmp_path)
    cdb = db.CoordDB(tmp_path / "c.db")
    cdb.spend(t[0], usd=s.budget_daily_usd, codex_calls=s.budget_daily_codex_calls)
    for n in range(2):
        j = _running(store, goal=f"g{n}")
        _to(store, j.job_id, JobState.NEEDS_HUMAN, "diag", "diagnosis_verdict_needs_human")
        fake = Fake(store)
        d, _ = _daemon(steward_home, tmp_path, fake, lambda: t[0], settings=s, cdb=cdb,
                       tg_send=lambda text: tgs.append(text) or True)
        if n == 0:
            _cursor_before_park(cdb, store, j.job_id)
        d.tick()
        ep = cdb.open_for(j.job_id) or _ep(cdb)
        assert ep["outcome"] == "gave_up" and "coordinator model budget exhausted" in ep["diagnosis"]
        assert not [c for c in fake.calls if "codex" in c or "claude" in c]
    assert len(tgs) == 1 and "budget" in tgs[0] and cdb.budget_today(t[0])["tg_sent"] == 1
    assert len(sent) == 2


# ---------- preemption (R17) ----------

def test_owner_event_after_trigger_preempts_without_verbs(steward_home, tmp_path, monkeypatch):
    sent = _count_asks(monkeypatch)
    store = Store()
    j, _ = _five_b(store)
    store.append_event(j.job_id, what="steered", evidence={"text": "I'll take it"}, rule="operator_steer",
                       judgment=Judgment.human)
    t = [time.time()]
    fake = Fake(store)
    d, cdb = _daemon(steward_home, tmp_path, fake, lambda: t[0])
    cdb.cursor_set("events", store.events_of(j.job_id, ("recovery escalated to human",))[-1].seq - 1)
    out = d.tick()
    eid = out["opened"][0]
    assert out["closed"] == [{"eid": eid, "outcome": "preempted"}]
    assert fake.verbs() == ["coordinator hold", "coordinator episode"]
    assert _ep(cdb)["outcome"] == "preempted" and sent == []
    assert store.kv_get(j.job_id, "coordinator_hold")["how"] == "preempted"


def test_agent_event_with_actor_does_not_preempt(steward_home, tmp_path, monkeypatch):
    _count_asks(monkeypatch)
    store = Store()
    j, _ = _five_b(store)
    with actor.bind("operator_agent:claude-code"):
        store.append_event(j.job_id, what="steered", evidence={"text": "x"}, rule="operator_steer",
                           judgment=Judgment.human)
    t = [time.time()]
    fake = Fake(store)
    d, cdb = _daemon(steward_home, tmp_path, fake, lambda: t[0])
    cdb.cursor_set("events", store.events_of(j.job_id, ("recovery escalated to human",))[-1].seq - 1)
    d.tick()
    assert "approve" in fake.verbs()


# ---------- stops ----------

def test_kill_file_stops(steward_home, tmp_path):
    coordinator_link.state_dir().mkdir(parents=True)
    coordinator_link.kill_file().write_text("x")
    Store()
    d, _ = _daemon(steward_home, tmp_path, Fake(Store()), time.time)
    assert d.tick() == {"stopped": "KILL"}
    assert coordinator_link.read_heartbeat() is None
    assert d.run_forever(sleep=lambda _s: None) == 0


def test_disabled_stops(steward_home, tmp_path):
    Store()
    s = Settings(steward_home=steward_home, steward_repo=tmp_path, enabled=False)
    d, _ = _daemon(steward_home, tmp_path, Fake(Store()), time.time, settings=s)
    assert d.tick() == {"stopped": "disabled"}


def test_run_forever_survives_a_crashing_tick(steward_home, tmp_path, monkeypatch):
    Store()
    d, _ = _daemon(steward_home, tmp_path, Fake(Store()), time.time)
    n = [0]

    def tick():
        n[0] += 1
        if n[0] == 1:
            raise RuntimeError("boom")
        return {"stopped": "KILL"}
    monkeypatch.setattr(d, "tick", tick)
    assert d.run_forever(sleep=lambda _s: None) == 0 and n[0] == 2


# ---------- ask-only, ask_by ----------

def test_ask_only_episode_holds_then_closes_asked_with_one_line(steward_home, tmp_path, monkeypatch):
    sent = _count_asks(monkeypatch)
    store = Store()
    j, _ = _five_b(store)
    t = [time.time()]
    cdb = db.CoordDB(tmp_path / "c.db")
    for k in range(3):
        e = cdb.open_episode(j.job_id, "needs_human:recovery_budget_exhausted", 1, now=t[0] - 20000 + k)
        cdb.close(e, "asked", "d", [], now=t[0] - 19000 + k)
    fake = Fake(store)
    d, _ = _daemon(steward_home, tmp_path, fake, lambda: t[0], cdb=cdb)
    _cursor_before_park(cdb, store, j.job_id)
    out = d.tick()
    ep = _ep(cdb)
    assert ep["outcome"] == "asked" and "episode cap reached (3 in 24 h)" in ep["diagnosis"]
    assert out["closed"] == [{"eid": ep["episode_id"], "outcome": "asked"}]
    # hold first (the relay handler sends the line only for a held episode), then the close: exactly one line
    assert fake.verbs() == ["coordinator hold", "coordinator episode"]
    assert len(sent) == 1 and "episode cap reached" in sent[0]
    hold = store.kv_get(j.job_id, "coordinator_hold")
    assert hold["episode_id"] == ep["episode_id"] and hold["state"] == "delivered" and hold["how"] == "asked"


def test_ask_by_elapsed_closes_asked(steward_home, tmp_path, monkeypatch):
    sent = _count_asks(monkeypatch)
    store = Store()
    j, _ = _five_b(store)
    t = [time.time()]
    fake = Fake(store, apply_approve=False)              # Steward ignores the approve: the job stays parked
    d, cdb = _daemon(steward_home, tmp_path, fake, lambda: t[0])
    _cursor_before_park(cdb, store, j.job_id)
    d.tick()
    assert _ep(cdb)["acted_at"] is not None and _ep(cdb)["unstuck_at"] is None
    t[0] += 500
    assert d.tick()["closed"] == []
    t[0] += 600                                          # 1100 s >= ask_by_s (1080)
    out = d.tick()
    ep = _ep(cdb)
    assert out["closed"] == [{"eid": ep["episode_id"], "outcome": "asked"}]
    assert "false_red" in ep["ask"] and len(ep["ask"]) <= 160
    assert len(sent) == 1


def test_reparked_after_acting_holds_again_and_moves_on(steward_home, tmp_path, monkeypatch):
    _count_asks(monkeypatch)
    store = Store()
    j, rec = _five_b(store)
    t = [time.time()]
    fake = Fake(store, claude_plan={"diagnosis": "d", "actions": [], "ask": "still stuck", "rule": None})
    d, cdb = _daemon(steward_home, tmp_path, fake, lambda: t[0])
    _cursor_before_park(cdb, store, j.job_id)
    d.tick()
    assert store.get_job(j.job_id).state is JobState.RUNNING
    coordinator_link.close_hold(store, j.job_id, "superseded", now=t[0])   # the sweep: the job moved on
    _to(store, j.job_id, JobState.NEEDS_HUMAN, "recovery escalated to human", "recovery_budget_exhausted")
    t[0] += 30
    out = d.tick()
    assert out["attached"]
    assert fake.verbs().count("coordinator hold") == 2
    ep = _ep(cdb)
    # false_red already tried -> the model rung asked
    assert ep["outcome"] == "asked" and "still stuck" in ep["diagnosis"]
    assert store.kv_get(j.job_id, "coordinator_hold")["state"] == "delivered"


# ---------- job-less health (R18) ----------

def test_health_episode_acts_or_closes_noted_without_a_line(steward_home, tmp_path, monkeypatch):
    sent = _count_asks(monkeypatch)
    Store()
    t = [time.time()]
    red = {"components": {"control": {"ok": False, "detail": "stale code: run deploy-self", "red_since": t[0] - 500}}}
    fake = Fake(Store())
    d, cdb = _daemon(steward_home, tmp_path, fake, lambda: t[0], health_fn=lambda: red)
    out = d.tick()
    ep = _ep(cdb)
    assert ep["job_id"] == "health:control" and ep["outcome"] == "noted"
    assert out["closed"] == [{"eid": ep["episode_id"], "outcome": "noted"}]
    assert fake.calls == [] and sent == []


def test_cockpit_down_runs_up_then_resolves_when_green(steward_home, tmp_path):
    Store()
    t = [time.time()]
    health = [{"components": {"control": {"ok": False, "detail": "no control pane", "red_since": t[0] - 500}}}]
    fake = Fake(Store())
    d, cdb = _daemon(steward_home, tmp_path, fake, lambda: t[0], health_fn=lambda: health[0],
                     watchdog_active=lambda: False)
    d.tick()
    assert fake.verbs() == ["up"] and fake.calls[0][-2:] == ["--workspace", "w1"]
    health[0] = {"components": {"control": {"ok": True, "detail": "ok"}}}
    t[0] += 15
    d.tick()
    t[0] += 700
    d.tick()
    ep = _ep(cdb)
    assert ep["outcome"] == "resolved" and fake.verbs() == ["up"]


# ---------- precision ----------

def test_apply_precision_disables_a_rung_after_two_overturns(tmp_path):
    cdb = db.CoordDB(tmp_path / "c.db")
    now = time.time()
    for i in range(2):
        e = cdb.open_episode(f"j{i}", "t", i, now=now)
        cdb.close(e, "resolved", "d", ["nudge"], now=now + 1,
                  rungs=[{"rung": "false_red", "classification": "env_dependent"}])
        cdb.overturn(e, "wrong", now=now + 2)
    daemon.apply_precision(cdb, now + 3)
    assert cdb.rung_disabled("false_red") and not cdb.rung_disabled("superseded_verify")
    # re-enabled by hand: the old overturns no longer count
    assert coord_main.main(["rung", "enable", "false_red"], cdb=cdb, clock=lambda: now + 4) == 0
    daemon.apply_precision(cdb, now + 5)
    assert not cdb.rung_disabled("false_red")


# ---------- re-exec (R15), heartbeat, cursor ----------

def test_reexec_when_stale_and_idle_not_while_deploy_pending(steward_home, tmp_path):
    store = Store()
    heads = iter(["aaaa", "bbbb", "bbbb", "bbbb"])
    execs = []
    t = [time.time()]
    d, _ = _daemon(steward_home, tmp_path, Fake(store), lambda: t[0], head_fn=lambda _r: next(heads),
                   execv=lambda exe, argv: execs.append(argv))
    deploy_pending.mark(store, "jobx", now=t[0])
    d.tick()
    assert execs == []
    deploy_pending.clear(store)
    d.tick()
    assert execs and execs[0][1:] == ["-m", "steward.coordinator", "run"]


def test_no_reexec_when_heads_match(steward_home, tmp_path):
    Store()
    execs = []
    d, _ = _daemon(steward_home, tmp_path, Fake(Store()), time.time, head_fn=lambda _r: "same",
                   execv=lambda exe, argv: execs.append(argv))
    d.tick()
    assert execs == []


def test_heartbeat_each_tick_and_cursor_persisted(steward_home, tmp_path):
    store = Store()
    j = _running(store)
    t = [time.time()]
    d, cdb = _daemon(steward_home, tmp_path, Fake(store), lambda: t[0], head_fn=lambda _r: "cafe")
    out = d.tick()
    hb = coordinator_link.read_heartbeat()
    assert hb["ts"] == t[0] and hb["head"] == "cafe" and hb["root"] == str(tmp_path)
    first = cdb.cursor_get("events")
    assert first == store.events(j.job_id)[-1].seq == out["cursor"]
    store.append_event(j.job_id, what="note", evidence={}, rule="x", judgment=Judgment.code)
    t[0] += 15
    d.tick()
    assert cdb.cursor_get("events") == first + 1
    assert coordinator_link.read_heartbeat()["ts"] == t[0]


def test_worker_thread_keeps_the_heartbeat_honest(steward_home, tmp_path, monkeypatch):
    _count_asks(monkeypatch)
    store = Store()
    j, _ = _five_b(store)
    gate, entered = threading.Event(), threading.Event()

    def slow_rerun(**kw):
        entered.set()
        assert gate.wait(10)
        return _rerun()(**kw)
    t = [time.time()]
    fake = Fake(store)
    d, cdb = _daemon(steward_home, tmp_path, fake, lambda: t[0], inline=False, rerun_fn=slow_rerun)
    _cursor_before_park(cdb, store, j.job_id)
    try:
        d.tick()
        assert entered.wait(10)
        t[0] += 15
        out = d.tick()                                     # step in flight: the tick still returns
        assert out["acted"] == [] and coordinator_link.read_heartbeat()["ts"] == t[0]
        gate.set()
        for _ in range(200):
            if d.idle():
                break
            time.sleep(0.02)
        t[0] += 15
        out = d.tick()
        assert out["acted"] and out["acted"][0]["rung"] == "false_red"
    finally:
        gate.set()
        d.shutdown()
    assert store.get_job(j.job_id).state is JobState.RUNNING


# ---------- digest ----------

def test_digest_once_a_day(steward_home, tmp_path):
    Store()
    calls = []
    s = Settings(steward_home=steward_home, steward_repo=tmp_path, digest_at="00:00")
    t = [time.time()]
    d, cdb = _daemon(steward_home, tmp_path, Fake(Store()), lambda: t[0], settings=s,
                     digest_fn=lambda **kw: calls.append(kw) or "https://x")
    d.tick()
    d.tick()
    assert len(calls) == 1 and cdb.cursor_get("digest_day") == time.strftime("%Y-%m-%d", time.localtime(t[0]))


# ---------- CLI ----------

def test_main_tick_prints_json(steward_home, tmp_path, capsys):
    Store()
    rc = coord_main.main(["tick"], run=Fake(Store()), head_fn=lambda _r: "")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["triggers"] == 0 and "cursor" in out
    assert (coordinator_link.state_dir() / "coordinator.db").exists()
    assert (coordinator_link.state_dir() / "coordinator.log").exists()


def test_main_overturn_and_rung(steward_home, tmp_path, capsys):
    cdb = db.CoordDB(coordinator_link.state_dir() / "coordinator.db")
    e = cdb.open_episode("j1", "t", 1, now=time.time())
    cdb.close(e, "resolved", "d", [], now=time.time(), rungs=[{"rung": "false_red", "classification": "x"}])
    assert coord_main.main(["overturn", e, "wrong call"]) == 0
    assert cdb.episodes_since(0)[0]["overturn_why"] == "wrong call"
    assert coord_main.main(["overturn", e, "again"]) == 1
    assert coord_main.main(["rung", "disable", "false_red"]) == 0
    assert cdb.rung_disabled("false_red")
    assert coord_main.main(["rung", "enable", "false_red"]) == 0
    assert not cdb.rung_disabled("false_red")
    assert coord_main.main(["rung", "disable", "nonsense"]) == 2


def test_main_status_and_episodes(steward_home, tmp_path, capsys):
    Store()
    cdb = db.CoordDB(coordinator_link.state_dir() / "coordinator.db")
    e = cdb.open_episode("abcdef0123", "needs_human:x", 1, now=time.time())
    coordinator_link.write_heartbeat(cursor=7, head="cafe", root="/r")
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return R(0, "inactive\n")
    assert coord_main.main(["status", "--json"], run=run) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["cursor"] == 7 and st["loaded_head"] == "cafe" and st["unit"] == "inactive"
    assert [x["episode_id"] for x in st["open"]] == [e] and st["heartbeat_age_s"] < 60
    assert all(a[:2] == ["systemctl", "--user"] for a in seen)
    assert coord_main.main(["episodes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["episode_id"] == e
    assert coord_main.main(["episodes", "--since", "2000-01-01T00:00:00"]) == 0
    assert "abcdef01" in capsys.readouterr().out


def test_main_watch_and_digest_absent_or_present(steward_home, capsys):
    import importlib.util
    for sub, mod in (("watch", "steward.coordinator.watch"), ("digest", "steward.coordinator.digest")):
        if importlib.util.find_spec(mod) is None:
            assert coord_main.main([sub], run=lambda *a, **k: R(1)) == 2
            assert "not available" in capsys.readouterr().out


def test_tab_is_idempotent_by_label(steward_home, tmp_path):
    tabs, panes, calls = [], [], []

    def herdr(argv, **kw):
        calls.append(argv)
        if argv[:3] == ["herdr", "pane", "list"]:
            return R(0, json.dumps({"result": {"panes": [{"pane_id": "w9:p1", "workspace_id": "w9"}, *panes]}}))
        if argv[:3] == ["herdr", "tab", "list"]:
            return R(0, json.dumps({"result": {"tabs": tabs}}))
        if argv[:3] == ["herdr", "tab", "create"]:
            tabs.append({"tab_id": "w9:t2", "label": "coordinator", "workspace_id": "w9"})
            panes.append({"pane_id": "w9:p7", "tab_id": "w9:t2", "workspace_id": "w9"})
            return R(0, json.dumps({"result": {"tab": {"tab_id": "w9:t2"}, "root_pane": {"pane_id": "w9:p7"}}}))
        return R(0, "")
    assert coord_main.main(["tab"], run=herdr) == 0
    assert coord_main.main(["tab"], run=herdr) == 0
    creates = [c for c in calls if c[:3] == ["herdr", "tab", "create"]]
    assert creates == [["herdr", "tab", "create", "--workspace", "w9", "--label", "coordinator", "--no-focus"]]
    runs = [c for c in calls if c[:3] == ["herdr", "pane", "run"]]
    assert len(runs) == 2 and all(r[3] == "w9:p7" and r[4].startswith("tail -F ") for r in runs)
    assert runs[0][4].endswith("coordinator.log")


# ---------- review fixes: atomic evidence, idempotent close, a proposal for every novel resolution ----------

def test_main_tick_merge_during_a_worker_step_keeps_both_keys(steward_home, tmp_path, monkeypatch):
    from steward.coordinator import lifecycle
    _count_asks(monkeypatch)
    store = Store()
    j, _ = _five_b(store)
    t = [time.time()]
    other = db.CoordDB(tmp_path / "c.db")          # the main tick's own connection, a stale snapshot

    def rerun(**kw):
        snap = other.open_for(j.job_id)
        lifecycle.merge_ev(other, snap, last_trigger={"kind": "needs_human:x", "seq": 99, "detail": {}})
        return _rerun()(**kw)
    fake = Fake(store)
    d, cdb = _daemon(steward_home, tmp_path, fake, lambda: t[0], rerun_fn=rerun)
    _cursor_before_park(cdb, store, j.job_id)
    d.tick()
    ev = json.loads(_ep(cdb)["evidence_json"])
    assert ev["last_trigger"]["seq"] == 99                       # main's write survived the worker's merges
    assert ev["classification"] == "env_dependent" and "acted_seq" in ev and "open_seq" in ev
    # the reverse: a main-tick merge from a snapshot taken before the worker acted keeps the worker's keys
    stale = dict(_ep(cdb))
    stale["evidence_json"] = json.dumps({"trigger_detail": {}})
    lifecycle.merge_ev(other, stale, repark={"seq": 5, "held": True})
    ev = json.loads(_ep(cdb)["evidence_json"])
    assert ev["acted_seq"] and ev["repark"] == {"seq": 5, "held": True} and ev["classification"] == "env_dependent"
    lifecycle.merge_ev(other, stale, repark=None)
    assert "repark" not in json.loads(_ep(cdb)["evidence_json"])


def test_close_twice_closes_once_and_relays_once(steward_home, tmp_path, monkeypatch):
    from steward.coordinator import lifecycle
    _count_asks(monkeypatch)
    store = Store()
    j = _running(store)
    fake = Fake(store)
    s = Settings(steward_home=steward_home, steward_repo=tmp_path)
    cdb = db.CoordDB(tmp_path / "c.db")
    eid = cdb.open_episode(j.job_id, "needs_human:x", 1, now=time.time())
    ep = cdb.open_for(j.job_id)
    ro = triggers.RoStore(steward_home / "steward.db", steward_home)
    first = lifecycle.close(cdb, ro, dict(ep), "resolved", "done", now=time.time(), settings=s, workspace="w1",
                            run=fake)
    second = lifecycle.close(cdb, ro, dict(ep), "preempted", "late", now=time.time(), settings=s, workspace="w1",
                             run=fake)
    assert first == {"eid": eid, "outcome": "resolved"} and second is None
    assert cdb.episodes_since(0)[0]["outcome"] == "resolved"
    assert len(fake.relays("episode")) == 1
    assert cdb.close(eid, "asked", "x", [], now=time.time()) is False
    assert cdb.episodes_since(0)[0]["outcome"] == "resolved"


def test_novel_resolution_without_rule_text_still_files_a_proposal(steward_home, tmp_path, monkeypatch):
    _count_asks(monkeypatch)
    store = Store()
    j = _running(store)                                      # RUNNING, runner dead, watchdog inactive
    t = [time.time() + 4000]                                 # idle > orphan_s
    fake = Fake(store)
    d, cdb = _daemon(steward_home, tmp_path, fake, lambda: t[0], watchdog_active=lambda: False)
    out = d.tick()
    assert out["opened"] and "runner-restart" in fake.verbs()
    assert json.loads(_ep(cdb)["rungs_json"]) == [{"rung": "dead_runner", "classification": "dead_runner"}]
    _to(store, j.job_id, JobState.WORKER_FINISHED, "worker finished", "x")
    _to(store, j.job_id, JobState.CHECKS_PASSED, "checks passed", "x")
    t[0] += 15
    out = d.tick()
    ep = _ep(cdb)
    assert ep["outcome"] == "resolved" and ep["rule_proposal_id"] == "42"
    tasks = [c for c in fake.calls if c[:2] == ["tasks", "add"]]
    assert len(tasks) == 1 and tasks[0][2].startswith("rule: dead_runner: Steward should settle this itself")
