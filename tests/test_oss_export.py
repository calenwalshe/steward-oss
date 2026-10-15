"""scripts/oss_export.py: filtering, marker scan, refusal of a non-empty out dir."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("oss_export", ROOT / "scripts" / "oss_export.py")
oss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oss)

CONFIG = """
[export]
include = ["pkg/", "README.md", "pyproject.toml", "docs/keep.md"]
exclude = ["**/__pycache__/**", ".planning/", "pkg/skip/"]
[markers]
patterns = ['Zed', '/home/agent']
email_allow_domains = ["example.com"]
email_allow_locals = ["noreply"]
scan_exempt = ["packaging/oss/export.toml"]
"""


def make_repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    files = {"packaging/oss/export.toml": CONFIG,
             "pyproject.toml": '[project]\nname="x"\nversion="1.2.3"\n', **files}
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body if isinstance(body, bytes) else body.encode())

    def g(*a):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@example.com", *a],
                       check=True, capture_output=True)
    g("init", "-q")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    return repo


def test_include_exclude(tmp_path):
    repo = make_repo(tmp_path, {
        "pkg/a.py": "x = 1\n", "pkg/__pycache__/a.pyc": b"\0\1", "pkg/skip/b.py": "y\n",
        ".planning/p.md": "plan\n", "other/c.txt": "c\n", "README.md": "hi\n",
        "docs/keep.md": "k\n", "docs/no.md": "n\n"})
    files, hits, sha, version = oss.build(repo, "HEAD", tmp_path / "out")
    assert files == ["README.md", "docs/keep.md", "pkg/a.py", "pyproject.toml"]
    assert hits == [] and version == "1.2.3" and sha


def test_marker_detection(tmp_path):
    repo = make_repo(tmp_path, {
        "pkg/a.py": "# by Zed\npath = '/home/agent/x'\nmail me@corp.io\nok@example.com\nnoreply@anthropic.com\n",
        "pkg/bin.dat": b"Zed\0binary"})
    _, hits, _, _ = oss.build(repo, "HEAD", tmp_path / "out")
    assert len(hits) == 3
    joined = "\n".join(hits)
    assert "pkg/a.py:1" in joined and "pkg/a.py:2" in joined and "me@corp.io" in joined
    assert "bin.dat" not in joined and "ok@example.com" not in joined


def test_refuses_non_empty_out(tmp_path):
    repo = make_repo(tmp_path, {"pkg/a.py": "x\n"})
    out = tmp_path / "out"
    out.mkdir()
    (out / "junk").write_text("x")
    with pytest.raises(SystemExit):
        oss.build(repo, "HEAD", out)


def test_main_commits_and_exit_codes(tmp_path):
    repo = make_repo(tmp_path, {"pkg/a.py": "Zed\n"})
    out = tmp_path / "out"
    assert oss.main(["--repo", str(repo), "--out", str(out)]) == 1
    log = subprocess.run(["git", "-C", str(out), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert log.startswith("Steward 1.2.3 (snapshot of ")
    assert oss.main(["--repo", str(repo), "--out", str(tmp_path / "out2"), "--allow-markers"]) == 0
