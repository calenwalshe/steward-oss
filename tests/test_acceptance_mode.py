"""Workbench posture (Sam, 2026-09-28): when acceptance mode is "human",
checks passing never auto-accepts — the job waits at NEEDS_HUMAN with the
evidence, and Sam accepts."""
from steward import closeout
from steward.models import JobState
from steward.store import Store


class _Contract:
    done_when = ["authorized checks pass"]


def _job(steward_home):
    from steward.models import Judgment

    s = Store()
    j = s.create_job("g", "/tmp").job_id
    for st in (JobState.CONTRACTED, JobState.RUNNING, JobState.WORKER_FINISHED, JobState.CHECKS_PASSED):
        s.set_state(j, st, what="t", evidence={}, rule="t", judgment=Judgment.code)
    return s, j


def test_default_mode_keeps_preauthorized_acceptance(steward_home):
    s, j = _job(steward_home)
    assert closeout.acceptance_mode() == "auto"


def test_human_mode_from_env(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "human")
    assert closeout.acceptance_mode() == "human"


def test_human_mode_from_config_file(steward_home, monkeypatch, tmp_path):
    import os
    from pathlib import Path
    Path(os.environ["STEWARD_CONFIG_DIR"], "steward.toml").write_text('acceptance = "human"\n')
    assert closeout.acceptance_mode() == "human"


def test_human_mode_sends_passing_job_to_needs_human(steward_home, monkeypatch):
    monkeypatch.setenv("STEWARD_ACCEPTANCE", "human")
    s, j = _job(steward_home)
    state = closeout._decide_acceptance(s, j, [], _Contract())
    assert state == JobState.NEEDS_HUMAN
    ev = [e for e in s.events(j) if e.rule == "acceptance_unresolved"][-1]
    assert any("Sam accepts" in r for r in ev.evidence["reasons"])


def test_auto_mode_blocks_nothing_and_accepts_only_with_a_finished_ruling(steward_home):
    s, j = _job(steward_home)
    # v2.7: nothing blocks acceptance, so the decision is "ask for the one completion ruling" (None, no state change) ...
    assert closeout._decide_acceptance(s, j, [], _Contract()) is None
    assert s.get_job(j).state == JobState.CHECKS_PASSED
    # ... and a finished ruling plus the guards is what reaches ACCEPTED
    ruling = {"status": "ruled", "ruling": "finished", "ruling_sha": "sha1", "sha": "sha1", "id": "rul-1"}
    assert closeout._decide_acceptance(s, j, [], _Contract(), commit_sha="sha1", ruling=ruling) == JobState.ACCEPTED
