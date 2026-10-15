import os
import sys
import time
from pathlib import Path

import pytest

from herdr_session import HerdrTestSession, herdr_available

# Throwaway test stores need no durability: without this every commit fsyncs, and parallel suites become disk-bound
# (see steward.store._apply_sync). Set at import so subprocesses the tests spawn inherit it too.
os.environ.setdefault("STEWARD_SQLITE_SYNC", "off")
# Zero-touch Task 17: read-only git calls must not take optional locks (a `git status` rewrites/locks the index).
os.environ.setdefault("GIT_OPTIONAL_LOCKS", "0")

# The checkout the suite runs from. STEWARD_GUARD_MAIN lets the guard's own test aim it at a throwaway repo.
_MAIN = Path(os.environ.get("STEWARD_GUARD_MAIN") or Path(__file__).resolve().parent.parent)


def _git_dir() -> Path:
    """MAIN/.git, or the gitdir a linked worktree's `.git` file points at."""
    dot = _MAIN / ".git"
    if dot.is_file():
        try:
            first = dot.read_text().strip()
            if first.startswith("gitdir:"):
                return Path(first[len("gitdir:"):].strip())
        except OSError:
            pass
    return dot


def _index_sig():
    gd = _git_dir()
    try:
        st = (gd / "index").stat()
        sig = (st.st_ino, st.st_mtime_ns)
    except OSError:
        sig = None
    return sig, (gd / "index.lock").exists()


@pytest.fixture
def steward_home(tmp_path, monkeypatch):
    """Point STEWARD_HOME at an isolated tmp directory for the duration of a test."""
    home = tmp_path / "steward_home"
    home.mkdir()
    monkeypatch.setenv("STEWARD_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _no_real_jev_calls(monkeypatch):
    """Tests never reach TypeSafe: drop any real key/switch inherited from the shell (Sam's
    profile exports TYPESAFE_API_KEY) and make the real network transport fail fast."""
    for name in ("TYPESAFE_API_KEY", "STEWARD_JEV", "TYPESAFE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    # The approval resolver spawns claude/codex: off unless a test builds an engine with fakes.
    monkeypatch.setenv("STEWARD_RESOLVER", "0")
    from steward import jev_observer

    def _blocked(self, *a, **k):
        raise AssertionError("tests must not call the real Jev API; inject a fake transport")

    monkeypatch.setattr(jev_observer.UrllibTransport, "post", _blocked)


@pytest.fixture(autouse=True)
def _no_runtime_drift_watch(monkeypatch):
    """The runner's runtime-drift check reads real CLI versions and may requalify: off unless a test builds its own."""
    monkeypatch.setenv("STEWARD_RUNTIME_DRIFT", "0")


@pytest.fixture(autouse=True)
def _no_live_autodeploy(monkeypatch):
    """A test that merges into "Steward itself" must never restart the live Steward: autodeploy is off unless a
    test turns it on AND injects fakes."""
    monkeypatch.setenv("STEWARD_AUTODEPLOY", "0")


@pytest.fixture(autouse=True)
def _synchronous_baseline(monkeypatch):
    """`steward new` would launch the background baseline as a detached process (latency #5): tests keep the
    synchronous baseline unless a test turns it on and injects a fake spawn."""
    monkeypatch.setenv("STEWARD_BG_BASELINE", "0")


@pytest.fixture(autouse=True)
def _isolated_steward_home(tmp_path_factory, monkeypatch):
    """No test may ever open the live store: a bare `Store()` resolves
    $STEWARD_HOME, so every test gets a throwaway one by default (the
    `steward_home` fixture still overrides it with its own path)."""
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path_factory.mktemp("steward_home_default")))


@pytest.fixture(autouse=True)
def _no_ambient_herdr_or_session(monkeypatch):
    """The suite runs inside a live Herdr pane / Claude session. Its env must never
    leak into a test: `steward new` would record the REAL pane+session as creator and
    a later accept/stop would push text into the operator's live pane. Also a hard
    guard: no test may ever send text/keys to a pane through a real subprocess."""
    for var in ("HERDR_PANE_ID", "HERDR_SOCKET_PATH", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID",
                "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(var, raising=False)
    from steward import runner_exec

    real_run = runner_exec.SubprocessRunner.run

    def guarded(self, argv, cwd, **kw):
        if list(argv[:3]) in (["herdr", "pane", "send-text"], ["herdr", "pane", "send-keys"], ["herdr", "pane", "run"]):
            raise AssertionError(f"test tried to drive a real Herdr pane: {argv[:4]}")
        if list(argv[:2]) == ["tg", "send"]:  # v2.6: no test or proof ever sends a real Telegram message
            raise AssertionError("test tried to send a real Telegram message via `tg send`")
        return real_run(self, argv, cwd, **kw)

    monkeypatch.setattr(runner_exec.SubprocessRunner, "run", guarded)


@pytest.fixture(autouse=True)
def _no_dash_health_probe(monkeypatch):
    """The status-pane renderer runs the 30s health check (Herdr socket, systemd, control ping) unless told
    not to; tests never probe the machine they run on."""
    monkeypatch.setenv("STEWARD_HEALTH", "0")


@pytest.fixture(autouse=True)
def _isolated_claude_dir(tmp_path_factory, monkeypatch):
    """Worktrees link the user's skills/agents; tests link from an empty throwaway dir."""
    monkeypatch.setenv("STEWARD_CLAUDE_DIR", str(tmp_path_factory.mktemp("claude_dir")))


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path_factory, monkeypatch):
    """Tests never read Sam's live ~/.config/steward (e.g. steward.toml's
    `acceptance = "human"` workbench setting)."""
    cfg = tmp_path_factory.mktemp("steward_config")
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("STEWARD_ACCEPTANCE", raising=False)


@pytest.fixture(autouse=True)
def _isolated_exceptions_registry(tmp_path, monkeypatch):
    """Every non-Herdr exception path calls `steward.exceptions.use()`
    (OPX-04), which creates a default registry file the first time none
    exists. Tests must never touch the real
    `~/.config/steward/exceptions.toml` (11-01-PLAN hard rule) — this
    redirects every test to a throwaway path via `$STEWARD_EXCEPTIONS`."""
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(tmp_path / "exceptions.toml"))


@pytest.fixture(autouse=True)
def _inprocess_control_relay(monkeypatch):
    """Every CLI write verb now relays through a control pane (OPX-01/02)
    instead of calling `StewardAPI` directly from the CLI process. The
    existing CLI test suite exercises `steward.cli.main(...)` end to end
    without a real Herdr control pane, so this default fixture gives every
    test an in-process `ControlPane` relay (the SAME store, the SAME
    handlers `steward control` would build) instead of a real Herdr socket
    — an injectable relay (11-01-PLAN's rule: "give tests an injectable
    relay ... rather than weakening assertions"), not a weakened assertion.
    tests/test_cli_relay.py restores the real `cli._relay` for the specific
    tests that exercise the real Herdr-based path."""
    import uuid

    from steward import cli
    from steward.control_pane import ControlPane

    def fake_relay(store, op, args, *, request_id=None, origin="cli", **_ignored):
        handlers = cli._build_control_handlers(store)
        pane = ControlPane(store, handlers, print_fn=lambda _line: None)
        cmd = {"request_id": request_id or str(uuid.uuid4()), "op": op, "args": args, "origin": origin}
        return pane.execute(cmd)

    monkeypatch.setattr(cli, "_relay", fake_relay)


@pytest.fixture
def herdr_test_session():
    """A named throwaway Herdr session (`steward-test-<pid>`), started fresh
    and always stopped + deleted on teardown. Skips cleanly when `herdr` is
    not on PATH — never touches the default/live session (FAB-04)."""
    if not herdr_available():
        pytest.skip("herdr is not installed; skipping Herdr fabric integration tests")
    session = HerdrTestSession()
    session.start()
    try:
        yield session
    finally:
        session.teardown()


@pytest.fixture(autouse=True)
def _isolated_research_inputs(tmp_path_factory, monkeypatch):
    """Tests never read the real ~/.claude.json or ~/.api-keys, and the
    process-wide granted-key mask never leaks between tests."""
    from steward import masking

    d = tmp_path_factory.mktemp("research_inputs")
    monkeypatch.setenv("STEWARD_CLAUDE_JSON", str(d / "claude.json"))
    monkeypatch.setenv("STEWARD_API_KEYS_FILE", str(d / "api-keys"))
    # Phase 19 (G11): the last source of the keys chain. Never the real ~/.api-keys.
    monkeypatch.setenv("STEWARD_LEGACY_KEYS_FILE", str(d / "legacy-keys"))
    # The keys chain reads the process environment first: ambient keys must not leak into a test.
    for ambient in ("SCRAPECREATORS_API_KEY", "FIRECRAWL_API_KEY", "TAVILY_API_KEY", "SERPAPI_KEY"):
        monkeypatch.delenv(ambient, raising=False)
    masking.clear()
    yield
    masking.clear()


@pytest.fixture(autouse=True)
def _owner_is_the_legacy_name(monkeypatch):
    """Phase 19 (G7): the owner's name is a setting. The whole suite runs under one pinned name so every
    existing assertion stays byte-for-byte unchanged; owner tests override with monkeypatch.setenv."""
    from steward import owner

    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    monkeypatch.delenv("STEWARD_OWNER_PRONOUNS", raising=False)
    monkeypatch.setenv("STEWARD_OWNER_LEGACY_NAMES", "Sam")  # the pinned name keeps its legacy pronouns and parsing
    owner.reset_cache()
    yield
    owner.reset_cache()


@pytest.fixture(autouse=True)
def _nested_runs_use_this_interpreter(monkeypatch):
    """Phase 19-10: several tests run a real nested `pytest` / `python3` (the baseline, verify and closeout checks)
    through a shell. Under a PATH that lacks the running interpreter's directory (a bare service environment, `env
    PATH=/usr/bin:/bin`) the nested `pytest` is not found (exit 127) and five tests failed for that reason alone.
    Prepending the directory of the interpreter that runs the suite is what an interactive shell with the venv already
    has: nested runs use this interpreter. It changes no assertion, fixture or verification config."""
    here = os.path.dirname(sys.executable)
    monkeypatch.setenv("PATH", here + os.pathsep + os.environ.get("PATH", ""))


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_repo: reads the real repository (git diff/log); exempt from the main-checkout index check")
    config.addinivalue_line(
        "markers",
        "legacy_ownership_off: run with STEWARD_OWNERSHIP=0 (continuous-ownership kill switch); used by tests that "
        "pin a legacy path the ownership layer supersedes (Plan 1, 2026-10-05)")


@pytest.fixture(autouse=True)
def _no_ambient_agent_identity(monkeypatch, request):
    """Workers and agent harnesses export identity variables (STEWARD_FORCED_ORIGIN=worker, CLAUDECODE=1, CODEX_*):
    none may leak into a test (Plan 1). A test marked legacy_ownership_off runs with the ownership layer off."""
    for var in ("STEWARD_FORCED_ORIGIN", "STEWARD_ACTOR", "STEWARD_OPERATOR", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_THREAD_ID", "STEWARD_OWNERSHIP"):
        monkeypatch.delenv(var, raising=False)
    if request.node.get_closest_marker("legacy_ownership_off") is not None:
        monkeypatch.setenv("STEWARD_OWNERSHIP", "0")


# --- reap leaked fake agents -------------------------------------------------------------------------------------
# Shim/fleet tests start real `steward.fake_agent` subprocesses; some outlive their test (orphaned to init), and 22
# were seen alive under one run. Every child inherits this marker, so a /proc scan finds them even after reparenting.
_RUN_MARK = f"steward-pytest-{os.getpid()}"
os.environ["STEWARD_TEST_RUN_MARK"] = _RUN_MARK


def _reap_fake_agents() -> int:
    import signal
    me, killed = os.getpid(), 0
    for ent in os.listdir("/proc"):
        if not ent.isdigit() or int(ent) == me:
            continue
        try:
            with open(f"/proc/{ent}/cmdline", "rb") as fh:
                if b"steward.fake_agent" not in fh.read():
                    continue
            with open(f"/proc/{ent}/environ", "rb") as fh:
                if _RUN_MARK.encode() not in fh.read():
                    continue
            os.kill(int(ent), signal.SIGKILL)
            killed += 1
        except OSError:
            continue
    return killed


@pytest.fixture(autouse=True, scope="module")
def _reap_fake_agents_per_module():
    yield
    _reap_fake_agents()


@pytest.fixture(autouse=True, scope="session")
def _reap_fake_agents_at_end():
    yield
    _reap_fake_agents()


@pytest.fixture(autouse=True)
def _calm_governor(monkeypatch, tmp_path_factory):
    """Governor readings never touch the real /proc, statvfs, steward.toml or usage.db: the default reader reports
    a calm machine, config comes from an empty dir, the budget DB does not exist. Governor tests inject their own."""
    from steward import governor

    class _Calm:
        def mem_available_gb(self): return 64.0
        def tmp_used_pct(self): return 1.0
        def disk_used_pct(self): return 1.0
        def load5(self): return 0.0

    monkeypatch.setattr(governor, "ProcReader", _Calm)
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path_factory.mktemp("steward_cfg_default")))
    if hasattr(governor, "USAGE_DB"):
        monkeypatch.setattr(governor, "USAGE_DB", tmp_path_factory.mktemp("usage_default") / "usage.db")


# u7 (stall fixes 2026-10-14): the main checkout's index.lock is also taken, every few seconds and for a few ms, by live
# processes outside the suite (the status line of sessions whose cwd is the main checkout runs `git status` without
# GIT_OPTIONAL_LOCKS=0, and its refresh rewrites the index). Neither /proc sampling (that git is gone in milliseconds)
# nor fanotify (unprivileged: pid 0) can name the creator, and PATH shims miss absolute-path git. The umask can: every
# process of this run (the test process, any child, any git however it is invoked) inherits it, and it is stamped into
# the mode of every file it creates, the lock included, and the index git renames into place. This run strips
# other-read; the box's live processes run with umask 002 (every quarantined stale lock in the main checkout is 0664).
# So, from ONE stat at teardown (no sampling race):
#   * a lock or a rewritten index without other-read was created by this run -> the test fails;
#   * one with other-read was provably created outside this run -> not the test's;
#   * an index rewritten in place (same inode: not how git writes) -> the test fails.
_RUN_UMASK = os.umask(0) | 0o004
os.umask(_RUN_UMASK)


def _made_by_this_run(st: os.stat_result) -> bool:
    """True unless the file's mode carries a permission bit this run's umask strips (then an outside process made it)."""
    return (st.st_mode & 0o777 & _RUN_UMASK) == 0


def _holders(path: Path) -> list[str]:
    """pids with `path` open (diagnostics only)."""
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    if os.readlink(f"/proc/{pid}/fd/{fd}") == str(path):
                        out.append(pid)
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return out


def _lock_verdict(lock_st: os.stat_result | None) -> str | None:
    """Why the teardown lock is this test's; None when it was provably created outside this run."""
    if lock_st is None or not _made_by_this_run(lock_st):
        return None
    lock = _git_dir() / "index.lock"
    held = _holders(lock) if os.path.isdir("/proc/self/fd") else []
    return (f"mode {oct(lock_st.st_mode & 0o777)}: created under this run's umask {oct(_RUN_UMASK)}; "
            f"{'held by pid ' + ','.join(held[:5]) if held else 'no live holder'}")


def _birth(path) -> tuple[int, int, int] | None:
    """(inode, birth s, birth ns) via glibc statx; None when unavailable. Outside refreshes recycle inode numbers (the
    lock renamed into place may reuse the inode the index had before), so only inode AND birth time name one file."""
    import ctypes

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        buf = ctypes.create_string_buffer(256)
        if libc.statx(-100, os.fsencode(str(path)), 0, 0x7FF | 0x800, buf) != 0:  # AT_FDCWD, BASIC_STATS|BTIME
            return None
    except (AttributeError, OSError):
        return None
    import struct

    mask, = struct.unpack_from("I", buf, 0)
    if not mask & 0x800:
        return None
    ino, = struct.unpack_from("Q", buf, 32)
    sec, nsec = struct.unpack_from("qI", buf, 80)
    return ino, sec, nsec


def _index_verdict(born_before, born_after, index_st: os.stat_result | None) -> str | None:
    """Why the index rewrite is this test's; None when an outside process renamed a new index into place."""
    if index_st is None:
        return "index removed"
    if born_before is None or born_after is None:
        return "cannot tell the index file's identity (no statx birth time)"
    if born_before == born_after:
        return "rewritten in place (same file): not an outside git's lock-and-rename"
    if _made_by_this_run(index_st):
        return f"new index mode {oct(index_st.st_mode & 0o777)}: created under this run's umask {oct(_RUN_UMASK)}"
    return None


@pytest.fixture(autouse=True)
def _main_checkout_guard(request):
    """Fail any test that rewrites the main checkout's git index or leaves an index.lock behind (zero-touch Task 17).
    real_repo tests are exempt from the index check but still fail on a lock left behind. u7: ownership is read from
    the files' modes (see above), so a live process outside the run never fails a test."""
    before, lock_before = _index_sig()
    born_before = _birth(_git_dir() / "index")
    yield
    gd = _git_dir()
    try:
        lock_st = os.stat(gd / "index.lock")  # one stat: what is there at teardown, and who made it
    except OSError:
        lock_st = None
    after, _ = _index_sig()
    real = request.node.get_closest_marker("real_repo") is not None
    if lock_st is not None and not lock_before:
        why = _lock_verdict(lock_st)
        if why is not None:
            pytest.fail(f"main checkout .git touched by {request.node.nodeid}: index.lock left behind ({why})")
    if not real and before != after:
        try:
            index_st = os.stat(gd / "index")
        except OSError:
            index_st = None
        why = _index_verdict(born_before, _birth(gd / "index"), index_st)
        if why is not None:
            pytest.fail(f"main checkout .git touched by {request.node.nodeid}: index rewritten ({why})")


_LIVE_ROOT_DEFAULT_MODULES = {"test_deployed", "test_health", "test_runner_heal", "test_watchdog_sandbox", "test_oauth_store"}


@pytest.fixture(autouse=True)
def _live_root_isolated(request, monkeypatch, tmp_path_factory):
    """stability.LIVE_ROOT defaults to the live checkout; tests never inspect it.

    Modules that assert the default itself (or build their own ``live`` dir) opt out.
    """
    if (request.node.get_closest_marker("real_repo") is None
            and request.node.module.__name__.split(".")[-1] not in _LIVE_ROOT_DEFAULT_MODULES):
        from steward import stability

        monkeypatch.setattr(stability, "LIVE_ROOT", tmp_path_factory.mktemp("liveroot"))
