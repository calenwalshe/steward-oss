"""Phase 29-03 tasks 1-2 (FLEET-22): side-by-side dev instance isolation, fake-only outbound, live-state sentinels,
exact build identity, preflight-only trial receipt and the bounded trial run. Temp 'live' roots, a temp git repo, a
real dev Store and fake lane adapters; real subprocesses only for git and one harmless dev child. No network."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest
from test_fleet_integration import git
from test_fleet_lanes import JOB
from test_fleet_learning_replay import note, run_replay
from test_fleet_tech_lead import TL

from steward import fleet_closeout as fc
from steward import fleet_dev as fd
from steward import fleet_runtime as rt
from steward.api import StewardAPI
from steward.runner_exec import CommandResult, FakeRunner, SubprocessRunner
from steward.store import Store

BASE_ENV = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "HERDR_PANE_ID": "live:pane", "STEWARD_EXCEPTIONS": "/live/x",
            "TELEGRAM_BOT_TOKEN": "live-secret", "STEWARD_HOME": "/live/home"}


class World:
    """A temp 'live' installation: real Store with rows, config, data, job worktrees, plus a pinned source repo."""

    def __init__(self, tmp):
        self.root = tmp / "live"
        self.home, self.cfg, self.data = self.root / "home", self.root / "cfg", self.root / "data"
        for d in (self.home, self.cfg, self.data, self.home / "jobs", self.home / "worktrees"):
            d.mkdir(parents=True)
        (self.cfg / "steward.toml").write_text('acceptance = "auto"\n')
        (self.data / "consent.secret").write_text("s3cret")
        live_store = Store(self.home)
        live_store.create_job("live goal", "/repo", job_id="live-job")
        live_store._conn.execute("INSERT INTO events (job_id, ts, what, evidence, rule, judgment)"
                                 " VALUES ('live-job', 't', 'seeded', '{}', 'r', 'code')")
        live_store._conn.commit()
        live_store._conn.close()
        for n in ("wt-a", "wt-b"):
            d = self.home / "worktrees" / n
            d.mkdir()
            (d / ".git").write_text(f"gitdir: /elsewhere/{n}\n")
        self.live = fd.LiveState(home=self.home, config=self.cfg, data=self.data, worktrees=self.home / "worktrees",
                                 jobs=self.home / "jobs", herdr_socket=str(self.root / "live.sock"),
                                 herdr_workspace="live-ws", service="steward-live.service")
        self.repo = tmp / "source_repo"
        self.repo.mkdir()
        (self.repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
        (self.repo / "steward").mkdir()
        (self.repo / "steward" / "mod.py").write_text("X = 1\n")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=T", "-c", "user.email=t@x", "commit", "-q", "-m", "init")
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.dev_root = tmp / "dev_root"

    def prepare(self, **kw):
        kw.setdefault("base_env", BASE_ENV)
        return fd.prepare_instance(self.repo, self.commit, self.dev_root, live=self.live, runner=SubprocessRunner(), **kw)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# ------------------------------------------------------------------------------------------ build identity


def test_snapshot_pins_the_exact_commit_with_a_deterministic_digest_and_label_and_leaves_version_alone(world):
    a = world.prepare()
    assert a.build["commit"] == world.commit and a.build["label"].startswith("fleet-dev+" + world.commit[:12])
    assert a.build["digest"].startswith("sha256:") and a.build["id"] == f"{world.commit[:12]}-{a.build['digest'][7:19]}"
    assert (a.src / "steward" / "mod.py").read_text() == "X = 1\n"
    assert a.src / "pyproject.toml" and a.version == "1.2.3"                      # public version untouched
    assert (a.src / "pyproject.toml").read_bytes() == (world.repo / "pyproject.toml").read_bytes()
    # same bytes, same digest; the build id does not depend on where the dev root is
    assert fd.tree_digest(a.src) == a.build["digest"]
    assert git(world.repo, "status", "--porcelain") == "" and git(world.repo, "worktree", "list").count("\n") == 0


def test_dev_environment_reconstructs_the_same_build_bound_instance_identity(world):
    inst = world.prepare()
    env = inst.env(BASE_ENV)

    assert env["STEWARD_FLEET_DEV_BUILD_ID"] == inst.build["id"]
    assert env["STEWARD_FLEET_DEV_BUILD_DIGEST"] == inst.build["digest"]
    assert fd.instance_identity_from_env(env) == inst.identity()


def test_digest_changes_when_one_source_byte_changes(world):
    inst = world.prepare()
    before = fd.tree_digest(inst.src)
    (inst.src / "steward" / "mod.py").write_text("X = 2\n")
    assert fd.tree_digest(inst.src) != before and inst.build["digest"] == before


def test_unresolvable_commit_refuses(world):
    with pytest.raises(fd.DevIsolationError, match="cannot resolve"):
        fd.prepare_instance(world.repo, "deadbeef" * 5, world.dev_root, live=world.live, runner=SubprocessRunner())


# ------------------------------------------------------------------------------------------ resolved paths


def test_dev_roots_are_distinct_absolute_and_clear_of_every_live_path(world):
    inst = world.prepare()
    roots = {k: fd.real(v) for k, v in inst.roots().items() if k != "dev_root"}
    assert len(set(roots.values())) == 4 and all(p.is_absolute() for p in roots.values())
    assert fd.validate(inst) == []
    for name, live_path in world.live.protected().items():
        for p in roots.values():
            assert not (p == live_path or p.is_relative_to(live_path) or live_path.is_relative_to(p)), name


@pytest.mark.parametrize("where", ["live_home", "inside_home", "inside_worktree_entry", "contains_config", "live_config",
                                   "inside_data", "symlink_into_home"])
def test_any_overlap_with_live_state_is_refused_before_anything_exists(world, tmp_path, where):
    target = {
        "live_home": world.home, "inside_home": world.home / "dev", "inside_worktree_entry": world.home / "worktrees" / "wt-a" / "d",
        "contains_config": world.root, "live_config": world.cfg, "inside_data": world.data / "dev",
        "symlink_into_home": tmp_path / "link"}[where]
    if where == "symlink_into_home":
        target.symlink_to(world.home)
    with pytest.raises(fd.DevIsolationError, match="overlaps live"):
        fd.prepare_instance(world.repo, world.commit, target, live=world.live, runner=SubprocessRunner())
    assert not (world.home / "dev").exists() and list((world.home / "worktrees").iterdir()) and \
        sorted(p.name for p in (world.home / "worktrees").iterdir()) == ["wt-a", "wt-b"]


def test_host_worktree_exemption_is_narrow(world):
    host = world.home / "worktrees" / "wt-a"
    inside = host / ".scratch" / "dev"
    inst = fd.prepare_instance(world.repo, world.commit, inside, live=world.live, runner=SubprocessRunner(), host=host)
    assert fd.validate(inst) == [] and inst.home.is_relative_to(host)
    other = world.home / "worktrees" / "wt-b" / "dev"
    with pytest.raises(fd.DevIsolationError, match="overlaps live"):                 # a different live worktree
        fd.prepare_instance(world.repo, world.commit, other, live=world.live, runner=SubprocessRunner(), host=host)
    with pytest.raises(fd.DevIsolationError, match="overlaps live"):                 # host named but root outside it
        fd.prepare_instance(world.repo, world.commit, world.home / "elsewhere", live=world.live,
                            runner=SubprocessRunner(), host=host)


def test_dev_roots_overlapping_each_other_are_refused(world):
    inst = world.prepare()
    inst.config = inst.home / "cfg"
    assert any("overlaps dev" in p for p in fd.validate(inst))


# ------------------------------------------------------------------------------------------ identities


def test_herdr_service_and_socket_identity_are_unique_and_live_identity_is_refused(world):
    inst = world.prepare()
    assert inst.workspace != "live-ws" and inst.service != "steward-live.service" and inst.socket != world.live.herdr_socket
    assert inst.service.startswith("steward-fleet-dev-") and inst.build["id"] in inst.service
    w2 = World(world.root.parent / "w2")
    b = fd.prepare_instance(w2.repo, w2.commit, w2.dev_root, live=w2.live, runner=SubprocessRunner())
    assert b.service != inst.service and b.workspace != inst.workspace
    for field, live_val in (("workspace", "live-ws"), ("service", "steward-live.service"),
                            ("socket", world.live.herdr_socket)):
        bad = fd.DevInstance(**{**inst.__dict__, field: live_val})
        assert fd.validate(bad), field


def test_inherited_live_herdr_identity_in_the_environment_is_refused(world):
    inst = world.prepare()
    env = inst.env(BASE_ENV)
    assert env["HERDR_WORKSPACE_ID"] == inst.workspace and env["HERDR_SOCKET_PATH"] == inst.socket
    assert "HERDR_PANE_ID" not in env and "STEWARD_EXCEPTIONS" not in env and "TELEGRAM_BOT_TOKEN" not in env
    assert env["STEWARD_HOME"] == str(inst.home) and env["STEWARD_CONFIG_DIR"] == str(inst.config)
    assert env["STEWARD_DATA_DIR"] == str(inst.data) and env["STEWARD_NOTIFIER"].startswith("fake://")
    inst.extra_env = {"HERDR_WORKSPACE_ID": "live-ws"}
    with pytest.raises(fd.DevIsolationError, match="live identity"):
        inst.env(BASE_ENV)
    inst.extra_env = {"TELEGRAM_BOT_TOKEN": "x"}
    with pytest.raises(fd.DevIsolationError, match="credential or destination"):
        inst.env(BASE_ENV)


# ------------------------------------------------------------------------------------------ outbound


GOOD = {"notifier": "fake://dev-notifier", "connector": "http://127.0.0.1:9999/hook", "tunnel": "http://localhost:1/x"}
BAD = ["https://api.telegram.org/bot1/sendMessage", "http://10.0.0.5/x", "http://127.0.0.1.evil.com/x", "fake://a.b.com",
       "mailto:sam@example.com", "tcp://example.com:22", "", "http://[::2]/x", "https://hooks.slack.com/x",
       "http://user@example.com@127.0.0.1/x"]


def test_loopback_and_fake_endpoints_are_the_only_ones_allowed():
    assert fd.check_outbound(GOOD) == [] and fd.check_outbound({"n": "http://[::1]:80/x"}) == []
    for target in BAD:
        assert fd.check_outbound({"notifier": target}), target
    assert fd.check_outbound({}) and fd.check_outbound({"a": "fake://ok", "b": BAD[0]})


def test_live_destination_in_config_blocks_the_start_and_nothing_is_created(world):
    for bad in ({"notifier": "https://api.telegram.org/bot1/sendMessage"}, {"notifier": "fake://ok", "tunnel": "http://10.1.1.1"}):
        with pytest.raises(fd.DevIsolationError, match="outbound"):
            world.prepare(outbound=bad)
        assert not world.dev_root.exists()
    inst = world.prepare(outbound=GOOD)
    inst.outbound = {**GOOD, "notifier": "https://example.com/hook"}
    assert any("outbound" in p for p in fd.validate(inst))


# ------------------------------------------------------------------------------------------ sentinels


def test_a_harmless_dev_child_operation_leaves_every_live_sentinel_unchanged(world):
    inst = world.prepare()
    scratch = inst.dev_root / "scratch"
    before = fd.snapshot_live(world.live, scratch, canaries=fd.canaries_for(inst, "dev-job"))
    probe = fd.run_child_probe(inst, SubprocessRunner(), base_env=BASE_ENV)
    assert probe["home"] == str(fd.real(inst.home)) and probe["workspace"] == inst.workspace
    assert probe["socket"] == inst.socket and probe["has_pane"] is False and probe["notifier"].startswith("fake://")
    assert (inst.home / "worktrees" / "probe" / "marker.txt").exists() and (inst.config / "probe.toml").exists()
    after = fd.snapshot_live(world.live, scratch, limits=before["store"]["limits"],
                             canaries=fd.canaries_for(inst, "dev-job"))
    verdict = fd.compare_live(before, after)
    assert verdict["unchanged"], verdict
    assert not (world.cfg / "probe.toml").exists() and not (world.home / "probe.db").exists()
    assert not (scratch / "live-store-copy").exists()


def test_any_live_store_or_worktree_activity_invalidates_the_trial_sentinel(world):
    inst = world.prepare()
    scratch = inst.dev_root / "scratch"
    can = fd.canaries_for(inst, "dev-job")
    existing_worktree_file = world.home / "worktrees" / "wt-b" / "source.py"
    existing_worktree_file.write_text("before\n")
    before = fd.snapshot_live(world.live, scratch, canaries=can)
    # A concurrent live operation invalidates trial evidence; it cannot be attributed to isolation.
    s = Store(world.home)
    s.create_job("another", "/r", job_id="live-job-2")
    s._conn.close()
    (world.home / "worktrees" / "wt-new").mkdir()
    ok = fd.compare_live(before, fd.snapshot_live(world.live, scratch, limits=before["store"]["limits"], canaries=can))
    assert not ok["unchanged"]
    assert any("live store content changed" in p for p in ok["problems"])
    assert any("live worktrees entries added" in p for p in ok["problems"])
    # A config edit, contents changed under a pre-existing worktree, or a dev job in the live Store also fail.
    (world.cfg / "steward.toml").write_text('acceptance = "manual"\n')
    assert any("config content" in p for p in fd.compare_live(
        before, fd.snapshot_live(world.live, scratch, limits=before["store"]["limits"], canaries=can))["problems"])
    (world.cfg / "steward.toml").write_text('acceptance = "auto"\n')
    existing_worktree_file.write_text("after\n")
    assert any("wt-b" in p for p in fd.compare_live(
        before, fd.snapshot_live(world.live, scratch, limits=before["store"]["limits"], canaries=can))["problems"])
    existing_worktree_file.write_text("before\n")
    s = Store(world.home)
    s.create_job("leak", "/r", job_id="dev-job")
    s._conn.close()
    bad = fd.compare_live(before, fd.snapshot_live(world.live, scratch, limits=before["store"]["limits"], canaries=can))
    assert not bad["unchanged"] and any("canary jobs:dev-job" in p for p in bad["problems"])


def test_a_rewritten_pre_existing_live_store_row_is_detected(world):
    inst = world.prepare()
    scratch = inst.dev_root / "scratch"
    before = fd.snapshot_live(world.live, scratch)
    assert "events" in before["store"]["prefix"] and before["store"]["prefix"]["events"]["limit"] >= 1
    conn = sqlite3.connect(world.home / "steward.db")
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET what='tampered' WHERE seq=1")
    conn.commit()
    conn.close()
    bad = fd.compare_live(before, fd.snapshot_live(world.live, scratch, limits=before["store"]["limits"]))
    assert any("live store content changed" in p for p in bad["problems"])


def test_live_sentinel_hashes_contents_behind_existing_directory_symlinks(world, tmp_path):
    inst = world.prepare()
    target = tmp_path / "external-job"
    target.mkdir()
    file = target / "state.txt"
    file.write_text("before\n")
    (world.home / "jobs" / "linked-job").symlink_to(target, target_is_directory=True)
    before = fd.snapshot_live(world.live, inst.dev_root / "scratch")

    file.write_text("after\n")
    after = fd.snapshot_live(world.live, inst.dev_root / "scratch", limits=before["store"]["limits"])

    verdict = fd.compare_live(before, after)
    assert not verdict["unchanged"]
    assert any("linked-job" in problem for problem in verdict["problems"])


def test_live_rollback_identity_is_observed_through_the_runner_only(world, tmp_path):
    live = fd.LiveState(**{**world.live.__dict__, "checkout": world.repo, "unit_file": world.cfg / "steward.toml"})
    calls = []

    def respond(argv, cwd):
        calls.append(argv)
        if argv[:3] == ["git", "-C", str(world.repo)]:
            return CommandResult(argv, cwd, 0, world.commit + "\n", "", 0)
        return CommandResult(argv, cwd, 0, "ActiveState=active\nMainPID=42\n", "", 0)

    ident = fd.live_rollback_identity(live, FakeRunner(respond))
    assert ident["checkout_head"] == world.commit and ident["public_version"] == "1.2.3"
    assert ident["service_state"] == {"ActiveState": "active", "MainPID": "42"} and len(ident["unit_sha256"]) == 64
    assert all(a[0] in ("git", "systemctl") and not any(w in a for w in ("restart", "stop", "start", "enable", "kill"))
               for a in calls)


# ------------------------------------------------------------------------------------------ preflight (task 1)


def dev_gate(inst, store, clock=None):
    kw = {"clock": clock} if clock else {}
    return rt.RolloutGate(store, build={"id": inst.build["id"], "digest": inst.build["digest"]},
                          instance_probe=inst.identity, **kw)


def rollback(world):
    return {"checkout_head": world.commit, "public_version": "1.2.3", "service": "steward-live.service"}


def test_preflight_receipt_binds_build_instance_job_cap_expiry_kill_generation_and_grants_only_trial(world):
    inst = world.prepare()
    store = Store(inst.home)
    gate = dev_gate(inst, store)
    before = fd.snapshot_live(world.live, inst.dev_root / "scratch", canaries=fd.canaries_for(inst, JOB))
    rec = fd.issue_preflight(inst, gate, job_id=JOB, before=before, rollback=rollback(world), ttl_s=600, max_actions=1)
    assert rec["grants"] == ["trial"] and rec["max_actions"] == 1 and rec["job_id"] == JOB
    row = gate.fleet.rollout_evidence("trial_preflight")[-1]
    assert row["build_id"] == inst.build["id"] and row["build_digest"] == inst.build["digest"] and row["job_id"] == JOB
    assert row["instance"] == inst.identity() and row["instance"]["service"] == inst.service
    assert row["instance"]["socket"] == inst.identity()["socket"] and row["instance"]["herdr"] == inst.workspace
    assert row["payload"]["kill_switch_generation"] == gate.kill_switch_generation() == 0
    b = row["payload"]["bindings"]
    assert b["outbound"] == inst.outbound and b["rollback"]["checkout_head"] == world.commit
    assert b["denies"] == ["active", "production_promotion"] and b["live_sentinel_digest"] == before["digest"]
    assert row["expires_at"] is not None and row["payload"]["max_actions"] == 1
    # it enables narrow trial authority only
    assert gate.trial_authority(JOB, audit=False)["valid"] and gate.trial_authority("other-job", audit=False)["valid"] is False
    assert gate.request_mode("active", actor="x")["accepted"] is False and gate.requested_mode() == "shadow"
    assert gate.eligibility()["eligible"] is False and gate.promotion_status()["eligible"] is False
    assert gate.fleet.rollout_evidence("trial_receipt") == []
    assert gate.fleet.rollout_evidence("active_owner_approval") == []
    assert gate.fleet.rollout_evidence("production_approval") == []


def test_preflight_refuses_a_gate_that_cannot_verify_or_is_bound_to_another_build(world):
    inst = world.prepare()
    store = Store(inst.home)
    before = fd.snapshot_live(world.live, inst.dev_root / "scratch")
    blind = rt.RolloutGate(store, build={"id": inst.build["id"], "digest": inst.build["digest"]})
    with pytest.raises(fd.DevIsolationError, match="unverifiable"):
        fd.issue_preflight(inst, blind, job_id=JOB, before=before, rollback={})
    other = rt.RolloutGate(store, build={"id": "x", "digest": "sha256:y"}, instance_probe=inst.identity)
    with pytest.raises(fd.DevIsolationError, match="not bound to the dev build"):
        fd.issue_preflight(inst, other, job_id=JOB, before=before, rollback={})
    (inst.home / fd.SENTINEL_NAME).unlink()
    with pytest.raises(fd.DevIsolationError, match="sentinel"):
        fd.issue_preflight(inst, dev_gate(inst, store), job_id=JOB, before=before, rollback={})
    assert store._conn.execute("SELECT COUNT(*) FROM fleet_rollout_evidence").fetchone()[0] == 0


# ------------------------------------------------------------------------------------------ bounded trial (task 2)


class Trial:
    """The dev instance plus the Phase 28 fake-adapter world living entirely in the dev Store/dev root."""

    def __init__(self, world, tmp_path, *, max_actions=1, ttl_s=600):
        self.world = world
        self.inst = world.prepare()
        (self.inst.dev_root / "work").mkdir()
        self.e = TL(self.inst.dev_root / "work", self.inst.home)
        self.gate = dev_gate(self.inst, self.e.store, clock=self.e.clock)
        self.e.ctl.rollout = self.gate
        self.scratch = self.inst.dev_root / "scratch"
        self.can = fd.canaries_for(self.inst, JOB)
        self.before = fd.snapshot_live(world.live, self.scratch, canaries=self.can)
        self.receipt = fd.issue_preflight(self.inst, self.gate, job_id=JOB, before=self.before,
                                          rollback=rollback(world), ttl_s=ttl_s, max_actions=max_actions)

    def after(self):
        return fd.snapshot_live(self.world.live, self.scratch, limits=self.before["store"]["limits"], canaries=self.can)


def replay_driver(t, monkeypatch):
    def driver(ctl):
        out, _ = run_replay(t.e, monkeypatch)
        return {"status": out["status"]}
    return driver


def test_trial_runs_one_capped_workflow_to_one_candidate_wholly_under_the_dev_root(world, tmp_path, monkeypatch):
    t = Trial(world, tmp_path)
    run = fd.run_trial(t.inst, t.gate, t.e.ctl, JOB, replay_driver(t, monkeypatch), clock=t.e.clock)
    assert run["ran"] and run["error"] is None and run["driver"]["status"] == "candidate"
    cand = fc.final_candidate(t.e.store, JOB)
    assert run["candidate_sha"] == cand["final_sha"] == cand["verified_sha"] and len(fc.candidates(t.e.store, JOB)) == 1
    assert run["actions_used"] == 1 and run["remaining"] == 0 and run["within_expiry"] and run["revoked"] == []
    for p in (t.e.repo, t.e.writer.integration_path(JOB), t.e.store.home):
        assert fd.real(p).is_relative_to(fd.real(t.inst.dev_root)), p
    # every further proposal is blocked: cap spent; nothing outside the dev root moved
    res = note(t.e, "post")
    assert res["outcome"] == "rejected" and "trial_cap_exhausted" in res["reason"]
    assert fd.compare_live(t.before, t.after())["unchanged"]
    assert t.gate.requested_mode() == "shadow" and not t.gate.eligibility()["eligible"]
    assert t.gate.fleet.rollout_evidence("trial_receipt") == []                    # raw evidence only, no receipt yet


def test_run_trial_refuses_without_valid_preflight_authority(world, tmp_path, monkeypatch):
    t = Trial(world, tmp_path)
    t.e.clock.t += 601
    out = fd.run_trial(t.inst, t.gate, t.e.ctl, JOB, lambda ctl: pytest.fail("must not run"), clock=t.e.clock)
    assert out == {"ran": False, "reasons": ["trial_expired"]}
    assert [r["payload"]["reasons"] for r in t.gate.fleet.rollout_evidence("trial_revoked")] == [["trial_expired"]]


def test_source_drift_between_calls_revokes_the_trial_and_no_lane_action_happens(world, tmp_path):
    t = Trial(world, tmp_path, max_actions=2)
    t.e.admit()
    assert note(t.e, "ok")["outcome"] == "applied"
    (t.inst.src / "steward" / "mod.py").write_text("X = 999\n")                         # build digest drift
    res = note(t.e, "drift")
    assert res["outcome"] == "rejected" and res["reason"] == "instance_identity_changed"
    (t.inst.src / "steward" / "mod.py").write_text("X = 1\n")                           # restoring does not restore
    assert note(t.e, "again")["outcome"] == "rejected" and len(t.e.ctl.owner_decisions(JOB)) == 1
    assert t.gate.fleet.rollout_evidence("trial_revoked")


@pytest.mark.parametrize("drift", ["sentinel", "outbound", "overlap", "workspace", "socket"])
def test_any_isolation_drift_revokes_authority(world, tmp_path, drift):
    t = Trial(world, tmp_path, max_actions=3)
    t.e.admit()
    inst = t.inst
    if drift == "sentinel":
        (inst.home / fd.SENTINEL_NAME).write_text(json.dumps({"nonce": "other"}))
    elif drift == "outbound":
        inst.outbound = {**inst.outbound, "notifier": "https://api.telegram.org/x"}
    elif drift == "overlap":
        inst.config = world.cfg
    elif drift == "workspace":
        inst.workspace = world.live.herdr_workspace
    else:
        inst.socket = world.live.herdr_socket
    res = note(t.e, "x")
    assert res["outcome"] == "rejected" and res["reason"] in ("instance_unverifiable", "instance_identity_changed")
    assert t.e.ctl.owner_decisions(JOB) == []


def test_kill_switch_activation_revokes_even_after_it_is_switched_back_off(world, tmp_path):
    t = Trial(world, tmp_path, max_actions=3)
    t.e.admit()
    t.gate.set_kill_switch(True, actor="sam")
    assert note(t.e, "a")["reason"] == "kill_switch"
    t.gate.set_kill_switch(False, actor="sam")
    res = note(t.e, "b")
    assert res["outcome"] == "rejected" and "kill_switch_generation_changed" in res["reason"]
    assert t.e.ctl.owner_decisions(JOB) == []


def test_the_api_exposes_the_dev_trial_read_only(world, tmp_path, monkeypatch):
    t = Trial(world, tmp_path)
    fd.run_trial(t.inst, t.gate, t.e.ctl, JOB, replay_driver(t, monkeypatch), clock=t.e.clock)
    def counts():
        return [t.e.store._conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]
                for n in ("fleet_rollout_evidence", "events", "fleet_events")]
    before = counts()
    st = StewardAPI(t.e.store).get_dev_trial_status(JOB, build_id=t.inst.build["id"], build_digest=t.inst.build["digest"])
    assert counts() == before
    assert st["preflight"]["max_actions"] == 1 and st["live_rollback"]["checkout_head"] == world.commit
    assert st["run"]["candidate_sha"] and st["receipt"] is None and st["owner_approval_recorded"] is False
    assert st["promotion"]["eligible"] is False and st["promotion"]["performs_promotion"] is False
    assert StewardAPI(t.e.store).get_dev_trial_status(JOB, build_id="x", build_digest="y")["preflight"] is None
