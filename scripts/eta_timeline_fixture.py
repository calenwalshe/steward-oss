#!/usr/bin/env python3
"""Build a content-free planning timeline from a repo's git history (phase 18-02, G21, T-18-06).

Reads the source repo with read-only git commands only (`git -C <repo> log / show / ls-tree`) and writes exactly one
file, `--out`: sorted-key JSON, 1-space indent. The output holds numbers, `.planning` file names and normalised commit
subjects (`<type(scope)>: <class>`, class one of plan, summary, verification, context, work) and nothing else: no commit
text, no file bodies. Only `.planning/ROADMAP.md`, `.planning/STATE.md` and `.planning/phases/*/NN-MM-PLAN.md|SUMMARY.md`
paths are kept. For a commit that adds or modifies ROADMAP.md / STATE.md the parsed phase->plans map / progress numbers
are recorded (with the same parsers the adapter uses), never the text.

    eta_timeline_fixture.py --repo R --base B --to T --job ID --t0 EPOCH --truth-min M --goal "..." \
        --attempt EPOCH[:rework] ... --done EPOCH ... --sent-back EPOCH ... --out FILE
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from steward import eta_structure  # noqa: E402

MAX_STR = 120
_SUBJECT_RE = re.compile(r"^([a-z]+)(?:\(([0-9.,-]+)\))?:")


def _git(repo: str, *args: str) -> str:
    res = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise SystemExit(f"eta_timeline_fixture: git {' '.join(args[:2])} failed: {res.stderr.strip()[:200]}")
    return res.stdout


def kept(path: str) -> bool:
    return path in (eta_structure._ROADMAP, eta_structure._STATE) or eta_structure._plan_name(path) is not None


def normalise(subject: str) -> str:
    """`docs(01-02): complete contract layer plan` -> `docs(01-02): summary`: the conventional prefix and a class."""
    m = _SUBJECT_RE.match(subject.strip())
    prefix = m.group(1) + (f"({m.group(2)})" if m.group(2) else "") if m else "chore"
    return f"{prefix}: {eta_structure.classify_subject(subject)}"


def build(repo: str, base: str, to: str, job: str, t0: float, truth_min: float, goal: str,
          attempts: list, done: list, sent_back: list) -> dict:
    span = f"{base}..{to}"
    planning = {}
    order = []
    out = _git(repo, "log", "--reverse", "--no-renames", "--format=%x1e%H%x1f%ct", "--name-status", span, "--", ".planning")
    for rec in out.split("\x1e"):
        if not rec.strip():
            continue
        head, _, rest = rec.partition("\n")
        sha, _, at = head.strip().partition("\x1f")
        changes = {"A": [], "M": [], "D": []}
        for line in rest.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0][:1] in changes and kept(parts[-1]):
                changes[parts[0][:1]].append(parts[-1])
        planning[sha] = changes
    commits = []
    for line in _git(repo, "log", "--reverse", "--format=%H%x1f%ct%x1f%s", span).splitlines():
        sha, at, subject = line.split("\x1f", 2)
        ch = planning.get(sha, {"A": [], "M": [], "D": []})
        roadmap = state = None
        if eta_structure._ROADMAP in ch["A"] + ch["M"]:
            roadmap = eta_structure.parse_roadmap(_git(repo, "show", f"{sha}:{eta_structure._ROADMAP}"))
        if eta_structure._STATE in ch["A"] + ch["M"]:
            state = eta_structure.parse_state_progress(_git(repo, "show", f"{sha}:{eta_structure._STATE}")) or None
        commits.append({"at": int(at), "subject": normalise(subject), "add": sorted(ch["A"]), "modify": sorted(ch["M"]),
                        "delete": sorted(ch["D"]), "roadmap": roadmap, "state": state})
    commits.sort(key=lambda c: c["at"])  # stable: equal times keep git's order
    base_files = sorted(p for p in _git(repo, "ls-tree", "-r", "--name-only", base, "--", ".planning").splitlines()
                        if eta_structure._plan_name(p))
    base_roadmap = base_state = None
    for path, which in ((eta_structure._ROADMAP, "roadmap"), (eta_structure._STATE, "state")):
        res = subprocess.run(["git", "-C", repo, "show", f"{base}:{path}"], capture_output=True, text=True, check=False)
        if res.returncode == 0:
            if which == "roadmap":
                base_roadmap = eta_structure.parse_roadmap(res.stdout)
            else:
                base_state = eta_structure.parse_state_progress(res.stdout) or None
    timeline = {
        "job": job, "goal": goal, "t0": t0, "truth_min": truth_min,
        "attempts": [{"at": a, "no": i + 1, "rework": r} for i, (a, r) in enumerate(attempts)],
        "done": sorted(done), "sent_back": sorted(sent_back),
        "base": {"files": base_files, "roadmap": base_roadmap, "state": base_state},
        "commits": commits,
    }
    _check(timeline)
    return timeline


def _check(obj, path: str = "") -> None:
    if isinstance(obj, str) and len(obj) > MAX_STR:
        raise SystemExit(f"eta_timeline_fixture: string over {MAX_STR} chars at {path}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _check(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check(v, f"{path}[{i}]")


def _attempt(text: str) -> tuple[float, bool]:
    at, _, flag = text.partition(":")
    return float(at), flag == "rework"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--job", required=True)
    ap.add_argument("--t0", type=float, required=True)
    ap.add_argument("--truth-min", type=float, required=True)
    ap.add_argument("--goal", required=True)
    ap.add_argument("--attempt", action="append", default=[], type=_attempt)
    ap.add_argument("--done", action="append", default=[], type=float)
    ap.add_argument("--sent-back", action="append", default=[], type=float)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    timeline = build(a.repo, a.base, a.to, a.job, a.t0, a.truth_min, a.goal, a.attempt, a.done, a.sent_back)
    Path(a.out).write_text(json.dumps(timeline, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
