"""Phase 31 (FLEET-28/29): per-lane runtime assignment with allowlist guardrails.

Eligible = roster entry enabled, role worker, runtime qualified for `start`, CLI version current. Auto-assign spreads
lanes across eligible workers (distinct runtimes first, then strengths, then cost); `--runtimes` pins; a pinned
runtime with no eligible roster entry, an off-roster model, or a stale CLI refuses BEFORE anything is written; skipped
workers are reported; nothing ever falls back to Claude. Real Store + temp Git; nothing spawns."""

from __future__ import annotations

import pytest
from test_fleet_wiring import PLAN, contracted_job, fleet_rows, launch, make_repo, tabs  # noqa: F401 (fixture)

from steward import fleet_launch, fleet_versions
from steward import fleet_registry as fr
from steward import fleet_runtime as rt
from steward.fleet_store import FleetStore
from steward.store import Store

ROSTER = """schema_version = 1
revision = 7

[[entries]]
id = "claude-sonnet-worker"
provider = "anthropic"
runtime = "claude-fabric"
model = "sonnet"
roles = ["worker"]
capabilities = ["edit", "tests"]
enabled = true
label = "Claude worker (unqualified runtime)"

[[entries]]
id = "codex-worker"
provider = "openai"
runtime = "codex"
model = "gpt-6-astra"
roles = ["worker"]
capabilities = ["edit", "tests"]
strengths = ["focused implementation"]
enabled = true
label = "Codex"

[[entries]]
id = "muse-worker"
provider = "meta"
runtime = "muse"
model = "muse-spark-1.3-contributor"
roles = ["worker"]
capabilities = ["edit", "tests"]
enabled = true
label = "Muse"

[[entries]]
id = "agy-worker"
provider = "google"
runtime = "agy"
model = "Gemini 3.8 Flash (High)"
roles = ["worker"]
capabilities = ["edit", "tests"]
enabled = true
label = "agy"

[[entries]]
id = "pi-deepseek-worker"
provider = "deepinfra"
runtime = "pi"
model = "deepseek-ai/DeepSeek-V4-Pro"
roles = ["worker"]
capabilities = ["edit", "tests"]
strengths = ["low cost"]
enabled = true
label = "DeepSeek"

[[entries]]
id = "astra-tech-lead"
provider = "openai"
runtime = "codex"
model = "gpt-6-astra"
roles = ["tech_lead"]
capabilities = ["plan", "review"]
enabled = true
label = "Astra"
priority = 1

[[entries]]
id = "fable-tech-lead"
provider = "anthropic"
runtime = "claude-fabric"
model = "claude-fable-5-1"
roles = ["tech_lead"]
capabilities = ["plan", "review"]
enabled = true
label = "Fable"
priority = 2
"""

VERSIONS = {"codex": "codex-cli 0.157.0", "muse": "Muse Code 1.4.2", "agy": "1.2.16", "pi": "0.87.1"}


@pytest.fixture
def world(steward_home, tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "roster.toml").write_text(ROSTER)
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfg))
    installed = dict(VERSIONS)
    monkeypatch.setattr(fleet_versions, "current_version", lambda r: installed.get(r))
    store = Store(steward_home)
    contracted_job(store, make_repo(tmp_path))
    for runtime in ("codex", "muse", "pi"):
        qualify(store, runtime)
    qualify(store, "agy", missing=("permission_relay",))   # live: headless agy auto-denies, cannot relay
    return store, installed, cfg / "roster.toml"


def qualify(store, runtime, *, missing=(), version=None):
    fs = FleetStore(store, version_probe=lambda r: None)
    v = version or VERSIONS[runtime]
    for cap in fr.RUNTIME_CAPABILITIES:
        fs.record_runtime_capability(runtime, cap, qualified=cap not in missing, adapter_version="1",
                                     evidence="probe", cli_version=v)
    ok = not fr.missing_capabilities(set(fr.RUNTIME_CAPABILITIES) - set(missing), "start")
    fs.record_qualification(runtime, "1", ["edit", "tests", "plan", "review"], qualified=ok, available=ok,
                            cli_version=v)


def nothing_written(store, job="J1"):
    assert not rt.is_fleet_job(store, job) and fleet_rows(store, job)["fleet_jobs"] == 0
    assert store.kv_get(job, fleet_launch.LAUNCH_KEY) is None and FleetStore(store).assignments(job) == []


# ------------------------------------------------------------------------------------------------- auto-assign

def test_auto_assign_spreads_lanes_across_distinct_eligible_runtimes_and_reports_skips(world):
    store, _, roster = world
    res = fleet_launch.prepare(store, "J1", max_lanes=2, roster_path=roster)
    lanes = res["assignments"]
    assert set(lanes) == {"lane-a", "lane-b"}
    runtimes = [lanes[ln]["runtime"] for ln in ("lane-a", "lane-b")]
    assert len(set(runtimes)) == 2 and set(runtimes) <= {"codex", "muse", "pi"}
    assert lanes["lane-a"] == {"roster_id": "pi-deepseek-worker", "runtime": "pi",
                               "model": "deepseek-ai/DeepSeek-V4-Pro", "rule": "auto_assign"}  # low cost first
    skipped = {s["id"]: s["reason"] for s in res["skipped"]}
    assert "permission_relay" in skipped["agy-worker"] and "fleet qualify agy" in skipped["agy-worker"]
    assert "claude-sonnet-worker" in skipped and "never qualified" in skipped["claude-sonnet-worker"]
    assert "fleet qualify claude-fabric" in skipped["claude-sonnet-worker"]
    rows = FleetStore(store).assignments("J1")
    assert {(r["lane_id"], r["roster_id"]) for r in rows} == {(ln, lanes[ln]["roster_id"]) for ln in lanes}
    assert all(r["policy_rule"] == "auto_assign" and r["actor"] == "steward_launch" for r in rows)


def test_auto_assign_is_deterministic_for_a_given_roster_and_graph(world, tmp_path, steward_home):
    store, _, roster = world
    first = fleet_launch.plan_assignments(FleetStore(store), fr.load_roster(roster), ["a", "b", "c", "d"],
                                          objectives={})
    again = fleet_launch.plan_assignments(FleetStore(store), fr.load_roster(roster), ["a", "b", "c", "d"],
                                          objectives={})
    assert first == again
    assert [first.lanes[t].runtime for t in ("a", "b", "c")] == ["pi", "codex", "muse"]  # distinct runtimes first
    assert first.lanes["d"].runtime == "pi"   # then round again


def test_strengths_matching_the_task_outrank_cost(world):
    store, _, roster = world
    plan = fleet_launch.plan_assignments(FleetStore(store), fr.load_roster(roster), ["a"],
                                         objectives={"a": "focused implementation of the parser"})
    assert plan.lanes["a"].runtime == "codex"


# ------------------------------------------------------------------------------------------------- pinning

def test_pinned_runtimes_take_the_lanes(world):
    store, _, roster = world
    res = fleet_launch.prepare(store, "J1", max_lanes=2, roster_path=roster, runtimes=["codex", "muse"])
    assert {v["runtime"] for v in res["assignments"].values()} == {"codex", "muse"}
    assert all(v["rule"] == "pinned" for v in res["assignments"].values())
    assert FleetStore(store).get_snapshot("J1")["pins"] == {ln: v["roster_id"] for ln, v in res["assignments"].items()}


def test_a_pinned_undispatchable_runtime_refuses_with_the_fix_and_writes_nothing(world):
    store, _, roster = world
    with pytest.raises(fleet_launch.FleetRefused) as exc:
        fleet_launch.prepare(store, "J1", max_lanes=2, roster_path=roster, runtimes=["codex", "agy"])
    assert exc.value.reason == "runtime_not_dispatchable"
    assert "agy" in exc.value.detail and "permission_relay" in exc.value.detail
    assert "steward fleet qualify agy" in exc.value.detail
    nothing_written(store)


def test_a_pinned_runtime_absent_from_the_roster_is_refused(world):
    store, _, roster = world
    with pytest.raises(fleet_launch.FleetRefused) as exc:
        fleet_launch.prepare(store, "J1", roster_path=roster, runtimes=["hermes"])
    assert exc.value.reason == "not_in_roster"
    nothing_written(store)


def test_off_roster_model_is_refused_although_its_runtime_is_installed_and_qualified(world):
    store, _, roster = world
    assert FleetStore(store).runtime_can("codex", "start") == (True, [])   # codex itself is qualified
    with pytest.raises(fleet_launch.FleetRefused) as exc:
        fleet_launch.prepare(store, "J1", roster_path=roster, runtimes=["codex:gpt-5.5"])
    assert exc.value.reason == "off_roster_model" and "gpt-5.5" in exc.value.detail
    nothing_written(store)
    res = fleet_launch.prepare(store, "J1", roster_path=roster, runtimes=["codex:gpt-6-astra"])
    assert {v["model"] for v in res["assignments"].values()} == {"gpt-6-astra"}


def test_a_stale_cli_version_is_skipped_by_auto_assign_and_refused_when_pinned(world):
    store, installed, roster = world
    installed["codex"] = "codex-cli 0.158.0"
    with pytest.raises(fleet_launch.FleetRefused) as exc:
        fleet_launch.prepare(store, "J1", roster_path=roster, runtimes=["codex"])
    assert exc.value.reason == "runtime_not_dispatchable" and "stale_version" in exc.value.detail
    assert "steward fleet qualify codex" in exc.value.detail
    nothing_written(store)
    res = fleet_launch.prepare(store, "J1", max_lanes=2, roster_path=roster)
    assert "codex" not in {v["runtime"] for v in res["assignments"].values()}
    assert "stale_version" in {s["id"]: s["reason"] for s in res["skipped"]}["codex-worker"]


def test_no_silent_claude_fallback(world):
    """Every structured worker undispatchable: the launch is refused - the unqualified Claude worker is not used."""
    store, installed, roster = world
    for r in ("codex", "muse", "pi"):
        installed[r] = "moved"
    with pytest.raises(fleet_launch.FleetRefused) as exc:
        fleet_launch.prepare(store, "J1", max_lanes=2, roster_path=roster)
    assert exc.value.reason == "no_dispatchable_worker" and "claude-sonnet-worker" in exc.value.detail
    nothing_written(store)
    with pytest.raises(fleet_launch.FleetRefused):
        fleet_launch.prepare(store, "J1", max_lanes=2, roster_path=roster, runtimes=["muse"])
    nothing_written(store)


def test_the_roster_is_the_allowlist_no_catalog_and_no_local_models(world):
    """Only roster entries are ever assigned; pi's only rostered model is DeepSeek V4 Pro."""
    store, _, roster = world
    r = fr.load_roster(roster)
    pi = [e for e in r.entries if e.runtime == "pi"]
    assert [e.model for e in pi] == ["deepseek-ai/DeepSeek-V4-Pro"]
    assert not [e for e in r.entries if any(x in e.model.lower() for x in ("ollama", "glm", "local"))]
    with pytest.raises(fleet_launch.FleetRefused) as exc:
        fleet_launch.prepare(store, "J1", roster_path=roster, runtimes=["pi:glm-4.6"])
    assert exc.value.reason == "off_roster_model"


def test_tech_leads_are_ordered_by_priority(world):
    store, _, roster = world
    r = fr.load_roster(roster)
    assert [e.id for e in fleet_launch.tech_leads(r)] == ["astra-tech-lead", "fable-tech-lead"]
    res = fleet_launch.prepare(store, "J1", max_lanes=2, roster_path=roster)
    assert res["tech_leads"] == ["astra-tech-lead", "fable-tech-lead"] and res["tech_lead"] == "astra-tech-lead"


def test_priority_is_optional_and_does_not_change_existing_roster_hashes():
    base = {"schema_version": 1, "revision": 1, "entries": [
        {"id": "w", "provider": "p", "runtime": "r", "model": "m", "roles": ["worker"], "capabilities": [],
         "strengths": [], "enabled": True, "label": "l"}]}
    before = fr.validate_roster(base).hash
    assert "priority" not in fr.validate_roster(base).canonical
    assert fr.validate_roster(base).hash == before
    with pytest.raises(fr.RosterError):
        fr.validate_roster({**base, "entries": [{**base["entries"][0], "priority": "first"}]})


# ------------------------------------------------------------------------------------------------- the CLI

def test_cli_launch_pins_runtimes_and_prints_the_lane_runtimes(world, tabs, capsys):  # noqa: F811
    store, _, _ = world
    assert launch("J1", "--fleet", "--max-lanes", "2", "--runtimes", "codex,muse") == 0
    choice = store.kv_get("J1", fleet_launch.LAUNCH_KEY)
    assert {v["runtime"] for v in choice["assignments"].values()} == {"codex", "muse"}


def test_cli_runtimes_without_fleet_launches_nothing(world, tabs, capsys):  # noqa: F811
    store, _, _ = world
    assert launch("J1", "--runtimes", "codex") != 0
    assert not tabs.calls
    nothing_written(store)


def test_cli_refused_pin_names_the_fix(world, tabs, capsys):  # noqa: F811
    store, _, _ = world
    assert launch("J1", "--fleet", "--runtimes", "agy") != 0
    out = capsys.readouterr()
    assert "steward fleet qualify agy" in out.out + out.err and not tabs.calls
    nothing_written(store)


def test_a_structured_only_fleet_does_not_need_the_fabric_worker(world, tabs, capsys):  # noqa: F811
    store, _, _ = world
    assert launch("J1", "--fleet", "--worker", "herdr-tui", "--runtimes", "codex,muse") == 0
    assert rt.is_fleet_job(store, "J1")


def test_cli_run_of_a_structured_fleet_needs_no_herdr(world, monkeypatch, capsys):
    from steward import cli
    store, _, roster = world
    fleet_launch.prepare(store, "J1", max_lanes=2, roster_path=roster, runtimes=["codex", "muse"])
    seen = {}
    monkeypatch.setattr(cli, "_cmd_run_fleet", lambda st, args, job: seen.setdefault("job", job.job_id) and 0)
    for v in ("HERDR_SOCKET_PATH", "HERDR_PANE_ID"):
        monkeypatch.delenv(v, raising=False)
    assert cli.main(["run", "J1", "--worker", "fabric", "--once"]) == 0
    assert seen["job"] == "J1"


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_max_lanes_accepts_one_to_four(world, n):
    store, _, roster = world
    assert fleet_launch.prepare(store, "J1", max_lanes=n, roster_path=roster)["max_lanes"] == n


def test_max_lanes_five_is_refused_and_the_default_stays_one(world):
    store, _, roster = world
    with pytest.raises(fleet_launch.FleetRefused, match="1..4"):
        fleet_launch.prepare(store, "J1", max_lanes=5, roster_path=roster)
    nothing_written(store)
    assert fleet_launch.prepare(store, "J1", roster_path=roster)["max_lanes"] == 1
