"""Isolated side-by-side dev Steward instance for the first bounded tech-lead trial (Phase 29-03, FLEET-22).

The live installation stays the stable build. This module only ever WRITES under one dedicated dev root; the live
Store, credentials, job worktrees, config, service and Herdr are observed read-only (paths, inodes, hashes). It
  * snapshots a pinned commit into the dev root (`git archive`, no linked worktree in the live checkout) and derives
    a deterministic build digest + a `fleet-dev` label; the public package version is never touched;
  * resolves distinct absolute STEWARD_HOME / STEWARD_CONFIG_DIR / STEWARD_DATA_DIR / worktree root and refuses any
    equal, ancestor or descendant overlap with each other or with live state before anything starts;
  * allocates a unique Herdr workspace, socket and transient service identity and refuses inherited live identity;
  * accepts only fake:// or loopback outbound endpoints and builds the child environment from scratch;
  * takes read-only live-state sentinels (Store, job worktrees, config/data) before and after the trial;
  * issues a preflight-only receipt (`trial` authority, never ordinary active or production) and, after the final
    sentinels pass, the completed-trial receipt.
Nothing here deploys, activates, releases, incorporates, notifies or contacts anyone. Every git / process call goes
through an injectable runner."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

from steward import fleet_runtime as rt

LABEL = "fleet-dev"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
DEFAULT_OUTBOUND = {"notifier": "fake://dev-notifier", "connector": "fake://dev-connector",
                    "tunnel": "fake://dev-tunnel"}
# Environment names that can carry a live destination, credential or identity. The child env is built from scratch;
# this list is the belt-and-braces check on whatever the caller adds.
LIVE_ENV_MARKERS = ("TOKEN", "SECRET", "KEY", "WEBHOOK", "TUNNEL", "PUBLIC_URL", "NOTIFIER", "TELEGRAM", "MOSHI",
                    "CONNECTOR", "RESOLVER", "EXCEPTIONS", "PANE_ID", "TAB_ID")
PASSTHROUGH_ENV = ("PATH", "LANG", "LC_ALL", "TZ")
SENTINEL_NAME = ".fleet-dev-sentinel.json"
PREFIX_TABLES = ("events", "fleet_events", "fleet_rollout_evidence")
IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".scratch", ".fleet-dev-root"}
PHASE_GATE_TESTS = {
    "FLEET-15": ("tests/test_fleet_tech_lead.py",),
    "FLEET-16": ("tests/test_fleet_tech_lead.py",),
    "FLEET-17": ("tests/test_fleet_tech_lead.py", "tests/test_fleet_cli_lifecycle.py",
                 "tests/test_fleet_dev_e2e.py"),
    "FLEET-18": ("tests/test_fleet_adapter_deadline.py", "tests/test_fleet_services.py"),
    "FLEET-19": ("tests/test_fleet_adapter_deadline.py", "tests/test_fleet_learning_replay.py"),
    "FLEET-20": ("tests/test_fleet_learning_replay.py", "tests/test_fleet_cli_lifecycle.py"),
    "FLEET-21": ("tests/test_fleet_learning_replay.py", "tests/test_fleet_closeout.py",
                 "tests/test_fleet_promotion_gate.py"),
}


class DevIsolationError(RuntimeError):
    """An isolation check failed: nothing may start."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# -- paths -----------------------------------------------------------------------------------------------------------


def real(p) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(p))))


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


@dataclass(frozen=True)
class LiveState:
    """What is observed (never written) about the live installation."""

    home: Path
    config: Path
    data: Path
    worktrees: Path
    jobs: Path
    herdr_socket: str
    herdr_workspace: str
    service: str
    checkout: Path | None = None
    unit_file: Path | None = None

    def protected(self) -> dict[str, Path]:
        out = {"home": real(self.home), "config": real(self.config), "data": real(self.data),
               "worktrees": real(self.worktrees), "jobs": real(self.jobs)}
        try:
            for child in sorted(Path(self.worktrees).iterdir()):
                out[f"worktree:{child.name}"] = real(child)
        except OSError:
            pass
        return out


def observe_live(env: Mapping[str, str] | None = None, *, service: str = "steward-mcp.service",
                 checkout=None, unit_file=None) -> LiveState:
    """The live roots from the ambient environment, exactly as the live runtime resolves them."""
    from steward import store as st

    env = os.environ if env is None else env
    home = Path(env.get("STEWARD_HOME") or st.DEFAULT_HOME)
    xdg_cfg = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    xdg_data = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return LiveState(
        home=home, config=Path(env.get("STEWARD_CONFIG_DIR") or Path(xdg_cfg) / "steward"),
        data=Path(env.get("STEWARD_DATA_DIR") or Path(xdg_data) / "steward"), worktrees=home / "worktrees",
        jobs=home / "jobs",
        herdr_socket=env.get("HERDR_SOCKET_PATH") or str(Path.home() / ".config" / "herdr" / "herdr.sock"),
        herdr_workspace=env.get("HERDR_WORKSPACE_ID") or "", service=service,
        checkout=Path(checkout) if checkout else None, unit_file=Path(unit_file) if unit_file else None)


def check_roots(dev: Mapping[str, Path], live: LiveState, *, host: Path | None = None) -> list[str]:
    """Problems with the dev roots: any equal/ancestor/descendant overlap between dev roots, or with a live root or
    live job worktree. `host` names the one live job worktree that physically hosts the dev root (the builder runs
    inside a live worktree); only that host and its ancestors are exempt, and only for dev paths inside the host."""
    problems = []
    resolved = {k: real(v) for k, v in dev.items()}
    if any(not Path(v).is_absolute() for v in dev.values()):
        problems.append("dev roots must be absolute")
    names = sorted(k for k in resolved if k != "dev_root")   # dev_root is the container of the others
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if _overlaps(resolved[a], resolved[b]):
                problems.append(f"dev {a} overlaps dev {b}")
    host_r = real(host) if host is not None else None
    for lname, lpath in live.protected().items():
        for dname, dpath in resolved.items():
            if not _overlaps(dpath, lpath):
                continue
            if host_r is not None and dpath.is_relative_to(host_r) and (lpath == host_r or host_r.is_relative_to(lpath)):
                continue  # the dev path lives inside the declared host worktree; the host/ancestors merely contain it
            problems.append(f"dev {dname} overlaps live {lname}")
    return problems


# -- outbound ----------------------------------------------------------------------------------------------------------


def check_outbound(policy: Mapping[str, str], *, live_destinations=()) -> list[str]:
    """Only fake:// or loopback endpoints (or a unix socket path) are allowed; anything else fails closed."""
    problems = []
    if not isinstance(policy, Mapping) or not policy:
        return ["outbound policy must name at least one fake/local endpoint"]
    live = {str(x) for x in live_destinations if x}
    for name, target in sorted(policy.items()):
        if not isinstance(target, str) or not target.strip():
            problems.append(f"outbound {name}: empty target")
            continue
        if target in live:
            problems.append(f"outbound {name}: is a live destination")
            continue
        u = urlparse(target)
        if u.scheme == "fake" and u.netloc and "." not in u.netloc and u.username is None:
            continue
        if u.scheme in ("http", "https", "ws", "wss") and (u.hostname or "") in LOOPBACK_HOSTS \
                and u.username is None and "@" not in u.netloc:
            continue
        problems.append(f"outbound {name}: {u.scheme or 'no scheme'}://{u.hostname or ''} is not fake/loopback")
    return problems


def outbound_canon(policy: Mapping[str, str]) -> str:
    return json.dumps(dict(policy), sort_keys=True, separators=(",", ":"))


# -- source snapshot and build identity ----------------------------------------------------------------------------


def tree_digest(root: Path) -> str:
    """Deterministic sha256 over relative path, executable bit and content of every file (VCS/cache dirs skipped)."""
    h = hashlib.sha256()
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.is_symlink():
                body = b"symlink:" + os.readlink(p).encode()
            else:
                body = p.read_bytes()
            rel = p.relative_to(root).as_posix()
            h.update(f"{rel}\0{int(os.access(p, os.X_OK))}\0{hashlib.sha256(body).hexdigest()}\n".encode())
    return "sha256:" + h.hexdigest()


def verify_phase_gates(source: Path, gate: rt.RolloutGate, *, actor: str) -> dict:
    """Run the fixed regression suites from the exact build tree and record output-bound phase receipts.

    The test selection is part of the shipped module and is never accepted from the caller. All commands are
    read-only pytest runs with network-facing test output suppressed by the test harness; only cache files under the
    exact source root may be created (excluded from the build digest). Receipts are written only after the complete
    source digest remains unchanged across every run.
    """
    source = Path(source).resolve()
    build = gate.build
    if not build or tree_digest(source) != build.get("digest"):
        raise DevIsolationError(["phase-gate source does not match the exact build digest"])
    if not build.get("id", "").endswith(build["digest"].removeprefix("sha256:")[:12]):
        raise DevIsolationError(["phase-gate build id does not match its source digest"])
    if gate.instance_probe is not None:
        try:
            identity = gate.instance_probe()
        except Exception as exc:  # noqa: BLE001 - unverifiable instance must fail closed
            raise DevIsolationError(["dev instance probe failed: " + str(exc)[:200]]) from exc
        if identity.get("build_id") != build["id"] or identity.get("build_digest") != build["digest"].removeprefix("sha256:")[:24]:
            raise DevIsolationError(["dev instance identity differs from phase-gate build"])

    measured = []
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(source), "PYTHONPATH": str(source),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    for gate_name, targets in PHASE_GATE_TESTS.items():
        command = [sys.executable, "-m", "pytest", "-q", *targets]
        try:
            result = subprocess.run(command, cwd=source, env=env, capture_output=True, text=True,
                                    timeout=600, check=False)
            exit_code = int(result.returncode)
            output = (result.stdout or "") + "\n" + (result.stderr or "")
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            output = str(exc.stdout or "") + "\n" + str(exc.stderr or "") + "\ntimeout"
        measured.append((gate_name, command, hashlib.sha256(output.encode()).hexdigest(), exit_code))

    if tree_digest(source) != build["digest"]:
        raise DevIsolationError(["phase-gate source changed while checks were running; no receipts recorded"])
    passed = {}
    for gate_name, command, output_digest, exit_code in measured:
        gate._record_phase_gate_receipt(gate_name, actor=actor, source_digest=build["digest"], command=command,
                                        output_digest=output_digest, exit_code=exit_code, build=build)
        passed[gate_name] = exit_code == 0
    return {"passed": all(passed.values()), "gates": passed, "build_id": build["id"],
            "build_digest": build["digest"]}


def build_label(commit: str, digest: str) -> dict:
    d = digest.removeprefix("sha256:")
    return {"id": f"{commit[:12]}-{d[:12]}", "digest": digest, "commit": commit,
            "label": f"{LABEL}+{commit[:12]}.{d[:12]}"}


def snapshot_source(repo, commit: str, dest: Path, runner) -> dict:
    """`git archive` of the pinned commit extracted under `dest` (the dev root). Reads the repo; writes only `dest`."""
    dest = Path(dest)
    r = runner.run(["git", "rev-parse", "--verify", f"{commit}^{{commit}}"], repo)
    if r.exit_code != 0 or len(r.stdout.strip()) != 40:
        raise DevIsolationError([f"cannot resolve pinned commit {commit!r}"])
    sha = r.stdout.strip()
    src = dest / "src"
    if src.exists():
        raise DevIsolationError([f"snapshot target {src} already exists"])
    dest.mkdir(parents=True, exist_ok=True)
    tar_path = dest / f"snapshot-{sha[:12]}.tar"
    a = runner.run(["git", "archive", "--format=tar", "-o", str(tar_path), sha], repo)
    if a.exit_code != 0 or not tar_path.exists():
        raise DevIsolationError([f"git archive failed: {a.stderr.strip()[:120]}"])
    src.mkdir()
    with tarfile.open(tar_path) as tf:
        tf.extractall(src, filter="data")
    tar_path.unlink()
    digest = tree_digest(src)
    return {"commit": sha, "src": src, "digest": digest, **build_label(sha, digest)}


def public_version(pyproject: Path) -> str | None:
    try:
        v = tomllib.loads(Path(pyproject).read_text()).get("project", {}).get("version")
        return v.strip() if isinstance(v, str) else None
    except (OSError, ValueError):
        return None


def version_at(runner, repo, rev: str) -> str | None:
    r = runner.run(["git", "show", f"{rev}:pyproject.toml"], repo)
    if r.exit_code != 0:
        return None
    try:
        v = tomllib.loads(r.stdout).get("project", {}).get("version")
        return v.strip() if isinstance(v, str) else None
    except ValueError:
        return None


# -- the dev instance ------------------------------------------------------------------------------------------------


@dataclass
class DevInstance:
    dev_root: Path
    home: Path
    config: Path
    data: Path
    worktrees: Path
    src: Path
    socket: str
    workspace: str
    service: str
    build: dict
    outbound: dict
    sentinel_nonce: str
    live: LiveState
    session_name: str = ""
    xdg_config_alias: Path | None = None
    host: Path | None = None
    version: str | None = None
    extra_env: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.build["label"]

    def roots(self) -> dict[str, Path]:
        return {"dev_root": self.dev_root, "home": self.home, "config": self.config, "data": self.data,
                "worktrees": self.worktrees}

    def env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """The child environment, built from scratch: dev roots, dev Herdr/service identity, fake outbound only."""
        base = os.environ if base is None else base
        out = {k: base[k] for k in PASSTHROUGH_ENV if k in base}
        out.update({
            "HOME": str(self.dev_root / "home-dir"), "STEWARD_HOME": str(self.home),
            "STEWARD_CONFIG_DIR": str(self.config), "STEWARD_DATA_DIR": str(self.data),
            "XDG_CONFIG_HOME": str(self.xdg_config_alias or (self.dev_root / "xdg-config")), "XDG_DATA_HOME": str(self.dev_root / "xdg-data"),
            "HERDR_CONFIG_PATH": str(self.dev_root / "xdg-config" / "herdr" / "config.toml"),
            "HERDR_WORKSPACE_ID": self.workspace, "HERDR_SOCKET_PATH": self.socket,
            "STEWARD_INSTALL_ROOT": str(self.src), "STEWARD_FLEET_DEV_LABEL": self.label,
            "STEWARD_FLEET_DEV_BUILD_ID": self.build["id"],
            "STEWARD_FLEET_DEV_BUILD_DIGEST": self.build["digest"],
            "STEWARD_FLEET_DEV_SENTINEL_NONCE": self.sentinel_nonce,
            "STEWARD_FLEET_DEV_OUTBOUND": outbound_canon(self.outbound),
            "STEWARD_FLEET_DEV_SERVICE": self.service, "PYTHONPATH": str(self.src), "PYTHONDONTWRITEBYTECODE": "1",
            "STEWARD_NOTIFIER": self.outbound.get("notifier", "fake://dev-notifier"),
        })
        out.update(self.extra_env)
        bad = env_problems(out, self.live)
        if bad:
            raise DevIsolationError(bad)
        return out

    def identity(self) -> dict:
        """What the trial gate compares on every call. Recomputed from the filesystem, so a swapped symlink, a
        modified source tree, a changed outbound policy or a missing sentinel changes it (or raises)."""
        problems = validate(self)
        if problems:
            raise DevIsolationError(problems)
        return {
            "home": str(real(self.home)), "config": str(real(self.config)), "data": str(real(self.data)),
            "worktrees": str(real(self.worktrees)), "service": self.service, "socket": str(real(self.socket)),
            "herdr": self.workspace, "outbound": hashlib.sha256(outbound_canon(self.outbound).encode()).hexdigest()[:16],
            "build_id": self.build["id"], "build_digest": tree_digest(self.src).removeprefix("sha256:")[:24],
            "sentinel": self.sentinel_nonce[:16]}


def env_problems(env: Mapping[str, str], live: LiveState) -> list[str]:
    problems = []
    live_vals = {str(live.home), str(live.config), str(live.data), str(live.worktrees), live.herdr_socket,
                 live.herdr_workspace, live.service}
    live_vals.discard("")
    for k, v in env.items():
        if k in PASSTHROUGH_ENV:
            continue
        if v in live_vals:
            problems.append(f"env {k} carries a live identity")
        if k not in ("STEWARD_NOTIFIER", "STEWARD_FLEET_DEV_LABEL") and any(m in k for m in LIVE_ENV_MARKERS) and \
                not k.startswith("STEWARD_FLEET_DEV"):
            problems.append(f"env {k} may carry a live credential or destination")
    for k in ("STEWARD_NOTIFIER",):
        if k in env and check_outbound({k: env[k]}):
            problems.append(f"env {k} is not fake/loopback")
    return problems


def validate(inst: DevInstance) -> list[str]:
    problems = check_roots(inst.roots(), inst.live, host=inst.host)
    problems += check_outbound(inst.outbound, live_destinations=())
    if inst.workspace == inst.live.herdr_workspace or not inst.workspace:
        problems.append("dev Herdr workspace is not unique")
    if inst.socket == inst.live.herdr_socket or real(inst.socket) == real(inst.live.herdr_socket):
        problems.append("dev Herdr socket equals the live socket")
    if inst.service == inst.live.service or not inst.service:
        problems.append("dev service identity equals the live service")
    if not real(inst.socket).is_relative_to(real(inst.dev_root)):
        problems.append("dev Herdr socket is outside the dev root")
    sent = inst.home / SENTINEL_NAME
    try:
        ok = json.loads(sent.read_text()).get("nonce") == inst.sentinel_nonce
    except (OSError, ValueError):
        ok = False
    if not ok:
        problems.append("dev sentinel missing or altered")
    return problems


def instance_identity_from_env(env: Mapping[str, str]) -> dict:
    """Rebuild and validate the immutable dev identity inside a restarted service process.

    This is the RolloutGate probe: it fails closed if any root, source byte, service/Herdr identity,
    outbound endpoint, or sentinel differs from the preflighted instance.
    """
    required = ("STEWARD_HOME", "STEWARD_CONFIG_DIR", "STEWARD_DATA_DIR", "STEWARD_INSTALL_ROOT",
                "STEWARD_FLEET_DEV_BUILD_ID", "STEWARD_FLEET_DEV_BUILD_DIGEST",
                "STEWARD_FLEET_DEV_SENTINEL_NONCE", "STEWARD_FLEET_DEV_OUTBOUND",
                "STEWARD_FLEET_DEV_SERVICE", "HERDR_SOCKET_PATH", "HERDR_WORKSPACE_ID")
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise DevIsolationError(["dev service identity missing: " + ", ".join(missing)])
    home, config, data, src = (real(env[k]) for k in ("STEWARD_HOME", "STEWARD_CONFIG_DIR",
                                                       "STEWARD_DATA_DIR", "STEWARD_INSTALL_ROOT"))
    try:
        outbound = json.loads(env["STEWARD_FLEET_DEV_OUTBOUND"])
    except (TypeError, ValueError) as exc:
        raise DevIsolationError(["dev outbound identity is unreadable"]) from exc
    if not isinstance(outbound, dict) or check_outbound(outbound):
        raise DevIsolationError(["dev outbound identity is not fake/loopback"])
    digest = tree_digest(src)
    expected_digest = env["STEWARD_FLEET_DEV_BUILD_DIGEST"]
    if digest != expected_digest:
        raise DevIsolationError(["dev source digest changed"])
    sentinel_nonce = env["STEWARD_FLEET_DEV_SENTINEL_NONCE"]
    try:
        sentinel = json.loads((home / SENTINEL_NAME).read_text())
    except (OSError, ValueError) as exc:
        raise DevIsolationError(["dev sentinel missing or unreadable"]) from exc
    if sentinel.get("nonce") != sentinel_nonce or sentinel.get("build") != env["STEWARD_FLEET_DEV_BUILD_ID"]:
        raise DevIsolationError(["dev sentinel identity changed"])
    dev_root = real(env.get("FLEET_DEV_ROOT") or home.parent)
    socket = str(real(env["HERDR_SOCKET_PATH"]))
    if not real(env["HERDR_SOCKET_PATH"]).is_relative_to(dev_root):
        raise DevIsolationError(["dev Herdr socket escaped its root"])
    build_id = env["STEWARD_FLEET_DEV_BUILD_ID"]
    if not build_id.endswith(digest.removeprefix("sha256:")[:12]):
        raise DevIsolationError(["dev build id does not match source digest"])
    return {"home": str(home), "config": str(config), "data": str(data), "worktrees": str(real(home.parent / "steward-worktrees")),
            "service": env["STEWARD_FLEET_DEV_SERVICE"], "socket": socket,
            "herdr": env["HERDR_WORKSPACE_ID"], "outbound": hashlib.sha256(
                outbound_canon(outbound).encode()).hexdigest()[:16], "build_id": build_id,
            "build_digest": digest.removeprefix("sha256:")[:24], "sentinel": sentinel_nonce[:16]}


def prepare_instance(repo, commit: str, dev_root, *, live: LiveState, runner, outbound: Mapping[str, str] | None = None,
                     host=None, token: str | None = None, base_env: Mapping[str, str] | None = None) -> DevInstance:
    """Create the dev root, snapshot the pinned commit, allocate identities and validate everything BEFORE any
    process may start. Raises DevIsolationError (and leaves nothing running) on any problem."""
    dev_root = real(dev_root)
    outbound = dict(DEFAULT_OUTBOUND if outbound is None else outbound)
    pre = check_outbound(outbound) + check_roots({"dev_root": dev_root}, live, host=host)
    if pre:
        raise DevIsolationError(pre)
    snap = snapshot_source(repo, commit, dev_root, runner)
    token = token or secrets.token_hex(6)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,20}", token):
        raise DevIsolationError(["dev instance token must contain only 1–20 letters, digits, '-' or '_'"])
    build = build_label(snap["commit"], snap["digest"])
    session_name = f"steward-test-fd-{build['digest'].removeprefix('sha256:')[:6]}-{token}"
    # Unix socket paths are limited to roughly 108 bytes. Use a private short
    # symlink for Herdr's XDG config root; its resolved target remains in dev_root.
    xdg_alias = Path(tempfile.gettempdir()) / f"hfd-{token}"
    if xdg_alias.exists() or xdg_alias.is_symlink():
        raise DevIsolationError(["private Herdr XDG alias already exists"])
    socket = dev_root / "xdg-config" / "herdr" / "sessions" / session_name / "herdr.sock"
    inst = DevInstance(
        dev_root=dev_root, home=dev_root / "steward-home", config=dev_root / "steward-config",
        data=dev_root / "steward-data", worktrees=dev_root / "steward-worktrees", src=snap["src"],
        socket=str(socket), workspace=session_name, session_name=session_name, xdg_config_alias=xdg_alias,
        service=f"steward-fleet-dev-{build['id']}-{token}.service", build=build, outbound=outbound,
        sentinel_nonce=secrets.token_hex(16), live=live, host=Path(host) if host else None,
        version=public_version(snap["src"] / "pyproject.toml"))
    for d in (inst.home, inst.config, inst.data, inst.worktrees, dev_root / "herdr", dev_root / "home-dir",
              dev_root / "xdg-config", dev_root / "xdg-data"):
        d.mkdir(parents=True, exist_ok=True)
    (inst.home / SENTINEL_NAME).write_text(json.dumps({"nonce": inst.sentinel_nonce, "build": build["id"],
                                                       "label": build["label"]}))
    problems = validate(inst)
    if base_env is not None:
        problems += [f"inherited {k} equals dev identity" for k in ("HERDR_WORKSPACE_ID", "HERDR_SOCKET_PATH")
                     if base_env.get(k) in (inst.workspace, inst.socket)]
    if problems:
        raise DevIsolationError(problems)
    inst.env(base_env)  # constructing it is the env check
    return inst


HERDR_INSTANCE_FILE = ".fleet-dev-herdr.json"


def _herdr_env(inst: DevInstance) -> dict[str, str]:
    env = inst.env()
    # Keep the test server from checking for or fetching remote updates. It runs only the installed local binary.
    config = Path(env["HERDR_CONFIG_PATH"])
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        config.write_text("[update]\nversion_check = false\nmanifest_check = false\n")
    return env


def _herdr_cli(inst: DevInstance, *args: str, timeout_s: float = 10.0):
    env = _herdr_env(inst)
    return subprocess.run(["herdr", "--session", inst.session_name, *args], cwd=inst.dev_root, env=env,
                          capture_output=True, text=True, timeout=timeout_s, check=False)


def ensure_herdr_session(inst: DevInstance, *, timeout_s: float = 30.0) -> dict:
    """Start or reattach to this dev instance's own headless Herdr session; never addresses the default session."""
    problems = validate(inst)
    if not inst.session_name or not Path(inst.socket).resolve().is_relative_to(inst.dev_root.resolve()):
        problems.append("dev Herdr session/socket is not bound inside its root")
    if problems:
        raise DevIsolationError(problems)
    if inst.xdg_config_alias is None:
        raise DevIsolationError(["dev Herdr XDG alias is missing"])
    if not inst.xdg_config_alias.is_symlink():
        if inst.xdg_config_alias.exists():
            raise DevIsolationError(["private Herdr XDG alias path is occupied"])
        inst.xdg_config_alias.symlink_to(inst.dev_root / "xdg-config", target_is_directory=True)
    if real(inst.xdg_config_alias) != real(inst.dev_root / "xdg-config"):
        raise DevIsolationError(["private Herdr XDG alias points outside the dev root"])
    env = _herdr_env(inst)
    listed = subprocess.run(["herdr", "session", "list", "--json"], cwd=inst.dev_root, env=env,
                            capture_output=True, text=True, timeout=10.0, check=False)
    if listed.returncode != 0:
        raise DevIsolationError(["cannot inspect isolated Herdr sessions: " + listed.stderr.strip()[:200]])
    try:
        sessions = json.loads(listed.stdout).get("sessions", [])
    except (TypeError, ValueError) as exc:
        raise DevIsolationError(["isolated Herdr session list is malformed"]) from exc
    session = next((s for s in sessions if s.get("name") == inst.session_name), None)
    if session is None:
        log_path = inst.dev_root / "herdr" / "herdr-server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            proc = subprocess.Popen(["herdr", "--session", inst.session_name, "server"], cwd=inst.dev_root,
                                    env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, close_fds=True)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            listed = subprocess.run(["herdr", "session", "list", "--json"], cwd=inst.dev_root, env=env,
                                    capture_output=True, text=True, timeout=5.0, check=False)
            if listed.returncode == 0:
                try:
                    sessions = json.loads(listed.stdout).get("sessions", [])
                    session = next((s for s in sessions if s.get("name") == inst.session_name), None)
                except (TypeError, ValueError):
                    session = None
                if session and session.get("running"):
                    break
            time.sleep(0.1)
        if not session or not session.get("running"):
            # Starting a server and receiving no ready session is a partial
            # launch. Reconcile only the exact named session before returning.
            check = subprocess.run(["herdr", "session", "list", "--json"], cwd=inst.dev_root, env=env,
                                   capture_output=True, text=True, timeout=10.0, check=False)
            if check.returncode == 0:
                try:
                    found = next((s for s in json.loads(check.stdout).get("sessions", [])
                                  if s.get("name") == inst.session_name), None)
                except (TypeError, ValueError):
                    found = None
                if found and Path(found.get("session_dir", "")).resolve().is_relative_to(inst.dev_root.resolve()):
                    if found.get("running"):
                        subprocess.run(["herdr", "session", "stop", inst.session_name], cwd=inst.dev_root,
                                       env=env, capture_output=True, text=True, timeout=15.0, check=False)
                    subprocess.run(["herdr", "session", "delete", inst.session_name], cwd=inst.dev_root,
                                   env=env, capture_output=True, text=True, timeout=15.0, check=False)
            if inst.xdg_config_alias.is_symlink() and real(inst.xdg_config_alias) == real(inst.dev_root / "xdg-config"):
                inst.xdg_config_alias.unlink()
            raise DevIsolationError([f"isolated Herdr session failed to start; see {log_path}"])
    session_dir = Path(session.get("session_dir", "")).resolve()
    socket = Path(session.get("socket_path", "")).resolve()
    if (not session.get("running") or not session_dir.is_relative_to(inst.dev_root.resolve())
            or socket != Path(inst.socket).resolve()):
        raise DevIsolationError(["isolated Herdr session identity/socket differs from the dev instance"])
    return {"name": inst.session_name, "session_dir": str(session_dir), "socket": str(socket),
            "running": True, "log": str(inst.dev_root / "herdr" / "herdr-server.log")}


def ensure_herdr_workspace(inst: DevInstance, *, cwd: Path | str, label: str = "fleet-dev") -> dict:
    """Create once or recover the exact trial workspace. The requested cwd must stay within the dev root."""
    ensure_herdr_session(inst)
    path = Path(cwd).resolve()
    if not path.is_relative_to(inst.dev_root.resolve()) or not path.is_dir():
        raise DevIsolationError(["dev Herdr workspace cwd must be an existing path inside the dev root"])
    metadata_path = inst.dev_root / HERDR_INSTANCE_FILE
    if metadata_path.exists():
        try:
            prior = json.loads(metadata_path.read_text())
        except (OSError, ValueError) as exc:
            raise DevIsolationError(["dev Herdr workspace receipt is unreadable"]) from exc
        if prior.get("session") != inst.session_name or not prior.get("workspace_id"):
            raise DevIsolationError(["dev Herdr workspace receipt belongs to another session"])
        current = _herdr_cli(inst, "workspace", "get", prior["workspace_id"])
        if current.returncode == 0:
            inst.workspace = prior["workspace_id"]
            return prior
    result = _herdr_cli(inst, "workspace", "create", "--cwd", str(path), "--label", label, "--no-focus")
    if result.returncode != 0:
        raise DevIsolationError(["cannot create isolated dev Herdr workspace: " + result.stderr.strip()[:200]])
    try:
        body = json.loads(result.stdout)["result"]
        workspace, tab, pane = body["workspace"], body["tab"], body["root_pane"]
        receipt = {"session": inst.session_name, "workspace_id": workspace["workspace_id"],
                   "tab_id": tab["tab_id"], "pane_id": pane["pane_id"], "cwd": str(path), "label": label}
    except (KeyError, TypeError, ValueError) as exc:
        raise DevIsolationError(["isolated dev workspace response is malformed"]) from exc
    inst.workspace = receipt["workspace_id"]
    metadata_path.write_text(json.dumps(receipt, sort_keys=True))
    return receipt


def stop_herdr_session(inst: DevInstance) -> dict:
    """Close the owned workspace and stop/delete only this named test session; retain dev logs and Store evidence."""
    env = _herdr_env(inst)
    listed = subprocess.run(["herdr", "session", "list", "--json"], cwd=inst.dev_root, env=env,
                            capture_output=True, text=True, timeout=10.0, check=False)
    if listed.returncode != 0:
        raise DevIsolationError(["cannot inspect isolated Herdr sessions before stop"])
    sessions = json.loads(listed.stdout).get("sessions", [])
    session = next((s for s in sessions if s.get("name") == inst.session_name), None)
    if session is None:
        if inst.xdg_config_alias and inst.xdg_config_alias.is_symlink():
            inst.xdg_config_alias.unlink()
        return {"stopped": True, "already_absent": True}
    if not Path(session.get("session_dir", "")).resolve().is_relative_to(inst.dev_root.resolve()):
        raise DevIsolationError(["refusing to stop a Herdr session outside the dev root"])
    metadata_path = inst.dev_root / HERDR_INSTANCE_FILE
    if metadata_path.exists():
        receipt = json.loads(metadata_path.read_text())
        if receipt.get("session") != inst.session_name:
            raise DevIsolationError(["refusing to close a workspace from another Herdr session"])
        if receipt.get("workspace_id"):
            closed = _herdr_cli(inst, "workspace", "close", receipt["workspace_id"])
            if closed.returncode != 0:
                raise DevIsolationError(["cannot close the isolated dev workspace: " + closed.stderr.strip()[:160]])
    stopped = subprocess.run(["herdr", "session", "stop", inst.session_name], cwd=inst.dev_root, env=env,
                             capture_output=True, text=True, timeout=15.0, check=False)
    if stopped.returncode != 0:
        raise DevIsolationError(["cannot stop the isolated Herdr session: " + stopped.stderr.strip()[:160]])
    deleted = subprocess.run(["herdr", "session", "delete", inst.session_name], cwd=inst.dev_root, env=env,
                             capture_output=True, text=True, timeout=15.0, check=False)
    if deleted.returncode != 0:
        raise DevIsolationError(["cannot delete the stopped isolated Herdr session: " + deleted.stderr.strip()[:160]])
    if inst.xdg_config_alias and inst.xdg_config_alias.is_symlink():
        inst.xdg_config_alias.unlink()
    return {"stopped": True, "session": inst.session_name, "log": str(inst.dev_root / "herdr" / "herdr-server.log")}


# -- live rollback identity ------------------------------------------------------------------------------------------


def _sha(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def live_rollback_identity(live: LiveState, runner) -> dict:
    """Observe (never touch) the current live build: checkout HEAD, its public version, the unit file hash and the
    service's own report of its running process. This is the rollback target and stays usable throughout."""
    out = {"service": live.service, "checkout": str(live.checkout) if live.checkout else None}
    if live.checkout:
        r = runner.run(["git", "-C", str(live.checkout), "rev-parse", "HEAD"], live.checkout)
        out["checkout_head"] = r.stdout.strip() if r.exit_code == 0 else None
        out["public_version"] = public_version(Path(live.checkout) / "pyproject.toml")
    if live.unit_file:
        out["unit_file"] = str(real(live.unit_file))
        out["unit_sha256"] = _sha(real(live.unit_file))
    s = runner.run(["systemctl", "--user", "show", "-p", "ActiveState,MainPID,ExecMainStartTimestamp",
                    live.service], Path.cwd())
    out["service_state"] = dict(line.split("=", 1) for line in s.stdout.splitlines() if "=" in line) \
        if s.exit_code == 0 else None
    return out


# -- live-state sentinels (read-only) --------------------------------------------------------------------------------


def _tree_hashes(root: Path) -> dict:
    root = Path(root)
    if not root.exists():
        return {"__missing__": True}
    return {"__tree_sha256__": _path_digest(root)}


def _path_digest(path: Path, _seen: frozenset[Path] = frozenset()) -> str:
    """Content digest for one existing file or directory, including symlink targets and file modes."""
    path = Path(path)
    h = hashlib.sha256()
    if path.is_symlink():
        target = real(path)
        h.update(b"symlink\0" + os.readlink(path).encode() + b"\0")
        if target in _seen or path in _seen:
            h.update(b"symlink-cycle")
        else:
            h.update(bytes.fromhex(_path_digest(target, _seen | {path})))
        return h.hexdigest()
    if path.is_file():
        h.update(b"file\0" + str(path.stat().st_mode & 0o777).encode() + b"\0")
        h.update(path.read_bytes())
        return h.hexdigest()
    if not path.is_dir():
        return hashlib.sha256(b"missing").hexdigest()
    resolved = real(path)
    if resolved in _seen:
        return hashlib.sha256(b"directory-cycle").hexdigest()
    seen = _seen | {resolved}
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = sorted(dirnames)
        base = Path(dirpath)
        for name in sorted(dirnames + filenames):
            child = base / name
            rel = child.relative_to(path).as_posix()
            mode = child.lstat().st_mode
            if child.is_symlink():
                body = b"symlink-content\0" + bytes.fromhex(_path_digest(child, seen))
            elif child.is_dir():
                body = b"directory"
            else:
                body = child.read_bytes()
            h.update(rel.encode() + b"\0" + str(mode & 0o777).encode() + b"\0")
            h.update(hashlib.sha256(body).digest())
    return h.hexdigest()


def _entry_identity(p: Path) -> dict:
    st = os.stat(p)
    ident = {"realpath": str(real(p)), "inode": st.st_ino, "device": st.st_dev}
    ident["content_sha256"] = _path_digest(Path(p))
    return ident


def _dir_entries(root: Path) -> dict:
    out = {}
    try:
        for child in sorted(Path(root).iterdir()):
            try:
                out[child.name] = _entry_identity(child)
            except OSError as exc:
                out[child.name] = {"realpath": str(real(child)), "error": f"{type(exc).__name__}: {exc}"}
    except OSError as exc:
        out["__error__"] = f"{type(exc).__name__}: {exc}"
    return out


def _store_view(live: LiveState, scratch: Path, limits: dict | None, canaries: Mapping[str, list]) -> dict:
    """Take a read-only SQLite backup into dev scratch and hash every logical table row.

    Trial evidence is valid only when the complete live Store is unchanged. Concurrent live Store activity makes
    the trial inconclusive rather than being mistaken for isolation.
    """
    db = Path(live.home) / "steward.db"
    view = {"path": str(real(db)), "exists": db.exists()}
    if not db.exists():
        return view
    st = os.stat(db)
    view.update({"inode": st.st_ino, "device": st.st_dev})
    copy_dir = Path(scratch) / "live-store-copy"
    shutil.rmtree(copy_dir, ignore_errors=True)
    copy_dir.mkdir(parents=True)
    last = None
    for _ in range(3):
        try:
            source = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
            conn = sqlite3.connect(copy_dir / "steward.db")
            conn.row_factory = None
            try:
                source.backup(conn)
                schema = hashlib.sha256(json.dumps(conn.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()).encode()).hexdigest()
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                table_hashes = {}
                for table in sorted(tables):
                    safe_table = '"' + table.replace('"', '""') + '"'
                    rows = conn.execute(f"SELECT * FROM {safe_table}").fetchall()
                    encoded = sorted(json.dumps(row, default=str, separators=(",", ":")) for row in rows)
                    table_hashes[table] = hashlib.sha256("\n".join(encoded).encode()).hexdigest()
                prefix, new_limits = {}, {}
                for t in PREFIX_TABLES:
                    if t not in tables:
                        continue
                    mx = conn.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {t}").fetchone()[0]
                    lim = mx if limits is None else limits.get(t, 0)
                    h = hashlib.sha256()
                    for row in conn.execute(f"SELECT * FROM {t} WHERE rowid<=? ORDER BY rowid", (lim,)):
                        h.update(json.dumps(row, default=str).encode())
                    prefix[t] = {"limit": lim, "sha256": h.hexdigest()}
                    new_limits[t] = mx if limits is None else lim
                found = {}
                for job_id in canaries.get("job_ids", []):
                    if "jobs" in tables:
                        found[f"jobs:{job_id}"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE job_id=?",
                                                               (job_id,)).fetchone()[0]
                    if "events" in tables:
                        found[f"events:{job_id}"] = conn.execute("SELECT COUNT(*) FROM events WHERE job_id=?",
                                                                 (job_id,)).fetchone()[0]
                for bid in canaries.get("build_ids", []):
                    if "fleet_rollout_evidence" in tables:
                        found[f"rollout_evidence:{bid}"] = conn.execute(
                            "SELECT COUNT(*) FROM fleet_rollout_evidence WHERE build_id=?", (bid,)).fetchone()[0]
                view.update({"schema_sha256": schema, "table_sha256": table_hashes, "prefix": prefix,
                             "canaries": found})
                view["limits"] = {t: p["limit"] for t, p in prefix.items()}
                if limits is None:
                    view["limits"] = new_limits
            finally:
                conn.close()
                source.close()
            break
        except sqlite3.Error as exc:
            last = exc
            time.sleep(0.2)
    else:
        view["error"] = f"{type(last).__name__}: {last}"
    shutil.rmtree(copy_dir, ignore_errors=True)
    return view


def snapshot_live(live: LiveState, scratch, *, limits: dict | None = None, canaries: Mapping[str, list] | None = None) -> dict:
    """Read-only path/identity/hash sentinel of the live Store, job worktrees and config/data. Pass the `store.limits`
    of the earlier snapshot as `limits` so the 'after' view hashes the same row prefix."""
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    canaries = canaries or {}
    snap = {
        "taken_at": time.time(),
        "roots": {k: str(v) for k, v in live.protected().items() if not k.startswith("worktree:")},
        "store": _store_view(live, scratch, limits, canaries),
        "worktrees": _dir_entries(live.worktrees), "jobs": _dir_entries(live.jobs),
        "config": _tree_hashes(live.config), "data": _tree_hashes(live.data),
    }
    snap["digest"] = hashlib.sha256(json.dumps(
        {k: snap[k] for k in ("roots", "worktrees", "jobs", "config", "data")} | {
            "store": {k: v for k, v in snap["store"].items() if k in ("path", "inode", "schema_sha256", "table_sha256")}},
        sort_keys=True).encode()).hexdigest()
    return snap


def compare_live(before: dict, after: dict) -> dict:
    """The sentinel verdict. Any live Store, worktree, jobs, config or data mutation invalidates the trial receipt."""
    problems = []
    if before["roots"] != after["roots"]:
        problems.append("live roots resolve differently")
    b, a = before["store"], after["store"]
    for k in ("path", "inode", "device", "exists"):
        if b.get(k) != a.get(k):
            problems.append(f"live store {k} changed")
    if "error" in b or "error" in a:
        problems.append("live store could not be fingerprinted")
    if b.get("schema_sha256") != a.get("schema_sha256"):
        problems.append("live store schema changed")
    if b.get("table_sha256") != a.get("table_sha256"):
        problems.append("live store content changed")
    for t, pre in (b.get("prefix") or {}).items():
        post = (a.get("prefix") or {}).get(t)
        if not post or post["sha256"] != pre["sha256"]:
            problems.append(f"live store {t} pre-existing rows changed")
    for name, n in (a.get("canaries") or {}).items():
        if n:
            problems.append(f"dev canary {name} present in the live store")
    added = {}
    for part in ("worktrees", "jobs"):
        if "__error__" in before[part] or "__error__" in after[part]:
            problems.append(f"live {part} contents could not be fingerprinted")
        for name, ident in before[part].items():
            if isinstance(ident, dict) and ident.get("error"):
                problems.append(f"live {part} entry {name} could not be fingerprinted")
            if after[part].get(name) != ident:
                problems.append(f"live {part} entry {name} moved or disappeared")
        for name, ident in after[part].items():
            if isinstance(ident, dict) and ident.get("error"):
                problems.append(f"live {part} entry {name} could not be fingerprinted")
        added[part] = sorted(set(after[part]) - set(before[part]))
        if added[part]:
            problems.append(f"live {part} entries added during trial: {', '.join(added[part][:8])}")
    for part in ("config", "data"):
        if "__missing__" in before[part] or "__missing__" in after[part]:
            problems.append(f"live {part} contents could not be fingerprinted")
        if before[part] != after[part]:
            problems.append(f"live {part} content changed")
    return {"unchanged": not problems, "problems": problems, "added_by_others": added,
            "before_digest": before["digest"], "after_digest": after["digest"]}


def canaries_for(inst: DevInstance, job_id: str) -> dict:
    return {"job_ids": [job_id], "build_ids": [inst.build["id"]]}


# -- harmless dev operation in a real child process ------------------------------------------------------------------

CHILD_PROBE = r"""
import json, os, sqlite3, pathlib
home = pathlib.Path(os.environ["STEWARD_HOME"]); cfg = pathlib.Path(os.environ["STEWARD_CONFIG_DIR"])
data = pathlib.Path(os.environ["STEWARD_DATA_DIR"]); wt = home / "worktrees" / "probe"
wt.mkdir(parents=True, exist_ok=True); (wt / "marker.txt").write_text("dev")
(cfg / "probe.toml").write_text('probe = true\n'); (data / "probe.txt").write_text("dev")
c = sqlite3.connect(home / "probe.db"); c.execute("create table if not exists t(x)"); c.execute("insert into t values (1)")
c.commit(); c.close()
print(json.dumps({"home": str(home.resolve()), "config": str(cfg.resolve()), "data": str(data.resolve()),
                  "workspace": os.environ.get("HERDR_WORKSPACE_ID"), "socket": os.environ.get("HERDR_SOCKET_PATH"),
                  "label": os.environ.get("STEWARD_FLEET_DEV_LABEL"), "notifier": os.environ.get("STEWARD_NOTIFIER"),
                  "has_pane": "HERDR_PANE_ID" in os.environ}))
"""


def run_child_probe(inst: DevInstance, runner, *, base_env: Mapping[str, str] | None = None) -> dict:
    r = runner.run([sys.executable, "-c", CHILD_PROBE], inst.dev_root, timeout=60, env=inst.env(base_env))
    if r.exit_code != 0:
        raise DevIsolationError([f"dev child probe failed: {r.stderr.strip()[:160]}"])
    return json.loads(r.stdout.strip().splitlines()[-1])


# -- preflight, bounded trial, completed-trial receipt ---------------------------------------------------------------


def issue_preflight(inst: DevInstance, gate: rt.RolloutGate, *, job_id: str, before: dict, rollback: dict,
                    ttl_s: float = 900.0, max_actions: int = 1) -> dict:
    """Validate everything, then record the preflight receipt. It authorizes ONLY a `trial` for this exact build,
    instance and job, with a cap, an expiry and the current kill-switch generation; no active or production grant."""
    problems = validate(inst)
    if gate.build != {"id": inst.build["id"], "digest": inst.build["digest"]}:
        problems.append("gate is not bound to the dev build")
    if gate.instance_probe is None:
        problems.append("gate has no instance probe: the trial would be unverifiable")
    if before["store"].get("error"):
        problems.append("live store sentinel unavailable")
    if problems:
        raise DevIsolationError(problems)
    identity = inst.identity()
    seq = gate.record_trial_preflight(
        job_id=job_id, instance=identity, isolated=True, low_risk=True, disposable=True, ttl_s=ttl_s,
        max_actions=max_actions, build=gate.build, bind_kill_switch=True,
        bindings={"label": inst.label, "commit": inst.build["commit"], "public_version": inst.version,
                  "outbound": dict(inst.outbound), "live_sentinel_digest": before["digest"],
                  "live_store_limits": before["store"].get("limits", {}), "rollback": rollback,
                  "grants": ["trial"], "denies": ["active", "production_promotion"]})
    return {"receipt_id": seq, "grants": ["trial"], "build": dict(gate.build), "instance": identity, "job_id": job_id,
            "max_actions": max_actions, "ttl_s": ttl_s, "kill_switch_generation": gate.kill_switch_generation()}


def run_trial(inst: DevInstance, gate: rt.RolloutGate, ctl, job_id: str, driver: Callable, *,
              clock: Callable[[], float] = time.time) -> dict:
    """Invoke the `trial` capability only for the preflighted disposable job. The gate (wired into the one
    JobController) revalidates build digest, resolved roots, service/socket/Herdr identity, outbound allowlist, the
    kill-switch generation, expiry and the action cap on EVERY proposal and revokes on drift. `driver(ctl)` runs the
    low-risk workflow and returns its own facts; this only records raw completion evidence (the receipt is Task 3)."""
    auth = gate.trial_authority(job_id)
    if not auth["valid"]:
        return {"ran": False, "reasons": auth["reasons"]}
    ctl.rollout = gate
    started = clock()
    error = None
    try:
        result = driver(ctl) or {}
    except Exception as exc:  # noqa: BLE001 - recorded as raw evidence; the trial just did not complete
        result, error = {}, f"{type(exc).__name__}: {exc}"
    ended = clock()
    end_auth = gate.trial_authority(job_id, audit=False)
    uses = [e for e in gate.fleet.rollout_evidence("trial_use", job_id=job_id)
            if e["payload"].get("preflight") == auth["preflight"]]
    revoked = [e["payload"]["reasons"] for e in gate.fleet.rollout_evidence("trial_revoked", job_id=job_id)]
    from steward import fleet_closeout as fc

    cand = fc.final_candidate(gate.store, job_id)
    run = {"started": started, "ended": ended, "within_expiry": ended < (auth["expires_at"] or 0),
           "actions_used": sum(u["payload"].get("n", 0) for u in uses), "remaining": end_auth["remaining"],
           "authority_at_end": {"valid": end_auth["valid"], "reasons": end_auth["reasons"]}, "revoked": revoked,
           "candidate_sha": (cand or {}).get("final_sha"), "verified_sha": (cand or {}).get("verified_sha"),
           "error": error, "driver": {k: v for k, v in result.items() if isinstance(v, (str, int, float, bool))},
           "preflight": auth["preflight"]}
    seq = gate.fleet.record_rollout_evidence("trial_run", build_id=gate.build["id"], build_digest=gate.build["digest"],
                                             job_id=job_id, payload=run)
    return {"ran": True, "run_id": seq, **run}


def complete_trial(inst: DevInstance, gate: rt.RolloutGate, *, job_id: str, after: dict, before: dict,
                   snapshot_fn: Callable[[], dict] | None = None) -> dict:
    """Validate the final live sentinels and every preflight-to-run binding, then (only then) issue the immutable
    completed-trial receipt. A changed sentinel or stale binding issues NOTHING. Never records owner approval."""
    problems = []
    pres = [e for e in gate.fleet.rollout_evidence("trial_preflight", job_id=job_id)
            if e["build_id"] == gate.build["id"] and e["build_digest"] == gate.build["digest"]]
    runs = [e for e in gate.fleet.rollout_evidence("trial_run", job_id=job_id)]
    if not pres or not runs:
        return {"issued": False, "problems": ["no preflight or no completed trial run"]}
    pre, run = pres[-1], runs[-1]["payload"]
    if run.get("preflight") != pre["seq"]:
        problems.append("trial run is not bound to the latest preflight")
    for e in gate.fleet.rollout_evidence("trial_revoked", job_id=job_id):
        if e["payload"].get("preflight") == pre["seq"]:
            # spending the whole cap is how a trial ends normally; any other revocation reason is a failed trial
            bad = [r for r in e["payload"].get("reasons", []) if r != "trial_cap_exhausted"]
            if bad:
                problems.append("trial authority was revoked: " + ",".join(bad))
    if not run.get("within_expiry"):
        problems.append("trial ran past its expiry")
    if run.get("error") or not run.get("candidate_sha"):
        problems.append("trial did not reach one candidate")
    if run.get("actions_used", 0) > pre["payload"]["max_actions"]:
        problems.append("trial exceeded its action cap")
    if pre["payload"].get("kill_switch_generation") != gate.kill_switch_generation():
        problems.append("kill-switch generation changed since preflight")
    try:
        if inst.identity() != pre["instance"]:
            problems.append("dev instance identity differs from the preflight")
    except DevIsolationError as exc:
        problems += exc.problems
    verdict = compare_live(before, after)
    problems += verdict["problems"]
    if problems:
        return {"issued": False, "problems": problems, "sentinel": verdict}
    evidence = {"commit": inst.build["commit"], "build_id": inst.build["id"], "build_digest": inst.build["digest"],
                "label": inst.label, "public_version": inst.version, "instance": pre["instance"], "job_id": job_id,
                "candidate_sha": run["candidate_sha"], "verified_sha": run.get("verified_sha"),
                "outbound": dict(inst.outbound), "sentinel": verdict, "preflight_receipt": pre["seq"],
                "actions_used": run["actions_used"], "max_actions": pre["payload"]["max_actions"],
                "kill_switch_generation": pre["payload"].get("kill_switch_generation"),
                "rollback": pre["payload"]["bindings"].get("rollback")}
    out = gate.record_trial_receipt(job_id=job_id, passed=True, source=gate.TRIAL_SOURCE, build=gate.build,
                                    detail="isolated dev trial completed", evidence=evidence)
    return {"issued": bool(out["passed"]), "receipt_id": out["receipt_id"], "evidence": evidence, "sentinel": verdict}


# -- operator path for the evidence writers --------------------------------------------------------------------------


def main(argv=None, *, store_factory=None, stdin=None, out=print) -> int:
    """`python -m steward.fleet_dev --home DIR --build-id ID --build-digest D <command>`. The operator path to the
    insert-only rollout-evidence writers that the API deliberately lacks. `approve` needs an interactive terminal on
    which the exact build id is typed; it is never inferred, and no command here promotes or deploys anything."""
    import argparse

    from steward.store import Store

    ap = argparse.ArgumentParser(prog="steward.fleet_dev")
    ap.add_argument("--home", required=True, help="the Store to write (always explicit; never the ambient live one)")
    ap.add_argument("--build-id", required=True)
    ap.add_argument("--build-digest", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    k = sub.add_parser("kill-switch")
    k.add_argument("state", choices=["on", "off"])
    sub.add_parser("exercise-kill-switch")
    f = sub.add_parser("false-action-review")
    f.add_argument("--reviewed", type=int, required=True)
    f.add_argument("--false", type=int, required=True, dest="false_n")
    gates = sub.add_parser("verify-phase-gates", help="run the fixed FLEET-15..21 suites against this exact source build")
    gates.add_argument("--source", required=True, help="exact source snapshot whose digest identifies this build")
    sub.add_parser("approve", help="approve a separate production-promotion action for this exact build")
    sub.add_parser("approve-active", help="approve ordinary active tech-lead mode for this exact build")
    args = ap.parse_args(argv)
    store = (store_factory or Store)(Path(args.home))
    build = {"id": args.build_id, "digest": args.build_digest}
    gate = rt.RolloutGate(store, build=build)
    actor = os.environ.get("USER", "operator")
    if args.cmd == "status":
        out(json.dumps(gate.status(), sort_keys=True, default=str))
    elif args.cmd == "kill-switch":
        gate.set_kill_switch(args.state == "on", actor=actor, reason="operator")
        out(f"kill switch {args.state}")
    elif args.cmd == "exercise-kill-switch":
        out(json.dumps(gate.exercise_kill_switch(actor=actor)))
    elif args.cmd == "false-action-review":
        out(json.dumps(gate.record_false_action_review(reviewed_actions=args.reviewed, false_actions=args.false_n,
                                                       reviewer=actor)))
    elif args.cmd == "verify-phase-gates":
        try:
            result = verify_phase_gates(Path(args.source), gate, actor=actor)
        except DevIsolationError as exc:
            out(json.dumps({"passed": False, "problems": exc.problems}))
            return 1
        out(json.dumps(result, sort_keys=True))
        return 0 if result["passed"] else 1
    elif args.cmd in ("approve", "approve-active"):
        stream = stdin if stdin is not None else sys.stdin
        if not stream.isatty():
            out("refused: approval needs an interactive terminal")
            return 2
        purpose = "production" if args.cmd == "approve" else "active"
        target = "a separate production-promotion action" if purpose == "production" else "ordinary active tech-lead mode"
        out(f"Approve exact build {args.build_id} ({args.build_digest}) for {target}? "
            "Type the build id to confirm:")
        if stream.readline().strip() != args.build_id:
            out("not approved")
            return 1
        gate.record_owner_approval(purpose=purpose, actor=actor, build=build)
        out(f"owner approval recorded for {target}; nothing was deployed")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
