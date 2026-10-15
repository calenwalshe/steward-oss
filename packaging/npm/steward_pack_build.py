#!/usr/bin/env python3
"""Assemble `dist/npm/` from an ALLOWLIST and (with --pack) turn it into an audited tarball (Phase 19, G1/G2/G17/G19).

Only the explicit patterns in `collect()` ever reach the output; a file that matches none of them is not copied, so a
new stray file fails closed. The version is read from pyproject.toml and stamped into package.json, herdr-plugin.toml
and steward/_version.py (one version, G17). Nothing here publishes or pushes anything and nothing contacts a registry.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

# Files taken from packaging/npm/ verbatim (then stamped): destination -> source name.
PACKAGING_FILES = {
    "package.json": "package.json",
    "README.md": "README.md",
    "LICENSE": "LICENSE",
    "herdr-plugin.toml": "herdr-plugin.toml",
    "bin/steward.js": "bin/steward.js",
}
EXECUTABLE = {"bin/steward.js"}
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".env", ".pem", ".key")


def _load_sibling(name: str, base: Path = HERE):
    path = base / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_pack_{name}_{abs(hash(str(path)))}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _walk(root: Path):
    """Regular files under `root` (no symlinks, no dotfiles, no bytecode, no databases), in sorted order."""
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or name.endswith(SKIP_SUFFIXES):
                continue
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def collect(repo_root: Path | str = REPO_ROOT) -> list[tuple[Path, str]]:
    """The allowlisted (source, destination) pairs, sorted by destination. Allowed: `steward/**/*.py` (except
    `_version.py`, which the stamp generates), `steward/templates/**` and the five packaging files. Nothing else."""
    repo = Path(repo_root)
    pairs: dict[str, Path] = {}
    pkg = repo / "steward"
    for src in _walk(pkg):
        rel = src.relative_to(repo).as_posix()
        if rel == "steward/_version.py":
            continue
        if rel.startswith("steward/templates/") or src.suffix == ".py":
            pairs[rel] = src
    for dest, name in PACKAGING_FILES.items():
        src = repo / "packaging" / "npm" / name
        if src.is_file():
            pairs[dest] = src
    return [(pairs[d], d) for d in sorted(pairs)]


def read_version(pyproject: Path | str) -> str:
    value = tomllib.loads(Path(pyproject).read_text()).get("project", {}).get("version")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"no [project] version in {pyproject}")
    return value.strip()


_SEMVER = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")


def _check_version(value: str) -> str:
    """The `--version` override must be a plain semver; anything else would be stamped into package.json as is."""
    text = str(value).strip()
    if not _SEMVER.fullmatch(text):
        raise ValueError(f"--version {value!r} is not a semantic version (for example 0.1.1)")
    return text


def _stamp_package_json(path: Path, version: str, full_name: str) -> None:
    source = json.loads(path.read_text())
    out = {"name": full_name, "version": version}
    out.update({k: v for k, v in source.items() if k not in ("name", "version")})
    path.write_text(json.dumps(out, indent=2) + "\n")


def _stamp_plugin(path: Path, version: str) -> None:
    text = path.read_text()
    new, n = re.subn(r'(?m)^version\s*=\s*"[^"]*"\s*$', f'version = "{version}"', text, count=1)
    if n != 1:
        raise ValueError(f"no top-level version line in {path}")
    path.write_text(new)


def stamp(out: Path, version: str, full_name: str) -> None:
    """Write the one version (and the npm package name) into every file that names it."""
    _stamp_package_json(out / "package.json", version, full_name)
    _stamp_plugin(out / "herdr-plugin.toml", version)
    (out / "steward" / "_version.py").write_text(
        '"""Stamped by the pack build (packaging/npm/steward_pack_build.py). Do not edit."""\n\n'
        f'VERSION = "{version}"\n'
        f'__version__ = "{version}"\n'
        f'NPM_PACKAGE = "{full_name}"\n'
    )


def _guard_out(out: Path, repo: Path) -> Path:
    resolved = out.resolve()
    repo_resolved = repo.resolve()
    if resolved == repo_resolved or resolved in repo_resolved.parents or resolved == Path.home().resolve() \
            or resolved == Path(resolved.anchor):
        raise ValueError(f"refusing to clean {resolved}: it is the repo, an ancestor of it, or home")
    return resolved


def build(out: Path | str, repo_root: Path | str = REPO_ROOT, pyproject: Path | str | None = None,
          version: str | None = None) -> Path:
    """Clean `out` and fill it from the allowlist with normalised modes (0644; bin/steward.js 0755); stamp the version
    (pyproject's, unless `version` overrides it: test use, for the upgrade tests' second tarball)."""
    repo = Path(repo_root)
    out = Path(out)
    _guard_out(out, repo)
    cfg = _load_sibling("pack_config", repo / "packaging" / "npm")
    version = _check_version(version) if version else read_version(pyproject or repo / "pyproject.toml")
    if out.exists():
        shutil.rmtree(out)
    for src, dest in collect(repo):
        target = out / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        target.chmod(0o755 if dest in EXECUTABLE else 0o644)
    (out / "steward").mkdir(parents=True, exist_ok=True)
    stamp(out, version, cfg.FULL_NAME)
    (out / "steward" / "_version.py").chmod(0o644)
    return out


class PackError(Exception):
    """The audit or `npm pack` refused: nothing was packed."""


def pack(out: Path | str, repo_root: Path | str = REPO_ROOT, manifest: Path | str | None = None,
         version: str | None = None) -> Path:
    """Build `out`, audit the file list npm would pack, run `npm pack` (whose `prepack` runs the audit again, from the
    environment variable below) into `out`'s parent, then audit the finished tarball. Returns the tarball path.
    `npm pack` never publishes; `--ignore-scripts` is NOT used here on purpose, so `prepack` runs."""
    out = Path(out)
    build(out, repo_root=repo_root, version=version)
    audit_path = HERE / "steward_pack_audit.py"
    audit = _load_sibling("steward_pack_audit")
    manifest = Path(manifest) if manifest else audit.MANIFEST
    if audit.run_prepack(out, manifest) != 0:
        raise PackError("the pre-pack audit failed; nothing was packed")
    dest = out.resolve().parent
    env = {**os.environ, "STEWARD_PACK_AUDIT": str(audit_path)}
    res = subprocess.run(["npm", "pack", "--pack-destination", str(dest)], cwd=out, env=env, capture_output=True,
                         text=True)
    if res.returncode != 0:
        raise PackError(f"npm pack failed ({res.returncode}): {(res.stderr or res.stdout).strip()[-400:]}")
    lines = [line.strip() for line in res.stdout.splitlines() if line.strip().endswith(".tgz")]
    if not lines:
        raise PackError("npm pack printed no tarball name")
    tarball = dest / lines[-1]
    if not tarball.is_file():
        raise PackError(f"npm pack did not produce {tarball}")
    if audit.run_tarball(tarball, manifest) != 0:
        tarball.unlink()
        raise PackError("the tarball audit failed; the tarball was removed")
    return tarball


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="steward_pack_build", description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(REPO_ROOT / "dist" / "npm"), help="where to assemble the package")
    parser.add_argument("--pack", action="store_true", help="also audit and `npm pack` the assembled package")
    parser.add_argument("--version", default=None, metavar="X",
                        help="stamp X instead of pyproject's version (test use: a second tarball to upgrade to)")
    args = parser.parse_args(argv)
    if args.version:
        try:
            _check_version(args.version)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.pack:
        try:
            tarball = pack(Path(args.out), version=args.version)
        except PackError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"packed {tarball}")
        print(f"install it with: npm i -g {tarball}")
        print("nothing was published: the package is private and distributed as this tarball only")
        return 0
    out = build(Path(args.out), version=args.version)
    print(f"built {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
