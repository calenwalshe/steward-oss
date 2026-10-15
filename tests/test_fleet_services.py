from __future__ import annotations

from types import SimpleNamespace

import pytest

from steward import fleet_graph as fg
from steward import fleet_registry as fr
from steward.fleet_services import build_fleet_services
from steward.herdr_transport import FleetMailbox
from steward.fleet_store import FleetStore
from steward.runner_exec import CommandResult
from steward.store import Store


def _roster():
    return fr.validate_roster({"schema_version": 1, "revision": 3, "entries": [
        {"id": "worker", "provider": "anthropic", "runtime": "claude-code", "model": "sonnet",
         "roles": ["worker"], "capabilities": ["edit", "tests"], "strengths": [], "enabled": True,
         "label": "worker"},
        {"id": "lead", "provider": "anthropic", "runtime": "claude-code", "model": "opus",
         "roles": ["tech_lead"], "capabilities": ["plan", "review"], "strengths": [], "enabled": True,
         "label": "lead"},
    ]})


def test_service_factory_attaches_single_controller_and_all_fleet_services(steward_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = Store(steward_home)
    store.create_job("fleet goal", str(repo), job_id="FLEET-SERVICES")
    fs = FleetStore(store)
    roster = _roster()
    fs.record_qualification("claude-code", "test", ["edit", "tests", "plan", "review"],
                             qualified=True, available=True)
    graph = fg.compile_graph({"schema_version": 1, "plan_blob_id": "a" * 40, "tasks": [{
        "id": "t1", "objective": "implement", "done_when": ["checks pass"], "depends_on": [],
        "owns": ["src/"], "required": True, "priority": 1,
    }]}, committed_plan_blob_id="a" * 40, goal="fleet goal")
    fs.configure_job(roster, graph, job_id="FLEET-SERVICES", pins={}, policy="default", tech_lead="lead",
                     limits={"max_lanes": 1})

    class CmdRunner:
        def run(self, argv, cwd, *, timeout=None, env=None):
            return CommandResult(list(argv), str(cwd), 0, "b" * 40 + "\n", "", 0.0)

    class RunnerStub:
        def handle_fleet_frame(self, *args):
            pass

    class MailboxSource:
        def read(self, timeout):
            return []

        def close(self):
            pass

    shared_router = FleetMailbox(MailboxSource())
    ctl, writer, closer, lead, mailbox = build_fleet_services(
        store, "FLEET-SERVICES", SimpleNamespace(sock="/tmp/test.sock", pane="pane-1"), CmdRunner(),
        runner=RunnerStub(), adapter_factory=lambda **kwargs: None, probe=lambda *args: "unknown",
        advisor=lambda packet: None, mailbox_router=shared_router,
    )
    try:
        assert ctl.integration is writer
        assert ctl.frame_handler.__self__ is not None
        assert closer.store is store
        assert lead.controller is ctl and lead.advisor is not None
        assert store.kv_get("FLEET-SERVICES", "fleet_base_sha") == "b" * 40
        assert store.kv_get("FLEET-SERVICES", "fleet_capability_gate") is True
        assert ctl.capability_check("FLEET-SERVICES", "claude-code", "start") == sorted(
            fr.ACTION_REQUIREMENTS["start"]
        )
        assert mailbox is shared_router
    finally:
        shared_router.close()


def test_service_factory_binds_trial_gate_to_current_dev_build_and_rechecks_instance(
        steward_home, tmp_path, monkeypatch):
    import json

    from steward import fleet_dev

    root = tmp_path / "dev"
    home, config, data, worktrees, src, herdr = (root / name for name in
                                                  ("steward-home", "steward-config", "steward-data",
                                                   "steward-worktrees", "src", "herdr"))
    for path in (home, config, data, worktrees, src, herdr):
        path.mkdir(parents=True)
    (src / "source.py").write_text("source = True\n")
    source_digest = fleet_dev.tree_digest(src)
    build_id = "123456789abc-" + source_digest.removeprefix("sha256:")[:12]
    nonce = "a" * 32
    (home / fleet_dev.SENTINEL_NAME).write_text(json.dumps({"nonce": nonce, "build": build_id}))
    endpoints = dict(fleet_dev.DEFAULT_OUTBOUND)
    values = {
        "STEWARD_HOME": str(home), "STEWARD_CONFIG_DIR": str(config), "STEWARD_DATA_DIR": str(data),
        "STEWARD_INSTALL_ROOT": str(src), "STEWARD_FLEET_DEV_BUILD_ID": build_id,
        "STEWARD_FLEET_DEV_BUILD_DIGEST": source_digest, "STEWARD_FLEET_DEV_SENTINEL_NONCE": nonce,
        "STEWARD_FLEET_DEV_OUTBOUND": json.dumps(endpoints, sort_keys=True, separators=(",", ":")),
        "STEWARD_FLEET_DEV_SERVICE": "steward-fleet-dev-test.service",
        "HERDR_SOCKET_PATH": str(herdr / "dev.sock"), "HERDR_WORKSPACE_ID": "fleet-dev-test",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    repo = tmp_path / "repo"
    repo.mkdir()
    store = Store(steward_home)
    store.create_job("fleet goal", str(repo), job_id="FLEET-DEV-GATE")
    fs = FleetStore(store)
    roster = _roster()
    fs.record_qualification("claude-code", "test", ["edit", "tests", "plan", "review"],
                             qualified=True, available=True)
    graph = fg.compile_graph({"schema_version": 1, "plan_blob_id": "a" * 40, "tasks": [{
        "id": "t1", "objective": "implement", "done_when": ["checks pass"], "depends_on": [],
        "owns": ["src/"], "required": True, "priority": 1,
    }]}, committed_plan_blob_id="a" * 40, goal="fleet goal")
    fs.configure_job(roster, graph, job_id="FLEET-DEV-GATE", pins={}, policy="default", tech_lead="lead",
                     limits={"max_lanes": 1})

    class CmdRunner:
        def run(self, argv, cwd, *, timeout=None, env=None):
            return CommandResult(list(argv), str(cwd), 0, "b" * 40 + "\n", "", 0.0)

    class MailboxSource:
        def read(self, timeout):
            return []

        def close(self):
            pass

    router = FleetMailbox(MailboxSource())
    ctl, _, _, _, _ = build_fleet_services(
        store, "FLEET-DEV-GATE", SimpleNamespace(sock="/tmp/test.sock", pane="pane-1"), CmdRunner(),
        runner=SimpleNamespace(handle_fleet_frame=lambda *args: None), adapter_factory=lambda **kwargs: None,
        probe=lambda *args: "unknown", advisor=lambda packet: None, mailbox_router=router,
    )
    try:
        assert ctl.rollout.build == {"id": build_id, "digest": source_digest}
        assert ctl.rollout.instance_probe() == fleet_dev.instance_identity_from_env(values)
        (src / "source.py").write_text("source = False\n")
        with pytest.raises(fleet_dev.DevIsolationError, match="source digest changed"):
            ctl.rollout.instance_probe()
    finally:
        router.close()


def test_tech_lead_advisor_requests_tool_disabled_claude_json(tmp_path):
    import json

    from steward.fleet_services import FleetModelAdvisor

    proposal = {"proposal_id": "p1", "trigger_id": "trg-1", "observed": {"graph_rev": 1},
                "evidence_refs": [], "rationale": "No action", "expected_effect": {}, "actions": []}

    class CmdRunner:
        def run(self, argv, cwd, *, timeout=None, env=None):
            assert argv[0:4] == ["env", "-u", "ANTHROPIC_API_KEY", "claude"]
            assert argv[-2:] == ["--tools", ""]
            assert "--restricted" in argv and "--strict-mcp-config" in argv
            return CommandResult(list(argv), str(cwd), 0, json.dumps({"result": json.dumps(proposal)}), "", 0.0)

    result = FleetModelAdvisor("claude-code", "opus", CmdRunner(), tmp_path)(
        {"trigger_id": "trg-1", "observed": {"graph_rev": 1}}
    )
    assert result == proposal
