"""`steward fleet qualify <runtime>`: run a runtime's capability probes and record ONLY what was observed.

Real Store + temp Git; the app-server is the scripted FakeServer (never spawns anything)."""

from __future__ import annotations

import json
import subprocess

import pytest
from test_fleet_adapter_deadline import FakeServer

from steward import fleet_qualify as fq
from steward import fleet_registry as fr
from steward.fleet_store import FleetStore
from steward.store import Store
from steward.worker import PROBE_LONG_BRIEF, CodexAppServerAdapter, codex_capability_probes


def fake_codex_suite(servers=None, **server_kw):
    """A codex suite whose adapters talk to FakeServers (recorded in `servers`)."""
    servers = servers if servers is not None else []

    def make_factory(model):
        def make():
            srv = FakeServer(**server_kw)
            servers.append(srv)
            return CodexAppServerAdapter(srv, model=model or "gpt-x")
        return make

    return fq.Suite(runtime="codex", adapter_version="1", make_factory=make_factory,
                    probes=codex_capability_probes)


ROSTER = {"schema_version": 1, "revision": 1, "entries": [
    {"id": "codex-worker", "provider": "openai", "runtime": "codex", "model": "gpt-x", "roles": ["worker"],
     "capabilities": ["edit", "shell"], "strengths": [], "enabled": True, "label": "Codex worker"}]}


def write_roster(cfg):
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "roster.toml").write_text(
        'schema_version = 1\nrevision = 1\n\n[[entries]]\nid = "codex-worker"\nprovider = "openai"\n'
        'runtime = "codex"\nmodel = "gpt-x"\nroles = ["worker"]\ncapabilities = ["edit", "shell"]\n'
        'strengths = []\nenabled = true\nlabel = "Codex worker"\n')


def test_qualify_records_every_observed_capability_and_qualifies_the_roster_runtime(steward_home):
    store = Store(steward_home)
    res = fq.qualify(store, "codex", roster=fr.validate_roster(ROSTER), wait_s=0, suites={"codex": fake_codex_suite()})
    assert res["results"] == {c: True for c in fr.RUNTIME_CAPABILITIES}
    assert res["qualified"] is True
    fs = FleetStore(store)
    assert fs.qualified_capabilities("codex") == set(fr.RUNTIME_CAPABILITIES)
    q = fs.qualification("codex")
    assert q["qualified"] and q["available"] and q["capabilities"] == ["edit", "shell"]
    assert fs.dispatchable(fr.validate_roster(ROSTER).entries[0])


def test_a_capability_that_is_not_demonstrated_is_recorded_not_qualified(steward_home):
    store = Store(steward_home)
    res = fq.qualify(store, "codex", roster=fr.validate_roster(ROSTER),
                     wait_s=0, suites={"codex": fake_codex_suite(bad_steer=True)})
    assert res["results"]["steer_ack"] is False
    obs = FleetStore(store).runtime_capabilities("codex")
    assert obs["steer_ack"]["qualified"] is False and obs["steer_ack"]["evidence"]
    assert res["qualified"] is True  # start does not need steering


def test_a_failed_dispatch_capability_leaves_the_runtime_unqualified(steward_home):
    store = Store(steward_home)
    res = fq.qualify(store, "codex", roster=fr.validate_roster(ROSTER),
                     wait_s=0, suites={"codex": fake_codex_suite(fail={"thread/start"})})
    assert res["results"]["launch"] is False and res["qualified"] is False
    fs = FleetStore(store)
    assert fs.qualification("codex")["qualified"] is False
    assert not fs.dispatchable(fr.validate_roster(ROSTER).entries[0])


def test_every_adapter_a_probe_creates_is_stopped_before_the_next_probe_runs(steward_home):
    servers = []
    suite = fake_codex_suite(servers)
    inner = suite.probes

    def probes(make, worktree, **kw):
        real = inner(make, worktree, **kw)

        def watch(name, fn):
            def run():
                assert all(not s.alive_ for s in servers), f"{name}: an earlier probe's server is still alive"
                return fn()
            return run
        return {k: watch(k, v) for k, v in real.items()}

    suite = fq.Suite(runtime="codex", adapter_version="1", make_factory=suite.make_factory, probes=probes)
    res = fq.qualify(Store(steward_home), "codex", wait_s=0, suites={"codex": suite})
    assert all(res["results"].values())
    assert servers and all(not s.alive_ for s in servers)


def test_only_runs_the_named_capabilities(steward_home):
    store = Store(steward_home)
    res = fq.qualify(store, "codex", only=["launch", "poll"], wait_s=0, suites={"codex": fake_codex_suite()})
    assert set(res["results"]) == {"launch", "poll"}
    assert set(FleetStore(store).runtime_capabilities("codex")) == {"launch", "poll"}
    with pytest.raises(fq.QualifyError):
        fq.qualify(store, "codex", only=["telepathy"], wait_s=0, suites={"codex": fake_codex_suite()})


def test_unknown_runtime_is_refused_and_records_nothing(steward_home):
    store = Store(steward_home)
    with pytest.raises(fq.QualifyError):
        fq.qualify(store, "nope", wait_s=0, suites={"codex": fake_codex_suite()})
    assert FleetStore(store).runtime_capabilities("nope") == {}


def test_multi_model_runtime_records_only_capabilities_shared_by_every_rostered_model(steward_home):
    store = Store(steward_home)
    data = {"schema_version": 1, "revision": 1, "entries": [
        {"id": "sonnet", "provider": "anthropic", "runtime": "claude-fabric", "model": "sonnet",
         "roles": ["worker"], "capabilities": ["edit"], "strengths": [], "enabled": True, "label": "Sonnet"},
        {"id": "opus", "provider": "anthropic", "runtime": "claude-fabric", "model": "opus",
         "roles": ["worker"], "capabilities": ["edit"], "strengths": [], "enabled": True, "label": "Opus"},
    ]}
    roster = fr.validate_roster(data)
    seen = []

    def probes(make, worktree, **kw):
        model = make.model
        seen.append(model)
        return {cap: (lambda cap=cap, model=model: (not (model == "opus" and cap == "usage"),
                                                       f"{model}:{cap}"))
                for cap in fr.RUNTIME_CAPABILITIES}

    def factory(model):
        return type("Factory", (), {"__call__": lambda self: object(), "model": model})()

    suite = fq.Suite(runtime="claude-fabric", adapter_version="2", make_factory=factory, probes=probes,
                     qualify_each_rostered_model=True)
    res = fq.qualify(store, "claude-fabric", roster=roster, wait_s=0, suites={"claude-fabric": suite})
    assert seen == ["opus", "sonnet"]
    assert res["model_results"]["opus"]["usage"] is False
    assert res["model_results"]["sonnet"]["usage"] is True
    assert res["results"]["usage"] is False
    assert res["qualified"] is True
    obs = FleetStore(store).runtime_capabilities("claude-fabric")
    assert obs["usage"]["qualified"] is False
    assert "opus" in obs["usage"]["evidence"] and "sonnet" in obs["usage"]["evidence"]
    assert FleetStore(store).qualification("claude-fabric")["qualified"] is True


def test_multi_model_suite_refuses_one_model_only(steward_home):
    store = Store(steward_home)
    data = {"schema_version": 1, "revision": 1, "entries": [
        {"id": "sonnet", "provider": "anthropic", "runtime": "claude-fabric", "model": "sonnet",
         "roles": ["worker"], "capabilities": ["edit"], "strengths": [], "enabled": True, "label": "Sonnet"},
        {"id": "opus", "provider": "anthropic", "runtime": "claude-fabric", "model": "opus",
         "roles": ["worker"], "capabilities": ["edit"], "strengths": [], "enabled": True, "label": "Opus"},
    ]}
    suite = fq.Suite(runtime="claude-fabric", adapter_version="2", make_factory=lambda m: lambda: None,
                     probes=lambda *a, **k: {"launch": lambda: (True, "ok")},
                     qualify_each_rostered_model=True)
    with pytest.raises(fq.QualifyError, match="every rostered model"):
        fq.qualify(store, "claude-fabric", model="sonnet", roster=fr.validate_roster(data), wait_s=0,
                   suites={"claude-fabric": suite})
    assert FleetStore(store).runtime_capabilities("claude-fabric") == {}


def test_probes_run_in_a_scratch_git_repo_under_steward_home(steward_home):
    seen = []

    def probes(make, worktree, **kw):
        seen.append(worktree)
        return {"launch": lambda: (True, "ok")}

    suite = fq.Suite(runtime="codex", adapter_version="1", make_factory=lambda m: None, probes=probes)
    fq.qualify(Store(steward_home), "codex", wait_s=0, suites={"codex": suite})
    wt = seen[0]
    assert steward_home in wt.parents
    assert subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True).returncode == 0


def test_codex_is_a_registered_suite_built_on_the_real_adapter_and_probes():
    suite = fq.SUITES["codex"]
    assert suite.runtime == "codex" and suite.adapter_version == CodexAppServerAdapter.adapter_version
    assert suite.probes is codex_capability_probes


def test_cli_qualify_prints_each_capability_and_roster_shows_qualified_caps(steward_home, tmp_path, monkeypatch,
                                                                              capsys):
    from steward import cli
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfg))
    write_roster(cfg)
    monkeypatch.setitem(fq.SUITES, "codex", fake_codex_suite(bad_steer=True))
    assert cli.main(["fleet", "qualify", "codex", "--timeout", "0.5"]) == 0
    out = capsys.readouterr().out
    assert "launch  qualified" in out and "steer_ack  NOT qualified" in out
    assert cli.main(["fleet", "roster"]) == 0
    line = capsys.readouterr().out
    assert "dispatchable" in line and "present, unqualified" not in line
    assert "caps=cancel,identity,launch" in line and "steer_ack" not in line.split("caps=")[1].split()[0]
    assert cli.main(["fleet", "roster", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert "steer_ack" not in row["runtime_capabilities"] and "launch" in row["runtime_capabilities"]


def test_cli_qualify_exit_codes(steward_home, tmp_path, monkeypatch, capsys):
    from steward import cli
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "cfg"))
    assert cli.main(["fleet", "qualify", "nope"]) == 1
    monkeypatch.setitem(fq.SUITES, "codex", fake_codex_suite(fail={"thread/start"}))
    assert cli.main(["fleet", "qualify", "codex", "--json", "--timeout", "0.5"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["qualified"] is False and out["results"]["launch"] is False


# ------------------------------------------------- defects the first LIVE `fleet qualify codex` run exposed (2026-10-04)

class StrictServer(FakeServer):
    """Models two behaviours of the real `codex app-server` 0.157 the plain fake hides:
    - a fresh process refuses every request until the `initialize` handshake;
    - `thread/resume` is refused while another live process is still the thread's writer;
    - an interrupt that lands before the turn is actually running is accepted ({}) but has no effect."""

    writers: dict = {}

    def __init__(self, *, drop_first_interrupts=1, **kw):
        super().__init__(**kw)
        self.initialized = False
        self.drop = drop_first_interrupts

    def request(self, method, params, timeout=30.0):
        from steward.worker import AppServerError
        if method == "initialize":
            self.initialized = True
        elif not self.initialized:
            raise AppServerError(f"{method}: not initialized")
        if method == "thread/resume":
            w = StrictServer.writers.get(params["threadId"])
            if w is not None and w is not self and w.alive_:
                raise AppServerError(f"thread {params['threadId']} already has an active writer")
        if method == "turn/interrupt" and self.drop > 0:
            self.drop -= 1
            self.requests.append((method, params))
            return {}
        if method == "turn/start" and params["input"][0]["text"] == PROBE_LONG_BRIEF:
            self.auto, was = False, self.auto  # a long turn: still running until interrupted
            try:
                return super().request(method, params, timeout)
            finally:
                self.auto = was
        res = super().request(method, params, timeout)
        if method in ("thread/start", "thread/resume"):
            StrictServer.writers[(res.get("thread") or {}).get("id")] = self
        return res


def strict_suite(**kw):
    def make_factory(model):
        return lambda: CodexAppServerAdapter(StrictServer(**kw), model=model or "gpt-x")
    return fq.Suite(runtime="codex", adapter_version="1", make_factory=make_factory, probes=codex_capability_probes)


@pytest.fixture(autouse=False)
def strict_writers():
    StrictServer.writers = {}
    yield
    StrictServer.writers = {}


def test_attach_on_a_fresh_process_performs_the_initialize_handshake(strict_writers, tmp_path):
    a = CodexAppServerAdapter(StrictServer(), model="gpt-x")
    ref = a.start("probe", tmp_path)
    a.stop()
    b = CodexAppServerAdapter(StrictServer(), model="gpt-x")
    assert b.attach(ref) is True
    assert [m for m, _ in b.transport.requests][:2] == ["initialize", "thread/resume"]


def test_an_interrupt_that_races_turn_start_is_resent_until_the_turn_ends(strict_writers, tmp_path):
    srv = StrictServer(drop_first_interrupts=1)
    srv.auto = False  # the turn stays running until interrupted
    a = CodexAppServerAdapter(srv, model="gpt-x")
    a.start("long job", tmp_path)
    a.interrupt()  # dropped by the server: the turn was not running yet
    frames = [e for _ in range(5) for e in a.observe().events]
    assert any(e.type == "error" and e.body.get("status") == "interrupted" for e in frames)
    assert [m for m, _ in srv.requests].count("turn/interrupt") == 2
    a.observe()
    assert [m for m, _ in srv.requests].count("turn/interrupt") == 2  # nothing re-sent once the turn ended


def test_codex_cancel_and_reattach_qualify_against_the_strict_server(strict_writers, steward_home):
    res = fq.qualify(Store(steward_home), "codex", wait_s=0, suites={"codex": strict_suite()})
    assert res["results"]["reattach"] is True, res["evidence"]["reattach"]
    assert res["results"]["cancel"] is True, res["evidence"]["cancel"]


# ------------------------------------------------------------------------------- CLI-version staleness (FLEET-27, D-02)

def versioned_suite(version="codex-cli 0.157.0", **kw):
    s = fake_codex_suite(**kw)
    return fq.Suite(runtime="codex", adapter_version="1", make_factory=s.make_factory, probes=s.probes,
                    version=lambda: version)


def test_qualify_records_the_cli_version_on_every_observation(steward_home):
    store = Store(steward_home)
    res = fq.qualify(store, "codex", roster=fr.validate_roster(ROSTER), wait_s=0,
                     suites={"codex": versioned_suite()})
    assert res["cli_version"] == "codex-cli 0.157.0"
    fs = FleetStore(store, version_probe=lambda rt: "codex-cli 0.157.0")
    assert fs.qualified_version("codex") == "codex-cli 0.157.0"
    assert fs.qualification("codex")["cli_version"] == "codex-cli 0.157.0"
    assert {o["cli_version"] for o in fs.runtime_capabilities("codex").values()} == {"codex-cli 0.157.0"}


def test_a_changed_cli_version_makes_the_runtime_undispatchable_until_requalified(steward_home):
    store = Store(steward_home)
    entry = fr.validate_roster(ROSTER).entries[0]
    fq.qualify(store, "codex", roster=fr.validate_roster(ROSTER), wait_s=0, suites={"codex": versioned_suite()})
    same = FleetStore(store, version_probe=lambda rt: "codex-cli 0.157.0")
    assert same.dispatchable(entry) and same.runtime_can("codex", "start") == (True, [])
    assert same.stale("codex") is None
    moved = FleetStore(store, version_probe=lambda rt: "codex-cli 0.158.0")
    assert not moved.dispatchable(entry)
    assert moved.runtime_can("codex", "start") == (False, ["stale_version"])
    assert "0.157.0" in moved.stale("codex") and "0.158.0" in moved.stale("codex")
    gone = FleetStore(store, version_probe=lambda rt: None)  # the CLI is no longer there at all
    assert not gone.dispatchable(entry) and gone.stale("codex")
    fq.qualify(store, "codex", roster=fr.validate_roster(ROSTER), wait_s=0,
               suites={"codex": versioned_suite("codex-cli 0.158.0")})
    assert moved.dispatchable(entry) and moved.stale("codex") is None


def test_a_qualification_without_a_recorded_version_is_not_version_governed(steward_home):
    store = Store(steward_home)
    fq.qualify(store, "codex", roster=fr.validate_roster(ROSTER), wait_s=0, suites={"codex": fake_codex_suite()})
    fs = FleetStore(store, version_probe=lambda rt: (_ for _ in ()).throw(AssertionError("must not probe")))
    assert fs.dispatchable(fr.validate_roster(ROSTER).entries[0]) and fs.stale("codex") is None


def test_an_unreadable_cli_version_refuses_the_qualify_and_records_nothing(steward_home):
    store = Store(steward_home)
    with pytest.raises(fq.QualifyError, match="version"):
        fq.qualify(store, "codex", wait_s=0, suites={"codex": versioned_suite(version=None)})
    assert FleetStore(store).runtime_capabilities("codex") == {}


def test_roster_shows_stale_requalify(steward_home, tmp_path, monkeypatch, capsys):
    from steward import cli
    from steward import fleet_versions
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfg))
    write_roster(cfg)
    monkeypatch.setitem(fq.SUITES, "codex", versioned_suite())
    assert cli.main(["fleet", "qualify", "codex", "--timeout", "0.5"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(fleet_versions, "current_version", lambda rt: "codex-cli 9.9.9")
    assert cli.main(["fleet", "roster"]) == 0
    out = capsys.readouterr().out
    assert "stale (re-qualify)" in out and "dispatchable" not in out.replace("not dispatchable", "")
    assert cli.main(["fleet", "roster", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["stale"] and row["qualified_version"] == "codex-cli 0.157.0" and row["dispatchable"] is False


def test_real_suites_read_their_cli_version_without_a_tty(monkeypatch):
    from steward import fleet_versions
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))

        class R:
            returncode, stdout = 0, "Muse Code 1.4.2 (1.4.2-R4684.1)\n"
        return R()
    monkeypatch.setenv("META_API_KEY", "sk-never")
    assert fleet_versions.read_version("muse", run=fake_run) == "Muse Code 1.4.2 (1.4.2-R4684.1)"
    argv, kw = calls[0]
    assert argv == ["muse", "--version"] and kw["stdin"] is not None and "META_API_KEY" not in kw["env"]
    assert fleet_versions.VERSION_ARGV == {"codex": ("codex", "--version"), "muse": ("muse", "--version"),
                                           "agy": ("agy", "--version"), "pi": ("pi", "--version"),
                                           "claude-fabric": ("claude", "--version")}
    assert fleet_versions.read_version("claude-fabric", run=fake_run) == "Muse Code 1.4.2 (1.4.2-R4684.1)"
    assert all(fq.SUITES[r].version is not None for r in ("codex", "muse", "agy", "pi"))
