from steward.checkpoint import Handoff, from_observation, load, save
from steward.store import Store


def test_from_observation_extracts_dollar_and_arrow_command_lines():
    output = (
        "some worker chatter\n"
        "$ pytest -q\n"
        "3 failed, 10 passed\n"
        "❯ git diff --stat\n"
        "more chatter\n"
    )

    handoff = from_observation(
        output, hypothesis="tests are flaky", next_action="rerun with -x", attempt_no=2
    )

    assert handoff.commands_tried == ["pytest -q", "git diff --stat"]
    assert handoff.hypothesis == "tests are flaky"
    assert handoff.next_action == "rerun with -x"
    assert handoff.attempt_no == 2
    assert handoff.results == []


def test_from_observation_with_no_command_lines_returns_empty_commands():
    handoff = from_observation("just prose, no commands", hypothesis="h", next_action="n", attempt_no=1)

    assert handoff.commands_tried == []


def test_save_then_load_round_trips_through_kv(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    handoff = Handoff(
        hypothesis="network flaked",
        commands_tried=["pytest -q"],
        results=["3 failed"],
        next_action="retry with backoff",
        attempt_no=1,
    )

    save(store, job.job_id, handoff)
    loaded = load(store, job.job_id)

    assert loaded == handoff


def test_load_returns_none_when_no_handoff_saved(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    assert load(store, job.job_id) is None


def test_save_writes_human_readable_markdown_file(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    handoff = Handoff(
        hypothesis="hung on network call",
        commands_tried=["pytest -q"],
        results=["timeout after 30s"],
        next_action="add explicit timeout",
        attempt_no=3,
    )

    save(store, job.job_id, handoff)

    path = store.home / "jobs" / job.job_id / "handoff-3.md"
    assert path.exists()
    text = path.read_text()
    assert "attempt 3" in text
    assert "hung on network call" in text
    assert "pytest -q" in text
    assert "add explicit timeout" in text


def test_save_with_no_commands_or_results_writes_none_observed_placeholders(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    handoff = Handoff(hypothesis="unknown", next_action="investigate", attempt_no=1)

    save(store, job.job_id, handoff)

    text = (store.home / "jobs" / job.job_id / "handoff-1.md").read_text()
    assert "(none observed)" in text
    assert "(none recorded)" in text


def test_handoff_survives_a_brand_new_store_instance(steward_home):
    store_a = Store(steward_home)
    job = store_a.create_job("goal", "/repo")
    handoff = Handoff(hypothesis="h", next_action="n", attempt_no=1)
    save(store_a, job.job_id, handoff)

    store_b = Store(steward_home)
    loaded = load(store_b, job.job_id)

    assert loaded == handoff
