"""v2.3 Flow investigation (phase 24): one bounded, structured, tool-less root-cause assessment per active episode."""

from __future__ import annotations

import json

import pytest

from steward import flow_investigator as fi
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store

REFS = ["turn:7", "event:12"]


def _episode(**kw):
    ep = {"episode_id": "flow-abc", "job_id": "job-1", "label": "retry_or_rebuild", "confidence": 0.9,
          "detector_version": "flow-v1", "mode": "active", "source_refs": list(REFS),
          "snapshot": {"label": "retry_or_rebuild", "turns": [["error", "build failed"], ["tool", "npm run build"]]},
          "job_revision": 3, "head_sha": "abc"}
    ep.update(kw)
    return ep


def _answer(**kw):
    a = {"root_cause": "The build script is rerun without fixing the missing dependency.",
         "evidence_refs": ["turn:7"], "recommended_action": "nudge",
         "instruction": "Stop rerunning the build; install the missing dependency first."}
    a.update(kw)
    return a


def _claude_stdout(answer, cost=0.05):
    inner = answer if isinstance(answer, str) else json.dumps(answer)
    return json.dumps({"type": "result", "result": inner, "total_cost_usd": cost})


# -- strict parsing (task 1) -----------------------------------------------------------------------------------


def test_valid_answer_is_accepted_and_normalised():
    got = fi.validate(_answer(), _episode())
    assert got == {"root_cause": _answer()["root_cause"], "evidence_refs": ["turn:7"],
                   "recommended_action": "nudge", "instruction": _answer()["instruction"]}


@pytest.mark.parametrize("bad,reason", [
    ("not a dict", "not_object"),
    ({**_answer(), "extra": 1}, "unknown_fields"),
    ({k: v for k, v in _answer().items() if k != "instruction"}, "missing_fields"),
    (_answer(recommended_action="merge"), "unknown_action"),
    (_answer(recommended_action="run_shell"), "unknown_action"),
    (_answer(evidence_refs=["turn:99"]), "invented_evidence"),
    (_answer(evidence_refs=[]), "no_evidence"),
    (_answer(evidence_refs="turn:7"), "bad_evidence_refs"),
    (_answer(root_cause=""), "bad_root_cause"),
    (_answer(root_cause="x" * 5000), "bad_root_cause"),
    (_answer(instruction=""), "missing_instruction"),
    (_answer(instruction="y" * 5000), "bad_instruction"),
    (_answer(instruction=7), "bad_instruction"),
])
def test_invalid_answers_are_rejected_with_a_reason(bad, reason):
    with pytest.raises(fi.Rejected) as exc:
        fi.validate(bad, _episode())
    assert exc.value.reason == reason


def test_no_action_needs_no_instruction():
    got = fi.validate(_answer(recommended_action="no_action", instruction=""), _episode())
    assert got["recommended_action"] == "no_action" and got["instruction"] == ""


def test_actions_are_finite_and_exclude_reserved_human_decisions():
    assert set(fi.RECOMMENDATIONS) == {"no_action", "nudge", "focus", "watcher_review", "recovery", "escalate_human"}
    for reserved in ("stop", "cancel", "permission", "approve", "accept", "merge", "release", "deploy", "contact"):
        assert reserved not in fi.RECOMMENDATIONS


def test_prompt_is_bounded_and_marks_evidence_untrusted():
    ep = _episode(snapshot={"turns": [["said", "z" * 20000]]})
    prompt = fi.build_prompt(ep, max_chars=3000)
    assert len(prompt) <= 3000 and "untrusted" in prompt and "turn:7" in prompt
    assert "recommended_action" in prompt and "tool" in prompt.lower()


def test_claude_investigator_has_no_tools_and_explicit_budgets():
    runner = FakeRunner(lambda argv, cwd: CommandResult(argv, cwd, 0, _claude_stdout(_answer()), "", 0.1))
    inv = fi.ClaudeFlowInvestigator(runner, model="sonnet", timeout=42.0, budget_usd=0.25)
    out = inv.investigate("PROMPT")
    argv = runner.calls[0]["argv"] if isinstance(runner.calls[0], dict) else runner.calls[0][0]
    assert argv[:3] == ["env", "-u", "ANTHROPIC_API_KEY"]
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
    assert argv[argv.index("--max-budget-usd") + 1] == "0.25" and argv[argv.index("--model") + 1] == "sonnet"
    assert out.cost_usd == 0.05 and json.loads(out.text)["recommended_action"] == "nudge"


@pytest.mark.legacy_ownership_off
def test_claude_investigator_surfaces_over_budget_and_failures():
    runner = FakeRunner(lambda argv, cwd: CommandResult(argv, cwd, 0, _claude_stdout(_answer(), cost=9.0), "", 0.1))
    with pytest.raises(fi.Rejected) as exc:
        fi.ClaudeFlowInvestigator(runner, model="m", budget_usd=0.25).investigate("P")
    assert exc.value.reason == "over_budget"
    runner = FakeRunner(lambda argv, cwd: CommandResult(argv, cwd, 1, "", "boom", 0.1))
    with pytest.raises(fi.Unavailable):
        fi.ClaudeFlowInvestigator(runner, model="m").investigate("P")
    runner = FakeRunner(lambda argv, cwd: CommandResult(argv, cwd, 124, "", "timeout", 42.0))
    with pytest.raises(fi.Unavailable):
        fi.ClaudeFlowInvestigator(runner, model="m").investigate("P")


def test_flow_investigation_budgets_are_explicit_and_bounded(tmp_path, monkeypatch):
    from steward import config

    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))
    s = config.flow_settings()
    assert s["investigation_timeout_s"] == fi.DEFAULT_TIMEOUT_S and s["investigation_budget_usd"] == fi.DEFAULT_BUDGET_USD
    assert s["investigation_prompt_chars"] == fi.DEFAULT_PROMPT_CHARS
    (tmp_path / "steward.toml").write_text('[flow]\ninvestigation_timeout_s = 99999\ninvestigation_budget_usd = -1\n'
                                           'investigation_prompt_chars = "x"\n')
    s = config.flow_settings()
    assert s["investigation_timeout_s"] == config.FLOW_MAX_TIMEOUT_S
    assert s["investigation_budget_usd"] == fi.DEFAULT_BUDGET_USD and s["investigation_prompt_chars"] == fi.DEFAULT_PROMPT_CHARS


# -- at-most-once lifecycle (task 2) ---------------------------------------------------------------------------


class FakeInvestigator:
    def __init__(self, *answers):
        self.answers = list(answers) or [fi.RawAnswer(json.dumps(_answer()), 0.05)]
        self.prompts = []

    def investigate(self, prompt):
        self.prompts.append(prompt)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def _stored_episode(store, label="retry_or_rebuild", mode="active", now=1000.0):
    job = store.create_job("goal", "/repo")
    rec, _ = store.record_flow_episode(job.job_id, episode_id=f"flow-{label}-{now}", label=label, confidence=0.9,
                                       detector_version="flow-v1", mode=mode, snapshot=_episode()["snapshot"],
                                       source_refs=list(REFS), job_revision=0, head_sha="abc", now=now,
                                       dedupe_window_s=900.0)
    return rec


def test_run_investigation_accepts_and_links_to_the_episode(steward_home):
    store = Store(steward_home)
    ep = _stored_episode(store)
    inv = FakeInvestigator()
    rec = fi.run_investigation(store, ep, inv, owner="p1", now=1001.0)
    assert rec["state"] == "accepted" and rec["result"]["recommended_action"] == "nudge"
    assert rec["episode_id"] == ep["episode_id"] and rec["cost_usd"] == 0.05
    whats = [e.what for e in store.events(ep["job_id"])]
    assert whats.count("flow_investigation_started") == 1 and whats.count("flow_investigation_accepted") == 1
    assert len(inv.prompts) == 1 and "turn:7" in inv.prompts[0]


def test_run_investigation_is_at_most_once(steward_home):
    store = Store(steward_home)
    ep = _stored_episode(store)
    inv = FakeInvestigator()
    fi.run_investigation(store, ep, inv, owner="p1", now=1001.0)
    assert fi.run_investigation(store, ep, inv, owner="p1", now=1002.0) is None
    assert fi.run_investigation(Store(steward_home), ep, inv, owner="p2", now=1003.0) is None
    assert len(inv.prompts) == 1


def test_concurrent_claims_call_the_model_once(steward_home):
    import threading

    store = Store(steward_home)
    ep = _stored_episode(store)
    inv = FakeInvestigator()
    threads = [threading.Thread(target=fi.run_investigation, args=(Store(steward_home), ep, inv),
                                kwargs={"owner": f"p{i}", "now": 1001.0}) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(inv.prompts) == 1 and len(store.flow_investigations(ep["job_id"])) == 1


@pytest.mark.parametrize("answer,state,reason", [
    (fi.RawAnswer("not json"), "rejected", "invalid_json"),
    (fi.RawAnswer(json.dumps(_answer(evidence_refs=["event:999"]))), "rejected", "invented_evidence"),
    (fi.RawAnswer(json.dumps(_answer(recommended_action="merge"))), "rejected", "unknown_action"),
    (fi.Rejected("over_budget", "9 > 0.5"), "rejected", "over_budget"),
    (fi.Unavailable("provider_exit_124"), "failed", "provider_exit_124"),
    (RuntimeError("kaboom"), "failed", "error:RuntimeError"),
])
def test_bad_outcomes_are_durable_no_ops(steward_home, answer, state, reason):
    store = Store(steward_home)
    ep = _stored_episode(store)
    rec = fi.run_investigation(store, ep, FakeInvestigator(answer), owner="p1", now=1001.0)
    assert rec["state"] == state and rec["reason"] == reason and rec["result"] is None
    assert [e.what for e in store.events(ep["job_id"])][-1] == f"flow_investigation_{state}"


def test_missing_provider_and_unregistered_path_fail_closed(steward_home, tmp_path, monkeypatch):
    store = Store(steward_home)
    ep = _stored_episode(store)
    assert fi.run_investigation(store, ep, None, owner="p1", now=1.0)["reason"] == "no_provider"
    ep2 = _stored_episode(store, label="blocked_handoff")
    monkeypatch.setattr(fi.exceptions, "use", lambda *a, **k: (_ for _ in ()).throw(fi.exceptions.UnregisteredPath("x")))
    inv = FakeInvestigator()
    rec = fi.run_investigation(store, ep2, inv, owner="p1", now=1.0)
    assert rec["state"] == "failed" and rec["reason"] == "supervisor_model_unregistered" and inv.prompts == []


def test_rejected_raw_output_is_bounded_and_masked(steward_home):
    from steward import masking

    store = Store(steward_home)
    ep = _stored_episode(store)
    masking.register(["literal-invest-secret-77"])
    try:
        rec = fi.run_investigation(store, ep, FakeInvestigator(fi.RawAnswer("literal-invest-secret-77 " + "q" * 5000)),
                                   owner="p1", now=1.0)
        assert rec["state"] == "rejected" and len(rec["raw_excerpt"]) <= fi.RAW_EXCERPT_MAX
        assert "literal-invest-secret-77" not in json.dumps(rec)
    finally:
        masking.clear()


def test_interrupted_attempts_resolve_conservatively_without_retry(steward_home):
    store = Store(steward_home)
    ep = _stored_episode(store)
    assert store.claim_flow_investigation(ep["episode_id"], ep["job_id"], owner="dead-process", now=5.0, model="m")
    swept = store.interrupt_flow_investigations(owner="new-process")
    assert [r["episode_id"] for r in swept] == [ep["episode_id"]]
    rec = store.flow_investigation(ep["episode_id"])
    assert rec["state"] == "interrupted"
    inv = FakeInvestigator()
    assert fi.run_investigation(store, ep, inv, owner="new-process", now=6.0) is None and inv.prompts == []
    assert store.finish_flow_investigation(ep["episode_id"], "accepted", result={"x": 1}) is None


# -- inspection and adversarial replay (task 3) ----------------------------------------------------------------


def _replay(steward_home, outcomes, labels):
    """Drive the real JevWatch: one friction label per tick (each a new episode), one scripted model outcome each."""
    from test_flow_watch import FAKE_KEY, AllActions, ScriptedTransport, _flow_resp, _running, _sig

    from steward import config, jev_observer
    from steward.jev_watch import JevWatch

    store = Store(steward_home)
    job = _running(store)
    store.kv_set(job.job_id, "supervisor_jev", "shadow")
    store.kv_set(job.job_id, config.FLOW_SWITCH_KEY, "active")
    now = [1000.0]
    transport = ScriptedTransport([_flow_resp(label) for label in labels], now)
    obs = jev_observer.JevObserver(transport=transport, enabled=True, api_key_fn=lambda: FAKE_KEY, clock=lambda: now[0])
    actions = AllActions()
    turns = [{"i": 7, "ts": "t", "kind": "error", "text": "build failed"}]
    w = JevWatch(store, obs, actions, lambda: now[0], activity_fn=lambda s, j: {"turns": turns})
    inv = FakeInvestigator(*outcomes)
    w.flow_investigator, w.flow_executor = inv, (lambda fn: fn())
    kv_before = {r["key"] for r in store._conn.execute("SELECT key FROM kv WHERE job_id = ?", (job.job_id,))}
    for _ in labels:
        w.step(job.job_id, _sig(), force=True)
        now[0] += 10.0
    return store, job.job_id, inv, actions, kv_before


def test_adversarial_replay_records_every_outcome_and_acts_on_none(steward_home):
    from steward import approvals

    labels = ["wrong_direction", "wrong_direction", "self_correction", "supervisor_correction", "retry_or_rebuild",
              "blocked_handoff", "supervision_churn", "smooth", "uncertain"]
    outcomes = [fi.RawAnswer(json.dumps(_answer(evidence_refs=["turn:7"]))),          # accepted
                fi.RawAnswer("{not json"),                                              # invalid JSON
                fi.RawAnswer(json.dumps(_answer(evidence_refs=["event:4242"]))),        # invented evidence
                fi.RawAnswer(json.dumps(_answer(recommended_action="merge"))),          # prohibited action
                fi.Unavailable("provider_error:TimeoutExpired"),                        # timeout
                fi.Rejected("over_budget", "3.00 > 0.50")]                              # over budget
    store, job_id, inv, actions, kv_before = _replay(steward_home, outcomes, labels)
    eps = store.flow_episodes(job_id)
    assert [e["label"] for e in eps] == ["wrong_direction", "self_correction", "supervisor_correction",
                                         "retry_or_rebuild", "blocked_handoff", "supervision_churn"]
    assert len(inv.prompts) == len(eps) == 6  # the duplicate wrong_direction tick never re-investigates
    states = [(r["state"], r["reason"]) for r in store.flow_investigations(job_id)]
    assert states == [("accepted", None), ("rejected", "invalid_json"), ("rejected", "invented_evidence"),
                      ("rejected", "unknown_action"), ("failed", "provider_error:TimeoutExpired"),
                      ("rejected", "over_budget")]
    digest = [e.what for e in store.events(job_id) if e.what.startswith(("flow_episode", "flow_investigation"))]
    assert digest == ["flow_episode_recorded", "flow_investigation_started", "flow_investigation_accepted",
                      "flow_episode_recorded", "flow_investigation_started", "flow_investigation_rejected",
                      "flow_episode_recorded", "flow_investigation_started", "flow_investigation_rejected",
                      "flow_episode_recorded", "flow_investigation_started", "flow_investigation_rejected",
                      "flow_episode_recorded", "flow_investigation_started", "flow_investigation_failed",
                      "flow_episode_recorded", "flow_investigation_started", "flow_investigation_rejected"]
    # zero action side effects: no route, steer, approval, recovery, watcher or state change
    from steward.steering import Steering
    from steward.models import JobState

    # the investigator acts on nothing: only the accepted proposal reaches the phase 25 controller, once
    assert [(d["action"], d["decision"]) for d in store.flow_decisions(job_id)] == [("nudge", "applied")]
    assert actions.calls == [] and [i["origin"] for i in Steering(store).all_instructions(job_id)] == ["flow"]
    assert approvals.list_approvals(store, job_id) == []
    whats = {e.what for e in store.events(job_id)}
    assert not {w for w in whats if w.startswith(("recovery", "watcher_", "jev_flag"))}
    kv_after = {r["key"] for r in store._conn.execute("SELECT key FROM kv WHERE job_id = ?", (job_id,))}
    assert kv_after - kv_before <= {"jev_stats", "jev_status", "jev_streak", "jev_exception_logged_at",
                                    "instructions", "job_revision"}  # the one controller-applied flow steer
    assert store.get_job(job_id).state == JobState.RUNNING


def test_api_links_investigations_to_episodes(steward_home):
    from steward.api import StewardAPI

    store, job_id, inv, actions, _ = _replay(steward_home, [fi.RawAnswer(json.dumps(_answer())),
                                                            fi.RawAnswer("nope")],
                                             ["retry_or_rebuild", "blocked_handoff"])
    view = StewardAPI(store).get_flow(job_id)
    assert view["mode"] == "active"
    [a, b] = view["episodes"]
    assert a["investigation"]["state"] == "accepted" and a["investigation"]["result"]["evidence_refs"] == ["turn:7"]
    assert b["investigation"]["state"] == "rejected" and b["investigation"]["reason"] == "invalid_json"
    assert b["investigation"]["raw_excerpt"] == "nope"
