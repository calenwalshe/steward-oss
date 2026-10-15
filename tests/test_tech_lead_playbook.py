"""SUP-15 / PRD D-06: the cross-job playbook - issue type, recipe (<= 400 chars), success/attempt counts.

Success strengthens, failure or owner overrule demotes; the top relevant entries by kind and score are read at the
start of each review within the token budget; `steward tech-lead playbook` lists it."""

from __future__ import annotations

import json
import subprocess

from steward import tech_lead_playbook as pb
from steward import tech_lead_policy as tpol
from steward.store import Store
from steward.models import Judgment
from steward.actor import Actor, ActorRef
from test_runner import _contracted_job


def test_record_strengthen_demote_and_rank_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    pb.record_attempt(store, "new_failures_repeated", "takeover", "takeover: remove the planted name; rerun the sweep")
    pb.record_attempt(store, "new_failures_repeated", "send_back", "send_back: tell the worker which comment")
    pb.record_outcome(store, "new_failures_repeated", "takeover", success=True)
    pb.record_outcome(store, "new_failures_repeated", "send_back", success=False)
    top = pb.top(store, "new_failures_repeated", n=2)
    assert [e["strategy"] for e in top] == ["takeover", "send_back"]
    assert top[0]["successes"] == 1 and top[0]["attempts"] == 1 and top[1]["failures"] == 1
    before = pb.top(store, "new_failures_repeated", n=1)[0]["score"]
    pb.demote(store, "new_failures_repeated", "takeover", reason="owner overrule")
    assert pb.top(store, "new_failures_repeated", n=1)[0]["score"] < before  # an overrule demotes
    for _ in range(3):
        pb.demote(store, "new_failures_repeated", "takeover", reason="owner overrule")
    assert pb.top(store, "new_failures_repeated", n=1)[0]["strategy"] == "send_back"


def test_recipe_is_bounded_and_kind_scoped_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    pb.record_attempt(store, "permission_ask_repeated", "unblock", "x" * 1000)
    e = pb.top(store, "permission_ask_repeated")[0]
    assert len(e["recipe"]) <= 400
    assert pb.top(store, "new_failures_repeated") == []


def test_playbook_is_global_to_the_steward_home_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    a = _contracted_job(store, "a").job_id
    b = _contracted_job(store, "b").job_id
    pb.record_attempt(store, "thrash", "takeover", "takeover: the fix", job_id=a)
    pb.record_outcome(store, "thrash", "takeover", success=True, job_id=a)
    entry = pb.top(store, "thrash")[0]
    assert entry["successes"] == 1 and entry["last_job"] == a
    assert pb.packet_entries(store, ["thrash"], tpol.Policy())[0]["strategy"] == "takeover"  # visible to job b
    assert b


def test_packet_entries_fit_the_token_budget_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    for i in range(30):
        pb.record_attempt(store, "thrash", f"s{i}", f"recipe {i} " + "y" * 380)
    entries = pb.packet_entries(store, ["thrash"], tpol.Policy(token_budget=1000))
    assert len(json.dumps(entries)) <= 1000 * 4 // 4 + 50 and len(entries) >= 1


def test_cli_lists_the_playbook_continuous_ownership_recovery_acceptance(steward_home, capsys):
    from steward import cli

    store = Store(steward_home)
    pb.record_attempt(store, "new_failures_repeated", "takeover", "takeover: remove the planted name")
    pb.record_outcome(store, "new_failures_repeated", "takeover", success=True)
    assert cli.main(["tech-lead", "playbook"]) == 0
    out = capsys.readouterr().out
    assert "new_failures_repeated" in out and "takeover" in out and "1/1" in out
    assert cli.main(["tech-lead", "playbook", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["strategy"] == "takeover"


def test_investigation_lessons_are_context_checked_advisory_and_correctable_continuous_ownership_recovery_acceptance(steward_home):
    import json

    store = Store(steward_home)
    job = _contracted_job(store, "apply relevant prior evidence").job_id
    worktree = store.home / "worktrees" / job
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    (worktree / "proof.txt").write_text("proof\n")
    subprocess.run(["git", "add", "proof.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "proof"], cwd=worktree, check=True)
    cited = store.append_event(job, what="captured verification failure", evidence={}, rule="test",
                               judgment=Judgment.code)
    ref = f"event:{cited.seq}"
    report = {"assessment": {"status": "assessed", "causes": [{"type": "controller_routing",
              "claim": "The review used a stale command.", "evidence": [ref]}]},
              "evidence": {"events": [{"seq": cited.seq, "citable": True}]}}
    with store._conn:
        store._conn.execute("INSERT INTO run_flags(flag_id,job_id,run_id,actor_kind,actor_id,description,state,created_epoch)"
                            " VALUES(?,?,?,?,?,?,?,?)", ("flag-1", job, f"{job}:a1", "owner", "fixture",
                            "test", "open", 1.0))
        store._conn.execute("INSERT INTO run_investigations(investigation_id,flag_id,job_id,evidence_digest,state,"
                            "report_json,remedy_json,created_epoch,finished_epoch) VALUES(?,?,?,?,?,?,?,?,?)",
                            ("inv-1", "flag-1", job, "digest", "done", json.dumps(report), "{}", 1.0, 2.0))
    lesson = pb.record_lesson(store, kind="verification_failing", strategy="inspect the current failure",
                              incident={"job_id": job, "flag_id": "flag-1", "run_id": f"{job}:a1"},
                              finding="The run failed because the checked command was stale.",
                              remedy="Rerun the current required command.",
                              conditions={"kinds": ["verification_failing"], **pb._current_job_context(store, job)},
                              evidence=[ref], provenance="investigation",
                              investigation_id="inv-1", actor=ActorRef(Actor.CONTROLLER, "investigation"), now=3.0)

    matched = pb.relevant_lessons(store, job, ["verification_failing"])

    assert len(matched) == 1 and matched[0]["lesson_id"] == lesson
    assert matched[0]["advisory_only"] is True and "grants remain controlling" in matched[0]["authority"]
    from steward import tech_lead_notebook
    packet = tech_lead_notebook.Memory(store).packet_section(
        job, [{"kind": "verification_failing", "data": {"test": "current verification"}}], tpol.Policy())
    assert [r["lesson_id"] for r in packet["prior_lessons"]] == [lesson]
    pb.transition_lesson(store, lesson, "demoted", actor=ActorRef(Actor.OWNER, "sam"),
                         reason="assumption corrected by owner", now=4.0, evidence=["owner-correction"])
    assert pb.relevant_lessons(store, job, ["verification_failing"]) == []
    history = store._conn.execute("SELECT COUNT(*) FROM playbook_lesson_history WHERE lesson_id=?", (lesson,)).fetchone()[0]
    assert history == 2
    import pytest
    with pytest.raises(ValueError, match="owner provenance"):
        pb.transition_lesson(store, lesson, "active", actor=ActorRef(Actor.CONTROLLER, "automated"),
                             reason="promote", now=5.0)


def test_lesson_assumption_mismatch_and_policy_proposal_do_not_change_policy_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, "do not blindly reuse a lesson").job_id
    context = pb._current_job_context(store, job)
    with store._conn:
        store._conn.execute("INSERT INTO run_flags(flag_id,job_id,run_id,actor_kind,actor_id,description,state,created_epoch)"
                            " VALUES(?,?,?,?,?,?,?,?)", ("flag-2", job, f"{job}:a0", "owner", "fixture",
                            "test", "open", 1.0))
        store._conn.execute("INSERT INTO run_investigations(investigation_id,flag_id,job_id,evidence_digest,state,"
                            "report_json,remedy_json,created_epoch,finished_epoch) VALUES(?,?,?,?,?,?,?,?,?)",
                            ("inv-2", "flag-2", job, "digest-2", "done", "{}", "{}", 1.0, 2.0))
    lesson = pb.record_lesson(store, kind="verification_failing", strategy="old revision fix",
                              incident={"job_id": job, "flag_id": "flag-2", "run_id": f"{job}:a0"},
                              finding="A supported prior finding.", remedy="Inspect.",
                              conditions={"kinds": ["verification_failing"], **context, "revision": "old-revision"},
                              evidence=["event:13"], provenance="investigation", investigation_id="inv-2",
                              actor=ActorRef(Actor.CONTROLLER, "investigation"), now=3.0)
    from steward import tech_lead_policy
    before = tech_lead_policy.load_or_defaults()[0]
    assert pb.relevant_lessons(store, job, ["verification_failing"]) == []
    pb.propose_policy_change(store, job, description="raise the retry threshold", evidence=["inv-2"],
                             actor=ActorRef(Actor.CONTROLLER, "investigation"), now=4.0)
    after = tech_lead_policy.load_or_defaults()[0]
    proposal = [e for e in store.events(job) if e.what == "playbook_policy_change_proposed"][-1]
    assert before == after and proposal.evidence["active"] is False and lesson


def test_investigation_lesson_requires_the_same_flag_and_run_identity_continuous_ownership_recovery_acceptance(steward_home):
    import pytest

    store = Store(steward_home)
    job = _contracted_job(store, "reject false incident attribution").job_id
    with store._conn:
        store._conn.execute("INSERT INTO run_flags(flag_id,job_id,run_id,actor_kind,actor_id,description,state,created_epoch)"
                            " VALUES(?,?,?,?,?,?,?,?)", ("flag-real", job, f"{job}:a0", "owner", "fixture",
                            "real run", "open", 1.0))
        store._conn.execute("INSERT INTO run_investigations(investigation_id,flag_id,job_id,evidence_digest,state,"
                            "report_json,created_epoch) VALUES(?,?,?,?,?,?,?)",
                            ("inv-real", "flag-real", job, "digest", "done", "{}", 1.0))
    with pytest.raises(ValueError, match="linked investigation"):
        pb.record_lesson(store, kind="verification_failing", strategy="misattributed",
                         incident={"job_id": job, "flag_id": "flag-other", "run_id": f"{job}:a1"},
                         finding="invented", remedy="retry", conditions={"kinds": ["verification_failing"]},
                         evidence=["event:1"], provenance="investigation", investigation_id="inv-real",
                         actor=ActorRef(Actor.CONTROLLER, "fixture"), now=2.0)


def test_lesson_requires_a_nonempty_matching_issue_kind_continuous_ownership_recovery_acceptance(steward_home):
    import pytest

    store = Store(steward_home)
    job = _contracted_job(store, "reject unmatchable lesson").job_id
    with pytest.raises(ValueError, match="issue kind"):
        pb.record_lesson(store, kind="verification_failing", strategy="no-op", incident={"job_id": job},
                         finding="supported", remedy="inspect", conditions={"kinds": []}, evidence=["event:1"],
                         provenance="owner_correction", actor=ActorRef(Actor.OWNER, "owner"), now=1.0)


def test_empty_investigation_cannot_seed_an_active_lesson_continuous_ownership_recovery_acceptance(steward_home):
    store = Store(steward_home)
    job = _contracted_job(store, "do not learn from an empty report").job_id
    with store._conn:
        store._conn.execute("INSERT INTO run_flags(flag_id,job_id,run_id,actor_kind,actor_id,description,state,created_epoch)"
                            " VALUES(?,?,?,?,?,?,?,?)", ("flag-empty", job, f"{job}:a0", "owner", "fixture",
                            "test", "open", 1.0))
        store._conn.execute("INSERT INTO run_investigations(investigation_id,flag_id,job_id,evidence_digest,state,"
                            "report_json,created_epoch) VALUES(?,?,?,?,?,?,?)",
                            ("inv-empty", "flag-empty", job, "digest", "done", "{}", 1.0))
    lesson = pb.record_lesson(store, kind="verification_failing", strategy="unsupported",
                              incident={"job_id": job, "flag_id": "flag-empty", "run_id": f"{job}:a0"},
                              finding="invented cause", remedy="retry", conditions={"kinds": ["verification_failing"]},
                              evidence=["event:12"], provenance="investigation", investigation_id="inv-empty",
                              actor=ActorRef(Actor.CONTROLLER, "investigation"), now=2.0)
    row = next(r for r in pb.lessons(store) if r["lesson_id"] == lesson)
    assert row["status"] == "unresolved"
    assert pb.relevant_lessons(store, job, ["verification_failing"]) == []
