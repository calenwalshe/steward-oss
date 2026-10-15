"""A dev instance gets a private persistent Herdr session/workspace and tears down only that session."""

from __future__ import annotations

import pytest
from pathlib import Path
from herdr_session import herdr_available

from steward import fleet_dev as fd
from test_fleet_dev_isolation import World

pytestmark = pytest.mark.integration


def test_dev_herdr_session_and_workspace_are_persistent_and_rooted_under_dev_instance(tmp_path):
    if not herdr_available():
        pytest.skip("herdr is not installed")
    world = World(tmp_path)
    inst = world.prepare()
    try:
        session = fd.ensure_herdr_session(inst)
        workspace = fd.ensure_herdr_workspace(inst, cwd=inst.dev_root, label="fleet-dev-trial")
        reattached = fd.ensure_herdr_session(inst)
        recovered_workspace = fd.ensure_herdr_workspace(inst, cwd=inst.dev_root, label="ignored-on-recovery")

        assert session["running"] and session["name"].startswith("steward-test-")
        assert reattached["name"] == session["name"] and reattached["socket"] == session["socket"]
        assert Path(session["socket"]).resolve() == Path(inst.socket).resolve()
        assert workspace["session"] == session["name"] and workspace["workspace_id"] == inst.workspace
        assert recovered_workspace["workspace_id"] == workspace["workspace_id"]
        assert workspace["pane_id"].startswith(workspace["workspace_id"] + ":p")
        assert inst.identity()["herdr"] == workspace["workspace_id"]
        assert fd.instance_identity_from_env(inst.env()) == inst.identity()

        stopped = fd.stop_herdr_session(inst)
        assert stopped["stopped"] and stopped["session"] == session["name"]
    finally:
        # Idempotently stop only this fixture's named session if an assertion fails partway through.
        try:
            fd.stop_herdr_session(inst)
        except fd.DevIsolationError:
            pass
