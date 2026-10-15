"""The pre-pack audit (G12, T-19-33): it checks what would actually ship. Every marker and path class is proven by a
seeded fixture. The seeds are synthetic strings assembled from parts inside this file; none is a real value."""

from __future__ import annotations

import io
import json
import re
import shutil
import tarfile
from pathlib import Path

import pytest

from npm_helpers import NPM, REPO, build_dist, copy_repo, load_module

audit = load_module("steward_pack_audit")

OWNER = "Cal" + "en"
SEEDS = {
    "owner-name": (f"# written for {OWNER}", OWNER),
    "owner-domain": ("HOST = 'x." + "cairn" + "labs" + ".org'", "cairn" + "labs"),
    "home-path": ("ROOT = '/ho" + "me/" + "someone" + "/stuff'", "/ho" + "me/" + "someone"),
    "work-dir": ("DIR = 'work/work-" + "2026" + "0101-000000'", "work-" + "2026"),
    "tailnet-host": ("URL = 'https://box.tail1234." + "ts" + ".net/x'", "box.tail1234." + "ts" + ".net"),
    "machine-name-2": ("# runs on " + "krip" + "ke", "krip" + "ke"),
    "intern-host": ("URL = 'https://app" + ".inte" + "rn." + "example.org'", ".inte" + "rn."),
    "ipv4": ("HOST = '10." + "1.2.3'", "10." + "1.2.3"),
    "machine-name": ("# runs on " + "tur" + "ing", "tur" + "ing"),
    "email": ("MAIL = 'someone" + "@" + "example.org'", "someone" + "@" + "example.org"),
    "token-sk": ("KEY = '" + "sk" + "-" + "a1b2c3d4e5f6g7h8i9j0k1" + "'", "sk" + "-" + "a1b2c3d4e5f6g7h8i9j0k1"),
    "token-ghp": ("KEY = '" + "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2" + "'", "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2"),
    "token-akia": ("KEY = '" + "AK" + "IA" + "A1B2C3D4E5F6G7H8" + "'", "AK" + "IA" + "A1B2C3D4E5F6G7H8"),
    "private-key": ("-----BEGIN " + "RSA PRIVATE" + " KEY-----", "-----BEGIN " + "RSA PRIVATE" + " KEY-----"),
    "moshi-link-id": ("LINK = 'moshi://herdr?workspace=w1&pane=p1'", "moshi://herdr?workspace=w1"),
}

GOOD = {
    "package.json": "{}\n",
    "README.md": "# Steward\n",
    "LICENSE": "All rights reserved.\n",
    "herdr-plugin.toml": 'id = "steward.steward"\n',
    "bin/steward.js": "#!/usr/bin/env node\n",
    "steward/__init__.py": "",
    "steward/cli.py": "print('hi')\n",
    "steward/owner.py": f"LEGACY = '{OWNER.lower()}'\n",
    "steward/templates/steward.toml": "[x]\n",
}


def payload(paths):
    return json.dumps([{"id": "x", "files": [{"path": p, "size": 1, "mode": 420} for p in sorted(paths)]}])


class Npm:
    """An injected `npm`: records argv and returns a canned `npm pack --dry-run --json` payload."""

    def __init__(self, paths=None, raw=None, code=0):
        self.paths, self.raw, self.code, self.calls = paths, raw, code, []

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), str(cwd)))
        return self.code, self.raw if self.raw is not None else payload(self.paths)


def tree(tmp, files):
    root = tmp / "dist"
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return root


def manifest_for(tmp, files):
    m = tmp / "manifest.txt"
    m.write_text("".join(f"{p}\n" for p in sorted(files)))
    return m


def prepack(tmp, files, manifest=None, runner=None):
    root = tree(tmp, files)
    out = io.StringIO()
    code = audit.run_prepack(root, manifest or manifest_for(tmp, files), runner or Npm(files), out)
    return code, out.getvalue()


def test_clean_tree_passes(tmp_path):
    code, out = prepack(tmp_path, GOOD)
    assert code == 0, out
    assert "ok" in out.lower()


def _marker_of(seed_id):
    return "machine-name" if seed_id == "machine-name-2" else seed_id


def test_seeds_cover_every_marker():
    assert {_marker_of(s) for s in SEEDS} == {m.name for m in audit.MARKERS}


@pytest.mark.parametrize("name", sorted(SEEDS))
def test_each_marker_fails_when_seeded(tmp_path, name):
    line, token = SEEDS[name]
    files = {**GOOD, "steward/seed.py": f"x = 1\n{line}\ny = 2\n"}
    code, out = prepack(tmp_path, files)
    assert code == 1
    finding = [l for l in out.splitlines() if l.startswith("steward/seed.py:2:")]
    assert len(finding) == 1, out
    assert _marker_of(name) in finding[0]
    assert token not in out  # masked: the full matched token is never printed
    assert token[: max(1, min(4, len(token) - 1))] in finding[0]


@pytest.mark.parametrize("text", [
    "HOST = '127.0.0.1'", "BIND = '0.0.0.0'", "LINK = 'moshi://herdr?workspace=<ws>&pane=<pane>'",
    "LINK = f'moshi://herdr?workspace={ws}&pane={pane}'", "LINK = 'moshi://herdr?workspace=%s&pane=%s'",
    "WHO = 'steward@localhost'", "# the calendar and the calendars", "KEYS = '~/.api-keys'",
    "V = '0.9.1'", "T = 'task-1234567890abcdef12345'", "R = 'risk-assessment-of-things-here'",
    "LOOP = '127.1.2.3'",
])
def test_legitimate_lookalikes_pass(tmp_path, text):
    code, out = prepack(tmp_path, {**GOOD, "steward/ok.py": text + "\n"})
    assert code == 0, out


def test_owner_name_allowed_only_in_owner_py(tmp_path):
    code, out = prepack(tmp_path, GOOD)  # GOOD already has the spelling in steward/owner.py
    assert code == 0, out
    assert OWNER.lower() in GOOD["steward/owner.py"]
    bad = {**GOOD, "steward/other.py": GOOD["steward/owner.py"]}
    (tmp_path / "again").mkdir()
    code, out = prepack(tmp_path / "again", bad)
    assert code == 1 and "steward/other.py:1:" in out and "owner-name" in out


@pytest.mark.parametrize("path", [
    "tests/x.py", ".planning/a.md", "docs/decisions/d.md", "deploy/u.service", "scripts/s.py", "x.db",
    "steward/extra.json", ".env", "id_rsa.pem", "steward/__pycache__/m.pyc", "spikes/s.py", "steward/templates/x.db",
    "steward/templates/.env", "steward/data.toml", "bin/other.js", "node_modules/a/b.js", "package-lock.json",
])
def test_paths_outside_the_allowlist_fail(tmp_path, path):
    files = {**GOOD, path: "x\n"}
    code, out = prepack(tmp_path, files)
    assert code == 1
    assert any(l.startswith(f"{path}:") for l in out.splitlines()), out


def test_allowed_paths_pass(tmp_path):
    for path in ("package.json", "herdr-plugin.toml", "steward/templates/steward.toml",
                 "steward/templates/units/steward-mcp.service", "steward/templates/cockpit/steward-dash.sh",
                 "steward/question_adapters/base.py"):
        assert audit.audit_paths([path], None) == [], path


def test_manifest_must_match_exactly(tmp_path):
    root = tree(tmp_path, GOOD)
    short = {k: v for k, v in GOOD.items() if k != "steward/cli.py"}
    out = io.StringIO()
    assert audit.run_prepack(root, manifest_for(tmp_path, short), Npm(GOOD), out) == 1
    assert "steward/cli.py" in out.getvalue() and "manifest" in out.getvalue()
    extra = {**GOOD, "steward/gone.py": ""}
    out = io.StringIO()
    assert audit.run_prepack(root, manifest_for(tmp_path, extra), Npm(GOOD), out) == 1
    assert "steward/gone.py" in out.getvalue()
    # order is irrelevant
    m = tmp_path / "shuffled.txt"
    m.write_text("".join(f"{p}\n" for p in sorted(GOOD, reverse=True)))
    assert audit.run_prepack(root, m, Npm(GOOD), io.StringIO()) == 0


def test_prepack_uses_ignore_scripts(tmp_path):
    runner = Npm(GOOD)
    code, out = prepack(tmp_path, GOOD, runner=runner)
    assert code == 0
    argv, _ = runner.calls[0]
    assert argv == ["npm", "pack", "--dry-run", "--json", "--ignore-scripts"]


@pytest.mark.parametrize("runner", [Npm(raw="not json"), Npm(raw="[]"), Npm(raw='[{"files": 3}]'),
                                    Npm(raw='[{"files": [{"nope": 1}]}]'), Npm(raw="{}"), Npm(paths=[], code=1)])
def test_unparsable_npm_output_fails_closed(tmp_path, runner):
    code, out = prepack(tmp_path, GOOD, runner=runner)
    assert code == 1
    assert "npm" in out


def test_a_file_npm_lists_but_is_missing_on_disk_fails(tmp_path):
    root = tree(tmp_path, GOOD)
    (root / "steward" / "cli.py").unlink()
    out = io.StringIO()
    assert audit.run_prepack(root, manifest_for(tmp_path, GOOD), Npm(GOOD), out) == 1
    assert "steward/cli.py" in out.getvalue()


def test_marker_in_a_template_is_found(tmp_path):
    line, token = SEEDS["email"]
    code, out = prepack(tmp_path, {**GOOD, "steward/templates/steward.toml": line + "\n"})
    assert code == 1 and "steward/templates/steward.toml:1:" in out


def _build_tiny_tarball(tmp, files):
    """A real tarball built with the build script's output tree and `npm pack` replaced by tarfile (no registry, no npm)."""
    out = tmp / "pkg.tgz"
    with tarfile.open(out, "w:gz") as tar:
        for rel, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"package/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out


def test_tarball_mode(tmp_path):
    tgz = _build_tiny_tarball(tmp_path, GOOD)
    out = io.StringIO()
    assert audit.run_tarball(tgz, manifest_for(tmp_path, GOOD), out) == 0, out.getvalue()
    line, token = SEEDS["ipv4"]
    (tmp_path / "x").mkdir()
    bad = _build_tiny_tarball(tmp_path / "x", {**GOOD, "steward/seed.py": line + "\n"})
    out = io.StringIO()
    assert audit.run_tarball(bad, manifest_for(tmp_path, {**GOOD, "steward/seed.py": ""}), out) == 1
    assert "steward/seed.py:1:" in out.getvalue() and token not in out.getvalue()
    stray = _build_tiny_tarball(tmp_path, {**GOOD, "tests/t.py": ""})
    out = io.StringIO()
    assert audit.run_tarball(stray, manifest_for(tmp_path, GOOD), out) == 1


def test_update_manifest_refuses_on_findings(tmp_path):
    line, _ = SEEDS["machine-name"]
    files = {**GOOD, "steward/seed.py": line + "\n"}
    root = tree(tmp_path, files)
    manifest = tmp_path / "m.txt"
    manifest.write_text("keep\n")
    out = io.StringIO()
    assert audit.update_manifest(root, manifest, Npm(files), out) == 1
    assert manifest.read_text() == "keep\n"
    out = io.StringIO()
    stray = {**GOOD, "tests/t.py": ""}
    (tmp_path / "s").mkdir()
    assert audit.update_manifest(tree(tmp_path / "s", stray), manifest, Npm(stray), out) == 1
    assert manifest.read_text() == "keep\n"


def test_update_manifest_rewrites_sorted(tmp_path):
    root = tree(tmp_path, GOOD)
    manifest = tmp_path / "m.txt"
    manifest.write_text("stale\n")
    assert audit.update_manifest(root, manifest, Npm(GOOD), io.StringIO()) == 0
    assert manifest.read_text().splitlines() == sorted(GOOD)
    assert audit.run_prepack(root, manifest, Npm(GOOD), io.StringIO()) == 0


def test_findings_print_path_line_marker(tmp_path):
    line, token = SEEDS["machine-name-2"]
    code, out = prepack(tmp_path, {**GOOD, "steward/seed.py": "\n\n" + line + "\n"})
    assert re.search(r"^steward/seed\.py:3: machine-name", out, re.M), out


def test_main_dist_mode(tmp_path):
    root = tree(tmp_path, GOOD)
    manifest = manifest_for(tmp_path, GOOD)
    assert audit.main(["--dist", str(root), "--manifest", str(manifest)], runner=Npm(GOOD)) == 0
    assert audit.main(["--dist", str(root), "--manifest", str(tmp_path / "other.txt")], runner=Npm(GOOD)) == 1


@pytest.fixture
def quiet_npm(tmp_path, monkeypatch):
    """Keep npm's cache and logs out of the real home."""
    monkeypatch.setenv("npm_config_cache", str(tmp_path / "npmcache"))
    monkeypatch.setenv("npm_config_update_notifier", "false")
    monkeypatch.setenv("npm_config_logs_dir", str(tmp_path / "npmlogs"))
    monkeypatch.setenv("npm_config_audit", "false")


@pytest.mark.skipif(not NPM, reason="npm is not installed")
def test_real_tree_passes(tmp_path, quiet_npm):
    dist = build_dist(tmp_path)
    out = io.StringIO()
    code = audit.run_prepack(dist, audit.MANIFEST, None, out)
    assert code == 0, out.getvalue()
    listed = audit.list_files(audit.default_runner, dist)
    assert sorted(listed) == sorted(audit.read_manifest(audit.MANIFEST))
    hits = sorted(p for p in listed if re.search(r"cal" r"en(?!dar)", (dist / p).read_text(errors="replace"), re.I))
    assert hits == []  # the legacy spelling is configuration now, not code: no shipped file names the owner


@pytest.mark.skipif(not NPM, reason="npm is not installed")
def test_real_tree_audit_runs_through_the_cli_entry(tmp_path, quiet_npm):
    dist = build_dist(tmp_path)
    assert audit.main(["--dist", str(dist)]) == 0


@pytest.mark.skipif(not NPM, reason="npm is not installed")
def test_pack_refuses_a_seeded_leak_and_makes_no_tarball(tmp_path, quiet_npm):
    build = load_module("steward_pack_build")
    repo = copy_repo(tmp_path)
    line, _ = SEEDS["email"]
    (repo / "steward" / "leaky.py").write_text(line + "\n")
    manifest = tmp_path / "manifest.txt"
    manifest.write_text(audit.MANIFEST.read_text() + "steward/leaky.py\n")
    with pytest.raises(build.PackError):
        build.pack(tmp_path / "dist" / "npm", repo_root=repo, manifest=manifest)
    assert not list((tmp_path / "dist").glob("*.tgz"))


@pytest.mark.skipif(not NPM, reason="npm is not installed")
def test_a_new_unlisted_file_fails_the_real_tree(tmp_path, quiet_npm):
    repo = copy_repo(tmp_path)
    (repo / "steward" / "brand_new.py").write_text("X = 1\n")
    dist = build_dist(tmp_path, repo)
    out = io.StringIO()
    assert audit.run_prepack(dist, audit.MANIFEST, None, out) == 1
    assert "steward/brand_new.py: manifest-missing" in out.getvalue()
