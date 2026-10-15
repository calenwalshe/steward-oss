#!/usr/bin/env python3
"""Build the public steward-oss snapshot from a git ref.

    python3 scripts/oss_export.py --ref HEAD --out DIR [--run-tests] [--allow-markers]

Takes `git archive <ref>`, keeps what packaging/oss/export.toml includes, scans the text files for
personal markers, and git-inits DIR with one commit. Exit 0 = clean (and tests passed if asked).
"""
from __future__ import annotations

import argparse
import fnmatch
import io
import re
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

CONFIG_PATH = "packaging/oss/export.toml"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=check)


def load_config(repo: Path, ref: str) -> dict:
    """The export config as committed at `ref` (falls back to the working tree copy)."""
    r = _git(repo, "show", f"{ref}:{CONFIG_PATH}", check=False)
    if r.returncode == 0:
        return tomllib.loads(r.stdout.decode())
    return tomllib.loads((repo / CONFIG_PATH).read_text())


def included(path: str, include: list[str], exclude: list[str]) -> bool:
    if not any(path == i or (i.endswith("/") and path.startswith(i)) for i in include):
        return False
    parts = path.split("/")
    for pat in exclude:
        if pat.endswith("/"):
            d = pat[:-1]
            if ("/" in d and path.startswith(pat)) or d in parts[:-1]:
                return False
        elif fnmatch.fnmatch(path, pat) or fnmatch.fnmatch("/" + path, pat):
            return False
    return True


def is_text(data: bytes) -> bool:
    return b"\0" not in data[:8000]


_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")  # pkg@1.2.3 is a version, not an address


def scan_text(path: str, text: str, markers: dict) -> list[str]:
    hits = []
    pats = [re.compile(p) for p in markers.get("patterns", [])]
    ok_dom = set(markers.get("email_allow_domains", []))
    ok_loc = set(markers.get("email_allow_locals", []))
    ok_suf = tuple(markers.get("email_allow_suffixes", []))
    ok_pre = tuple(markers.get("email_allow_prefixes", []))
    for n, line in enumerate(text.splitlines(), 1):
        for p in pats:
            if p.search(line):
                hits.append(f"{path}:{n}: /{p.pattern}/: {line.strip()[:120]}")
        for m in EMAIL_RE.finditer(line):
            local = m.group(0).split("@")[0]
            dom = m.group(1).lower()
            if (dom in ok_dom or local in ok_loc or dom.endswith(ok_suf) or dom.startswith(ok_pre) or _VERSION_RE.match(dom)):
                continue
            hits.append(f"{path}:{n}: email {m.group(0)}")
    return hits


def build(repo: Path, ref: str, out: Path) -> tuple[list[str], list[str], str, str]:
    """Write the filtered tree into `out`. Returns (files, marker hits, short sha, version)."""
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"refusing to export into a non-empty directory: {out}")
    cfg = load_config(repo, ref)
    include, exclude = cfg["export"]["include"], cfg["export"].get("exclude", [])
    markers = cfg.get("markers", {})
    exempt = set(markers.get("scan_exempt", []))
    sha = _git(repo, "rev-parse", "--short", ref).stdout.decode().strip()
    archive = _git(repo, "archive", "--format=tar", ref).stdout
    out.mkdir(parents=True, exist_ok=True)
    files, hits = [], []
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for m in tar.getmembers():
            if not m.isfile() or not included(m.name, include, exclude):
                continue
            data = tar.extractfile(m).read()
            dest = out / m.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            dest.chmod(0o755 if m.mode & 0o111 else 0o644)
            files.append(m.name)
            if m.name not in exempt and is_text(data):
                hits += scan_text(m.name, data.decode("utf-8", "replace"), markers)
    version = "unknown"
    pp = out / "pyproject.toml"
    if pp.exists():
        version = tomllib.loads(pp.read_text()).get("project", {}).get("version", "unknown")
    return sorted(files), hits, sha, version


def git_commit(repo: Path, out: Path, sha: str, version: str) -> None:
    def cfg(key: str) -> str:
        r = subprocess.run(["git", "-C", str(repo), "config", key], capture_output=True, text=True)
        return r.stdout.strip()
    name, email = cfg("user.name") or "Steward", cfg("user.email") or "noreply@example.com"

    def run(*a: str) -> None:
        subprocess.run(["git", "-C", str(out), *a], check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("add", "-A")
    run("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q",
        "-m", f"Steward {version} (snapshot of {sha})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--out", required=True)
    ap.add_argument("--run-tests", action="store_true")
    ap.add_argument("--allow-markers", action="store_true")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    files, hits, sha, version = build(Path(a.repo), a.ref, out)
    git_commit(Path(a.repo), out, sha, version)
    print(f"exported {len(files)} files to {out} (Steward {version}, {sha})")
    print(f"marker hits: {len(hits)}")
    for h in hits:
        print("  " + h)
    rc = 0
    if hits and not a.allow_markers:
        print("FAIL: personal markers found (use --allow-markers to override)")
        rc = 1
    if a.run_tests:
        r = subprocess.run([sys.executable, "-m", "pytest", "-n", "8", "-q", "-p", "no:cacheprovider"],
                           cwd=out, capture_output=True, text=True)
        tail = "\n".join(r.stdout.strip().splitlines()[-5:])
        print(f"tests: {'PASS' if r.returncode == 0 else 'FAIL'} (exit {r.returncode})\n{tail}")
        rc = rc or (0 if r.returncode == 0 else 2)
    return rc


if __name__ == "__main__":
    sys.exit(main())
