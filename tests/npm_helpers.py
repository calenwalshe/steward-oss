"""Shared helpers for the npm packaging tests (Phase 19, plan 08).

`packaging/` has no `__init__.py` on purpose (a `packaging` package would shadow PyPI's `packaging`, which pytest
imports), so the packaging modules are loaded by file path. Nothing here touches the network or a registry; fake
interpreters and fake `uv` are tiny Python scripts under a tmp directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
NPM = shutil.which("npm")


def load_module(name: str, repo: Path | None = None):
    """Import `packaging/npm/<name>.py` from `repo` (default: the real repo) by path, never by package name."""
    path = (repo or REPO) / "packaging" / "npm" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_npm_{name}_{abs(hash(str(path)))}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def copy_repo(tmp: Path) -> Path:
    """A copy of the parts of the repo the build reads (steward/, packaging/, pyproject.toml), for tests that plant
    stray files or bump the version without touching the real tree."""
    dest = tmp / "repo"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(REPO / "steward", dest / "steward", ignore=ignore)
    shutil.copytree(REPO / "packaging", dest / "packaging", ignore=ignore)
    shutil.copy(REPO / "pyproject.toml", dest / "pyproject.toml")
    return dest


def build_dist(tmp: Path, repo: Path | None = None) -> Path:
    """Run the build into `tmp/dist/npm` and return that directory."""
    build = load_module("steward_pack_build", repo)
    out = tmp / "dist" / "npm"
    build.build(out, repo_root=repo or REPO)
    return out


def pyproject_version(repo: Path | None = None) -> str:
    import tomllib

    return tomllib.loads(((repo or REPO) / "pyproject.toml").read_text())["project"]["version"]


_FAKE_PY = '''#!{python}
import json, os, signal, sys
if sys.argv[1:2] == ["-c"]:
    print({version!r})
    raise SystemExit(0)
record = os.environ.get("FAKE_PY_RECORD")
if record:
    with open(record, "w") as fh:
        json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "name": {name!r}}}, fh)
sig = os.environ.get("FAKE_PY_SIGNAL")
if sig:
    os.kill(os.getpid(), getattr(signal, sig))
raise SystemExit(int(os.environ.get("FAKE_PY_EXIT", "0")))
'''


def fake_python(bin_dir: Path, name: str, version: str = "3.12") -> Path:
    """An executable `name` that answers the shim's `-c` version probe with `version` and otherwise records its argv,
    environment and cwd as JSON to $FAKE_PY_RECORD, then exits with $FAKE_PY_EXIT (or dies by $FAKE_PY_SIGNAL)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(_FAKE_PY.format(python=sys.executable, version=version, name=name))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


_FAKE_UV = '''#!{python}
import os, sys
log = os.environ["FAKE_UV_LOG"]
with open(log, "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
marker = os.environ.get("FAKE_UV_MARKER", "")
args = sys.argv[1:]
if args[:2] == ["python", "find"]:
    target = os.environ.get("FAKE_UV_PYTHON", "")
    if target and (not marker or os.path.exists(marker)):
        print(target)
        raise SystemExit(0)
    raise SystemExit(1)
if args[:2] == ["python", "install"]:
    if marker:
        open(marker, "w").close()
    raise SystemExit(0)
raise SystemExit(2)
'''


def fake_uv(bin_dir: Path) -> Path:
    """A `uv` that records its argv to $FAKE_UV_LOG; `python find` prints $FAKE_UV_PYTHON (once $FAKE_UV_MARKER
    exists, when that is set); `python install` creates the marker."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "uv"
    path.write_text(_FAKE_UV.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def link_tool(bin_dir: Path, name: str) -> None:
    """Make a real system tool (for example `stty`) visible on an otherwise empty tmp PATH."""
    real = shutil.which(name)
    if real and not (bin_dir / name).exists():
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / name).symlink_to(real)


def shim_env(tmp: Path, path_dirs: list[Path], **extra: str) -> dict[str, str]:
    """A minimal environment: PATH is exactly `path_dirs`, HOME and XDG_* under tmp, nothing inherited."""
    env = {"PATH": os.pathsep.join(str(p) for p in path_dirs), "HOME": str(tmp / "home"),
           "XDG_CACHE_HOME": str(tmp / "cache"), "XDG_CONFIG_HOME": str(tmp / "xdgconfig"),
           "STEWARD_HOME": str(tmp / "stewardhome"), "STEWARD_CONFIG_DIR": str(tmp / "stewardconfig")}
    (tmp / "home").mkdir(exist_ok=True)
    env.update(extra)
    return env


def run_shim(dist: Path, args: list[str], env: dict[str, str], cwd: Path | None = None, stdin: str | None = "",
             timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([NODE, str(dist / "bin" / "steward.js"), *args], env=env, cwd=cwd, input=stdin,
                          capture_output=True, text=True, timeout=timeout)


def read_record(path: Path) -> dict:
    return json.loads(path.read_text())
