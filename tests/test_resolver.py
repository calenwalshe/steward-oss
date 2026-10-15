"""Risk-routed resolvers for held approvals (steward.resolver). No real model, codex, Jev or Herdr:
every subprocess is a FakePopen, Jev is a FakeTransport, the store is a tmp one."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from steward import approvals as approvals_module
from steward import cli
from steward import resolver as rs
from steward import watchers as feed
from steward.contract import Contract, store_contract
from steward.jev_observer import FakeTransport, JevObserver, TransportResponse
from steward.models import JobState, Judgment
from steward.store import Store
from steward.supervisors import supervisor_rows

WT = "/home/user/.local/state/steward/worktrees/j"


# -- classification -------------------------------------------------------------------------


def _tier(cmd, reason, tool="Bash", inp=None):
    return rs.classify_command(tool, inp if inp is not None else {"command": cmd}, reason, WT)[0]


@pytest.mark.parametrize("cmd,reason,tier", [
    # LOW: rule false positives on local text, scratch writes
    ("git commit -m 'document server.pem handling'", "reads a credential path: the .pem", "low"),
    ("echo 'keys live in ~/.ssh/ ok' > notes.txt", "reads a credential path: ~/.ssh/", "low"),
    ("cp x /var/tmp/x", "cp writes to a path outside the worktree: /var/tmp/x", "low"),
    # MEDIUM: writes/deletes outside the worktree, destructive git, installs
    ("rm -rf /home/user/scratch-test-dir", "rm targets a path outside the worktree: /home/user/scratch-test-dir", "medium"),
    ("cp a /home/user/out/a", "cp writes to a path outside the worktree: /home/user/out/a", "medium"),
    ("git reset --hard HEAD~3", "", "medium"),
    ("pip install requests", "", "medium"),
    # HIGH: sudo, credentials read, remote, deploy infra, unknown
    ("sudo apt update", "sudo", "high"),
    ("cat ~/.ssh/id_rsa", "reads a credential path: ~/.ssh/id_rsa", "high"),
    ("cat server.pem", "reads a credential path: server.pem", "high"),
    ("ssh some-host uptime", "remote transfer (ssh/scp/rsync)", "high"),
    ("rsync -a . host:/x", "remote transfer (ssh/scp/rsync)", "high"),
    ("terraform apply", "terraform apply", "high"),
    ("kubectl apply -f x.yaml", "kubectl apply", "high"),
    ("rm /etc/hosts", "rm targets a path outside the worktree: /etc/hosts", "high"),
    ("frobnicate --now", "some future rule", "high"),  # unknown -> high
    ("frobnicate", "", "high"),
    # FLOOR: outbound contact, publish, push
    ("git push origin main", "git push", "floor"),
    ("gh pr merge 12", "gh pr merge", "floor"),
    ("gh release create v1", "gh release", "floor"),
    ("npm publish", "package publish", "floor"),
    ("twine upload dist/*", "twine upload (PyPI publish)", "floor"),
    ("docker push me/img", "docker push", "floor"),
    ("vercel deploy --prod", "deploy", "floor"),
    ("tg send 'hi'", "tg (telegram)", "floor"),
    ("mail -s hi a@b.c", "mail", "floor"),
    ("curl -X POST https://example.com/hook -d x", "curl/wget with a write method to a remote host", "floor"),
    ("curl -X POST http://localhost:8000/x -d x", "curl/wget with a write method to a remote host", "high"),
])
def test_classify_by_category(cmd, reason, tier):
    assert _tier(cmd, reason) == tier


def test_floor_matched_on_command_text_even_when_reason_is_wrong():
    assert _tier("cd x && git push --force", "reads a credential path: .pem") == "floor"
    assert _tier("git -C repo push", "") == "floor"


def test_write_tool_outside_worktree_tiers_by_path():
    w = lambda p: rs.classify_command("Write", {"file_path": p}, f"Write path outside the worktree: {p}", WT)[0]  # noqa: E731
    assert w("/var/tmp/a.txt") == "low"
    assert w("/home/user/notes/a.txt") == "medium"
    assert w("/etc/cron.d/x") == "high"


def test_extra_floor_patterns_from_config_only_add():
    assert rs.classify_command("Bash", {"command": "make ship"}, "", WT, ("make ship",))[0] == "floor"
    assert rs.classify_command("Bash", {"command": "git push"}, "", WT, ())[0] == "floor"  # built-ins can't be removed


# -- helpers --------------------------------------------------------------------------------


class FakePopen:
    instances: list = []
    reply: object = None  # dict -> JSON reply; str -> raw text; None -> nothing
    finish = True
    cost = 0.03

    def __init__(self, argv, **kw):
        self.argv, self.kw = argv, kw
        self.rc = None
        self.pid = 999
        self.killed = False
        text = FakePopen.reply if isinstance(FakePopen.reply, str) else json.dumps(FakePopen.reply)
        if FakePopen.finish and FakePopen.reply is not None:
            if "codex" in argv:
                with open(argv[argv.index("-o") + 1], "w") as fh:
                    fh.write(text)
            else:
                kw["stdout"].write(json.dumps({"type": "result", "result": text, "total_cost_usd": FakePopen.cost}))
                kw["stdout"].flush()
            self.rc = 0
        FakePopen.instances.append(self)

    def poll(self):
        return self.rc

    def kill(self):
        self.killed = True
        self.rc = -9


@pytest.fixture
def popen():
    FakePopen.instances, FakePopen.finish, FakePopen.cost = [], True, 0.03
    FakePopen.reply = {"decision": "approve", "reason": "false positive", "risk_notes": ""}
    return FakePopen


class Clock:
    def __init__(self):
        self.t = datetime.now(timezone.utc).timestamp() + 60  # every approval is already 60s old

    def __call__(self):
        return self.t


def _setup(steward_home, notes=None, popen_cls=FakePopen, cfg=None, jev=None, clock=None):
    store = Store(steward_home)
    job = store.create_job("ship the widget", "/tmp/somerepo")
    store_contract(store, job.job_id, Contract(goal="ship the widget", boundaries=["stay in the repo"], done_when=["tests pass"]))
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (JobState.RUNNING.value, job.job_id))
    eng = rs.ResolverEngine(store, popen=popen_cls, clock=clock or Clock(), config=cfg or rs.ResolverConfig(), jev=jev,
                            notify=(lambda *a: notes.append(a)) if notes is not None else None,
                            activity_fn=lambda s, j: {"turns": [{"kind": "said", "text": "writing the key doc"}]})
    return store, job.job_id, eng


def _hold(store, job_id, eng, command, reason, tool="Bash"):
    inp = {"command": command} if tool == "Bash" else {"file_path": command}
    rec = approvals_module.open_approval(store, job_id, f"worker permission: {tool} {inp}"[:300], kind="permission",
                                         worker_request_id="r-" + str(len(store.events(job_id))))
    deferred = eng.record_hold(job_id, rec["id"], tool, inp, reason)
    return rec, deferred


LOW_HOLD = ("git commit -m 'note about server.pem'", "reads a credential path: the .pem")
MEDIUM_HOLD = ("rm -rf /home/user/scratch-test-dir", "rm targets a path outside the worktree: /home/user/scratch-test-dir")
HIGH_HOLD = ("ssh some-host uptime", "remote transfer (ssh/scp/rsync)")
FLOOR_HOLD = ("git push origin main", "git push")


def _finish_fake(store, jid, rec, proc):
    out = store.kv_get(jid, f"resolver_inflight:{rec['id']}")["out"]
    with open(out, "w") as fh:
        fh.write(json.dumps({"result": json.dumps(FakePopen.reply), "total_cost_usd": 0.01}))
    proc.rc = 0


def _run(eng, job_id, ticks=2):
    for _ in range(ticks):
        eng.step(job_id)


def _decisions(store, job_id):
    return [e for e in store.events(job_id) if e.what == "resolver_decision"]


# -- each tier invokes the right model/CLI ---------------------------------------------------


def test_low_tier_runs_sonnet_via_claude_p_readonly(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    _hold(store, jid, eng, *LOW_HOLD)
    eng.step(jid)
    argv = popen.instances[0].argv
    assert argv[:5] == ["env", "-u", "ANTHROPIC_API_KEY", "claude", "-p"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "Bash" in argv[argv.index("--disallowedTools"):]


def test_medium_tier_runs_opus(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    _hold(store, jid, eng, *MEDIUM_HOLD)
    eng.step(jid)
    argv = popen.instances[0].argv
    assert argv[argv.index("--model") + 1] == "opus" and "claude" in argv


def test_high_tier_runs_codex_astra_read_only(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    _hold(store, jid, eng, *HIGH_HOLD)
    eng.step(jid)
    argv = popen.instances[0].argv
    assert argv[:5] == ["env", "-u", "OPENAI_API_KEY", "codex", "exec"]
    assert argv[argv.index("-m") + 1] == "gpt-6-astra" and argv[argv.index("-s") + 1] == "read-only"


def test_brief_has_goal_command_rule_turns_and_worktree_redacted(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    _hold(store, jid, eng, "echo password=hunter2 > /home/user/x.txt", "redirect targets a path outside the worktree: /home/user/x.txt")
    eng.step(jid)
    prompt = next((store.home / "resolver" / jid).glob("*.prompt.txt")).read_text()
    for needle in ("ship the widget", "stay in the repo", "redirect targets a path outside", "writing the key doc",
                   str(store.home / "worktrees" / jid), "echo password=[redacted]", '"decision"'):
        assert needle in prompt
    assert "hunter2" not in prompt


# -- apply -----------------------------------------------------------------------------------


def test_approve_is_applied_with_resolver_decided_by_and_relayed(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    rec, deferred = _hold(store, jid, eng, *LOW_HOLD)
    assert deferred == "low"
    _run(eng, jid)
    a = approvals_module.get_approval(store, jid, rec["id"])
    assert a["status"] == "approved" and a["decided_by"] == "resolver:sonnet"
    ev = next(e for e in store.events(jid) if e.what == "approval approved")
    assert ev.judgment == Judgment.model_informed_code_authorized
    d = _decisions(store, jid)[0]
    assert d.evidence["applied"] == "approve" and d.evidence["tier"] == "low" and d.evidence["cost_usd"] == 0.03
    # the runner's existing relay path answers the worker for any decided permission approval
    from steward.worker import FakeWorker, WorkerObservation
    from steward.runner import Runner
    from steward.recovery import RecoveryPolicy
    from steward.diagnosis import FakeDiagnoser, Diagnosis
    from steward.runner_exec import FakeRunner
    worker = FakeWorker([WorkerObservation(lifecycle="running", output_tail="", pid_alive=True, ts="t")])
    runner = Runner(store, lambda *_: worker, FakeRunner(), FakeDiagnoser(Diagnosis("needs_human", "", "")), policy=RecoveryPolicy(),
                    resolver=eng)
    runner._relay_permission_decisions(jid, worker)
    assert approvals_module.get_approval(store, jid, rec["id"])["relayed_at"]


def test_deny_is_applied(steward_home, popen):
    popen.reply = {"decision": "deny", "reason": "would delete user data", "risk_notes": "no"}
    store, jid, eng = _setup(steward_home)
    rec, _ = _hold(store, jid, eng, *MEDIUM_HOLD)
    _run(eng, jid)
    a = approvals_module.get_approval(store, jid, rec["id"])
    assert a["status"] == "denied" and a["decided_by"] == "resolver:opus"


def test_escalate_keeps_pending_notifies_with_reason_and_panel_shows_it(steward_home, popen):
    popen.reply = {"decision": "escalate_human", "reason": "cannot tell what the target holds", "risk_notes": "irreversible"}
    notes = []
    store, jid, eng = _setup(steward_home, notes)
    rec, _ = _hold(store, jid, eng, *MEDIUM_HOLD)
    assert not notes  # the initial ping is deferred while a resolver looks
    _run(eng, jid)
    a = approvals_module.get_approval(store, jid, rec["id"])
    assert a["status"] == "pending" and not a["resolver_pending"]
    assert notes and notes[0][1] == "NEEDS_YOU" and "cannot tell" in notes[0][3]
    from steward import evidence
    panel = "\n".join(evidence.panel(store, jid, 80))
    assert "resolver opus" in panel and "cannot tell what the target holds" in panel and "irreversible" in panel


def test_one_attempt_per_approval(steward_home, popen):
    popen.reply = {"decision": "escalate_human", "reason": "unsure", "risk_notes": ""}
    store, jid, eng = _setup(steward_home)
    _hold(store, jid, eng, *MEDIUM_HOLD)
    _run(eng, jid, 4)
    assert len(popen.instances) == 1


def test_too_young_approvals_wait(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    eng.clock = lambda: datetime.now(timezone.utc).timestamp() + 5
    _hold(store, jid, eng, *LOW_HOLD)
    eng.step(jid)
    assert popen.instances == []


def test_polls_later_ticks_when_still_running(steward_home, popen):
    popen.finish = False
    store, jid, eng = _setup(steward_home)
    rec, _ = _hold(store, jid, eng, *LOW_HOLD)
    eng.step(jid)
    assert approvals_module.get_approval(store, jid, rec["id"])["status"] == "pending"
    proc = popen.instances[0]
    _finish_fake(store, jid, rec, proc)
    eng.step(jid)
    assert approvals_module.get_approval(store, jid, rec["id"])["status"] == "approved"


def test_decision_after_human_already_answered_is_discarded(steward_home, popen):
    popen.finish = False
    store, jid, eng = _setup(steward_home)
    rec, _ = _hold(store, jid, eng, *LOW_HOLD)
    eng.step(jid)
    approvals_module.decide(store, jid, rec["id"], "deny")
    proc = popen.instances[0]
    _finish_fake(store, jid, rec, proc)
    eng.step(jid)
    assert approvals_module.get_approval(store, jid, rec["id"])["decided_by"] == "human"
    assert _decisions(store, jid)[0].evidence["applied"] == "discarded"


# -- strict parsing / failure -> escalate ------------------------------------------------------


@pytest.mark.parametrize("text,ok", [
    ('{"decision":"approve","reason":"fine","risk_notes":""}', True),
    ('```json\n{"decision":"deny","reason":"no"}\n```', True),
    ('sure! {"decision":"approve","reason":"fine"}', False),
    ('{"decision":"yes","reason":"fine"}', False),
    ('{"decision":"approve"}', False),
    ('{"decision":"approve","reason":"  "}', False),
    ('{"decision":"approve","reason":"x","extra":1}', False),
    ('{"decision":"approve","reason":5}', False),
    ('[1]', False), ('', False), ('not json', False),
])
def test_parse_reply_is_strict(text, ok):
    assert (rs.parse_reply(text) is not None) is ok


@pytest.mark.parametrize("reply", ["I think it is fine", '{"decision":"maybe","reason":"x"}', {"decision": "approve"}])
def test_malformed_output_escalates(steward_home, popen, reply):
    popen.reply = reply
    notes = []
    store, jid, eng = _setup(steward_home, notes)
    rec, _ = _hold(store, jid, eng, *LOW_HOLD)
    _run(eng, jid)
    assert approvals_module.get_approval(store, jid, rec["id"])["status"] == "pending"
    d = _decisions(store, jid)[0]
    assert d.evidence["applied"] == "escalate_human" and d.evidence["error"] and notes


def test_timeout_kills_and_escalates(steward_home, popen):
    popen.finish = False
    notes = []
    clock = Clock()
    store, jid, eng = _setup(steward_home, notes, clock=clock)
    rec, _ = _hold(store, jid, eng, *LOW_HOLD)
    eng.step(jid)
    clock.t += rs.TIMEOUT_S + 1
    eng.step(jid)
    assert popen.instances[0].killed
    assert approvals_module.get_approval(store, jid, rec["id"])["status"] == "pending"
    assert "timed out" in _decisions(store, jid)[0].evidence["reason"] and notes


def test_spawn_failure_escalates(steward_home):
    def boom(*a, **k):
        raise OSError("no claude")

    store, jid, eng = _setup(steward_home, popen_cls=boom)
    rec, _ = _hold(store, jid, eng, *LOW_HOLD)
    eng.step(jid)
    assert approvals_module.get_approval(store, jid, rec["id"])["status"] == "pending"
    assert "could not start" in _decisions(store, jid)[0].evidence["reason"]


# -- authority is enforced in code -------------------------------------------------------------


def test_floor_never_auto_approved_even_when_resolver_says_approve(steward_home, popen):
    notes = []
    store, jid, eng = _setup(steward_home, notes)
    rec, deferred = _hold(store, jid, eng, *FLOOR_HOLD)
    assert deferred is None  # a floor hold pings Sam at once (caller notifies)
    _run(eng, jid)
    a = approvals_module.get_approval(store, jid, rec["id"])
    assert a["status"] == "pending"
    d = _decisions(store, jid)[0].evidence
    assert d["decision"] == "approve" and d["applied"] == "escalate_human" and d["tier"] == "floor"
    assert "floor" in d["reason"]
    assert popen.instances[0].argv[popen.instances[0].argv.index("-m") + 1] == "gpt-6-astra"


def test_floor_deny_is_allowed(steward_home, popen):
    popen.reply = {"decision": "deny", "reason": "pushing to a remote is out of scope", "risk_notes": ""}
    store, jid, eng = _setup(steward_home)
    rec, _ = _hold(store, jid, eng, *FLOOR_HOLD)
    _run(eng, jid)
    assert approvals_module.get_approval(store, jid, rec["id"])["status"] == "denied"


def test_enforce_low_cannot_approve_medium_or_high_and_never_floor():
    approve = rs.Reply("approve", "ok")
    assert rs.enforce("low", "low", approve)[0] == "approve"
    assert rs.enforce("medium", "low", approve)[0] == "escalate_human"
    assert rs.enforce("high", "low", approve)[0] == "escalate_human"
    assert rs.enforce("high", "medium", approve)[0] == "escalate_human"
    assert rs.enforce("medium", "medium", approve)[0] == "approve"
    assert rs.enforce("high", "high", approve)[0] == "approve"
    for resolver_tier in ("low", "medium", "high"):
        assert rs.enforce("floor", resolver_tier, approve)[0] == "escalate_human"
    assert rs.enforce("floor", "high", rs.Reply("deny", "no"))[0] == "deny"
    assert rs.enforce("bogus", "high", approve)[0] == "escalate_human"


def test_acceptance_and_general_approvals_are_never_touched(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    rec = approvals_module.open_approval(store, jid, "checks passed; acceptance unresolved", kind="acceptance")
    _run(eng, jid)
    assert popen.instances == [] and approvals_module.get_approval(store, jid, rec["id"])["status"] == "pending"


# -- Jev: bump up only ---------------------------------------------------------------------------


def _jev(choice, conf=0.9):
    payload = {"answers": {"risk": {"choice": choice, "confidence": conf, "probabilities": {}}}}
    transport = FakeTransport(TransportResponse(200, json.dumps(payload).encode()))
    return JevObserver(transport=transport, enabled=True, api_key_fn=lambda: "k"), transport


def test_jev_can_bump_up(steward_home, popen):
    jev, transport = _jev("high")
    store, jid, eng = _setup(steward_home, jev=jev)
    _hold(store, jid, eng, *LOW_HOLD)
    eng.step(jid)
    assert "codex" in popen.instances[0].argv
    ev = next(e for e in store.events(jid) if e.what == "risk_classified")
    assert ev.evidence["tier"] == "high" and ev.evidence["by"] == "jev" and "jev bumped" in ev.evidence["reason"]
    body = json.loads(transport.calls[0]["body"])
    assert "risk" in body["questions"] and "note about" in body["state"]["held_call"]


def test_jev_can_never_bump_down_or_to_floor(steward_home, popen):
    jev, _ = _jev("low")
    store, jid, eng = _setup(steward_home, jev=jev)
    _hold(store, jid, eng, *MEDIUM_HOLD)
    eng.step(jid)
    ev = next(e for e in store.events(jid) if e.what == "risk_classified")
    assert ev.evidence["tier"] == "medium" and ev.evidence["by"] == "rules"
    assert rs.bump("high", "low") == "high" and rs.bump("low", "high") == "high"
    assert "floor" not in __import__("steward.jev_observer", fromlist=["x"]).RISK_CHOICES


def test_jev_unsure_or_unavailable_leaves_rules_tier(steward_home, popen):
    jev, _ = _jev("high", conf=0.2)
    store, jid, eng = _setup(steward_home, jev=jev)
    _hold(store, jid, eng, *LOW_HOLD)
    eng.step(jid)
    assert next(e for e in store.events(jid) if e.what == "risk_classified").evidence["tier"] == "low"
    store2, jid2, eng2 = _setup(steward_home, jev=JevObserver(enabled=False))
    _hold(store2, jid2, eng2, *LOW_HOLD)
    eng2.step(jid2)
    assert next(e for e in store2.events(jid2) if e.what == "risk_classified").evidence["by"] == "rules"


def test_risk_classified_recorded_by_rules(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    _hold(store, jid, eng, *HIGH_HOLD)
    eng.step(jid)
    ev = next(e for e in store.events(jid) if e.what == "risk_classified")
    assert ev.evidence["tier"] == "high" and ev.evidence["by"] == "rules" and ev.evidence["reason"]


# -- budget, switch, config ----------------------------------------------------------------------


@pytest.mark.legacy_ownership_off
def test_budget_per_hour_exhausted_escalates_without_calling(steward_home, popen):
    notes = []
    store, jid, eng = _setup(steward_home, notes, cfg=rs.ResolverConfig(max_per_hour=2))
    recs = [_hold(store, jid, eng, f"git commit -m 'x{i}.pem'", "reads a credential path: x.pem")[0] for i in range(3)]
    _run(eng, jid)
    assert len(popen.instances) == 2
    third = approvals_module.get_approval(store, jid, recs[2]["id"])
    assert third["status"] == "pending" and "budget" in third["resolver"]["reason"] and notes


@pytest.mark.legacy_ownership_off
def test_spend_cap_uses_reported_cost(steward_home, popen):
    popen.cost = 5.0
    store, jid, eng = _setup(steward_home, cfg=rs.ResolverConfig(max_usd_per_hour=1.0))
    _hold(store, jid, eng, *LOW_HOLD)
    _run(eng, jid)
    rec2, _ = _hold(store, jid, eng, "git commit -m 'b.pem'", "reads a credential path: b.pem")
    _run(eng, jid)
    assert len(popen.instances) == 1
    assert "spend cap" in approvals_module.get_approval(store, jid, rec2["id"])["resolver"]["reason"]


def test_per_job_switch_off_means_no_resolution_and_immediate_ping(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    store.kv_set(jid, rs.SWITCH_KEY, "off")
    rec, deferred = _hold(store, jid, eng, *LOW_HOLD)
    assert deferred is None
    _run(eng, jid)
    assert popen.instances == [] and approvals_module.get_approval(store, jid, rec["id"])["status"] == "pending"


def test_globally_disabled(steward_home, popen):
    store, jid, eng = _setup(steward_home, cfg=rs.ResolverConfig(enabled=False))
    _, deferred = _hold(store, jid, eng, *LOW_HOLD)
    _run(eng, jid)
    assert deferred is None and popen.instances == []


def test_load_config_defaults_toml_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("STEWARD_RESOLVER")
    c = rs.load_config(tmp_path)
    assert (c.enabled, c.low, c.medium, c.high) == (True, "sonnet", "opus", "gpt-6-astra")
    (tmp_path / "steward.toml").write_text('[resolver]\nenabled = true\nlow = "haiku"\nmedium = "opus"\nhigh = "gpt-6-astra"\nfloor = ["make ship"]\n')
    c = rs.load_config(tmp_path)
    assert c.low == "haiku" and c.floor == ("make ship",)
    (tmp_path / "steward.toml").write_text("[resolver]\nenabled = false\n")
    assert rs.load_config(tmp_path).enabled is False
    monkeypatch.setenv("STEWARD_RESOLVER", "0")
    (tmp_path / "steward.toml").write_text("[resolver]\nenabled = true\n")
    assert rs.load_config(tmp_path).enabled is False


# -- alerts, visibility, log ---------------------------------------------------------------------


def test_alerts_skip_asks_a_resolver_is_still_looking_at(steward_home, popen):
    from steward import alerts

    store, jid, eng = _setup(steward_home)
    _hold(store, jid, eng, *LOW_HOLD)
    assert not [r for r in alerts.reasons(store, jid) if r[0] == "needs_you"]
    popen.reply = {"decision": "escalate_human", "reason": "unsure", "risk_notes": ""}
    _run(eng, jid)
    assert [r for r in alerts.reasons(store, jid) if r[0] == "needs_you"]


def test_supervisor_row_and_feed_lines(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    _hold(store, jid, eng, *LOW_HOLD)
    popen.reply = {"decision": "approve", "reason": ".pem false positive", "risk_notes": ""}
    _run(eng, jid)
    popen.reply = {"decision": "escalate_human", "reason": "unsure about target", "risk_notes": ""}
    _hold(store, jid, eng, *MEDIUM_HOLD)
    _run(eng, jid)
    row = next(r for r in supervisor_rows(store, jid) if r["role"] == "resolver")
    assert row["model"] == "auto" and row["switch"] == "on" and row["next"] == "off"
    assert "2 resolved (1 approve, 1 escalate)" in row["display"] and "last: escalate unsure about target" in row["display"]
    lines = [t for _, r, t in feed.collect(store.events(jid)) if r == "resolver"]
    assert lines == ["resolver sonnet: approve — .pem false positive", "resolver opus: escalate — unsure about target"]
    store.kv_set(jid, rs.SWITCH_KEY, "off")
    row = next(r for r in supervisor_rows(store, jid) if r["role"] == "resolver")
    assert row["status"] == "off" and row["next"] == "on"


def test_resolver_log_command(steward_home, popen, capsys):
    store, jid, eng = _setup(steward_home)
    _hold(store, jid, eng, *LOW_HOLD)
    _run(eng, jid)
    _hold(store, jid, eng, *FLOOR_HOLD)
    _run(eng, jid)
    lines = rs.log_lines(store, jid)
    assert len(lines) == 2
    assert "sonnet -> approve" in lines[0] and "false positive" in lines[0]
    assert "gpt-6-astra -> escalate_human (said approve)" in lines[1]
    assert cli.main(["resolver", "log", jid]) == 0
    out = capsys.readouterr().out
    assert "sonnet -> approve" in out and "gpt-6-astra -> escalate_human" in out
    assert cli.main(["resolver", "log"]) == 0 and "sonnet -> approve" in capsys.readouterr().out
    assert cli.main(["resolver", "log", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["applied"] == "approve"


def test_supervisor_switch_command_reaches_resolver(steward_home):
    p = cli._build_parser()
    ns = p.parse_args(["supervisor", "abc", "resolver", "off"])
    assert cli._op_and_args_for(ns) == ("supervisor", {"job_id": "abc", "role": "resolver", "state": "off"})
    handlers = cli._build_control_handlers(store := Store(steward_home))
    job = store.create_job("g", "/repo")
    r = handlers["supervisor"]({"args": {"job_id": job.job_id, "role": "resolver", "state": "off"}})
    assert r["accepted"] and rs.job_switch(store, job.job_id) == "off"
    r = handlers["supervisor"]({"args": {"job_id": job.job_id, "role": "resolver", "state": "toggle"}})
    assert r["accepted"] and rs.job_switch(store, job.job_id) == "on"


# -- runner wiring -------------------------------------------------------------------------------


def test_runner_holds_defers_ping_then_resolver_applies(steward_home, popen):
    from steward.diagnosis import Diagnosis, FakeDiagnoser
    from steward.recovery import RecoveryPolicy
    from steward.runner import Runner
    from steward.runner_exec import FakeRunner
    from steward.worker import FakeWorker, WorkerObservation

    store, jid, eng = _setup(steward_home)
    notes = []
    worker = FakeWorker([WorkerObservation(lifecycle="running", output_tail="", pid_alive=True, ts="t")])
    runner = Runner(store, lambda *_: worker, FakeRunner(), FakeDiagnoser(Diagnosis("needs_human", "", "")),
                    policy=RecoveryPolicy(), resolver=eng)
    runner._notify = lambda *a: notes.append(a)
    frame = type("F", (), {"seq": 3, "body": {"request_id": "r1", "tool": "Bash", "input": {"command": "echo ~/.ssh/id_rsa"}}})()
    runner._open_fabric_permission(jid, frame)
    rec = approvals_module.list_approvals(store, jid)[0]
    assert rec["status"] == "pending" and rec["resolver_pending"] and not notes  # held, ping deferred
    hold = store.kv_get(jid, f"hold:{rec['id']}")
    assert hold["tool"] == "Bash" and hold["reason"].startswith("reads a credential path")
    _run(eng, jid)
    assert approvals_module.get_approval(store, jid, rec["id"])["decided_by"] == "resolver:sonnet"


def test_runner_floor_hold_pings_immediately(steward_home, popen):
    from steward.diagnosis import Diagnosis, FakeDiagnoser
    from steward.recovery import RecoveryPolicy
    from steward.runner import Runner
    from steward.runner_exec import FakeRunner
    from steward.worker import FakeWorker, WorkerObservation

    store, jid, eng = _setup(steward_home)
    notes = []
    worker = FakeWorker([WorkerObservation(lifecycle="running", output_tail="", pid_alive=True, ts="t")])
    runner = Runner(store, lambda *_: worker, FakeRunner(), FakeDiagnoser(Diagnosis("needs_human", "", "")),
                    policy=RecoveryPolicy(), resolver=eng)
    runner._notify = lambda *a: notes.append(a)
    frame = type("F", (), {"seq": 3, "body": {"request_id": "r1", "tool": "Bash", "input": {"command": "git push origin main"}}})()
    runner._open_fabric_permission(jid, frame)
    assert len(notes) == 1 and notes[0][1] == "NEEDS_YOU"


# -- Codex MCP card (job 6369d958 stall), full read visibility, opaque prompts ----------------------------

_CODEX_PANE = (
    'Allow Stew to run tool "stew.get_recent_events"?\n{"job_id": "6369d958", "limit": 20}\n'
    "1. Allow\n2. Allow for this session\n3. Always allow\n4. Cancel\nenter to submit | esc to cancel\n"
)


def _pane_hold(store, jid, pane_text):
    """A pane-scraped approval exactly as the runner opens it (no recorded hold): heartbeat's extraction."""
    from steward.heartbeat import extract_permission_request

    return approvals_module.open_approval(
        store, jid, f"worker permission: {extract_permission_request(pane_text)}", kind="permission")


def test_codex_stew_readonly_card_is_approved_by_rules_without_a_model_or_a_human(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    rec = _pane_hold(store, jid, _CODEX_PANE)
    eng.step(jid)
    a = approvals_module.get_approval(store, jid, rec["id"])
    assert a["status"] == "approved" and a["decided_by"] == "resolver:rules"
    assert popen.instances == []  # no resolver process spawned
    d = _decisions(store, jid)[0]
    assert d.evidence["tier"] == "routine" and d.evidence["applied"] == "approve"


def test_codex_stew_write_tool_card_is_not_routine(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    rec = _pane_hold(store, jid, _CODEX_PANE.replace("get_recent_events", "steer_job"))
    eng.step(jid)
    assert approvals_module.get_approval(store, jid, rec["id"])["status"] == "pending"
    assert len(popen.instances) == 1  # goes the normal tiered (high -> astra) path


def test_opaque_prompt_brief_carries_pane_transcript_and_git(steward_home, popen):
    store, jid, eng = _setup(steward_home)
    eng.context_fn = lambda s, j: {"pane": "Do you grant ACCESS to the thing?\nexport K=sk-abcdefghijklmnop1234",
                                   "transcript": [{"kind": "said", "text": "calling the tool"}],
                                   "git": "## master\n M a.py"}
    eng.activity_fn = lambda s, j: {"turns": []}
    approvals_module.open_approval(store, jid, "worker permission: worker requests permission (see pane)", kind="permission")
    eng.step(jid)
    prompt = next((store.home / "resolver" / jid).glob("*.prompt.txt")).read_text()
    assert "Do you grant ACCESS to the thing?" in prompt and "calling the tool" in prompt and " M a.py" in prompt
    assert "READ the pane yourself" in prompt


def test_worker_picture_uses_codex_rollout_when_no_claude_transcript(steward_home, tmp_path):
    from steward import worker_view

    store = Store(steward_home)
    job = store.create_job("g", "/tmp/somerepo")
    wt = store.home / "worktrees" / job.job_id
    sess = tmp_path / "sessions" / "2026" / "10" / "05"
    sess.mkdir(parents=True)
    lines = [{"type": "session_meta", "payload": {"cwd": str(wt)}},
             {"type": "event_msg", "payload": {"item": {"type": "AgentMessage", "content": [{"type": "Text", "text": "calling stew"}]}}},
             {"type": "event_msg", "payload": {"item": {"type": "item_completed"}}}]
    lines[1]["payload"]["type"] = lines[2]["payload"]["type"] = "item_completed"
    (sess / "rollout-x.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    turns = worker_view.transcript_turns(store, job.job_id, projects_dir=tmp_path / "none", sessions_dir=tmp_path / "sessions")
    assert [t["text"] for t in turns] == ["calling stew"]


def test_pane_screen_is_redacted_and_uses_herdr_pane_read(steward_home):
    import subprocess
    from steward import worker_view

    store = Store(steward_home)
    job = store.create_job("g", "/tmp/somerepo")
    store.kv_set(job.job_id, "herdr_pane", "w1:p2")
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="hello\nexport TOKEN=sk-abcdefghijklmnop1234\n\n", stderr="")

    out = worker_view.pane_screen(store, job.job_id, run=fake_run)
    assert seen[0][:4] == ["herdr", "pane", "read", "w1:p2"] and "40" in seen[0]
    assert "hello" in out and "sk-abcdefghijklmnop1234" not in out


def _fake_codex_rollout(tmp_path, wt, texts):
    sess = tmp_path / "sessions" / "2026" / "10" / "06"
    sess.mkdir(parents=True)
    lines = [{"type": "session_meta", "payload": {"cwd": str(wt)}}]
    for t in texts:
        lines.append({"type": "event_msg", "payload": {"type": "item_completed",
                      "item": {"type": "AgentMessage", "content": [{"type": "Text", "text": t}]}}})
    (sess / "rollout-x.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return tmp_path / "sessions"


def test_result_and_transcript_fall_back_to_codex_rollout(steward_home, tmp_path):
    import io
    from steward import evidence, session, transcript

    store = Store(steward_home)
    job = store.create_job("g", "/tmp/somerepo")
    wt = store.home / "worktrees" / job.job_id
    final = "all done\nsecret sk-abcdefghijklmnop1234 here"
    sessions = _fake_codex_rollout(tmp_path, wt, ["first", final])
    none = tmp_path / "none"
    assert evidence.last_worker_text(store, job.job_id, none, sessions) == final
    res = session.build_result(store, job.job_id, projects_dir=none, sessions_dir=sessions)
    assert res["worker_message"].startswith("all done") and "sk-abcdefghijklmnop1234" not in res["worker_message"]
    out = io.StringIO()
    transcript.run(store, job.job_id, out=out, projects_dir=none, sessions_dir=sessions)
    assert "first" in out.getvalue() and "all done" in out.getvalue()
    assert "sk-abcdefghijklmnop1234" not in out.getvalue()
    assert evidence.last_worker_text(store, job.job_id, none, tmp_path / "empty") is None
