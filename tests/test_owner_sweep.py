"""G7 guard (Phase 19-02): the owner's name lives in steward/owner.py and nowhere else in the package."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "steward"
NAME = re.compile("cal" "en(?!dar)", re.IGNORECASE)  # no word boundary: `waiting_on_<name>` counts, `calendar` does not


def _package_files() -> list[Path]:
    files = sorted(PKG.rglob("*.py"))
    templates = PKG / "templates"
    if templates.is_dir():
        files += sorted(p for p in templates.rglob("*") if p.is_file() and p.suffix != ".pyc")
    return files


def test_no_owner_name_outside_owner_module():
    hits = []
    for path in _package_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel == "steward/owner.py":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if NAME.search(line):
                hits.append(f"{rel}:{n}: {line.strip()[:100]}")
    assert not hits, "the owner's name leaked outside steward/owner.py:\n" + "\n".join(hits)
