from pathlib import Path

import pytest

from steward.worker import (
    CodexAppServerAdapter,
    DeliveryResult,
    FakeWorker,
    WorkerObservation,
)


def test_start_records_brief_and_worktree_and_returns_ref(tmp_path):
    worker = FakeWorker()

    ref = worker.start("do the thing", tmp_path)

    assert isinstance(ref, str) and ref
    assert worker.started_brief == "do the thing"
    assert worker.worktree == Path(tmp_path)


def test_deliver_records_text_and_returns_ok():
    worker = FakeWorker()
    worker.start("brief", Path("/tmp"))

    result = worker.deliver("instruction rev=1: focus on X")

    assert isinstance(result, DeliveryResult)
    assert result.ok is True
    assert worker.delivered == ["instruction rev=1: focus on X"]


def test_observe_steps_through_scripted_observations():
    obs1 = WorkerObservation(lifecycle="working", output_tail="thinking...", pid_alive=True, ts="t1")
    obs2 = WorkerObservation(lifecycle="idle", output_tail="STEWARD-DONE", pid_alive=True, ts="t2")
    worker = FakeWorker(script=[obs1, obs2])
    worker.start("brief", Path("/tmp"))

    assert worker.observe() == obs1
    assert worker.observe() == obs2
    # Once the script is exhausted, observe() keeps returning a steady idle state.
    steady = worker.observe()
    assert steady.lifecycle == "idle"


def test_auto_ack_emits_ack_in_next_output_after_delivery():
    worker = FakeWorker(ack_revs=True)
    worker.start("brief", Path("/tmp"))

    worker.deliver("please focus, this is rev=3")
    obs = worker.observe()

    assert "STEWARD-ACK rev=3" in obs.output_tail


def test_callable_script_item_can_edit_worktree_files(tmp_path):
    def _edit(worker):
        (worker.worktree / "touched.txt").write_text("edited by fake worker\n")
        return WorkerObservation(lifecycle="working", output_tail="edited a file", pid_alive=True, ts="t")

    worker = FakeWorker(script=[_edit])
    worker.start("brief", tmp_path)

    obs = worker.observe()

    assert obs.output_tail == "edited a file"
    assert (tmp_path / "touched.txt").read_text() == "edited by fake worker\n"


def test_interrupt_and_stop_are_recorded():
    worker = FakeWorker()
    worker.start("brief", Path("/tmp"))

    worker.interrupt()
    confirmed = worker.stop()

    assert worker.interrupted is True
    assert confirmed is True
    assert worker.stopped is True


def test_answer_prompt_records_approval_and_returns_ok():
    worker = FakeWorker()
    worker.start("brief", Path("/tmp"))

    result = worker.answer_prompt(approve=True)

    assert isinstance(result, DeliveryResult)
    assert result.ok is True
    assert worker.answered == [True]


def test_answer_prompt_records_denial():
    worker = FakeWorker()
    worker.start("brief", Path("/tmp"))

    worker.answer_prompt(approve=False)

    assert worker.answered == [False]


def test_codex_app_server_adapter_is_a_seam_only():
    with pytest.raises(NotImplementedError):
        CodexAppServerAdapter()
