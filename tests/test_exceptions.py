from __future__ import annotations

import pytest

from fixtures.repos import python_repo

from steward import exceptions, notify
from steward.exceptions import UnregisteredPath
from steward.models import Judgment
from steward.runner_exec import SubprocessRunner
from steward.shim import Shim
from steward.store import Store
from steward.worktree import create_worktree


def test_load_creates_default_registry_when_missing(tmp_path):
    path = tmp_path / "config" / "exceptions.toml"
    assert not path.exists()

    registry = exceptions.load(path)

    assert path.exists()
    for name in ("git", "model-api", "chatgpt-mcp", "phone-notify", "herdr-api", "supervisor-model"):
        assert name in registry
        assert registry[name].direction in ("in", "out")
        assert registry[name].purpose
        assert registry[name].audit in ("event", "none")


def test_default_registry_supervisor_model_entry_shape(tmp_path):
    registry = exceptions.load(tmp_path / "exceptions.toml")
    entry = registry["supervisor-model"]
    assert entry.direction == "out"
    assert "Sol" in entry.purpose and "Astra" in entry.purpose and "Jev" in entry.purpose


def test_default_registry_has_the_read_only_worker_check_wait_path(tmp_path):
    """v2.7 Phase 42 (SRD-14): `steward check wait` reads the store from the worker's shell; SP-34 registers it."""
    entry = exceptions.load(tmp_path / "exceptions.toml")["worker-check-wait"]
    assert entry.direction == "in" and entry.audit == "event"
    assert "read-only" in entry.purpose and "check" in entry.purpose


# ------------------------------------------------------------------- sync()


_PRE_12_02_REGISTRY = (
    '[git]\ndirection = "out"\npurpose = "worktree creation and commit/diff reads"\naudit = "event"\n\n'
    '[model-api]\ndirection = "out"\npurpose = "the worker\'s own LLM calls (claude -p)"\naudit = "event"\n\n'
    '[chatgpt-mcp]\ndirection = "in"\npurpose = "ChatGPT MCP writes, relayed into the control pane"\naudit = "event"\n\n'
    '[phone-notify]\ndirection = "out"\npurpose = "phone notifications (Telegram/Moshi)"\naudit = "event"\n\n'
    '[herdr-api]\ndirection = "out"\npurpose = "the fabric itself (recorded for completeness)"\naudit = "event"\n'
)


def test_sync_appends_missing_supervisor_model_entry_to_an_existing_registry(tmp_path):
    path = tmp_path / "exceptions.toml"
    path.write_text(_PRE_12_02_REGISTRY)

    added = exceptions.sync(path)

    assert sorted(added) == ["research-mcp", "supervisor-model", "worker-check-wait"]
    registry = exceptions.load(path)
    assert "supervisor-model" in registry
    assert "git" in registry  # untouched


def test_sync_is_idempotent(tmp_path):
    path = tmp_path / "exceptions.toml"
    path.write_text(_PRE_12_02_REGISTRY)

    first = exceptions.sync(path)
    second = exceptions.sync(path)

    assert sorted(first) == ["research-mcp", "supervisor-model", "worker-check-wait"]
    assert second == []


def test_sync_never_rewrites_an_already_complete_registry(tmp_path):
    path = tmp_path / "exceptions.toml"
    exceptions.load(path)  # full default registry, including supervisor-model
    before = path.read_text()

    added = exceptions.sync(path)

    assert added == []
    assert path.read_text() == before


def test_sync_creates_a_fresh_default_registry_when_none_exists(tmp_path):
    path = tmp_path / "does-not-exist" / "exceptions.toml"

    added = exceptions.sync(path)

    assert added == []
    assert path.exists()
    registry = exceptions.load(path)
    assert "supervisor-model" in registry


def test_env_override_path(tmp_path, monkeypatch):
    path = tmp_path / "custom.toml"
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(path))

    registry = exceptions.load()

    assert path.exists()
    assert "git" in registry


def test_use_registered_name_records_exception_used_event(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    path = steward_home / "exceptions.toml"

    entry = exceptions.use(store, job.job_id, "git", {"detail": "worktree add"}, path=path)

    assert entry.name == "git"
    events = store.events(job.job_id)
    assert len(events) == 1
    assert events[0].what == "exception_used"
    assert events[0].judgment == Judgment.code
    assert events[0].evidence["name"] == "git"
    assert events[0].evidence["detail"] == "worktree add"


def test_use_unregistered_name_records_violation_and_raises(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    path = steward_home / "exceptions.toml"
    exceptions.load(path)  # seed the default registry first

    with pytest.raises(UnregisteredPath):
        exceptions.use(store, job.job_id, "carrier-pigeon", {}, path=path)

    events = store.events(job.job_id)
    assert len(events) == 1
    assert events[0].what == "exception_violation"
    assert events[0].evidence["name"] == "carrier-pigeon"


def test_use_with_no_job_id_does_not_touch_store(steward_home):
    store = Store(steward_home)
    path = steward_home / "exceptions.toml"

    entry = exceptions.use(store, None, "git", {}, path=path)

    assert entry.name == "git"


def test_use_with_no_job_id_unregistered_still_raises(steward_home):
    store = Store(steward_home)
    path = steward_home / "exceptions.toml"
    exceptions.load(path)

    with pytest.raises(UnregisteredPath):
        exceptions.use(store, None, "carrier-pigeon", {}, path=path)


def test_registry_path_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(tmp_path / "env.toml"))
    explicit = tmp_path / "explicit.toml"
    assert exceptions.registry_path(explicit) == explicit
    assert exceptions.registry_path() == tmp_path / "env.toml"


# ------------------------------------------------------- wiring (task 3)


def test_create_worktree_uses_git_exception_and_records_it(steward_home, tmp_path):
    repo = python_repo(tmp_path)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    runner = SubprocessRunner()

    create_worktree(repo, job.job_id, runner, base_dir=tmp_path / "worktrees", store=store)

    events = [e for e in store.events(job.job_id) if e.what == "exception_used"]
    assert len(events) == 1
    assert events[0].evidence["name"] == "git"
    assert events[0].evidence["action"] == "worktree_add"


def test_create_worktree_unregistered_git_raises_and_never_touches_disk(steward_home, tmp_path, monkeypatch):
    repo = python_repo(tmp_path)
    store = Store(steward_home)
    job = store.create_job("goal", str(repo))
    runner = SubprocessRunner()
    registry_path = tmp_path / "no-git-registry.toml"
    registry_path.write_text(
        '[model-api]\ndirection = "out"\npurpose = "x"\naudit = "event"\n'
    )
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(registry_path))
    base_dir = tmp_path / "worktrees"

    with pytest.raises(UnregisteredPath):
        create_worktree(repo, job.job_id, runner, base_dir=base_dir, store=store)

    assert not (base_dir / job.job_id).exists()
    violations = [e for e in store.events(job.job_id) if e.what == "exception_violation"]
    assert len(violations) == 1


def test_create_worktree_with_no_store_skips_exceptions_check(tmp_path):
    """`store=None` (the default) is the escape hatch existing callers that
    have no `Store` on hand rely on — unaffected by this wiring."""
    repo = python_repo(tmp_path)
    runner = SubprocessRunner()

    worktree_path = create_worktree(repo, "job-no-store", runner, base_dir=tmp_path / "worktrees")

    assert worktree_path.is_dir()


def test_shim_start_checks_exceptions_use_before_spawning_agent():
    calls = []

    class FakeApi:
        def send(self, pane_id, frame):
            return {"type": "ok"}

    class FakeMailbox:
        def read(self, timeout):
            return []

    def spawn(argv):
        calls.append("spawn")
        return object()

    def exceptions_use():
        calls.append("exceptions_use")

    shim = Shim("job1", "peer", FakeApi(), FakeMailbox(), spawn, ["fake"], exceptions_use=exceptions_use)
    shim.start()

    assert calls == ["exceptions_use", "spawn"]


def test_shim_start_never_spawns_when_exceptions_use_raises():
    calls = []

    class FakeApi:
        def send(self, pane_id, frame):
            return {"type": "ok"}

    class FakeMailbox:
        def read(self, timeout):
            return []

    def spawn(argv):
        calls.append("spawn")
        return object()

    def exceptions_use():
        raise UnregisteredPath("model-api")

    shim = Shim("job1", "peer", FakeApi(), FakeMailbox(), spawn, ["fake"], exceptions_use=exceptions_use)

    with pytest.raises(UnregisteredPath):
        shim.start()
    assert calls == []


def test_emit_uses_phone_notify_exception_when_registered(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    sent = []

    class FakeNotifier:
        def send(self, event):
            sent.append(event)
            return True

    event = notify.PhoneEvent(
        kind="RUNNING", job_id=job.job_id, title="t", body="b", deep_link=None, revision=1
    )

    ok = notify.emit(store, FakeNotifier(), event)

    assert ok is True
    assert len(sent) == 1
    used = [e for e in store.events(job.job_id) if e.what == "exception_used"]
    assert len(used) == 1
    assert used[0].evidence["name"] == "phone-notify"


def test_emit_skips_send_when_phone_notify_unregistered(steward_home, tmp_path, monkeypatch):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    sent = []

    class FakeNotifier:
        def send(self, event):
            sent.append(event)
            return True

    registry_path = tmp_path / "no-phone-notify.toml"
    registry_path.write_text('[git]\ndirection = "out"\npurpose = "x"\naudit = "event"\n')
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(registry_path))

    event = notify.PhoneEvent(
        kind="RUNNING", job_id=job.job_id, title="t", body="b", deep_link=None, revision=1
    )

    ok = notify.emit(store, FakeNotifier(), event)

    assert ok is False
    assert sent == []
    violations = [e for e in store.events(job.job_id) if e.what == "exception_violation"]
    assert len(violations) == 1


def test_emit_never_checks_exception_for_null_notifier(steward_home, tmp_path, monkeypatch):
    """`NullNotifier` never actually leaves Steward, so it needs no
    registry entry at all — even with an empty registry, `emit` never
    raises or records a violation for it."""
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    registry_path = tmp_path / "empty.toml"
    registry_path.write_text("")
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(registry_path))

    event = notify.PhoneEvent(
        kind="RUNNING", job_id=job.job_id, title="t", body="b", deep_link=None, revision=1
    )

    ok = notify.emit(store, notify.NullNotifier(), event)

    assert ok is True
    assert store.events(job.job_id) == []
