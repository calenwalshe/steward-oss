#!/usr/bin/env python3
"""The pre-pack audit (Phase 19, G12): prove that nothing personal ships.

It checks what would REALLY be packed: the path list comes from `npm pack --dry-run --json --ignore-scripts` (the flag
matters: a dry run executes `prepack`, which is this audit, so without it the audit would recurse), the contents come
from the package directory (or from a tarball with --tarball). Three checks, all fail-closed:

  1. every path is on the allowlist and on none of the deny lists (`audit_paths`);
  2. the path list equals the committed manifest, packaging/npm/manifest.txt, exactly (`audit_paths`);
  3. no shipped file contains a marker: the owner's name, internal hosts and paths, addresses, e-mail addresses,
     tokens, private keys, deep links that carry an id (`scan_text`).

Findings print as `path:line: marker (masked text)`; the matched text is cut to a few characters so the audit's own
output never repeats the thing it found. This file is never shipped (its regexes name the markers), so its literals
may live here. Every exemption below is a named entry with a reason and a test in tests/test_npm_audit.py.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, TextIO

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.txt"
NPM_ARGV = ["npm", "pack", "--dry-run", "--json", "--ignore-scripts"]

Runner = Callable[[list[str], "str | Path"], "tuple[int, str]"]


class AuditError(Exception):
    """The audit could not establish what would ship (npm failed or printed something unparsable)."""


# --- path rules --------------------------------------------------------------------------------------------------

ALLOWED_FILES = {"package.json", "README.md", "LICENSE", "herdr-plugin.toml", "bin/steward.js"}
DENIED_DIRS = {"tests", "spikes", ".planning", "deploy", "scripts", "docs", "packaging", "dist", "node_modules",
               "__pycache__", ".git", ".github"}
DENIED_SUFFIXES = (".db", ".db-wal", ".db-shm", ".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx", ".sqlite")
DENIED_NAMES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")


def _path_problem(path: str) -> tuple[str, str] | None:
    """(marker, why) when `path` may not ship, else None."""
    pure = PurePosixPath(path)
    parts = pure.parts
    if path.startswith("/") or ".." in parts:
        return "path-denied", "absolute or parent-relative path"
    for part in parts[:-1]:
        if part in DENIED_DIRS:
            return "path-denied", f"{part}/ never ships"
    name = pure.name
    if name.startswith(".env") or name in DENIED_NAMES or name.startswith(tuple(f"{n}." for n in DENIED_NAMES)):
        return "path-denied", "environment or key file"
    if name.endswith(DENIED_SUFFIXES):
        return "path-denied", f"{pure.suffix or name} files never ship"
    if path in ALLOWED_FILES:
        return None
    if parts and parts[0] == "steward" and len(parts) >= 2:
        if parts[1] == "templates" and len(parts) >= 3:
            return None
        if pure.suffix == ".py":
            return None
    return "path-not-allowlisted", "not in the allowlist (bin/steward.js, package files, steward/**/*.py, steward/templates/**)"


# --- markers -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Marker:
    name: str
    pattern: re.Pattern
    why: str
    exempt_files: dict = None  # path -> reason: files in which this marker is allowed


def _m(name: str, regex: str, why: str, flags: int = 0, exempt_files: dict | None = None) -> Marker:
    return Marker(name, re.compile(regex, flags), why, exempt_files or {})


MARKERS: tuple[Marker, ...] = (
    _m("owner-name", r"calen(?!dar)|walshe", "the owner's name", re.I,
       {"steward/owner.py": "the one place the legacy owner spelling lives (G7 compatibility keys)"}),
    _m("owner-domain", r"cairn[-_ ]?(?:gate[-_ ]?)?labs", "the owner's domain or lab", re.I),
    _m("home-path", r"(?:/home|/Users)/[A-Za-z0-9_.][A-Za-z0-9_.-]*", "a user home directory path"),
    _m("work-dir", r"work-2026", "a personal work-directory name"),
    _m("tailnet-host", r"[A-Za-z0-9][A-Za-z0-9.-]*\.ts\.net\b", "a tailnet host name", re.I),
    _m("intern-host", r"\.intern\.", "an internal host name", re.I),
    _m("ipv4", r"(?<!\w)(?<!\d\.)(?:\d{1,3}\.){3}\d{1,3}(?!\w)(?!\.\d)", "a non-loopback IPv4 address"),
    _m("machine-name", r"\btur" r"ing\b|kripke", "a personal machine name", re.I),
    _m("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", "an e-mail address"),
    _m("token-sk", r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}", "an API key"),
    _m("token-ghp", r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}", "a GitHub token"),
    _m("token-akia", r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}", "an AWS access key id"),
    _m("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key block"),
    _m("moshi-link-id", r"moshi://[^\s\"'<>]*?(?:workspace|pane)=(?![<{%$])[^&\s\"'<>{%$]+",
       "a moshi:// deep link that carries a real id (placeholders are fine)"),
)

# Named, tested lookalike exemptions that are not whole-file.
ALLOWED_IPV4 = {"0.0.0.0": "the unspecified address (a bind-all default)"}
LOOPBACK_NET = ipaddress.ip_network("127.0.0.0/8")  # loopback is not a leak


def _mask(text: str) -> str:
    """The first few characters of a match plus an ellipsis; never the whole match."""
    keep = max(1, min(4, len(text) - 1))
    return text[:keep] + "…"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    marker: str
    text: str = ""
    why: str = ""

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        tail = f" ({self.text})" if self.text else ""
        why = f" - {self.why}" if self.why else ""
        return f"{where}: {self.marker}{tail}{why}"


def _ipv4_exempt(text: str) -> bool:
    if text in ALLOWED_IPV4:
        return True
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return True  # not an address (an octet above 255: a version string or a number)
    return addr in LOOPBACK_NET


def scan_text(path: str, text: str) -> list[Finding]:
    """Every marker hit in `text`, as findings with the matched text masked."""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for marker in MARKERS:
            if path in marker.exempt_files:
                continue
            for hit in marker.pattern.finditer(line):
                if marker.name == "ipv4" and _ipv4_exempt(hit.group(0)):
                    continue
                findings.append(Finding(path, number, marker.name, _mask(hit.group(0)), marker.why))
    return findings


def audit_paths(paths: Iterable[str], manifest: Iterable[str] | None) -> list[Finding]:
    """Path rules for every path and, when `manifest` is given, exact equality with it (order is irrelevant)."""
    shipped = sorted(set(paths))
    findings: list[Finding] = []
    for path in shipped:
        problem = _path_problem(path)
        if problem:
            findings.append(Finding(path, None, problem[0], why=problem[1]))
    if manifest is not None:
        wanted = set(manifest)
        for path in shipped:
            if path not in wanted:
                findings.append(Finding(path, None, "manifest-missing", why="shipped but not in manifest.txt"))
        for path in sorted(wanted - set(shipped)):
            findings.append(Finding(path, None, "manifest-extra", why="in manifest.txt but not shipped"))
    return findings


# --- running -----------------------------------------------------------------------------------------------------


def default_runner(argv: list[str], cwd: "str | Path") -> tuple[int, str]:
    res = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)
    return res.returncode, res.stdout


def list_files(runner: Runner, cwd: "str | Path") -> list[str]:
    """The path list of `npm pack --dry-run --json --ignore-scripts`; AuditError on anything unparsable."""
    try:
        code, stdout = runner(list(NPM_ARGV), cwd)
    except OSError as exc:
        raise AuditError(f"npm could not run: {exc}") from exc
    if code != 0:
        raise AuditError(f"npm pack --dry-run exited {code}")
    try:
        data = json.loads(stdout)
        files = data[0]["files"]
        paths = [entry["path"] for entry in files]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AuditError("npm pack --dry-run --json output was not a file list") from exc
    if not paths or not all(isinstance(p, str) and p for p in paths):
        raise AuditError("npm pack --dry-run --json listed no files")
    return paths


def read_manifest(path: Path | str = MANIFEST) -> list[str]:
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise AuditError(f"cannot read the manifest {path}: {exc.strerror or exc}") from exc
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _audit(paths: list[str], read: Callable[[str], str | None], manifest: list[str] | None) -> list[Finding]:
    findings = audit_paths(paths, manifest)
    for path in sorted(set(paths)):
        text = read(path)
        if text is None:
            findings.append(Finding(path, None, "missing-on-disk", why="npm lists it but it cannot be read"))
        else:
            findings.extend(scan_text(path, text))
    return findings


def _report(findings: list[Finding], out: TextIO, count: int) -> int:
    if findings:
        for finding in findings:
            print(finding.render(), file=out)
        print(f"pack audit FAILED: {len(findings)} finding(s) in {count} file(s)", file=out)
        return 1
    print(f"pack audit ok: {count} file(s), manifest matches, no markers", file=out)
    return 0


def _dir_reader(root: Path) -> Callable[[str], str | None]:
    def read(rel: str) -> str | None:
        target = root / rel
        if not target.is_file() or target.is_symlink():
            return None
        return target.read_bytes().decode("utf-8", errors="replace")

    return read


def run_prepack(cwd: "str | Path", manifest: "Path | str | None" = MANIFEST, runner: Runner | None = None,
                out: TextIO | None = None) -> int:
    """Audit the package in `cwd`: paths from npm's dry run, contents from the directory. Exit status 0 or 1."""
    out = out or sys.stdout
    root = Path(cwd)
    try:
        paths = list_files(runner or default_runner, root)
        findings = _audit(paths, _dir_reader(root), read_manifest(manifest or MANIFEST))
    except AuditError as exc:
        print(f"pack audit FAILED: {exc}", file=out)
        return 1
    return _report(findings, out, len(set(paths)))


def _tar_members(tgz: Path) -> dict[str, bytes | None]:
    members: dict[str, bytes | None] = {}
    with tarfile.open(tgz, "r:*") as tar:
        for info in tar:
            name = info.name
            if name.startswith("package/"):
                name = name[len("package/"):]
            if info.isdir():
                continue
            if not info.isfile():
                members[name] = None  # a link or device: reported as unreadable
                continue
            members[name] = tar.extractfile(info).read()
    return members


def run_tarball(tgz: "Path | str", manifest: "Path | str | None" = MANIFEST, out: TextIO | None = None) -> int:
    """The same verdicts on the finished tarball (prefix `package/` stripped)."""
    out = out or sys.stdout
    try:
        members = _tar_members(Path(tgz))
        manifest_paths = read_manifest(manifest or MANIFEST)
    except (OSError, tarfile.TarError, AuditError) as exc:
        print(f"pack audit FAILED: cannot read {tgz}: {exc}", file=out)
        return 1

    def read(rel: str) -> str | None:
        data = members.get(rel)
        return None if data is None else data.decode("utf-8", errors="replace")

    findings = _audit(sorted(members), read, manifest_paths)
    return _report(findings, out, len(members))


def update_manifest(cwd: "str | Path", manifest: "Path | str" = MANIFEST, runner: Runner | None = None,
                    out: TextIO | None = None) -> int:
    """Rewrite the manifest from the package's real file list, but only when every other check passes."""
    out = out or sys.stdout
    root = Path(cwd)
    try:
        paths = sorted(set(list_files(runner or default_runner, root)))
        findings = _audit(paths, _dir_reader(root), None)
    except AuditError as exc:
        print(f"pack audit FAILED: {exc}", file=out)
        return 1
    if findings:
        print("manifest NOT updated:", file=out)
        return _report(findings, out, len(paths))
    Path(manifest).write_text("".join(f"{p}\n" for p in paths))
    print(f"manifest updated: {len(paths)} file(s) in {manifest}", file=out)
    return 0


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(prog="steward_pack_audit", description="audit what npm would pack")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepack", action="store_true", help="audit the package in the current directory (npm prepack)")
    mode.add_argument("--dist", metavar="DIR", help="audit the package assembled in DIR")
    mode.add_argument("--tarball", metavar="FILE", help="audit a finished tarball")
    parser.add_argument("--update-manifest", action="store_true", help="rewrite manifest.txt (only if the audit passes)")
    parser.add_argument("--manifest", default=str(MANIFEST), help="the committed file list (default: manifest.txt here)")
    args = parser.parse_args(argv)
    if args.tarball:
        if args.update_manifest:
            parser.error("--update-manifest needs --dist or --prepack")
        return run_tarball(args.tarball, args.manifest)
    cwd = Path(args.dist) if args.dist else Path.cwd()
    if args.update_manifest:
        return update_manifest(cwd, args.manifest, runner)
    return run_prepack(cwd, args.manifest, runner)


if __name__ == "__main__":
    raise SystemExit(main())
