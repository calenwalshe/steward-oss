"""Restore from the deployed revision, never from an untested checkout (issue W, spec §5)."""
import os
import subprocess
from pathlib import Path

import pytest

from steward import deployed, launch, proclock, stability


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "live"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "f").write_text("a")
    _git(r, "add", "f")
    _git(r, "commit", "-q", "-m", "A")
    a = _git(r, "rev-parse", "HEAD")
    (r / "f").write_text("b")
    _git(r, "commit", "-q", "-am", "B")
    b = _git(r, "rev-parse", "HEAD")
    return r, a, b


def test_no_record_and_no_pending_is_the_live_checkout(tmp_path, repo):
    live, _a, b = repo
    home = tmp_path / "home"
    assert deployed.resolve_head(home, live, pending=False) == b
    assert deployed.launch_root(home, live, pending=False) == (live, None)


def test_record_behind_head_pins_a_worktree_at_the_deployed_head(tmp_path, repo):
    live, a, b = repo
    home = tmp_path / "home"
    deployed.record(home, a, str(live))
    root, export = deployed.launch_root(home, live, pending=True)
    assert root == home / "deployed" / a[:12] and export == live
    assert _git(root, "rev-parse", "HEAD") == a
    assert (root / "f").read_text() == "a"
    assert deployed.launch_root(home, live, pending=True) == (root, live)  # reused


def test_record_equal_to_head_is_live(tmp_path, repo):
    live, _a, b = repo
    home = tmp_path / "home"
    deployed.record(home, b, str(live))
    assert deployed.launch_root(home, live, pending=True) == (live, None)


def test_control_identity_head_is_used_without_a_deploy_record(tmp_path, repo):
    live, a, _b = repo
    home = tmp_path / "home"
    proclock.write_json(proclock.control_file(home), {"pid": 1, "head": a})
    assert deployed.resolve_head(home, live, pending=True) == a


def test_unknown_while_a_merge_awaits_deploy_raises(tmp_path, repo):
    live, _a, _b = repo
    with pytest.raises(deployed.UnknownDeployed):
        deployed.launch_root(tmp_path / "home", live, pending=True)


def test_pending_is_read_from_the_store_read_only(tmp_path, repo):
    from steward import deploy_pending
    from steward.store import Store

    live, _a, _b = repo
    home = tmp_path / "home"
    assert deployed.pending_mark(home) is False  # no DB at all
    store = Store(home)
    assert deployed.pending_mark(home) is False
    deploy_pending.mark(store, "j1")
    assert deployed.pending_mark(home) is True


def test_prune_keeps_the_newest_and_the_current(tmp_path, repo):
    live, a, b = repo
    home = tmp_path / "home"
    shas = [a]
    for i in range(4):
        (live / "f").write_text(str(i))
        _git(live, "commit", "-q", "-am", f"c{i}")
        shas.append(_git(live, "rev-parse", "HEAD"))
    for i, sha in enumerate(shas[:4]):
        deployed.record(home, sha, str(live))
        root, _ = deployed.launch_root(home, live, pending=True)
        os.utime(root, (1000 + i, 1000 + i))
    kept = sorted(p.name for p in (home / "deployed").iterdir())
    assert len(kept) == deployed.KEEP and shas[3][:12] in kept and shas[0][:12] not in kept


def test_prepare_reexec_moves_to_the_live_root():
    env = {"STEWARD_LIVE_ROOT": "/live", "X": "1"}
    went = []
    deployed.prepare_reexec(env, chdir=went.append)
    assert went == ["/live"] and "STEWARD_LIVE_ROOT" not in env
    deployed.prepare_reexec(env, chdir=went.append)
    assert went == ["/live"]


def test_run_command_starts_from_the_pinned_root(tmp_path):
    cmd = launch.worker_run_command("j1", worker="fabric", kind="claude", pane="w:p1", sock="/s", agent_args=[],
                                    interval=15.0, root=Path("/pinned/tree"), live_root=Path("/live"))
    assert "cd /pinned/tree &&" in cmd and "export STEWARD_LIVE_ROOT=/live" in cmd
    plain = launch.worker_run_command("j1", worker="fabric", kind="claude", pane="w:p1", sock="/s", agent_args=[],
                                      interval=15.0)
    assert "STEWARD_LIVE_ROOT" not in plain


def test_deploy_self_records_the_deployed_head(tmp_path):
    from steward import deploy_self
    from steward.store import Store

    store = Store(tmp_path / "home")

    class R:
        def run(self, *a, **k):
            from types import SimpleNamespace
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    pongs = iter([{"started_at": 1, "head": "old"}, {"started_at": 2, "head": "abc123"}])
    out = deploy_self.deploy(store, root=tmp_path, bin_dir=tmp_path / "bin", runner=R(),
                             relay_fn=lambda op, a: {"accepted": True}, import_check_fn=lambda: None,
                             health_fn=lambda: (True, "ok"), signal_file=str(tmp_path / "sig"),
                             ping_fn=lambda: next(pongs), head_fn=lambda: "abc123", sleep=lambda s: None)
    assert out["ok"], out
    assert deployed.load(store.home)["head"] == "abc123"


def test_live_root_defaults_to_repo_root():
    assert stability.LIVE_ROOT == Path(os.environ.get("STEWARD_LIVE_ROOT") or stability.REPO_ROOT)


def test_self_restart_checks_and_records_the_live_root():
    import inspect

    from steward import selfrestart

    assert inspect.signature(selfrestart.import_check).parameters["root"].default == stability.LIVE_ROOT
    assert "LIVE_ROOT" in inspect.getsource(selfrestart.SelfRestarter.__init__)
