"""The committed content-free planning timelines (phase 18-02, G21, T-18-06)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eta_fixtures import FIXTURES, load_timeline

JOBS = sorted(p.stem for p in FIXTURES.glob("*.json") if p.stem != "jev_contexts")  # jev_contexts.json is the 18-04 Jev smoke input, not a timeline
SUBJECT = re.compile(r"^[a-z]+(\([0-9.,-]+\))?: (plan|summary|verification|context|work)$")
KEYS = {"job", "goal", "t0", "truth_min", "attempts", "done", "sent_back", "base", "commits"}


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def test_three_calibration_timelines_are_committed():
    assert JOBS == ["06ea6a04", "a7d292d8", "ed11ba3c"]


@pytest.mark.parametrize("job", JOBS)
def test_timeline_shape_and_privacy(job):
    tl = load_timeline(job)
    assert set(tl) == KEYS
    assert set(tl["base"]) == {"files", "roadmap", "state"}
    assert tl["job"] == job
    last = 0
    for c in tl["commits"]:
        assert set(c) == {"at", "subject", "add", "modify", "delete", "roadmap", "state"}
        assert SUBJECT.match(c["subject"]), c["subject"]
        assert c["at"] >= last
        last = c["at"]
        for p in c["add"] + c["modify"] + c["delete"]:
            assert p.startswith(".planning/")
    for p in tl["base"]["files"]:
        assert p.startswith(".planning/")
    assert max(len(s) for s in _strings(tl)) <= 120


def _at(tl, c):
    return (c["at"] - tl["t0"]) / 60


def test_a7d292d8_timeline_facts():
    tl = load_timeline("a7d292d8")
    assert tl["truth_min"] == 128.4
    plan = [c for c in tl["commits"] if c["add"] and "PLAN" in c["add"][0]]
    assert len(plan) == 1 and abs(_at(tl, plan[0]) - 34.2) <= 0.2
    assert [Path(p).name for p in plan[0]["add"]] == [f"16-0{i}-PLAN.md" for i in range(1, 8)]
    summ = [c for c in tl["commits"] if c["add"] and "SUMMARY" in c["add"][0]]
    assert [len(c["add"]) for c in summ] == [1] * 7
    for c, want in zip(summ, (40.8, 53.8, 63.8, 78.7, 95.2, 109.0, 125.5)):
        assert abs(_at(tl, c) - want) <= 0.2
    names = tl["base"]["files"]
    assert len(names) == 51
    assert not [n for n in names if "/16-" in n]


def test_06ea6a04_and_ed11ba3c_timeline_facts():
    tl = load_timeline("06ea6a04")
    assert tl["truth_min"] == 423.0 and tl["base"]["files"] == [] and len(tl["attempts"]) == 1
    first = tl["commits"][0]
    assert abs(_at(tl, first) - 13.7) <= 0.2 and ".planning/ROADMAP.md" in first["add"] and first["roadmap"] == {str(i): None for i in range(1, 9)}
    plans = sum(1 for c in tl["commits"] for p in c["add"] if p.endswith("-PLAN.md"))
    sums = sum(1 for c in tl["commits"] for p in c["add"] if p.endswith("-SUMMARY.md"))
    assert (plans, sums) == (23, 23)
    ed = load_timeline("ed11ba3c")
    assert ed["truth_min"] == 181.2 and [a["rework"] for a in ed["attempts"]] == [False, True]
    assert len(ed["done"]) == 2 and len(ed["sent_back"]) == 1
    assert sum(1 for c in ed["commits"] for p in c["add"] if p.endswith("-PLAN.md")) == 7


def _source_repo(job):
    import subprocess

    repo = {"06ea6a04": "/home/user/work/steward-android"}.get(job, str(Path(__file__).resolve().parents[1]))
    base, to = {"06ea6a04": ("da1182d", "3406179"), "a7d292d8": ("4bb3730", "f72c9d2"), "ed11ba3c": ("fc0b9a1", "dcb7bd9")}[job]
    for c in (base, to):
        if subprocess.run(["git", "-C", repo, "cat-file", "-e", c], capture_output=True).returncode != 0:
            pytest.skip(f"{job}: source commit {c} is not in {repo}")
    return repo, base, to


@pytest.mark.parametrize("job", JOBS)
def test_builder_reproduces_the_committed_fixture(job):
    """The builder (read-only git) regenerates the committed timeline byte for byte from the source repo."""
    import json
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import eta_timeline_fixture as builder

    repo, base, to = _source_repo(job)
    tl = load_timeline(job)
    att = [(a["at"], a["rework"]) for a in tl["attempts"]]
    got = builder.build(repo, base, to, job, tl["t0"], tl["truth_min"], tl["goal"], att, tl["done"], tl["sent_back"])
    assert json.loads(json.dumps(got, sort_keys=True)) == tl
