"""Production adapter selection per runtime for snapshot-bound (`new --fleet-plan`) fleet jobs, and the invariant
that the roster's `dispatchable` and `build_fleet_services` can never disagree (2026-10-06: muse-worker and
agy-worker showed dispatchable, every fleet job using them died at start with "no production adapter")."""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from steward import fleet_graph as fg
from steward import fleet_live
from steward import fleet_registry as fr
from steward import fleet_runtime
from steward.fabric_worker import FabricWorker
from steward.fleet_services import FleetModelAdvisor, FleetServicesError, build_fleet_services, needs_herdr
from steward.fleet_store import FleetStore
from steward.herdr_transport import FleetMailbox
from steward.runner_exec import CommandResult
from steward.store import Store
from steward.worker import CodexAppServerAdapter

PROVIDER = {"claude-code": "anthropic", "claude-fabric": "anthropic", "codex": "openai", "muse": "meta",
            "agy": "google", "pi": "deepinfra"}


def _entry(rid, runtime, model, roles):
    return {"id": rid, "provider": PROVIDER.get(runtime, "other"), "runtime": runtime, "model": model,
            "roles": roles, "capabilities": ["edit", "tests", "plan", "review"], "strengths": [], "enabled": True,
            "label": rid}


def _qualify(fs, runtime, caps=fr.RUNTIME_CAPABILITIES):
    fs.record_qualification(runtime, "test", ["edit", "tests", "plan", "review"], qualified=True, available=True)
    for cap in caps:
        fs.record_runtime_capability(runtime, cap, qualified=True, adapter_version="1", evidence="test probe")


class CmdRunner:
    def run(self, argv, cwd, *, timeout=None, env=None):
        return CommandResult(list(argv), str(cwd), 0, "b" * 40 + "\n", "", 0.0)


class MailboxSource:
    def read(self, timeout):
        return []

    def close(self):
        pass


def _job(store, job_id, tmp_path, worker_runtime, *, lead_runtime="codex", qualify_worker=True):
    repo = tmp_path / f"repo-{job_id}"
    repo.mkdir()
    store.create_job("fleet goal", str(repo), job_id=job_id)
    fs = FleetStore(store)
    roster = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [
        _entry("w", worker_runtime, f"{worker_runtime}-model", ["worker"]),
        _entry("lead", lead_runtime, f"{lead_runtime}-lead-model", ["tech_lead"]),
    ]})
    _qualify(fs, lead_runtime)
    if qualify_worker:
        _qualify(fs, worker_runtime)
    graph = fg.compile_graph({"schema_version": 1, "plan_blob_id": "a" * 40, "tasks": [
        {"id": t, "objective": f"do {t}", "done_when": ["checks pass"], "depends_on": [], "owns": [f"{t}/"],
         "required": True, "priority": 1} for t in ("t1", "t2")]}, committed_plan_blob_id="a" * 40, goal="fleet goal")
    fs.configure_job(roster, graph, job_id=job_id, pins={}, policy="default", tech_lead="lead",
                     limits={"max_lanes": 2})
    return fs, roster


@pytest.fixture
def no_herdr(monkeypatch):
    for key in [k for k in os.environ if k.startswith("HERDR_")]:
        monkeypatch.delenv(key)


def _build(store, job_id, *, herdr=True, structured=None, spawned=None):
    calls = spawned if spawned is not None else []

    def fake_structured(runtime, **kw):
        calls.append((runtime, kw))
        return SimpleNamespace(runtime=runtime, kw=kw, process_identity=lambda: {"pid": 4242})

    def fake_spawn(*args, **kw):
        calls.append(("spawn", kw))
        return lambda worktree: "pane-lane"

    args = SimpleNamespace(sock="/tmp/test.sock", pane="pane-1") if herdr else SimpleNamespace(sock=None, pane=None)
    return build_fleet_services(
        store, job_id, args, CmdRunner(), runner=SimpleNamespace(handle_fleet_frame=lambda *a: None),
        advisor=lambda packet: None, structured=structured or fake_structured, fabric_spawn=fake_spawn,
        mailbox_source=MailboxSource() if herdr else None,
    )


def _make(ctl, store, fs, job_id, lane="lane-t1"):
    fs.record_assignment(job_id, lane, roster_id="w", actor="test", reason="test", policy_rule="pinned")
    return ctl.factory(job_id=job_id, lane_id=lane, task_id=lane.removeprefix("lane-"), attempt_no=1, epoch=1)


@pytest.mark.parametrize("runtime", sorted(fr.PRODUCTION_RUNTIMES["worker"]))
def test_every_production_worker_runtime_selects_its_own_adapter(steward_home, tmp_path, runtime):
    store = Store(steward_home)
    fs, _ = _job(store, "J-" + runtime, tmp_path, runtime)
    calls = []
    ctl, _, _, _, mailbox = _build(store, "J-" + runtime, spawned=calls)
    try:
        adapter = _make(ctl, store, fs, "J-" + runtime)
        if runtime in fr.PANE_RUNTIMES:
            assert isinstance(adapter, FabricWorker)
            assert calls == [("spawn", {"sock": "/tmp/test.sock", "agent": "claude-code", "agent_script": None,
                                        "extra_args": [], "model": f"{runtime}-model", "task": "t1",
                                        "lane": "lane-t1", "attempt": 1, "epoch": 1})]
        elif runtime == "codex":
            assert isinstance(adapter, CodexAppServerAdapter) and calls == []
        else:
            assert isinstance(adapter, fleet_live.StructuredLane)
            assert adapter.requested == {"provider": PROVIDER[runtime], "runtime": runtime,
                                         "model": f"{runtime}-model"}
            assert adapter.process_identity() == {"pid": 4242, "runtime": runtime}
            (rt_name, kw), = calls
            assert rt_name == runtime and kw["model"] == f"{runtime}-model" and kw["home"] == store.home
            assert (kw["job_id"], kw["lane_id"], kw["task_id"], kw["attempt_no"], kw["epoch"]) == (
                "J-" + runtime, "lane-t1", "t1", 1, 1)
            assert kw["head_fn"] is fleet_live.git_head
        # every operation stays checked against the recorded qualification evidence
        assert ctl.capability_check("J-" + runtime, runtime, "start") == []
    finally:
        if mailbox is not None:
            mailbox.close()


def test_default_structured_builds_the_real_muse_agy_pi_adapters(tmp_path):
    from steward.agy_worker import AgyStreamAdapter
    from steward.muse_worker import MuseServeAdapter
    from steward.pi_worker import PI_MODEL, PiRpcAdapter

    kw = dict(model="m", job_id="J", lane_id="lane-t1", task_id="t1", attempt_no=1, epoch=1, lease_check=None,
              head_fn=None, home=tmp_path)
    expected = {"muse": MuseServeAdapter, "agy": AgyStreamAdapter, "pi": PiRpcAdapter, "codex": CodexAppServerAdapter}
    models = {"pi": PI_MODEL}
    for runtime, cls in expected.items():
        assert isinstance(fleet_live.default_structured(runtime, **{**kw, "model": models.get(runtime, "m")}), cls)
    # the services table never names a structured runtime the shared constructor cannot build
    assert fr.PRODUCTION_RUNTIMES["worker"] - fr.PANE_RUNTIMES == set(expected)


def test_unqualified_runtime_still_cannot_start_even_with_an_adapter(steward_home, tmp_path):
    store = Store(steward_home)
    fs, roster = _job(store, "J-gate", tmp_path, "muse")
    ctl, *_ = _build(store, "J-gate", herdr=False)
    fs.record_runtime_capability("muse", "permission_relay", qualified=False, adapter_version="1",
                                 evidence="later probe: not relayed")
    assert ctl.capability_check("J-gate", "muse", "start") == ["permission_relay"]


@pytest.mark.parametrize("runtime", sorted(fr.PRODUCTION_RUNTIMES["worker"] | {"ollama", "gemini-cli"}))
def test_roster_dispatchable_agrees_with_fleet_services(steward_home, tmp_path, runtime):
    """The invariant: a qualified worker is `dispatchable` in the roster view iff fleet services can build its
    lane adapter. Both read `fleet_registry.PRODUCTION_RUNTIMES`."""
    store = Store(steward_home)
    fs = FleetStore(store)
    _qualify(fs, runtime)
    entry = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [
        _entry("w", runtime, "m", ["worker"])]}).entries[0]
    row = fs.roster_view(fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [
        _entry("w", runtime, "m", ["worker"])]}))[0]
    runnable = runtime in fr.PRODUCTION_RUNTIMES["worker"]
    assert fs.dispatchable(entry) is runnable and row["dispatchable"] is runnable
    assert row["no_production_adapter"] == ([] if runnable else ["worker"])
    if not runnable:
        return
    fs2, _ = _job(store, "J-agree", tmp_path, runtime)
    ctl, _, _, _, mailbox = _build(store, "J-agree")
    try:
        assert _make(ctl, store, fs2, "J-agree") is not None
    finally:
        if mailbox is not None:
            mailbox.close()


def test_a_runtime_dropped_from_the_table_is_refused_by_both_views_at_once(steward_home, tmp_path, monkeypatch):
    store = Store(steward_home)
    fs, roster = _job(store, "J-drop", tmp_path, "muse")
    monkeypatch.setitem(fr.PRODUCTION_RUNTIMES, "worker", fr.PRODUCTION_RUNTIMES["worker"] - {"muse"})
    assert not fs.dispatchable(roster.get("w"))
    with pytest.raises(FleetServicesError, match="no production adapter: w"):
        _build(store, "J-drop", herdr=False)


def test_tech_lead_runtime_table_matches_the_advisor(steward_home, tmp_path):
    for runtime in sorted(fr.PRODUCTION_RUNTIMES["tech_lead"]):
        seen = []

        class Runner:
            def run(self, argv, cwd, *, timeout=None, env=None):
                seen.append(argv)
                return CommandResult(list(argv), str(cwd), 1, "", "boom", 0.0)

        with pytest.raises(FleetServicesError, match="exited 1"):
            FleetModelAdvisor(runtime, "m", Runner(), tmp_path)({"trigger_id": "t", "observed": {}})
        assert seen and (seen[0][3] == "claude" if runtime.startswith("claude") else seen[0][0] == "codex")
    # a muse tech lead is never dispatchable (no advisor), so a snapshot can never freeze one
    fs = FleetStore(Store(steward_home))
    _qualify(fs, "muse")
    lead = fr.validate_roster({"schema_version": 1, "revision": 1, "entries": [
        _entry("lead", "muse", "m", ["tech_lead"])]}).entries[0]
    assert not fs.dispatchable(lead)


def test_claude_fabric_tech_lead_is_accepted(steward_home, tmp_path):
    store = Store(steward_home)
    _job(store, "J-fable", tmp_path, "muse", lead_runtime="claude-fabric")
    ctl, _, _, lead, mailbox = _build(store, "J-fable", herdr=False)
    assert mailbox is None and lead.controller is ctl


def test_structured_only_job_runs_without_herdr(steward_home, tmp_path, no_herdr):
    store = Store(steward_home)
    fs, _ = _job(store, "J-agy", tmp_path, "agy")
    assert needs_herdr(store, "J-agy") is False
    ctl, _, _, _, mailbox = _build(store, "J-agy", herdr=False)
    assert mailbox is None
    assert isinstance(_make(ctl, store, fs, "J-agy"), fleet_live.StructuredLane)


def test_claude_fabric_job_without_herdr_is_refused(steward_home, tmp_path, no_herdr):
    store = Store(steward_home)
    _job(store, "J-cf", tmp_path, "claude-fabric")
    assert needs_herdr(store, "J-cf") is True
    with pytest.raises(FleetServicesError, match="Herdr"):
        _build(store, "J-cf", herdr=False)


def test_structured_lane_probe_reads_the_process_group_leader(steward_home, tmp_path):
    store = Store(steward_home)
    fs, _ = _job(store, "J-probe", tmp_path, "pi")
    ctl, *_ = _build(store, "J-probe", herdr=False)
    fs.record_assignment("J-probe", "lane-t1", roster_id="w", actor="test", reason="test", policy_rule="pinned")
    proc = subprocess.Popen(["sleep", "30"])
    try:
        assert ctl.probe("J-probe", "lane-t1", {"pid": proc.pid}) == fleet_runtime.ALIVE
    finally:
        proc.kill()
        proc.wait()
    assert ctl.probe("J-probe", "lane-t1", {"pid": proc.pid}) == fleet_runtime.DEAD
    assert ctl.probe("J-probe", "lane-t1", {}) == fleet_runtime.UNKNOWN


def test_shared_mailbox_router_still_requires_herdr(steward_home, tmp_path, no_herdr):
    store = Store(steward_home)
    _job(store, "J-router", tmp_path, "muse")
    router = FleetMailbox(MailboxSource())
    try:
        with pytest.raises(FleetServicesError, match="Herdr"):
            build_fleet_services(store, "J-router", SimpleNamespace(sock=None, pane=None), CmdRunner(),
                                 runner=SimpleNamespace(handle_fleet_frame=lambda *a: None),
                                 advisor=lambda p: None, mailbox_router=router)
    finally:
        router.close()
