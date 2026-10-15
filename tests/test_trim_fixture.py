"""16-07 task 1: the read-only fixture trimmer is content-free, an independent recompute, deterministic and read-only."""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

from steward import tokens

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "trim_efficiency_fixture.py"
SECRETS = ["SECRET-PROMPT", "SECRET-TEXT", "SECRET-CMD", "SECRET-PATH", "SECRET-CODE", "SECRET-OUTPUT",
           "SECRET-SUB", "SECRET-CHAT", "SECRET-META", "SECRET-WATCHER", "SECRET-THINK"]
W = "wwww1111-0000-0000-0000-000000000000"


def _iso(sec: int) -> str:
    return f"2026-09-29T10:{sec // 60:02d}:{sec % 60:02d}.500Z"


def _asst(sec, mid, block, usage, model="claude-sonnet-5-5", stop=None, effort="high"):
    return {"type": "assistant", "timestamp": _iso(sec), "effort": effort,
            "message": {"id": mid, "model": model, "role": "assistant", "stop_reason": stop, "usage": usage,
                        "content": [block]}}


def _u(i, o, cr, cc):
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc}


def _user(sec, content):
    return {"type": "user", "timestamp": _iso(sec), "message": {"role": "user", "content": content}}


def _write(path: Path, objs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(o) for o in objs) + "\n")


# usage table: message id -> (model, [in, out, cr, cc]) as the independent hand computation sees it
U = {
    "w1": ("claude-sonnet-5-5", [10, 150, 5000, 200]),  # the final (largest) output of a streamed id
    "w2": ("claude-sonnet-5-5", [10, 300, 6000, 0]),
    "w3": ("claude-opus-5-5", [20, 50, 1000, 400]),
    "s1": ("claude-sonnet-5-5", [10, 500, 9000, 100]),
    "s2": ("claude-haiku-4-5", [5, 80, 100, 50]),
    "c1": ("claude-sonnet-5-5", [7, 70, 700, 70]),
    "p1": ("claude-haiku-4-5", [4, 40, 400, 40]),
}


def make_project(root: Path) -> Path:
    proj = root / "proj"
    a = lambda sec, mid, block, **kw: _asst(sec, mid, block, _u(*U[mid][1]), model=U[mid][0], **kw)  # noqa: E731
    _write(proj / f"{W}.jsonl", [
        _user(0, "SECRET-PROMPT do the thing"),
        _asst(1, "w1", {"type": "thinking", "thinking": "SECRET-THINK", "signature": "sig"}, _u(10, 100, 5000, 200)),
        _asst(2, "w1", {"type": "text", "text": "SECRET-TEXT hello"}, _u(10, 100, 5000, 200)),
        # a streamed repeat of w1: the LARGER output must win in the independent recompute
        _asst(3, "w1", {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "SECRET-CMD ls"}},
              _u(10, 100, 5000, 200), stop="tool_use"),
        _user(4, [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "SECRET-OUTPUT " + "o" * 30}]),
        a(5, "w2", {"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {"file_path": "/SECRET-PATH/a.py"}},
          stop="tool_use"),
        _user(6, [{"type": "tool_result", "tool_use_id": "toolu_2", "content": [{"type": "text", "text": "SECRET-OUTPUT"}]}]),
        a(7, "w3", {"type": "tool_use", "id": "toolu_3", "name": "Write",
                    "input": {"file_path": "/SECRET-PATH/b.py", "content": "SECRET-CODE" + "c" * 89}}, stop="tool_use"),
        {"type": "attachment", "timestamp": _iso(8), "attachment": {"text": "SECRET-TEXT"}},
        {"type": "last-prompt", "lastPrompt": "SECRET-PROMPT"},  # no timestamp: dropped
    ])
    with open(proj / f"{W}.jsonl", "a") as fh:  # the larger-output repeat of w1
        fh.write(json.dumps(a(9, "w1", {"type": "text", "text": "x"})) + "\n")
    sub = proj / W / "subagents"
    _write(sub / "agent-a1.jsonl", [
        _user(20, "SECRET-SUB prompt"),
        a(21, "s1", {"type": "text", "text": "SECRET-TEXT sub"}, stop="end_turn"),
        a(22, "s2", {"type": "text", "text": "done"}, stop="end_turn"),
        a(23, "w2", {"type": "text", "text": "shared with the worker"}),  # shared id: the worker owns it
    ])
    (sub / "agent-a1.meta.json").write_text(json.dumps({
        "agentType": "gsd-executor", "description": "Execute phase 4 plans", "toolUseId": "toolu_x", "spawnDepth": 1,
        "requestShape": "background", "requestNonInteractive": True, "extra": "SECRET-META"}))
    _write(proj / "cccc2222.jsonl", [_user(30, "SECRET-CHAT question"),
                                     a(31, "c1", {"type": "text", "text": "SECRET-TEXT answer"}, stop="end_turn"),
                                     a(32, "w1", {"type": "text", "text": "fork of the worker's history"})])
    _write(proj / "pppp3333.jsonl", [
        _user(40, "You are a Steward custom watcher named pace, attached to job SECRET-WATCHER. Do the thing."),
        a(41, "p1", {"type": "text", "text": "ok"}, stop="end_turn")])
    return proj


def run_trim(proj: Path, out: Path, *extra, check=True):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--project-dir", str(proj), "--worker-session", W, "--job", "j1",
         "--out", str(out), *extra], capture_output=True, text=True, check=check, cwd=ROOT)


def load(out: Path) -> dict:
    return json.loads(gzip.decompress(out.read_bytes()))


def test_trim_drops_every_secret(tmp_path):
    proj = make_project(tmp_path)
    out = tmp_path / "f.json.gz"
    run_trim(proj, out)
    raw = gzip.decompress(out.read_bytes()).decode()
    for s in SECRETS:
        assert s not in raw, s
    data = json.loads(raw)
    by_rel = {f["rel"]: f for f in data["files"]}
    sub = by_rel[f"{W}/subagents/agent-a1.jsonl"]
    assert sub["kind"] == "subagent"
    assert sub["meta"] == {"agentType": "gsd-executor", "description": "Execute phase 4 plans",
                           "toolUseId": "toolu_x", "spawnDepth": 1, "requestShape": "background",
                           "requestNonInteractive": True}
    assert by_rel[f"{W}.jsonl"]["kind"] == "worker" and by_rel["cccc2222.jsonl"]["kind"] == "toplevel"
    assert by_rel["pppp3333.jsonl"]["marker"] == "You are a Steward custom watcher named pace, "
    assert by_rel["cccc2222.jsonl"]["marker"] == ""
    # allow-list schema: an assistant record holds only numbers, ids, hashes and tool names
    a = [r for r in by_rel[f"{W}.jsonl"]["lines"] if r["k"] == "a"]
    assert set(a[0]) == {"t", "k", "id", "m", "u", "v", "s", "e", "tu"}
    tool = next(t for r in a for t in r["tu"] if t[1] == "Bash")
    assert tool[2] == tokens._key10("SECRET-CMD ls")
    write = next(t for r in a for t in r["tu"] if t[1] == "Write")
    assert write[3] == tokens._key10("/SECRET-PATH/b.py") and write[4] == 100
    users = [r for r in by_rel[f"{W}.jsonl"]["lines"] if r["k"] == "u"]
    assert users[1]["r"] == [["toolu_1", len("SECRET-OUTPUT ") + 30]]
    assert users[2]["r"] == [["toolu_2", len("SECRET-OUTPUT")]]
    assert not any(r["k"] == "o" and r.get("y") == "last-prompt" for r in by_rel[f"{W}.jsonl"]["lines"])


def test_trim_expected_is_an_independent_recompute(tmp_path):
    proj = make_project(tmp_path)
    out = tmp_path / "f.json.gz"
    run_trim(proj, out)
    exp = load(out)["expected"]
    # worker owns w1 (largest output 150), w2, w3; the sub owns s1, s2 (w2 is the worker's); c1 chat; p1 watcher.
    # The chat's fork of w1 and the sub's w2 are duplicates and count nothing.
    px = tokens.price_usd
    worker = px(*U["w1"]) + px(*U["w2"]) + px(*U["w3"])
    sub = px(*U["s1"]) + px(*U["s2"])
    chat = px(*U["c1"])
    watcher = px(*U["p1"])
    assert abs(exp["usd_worker"] - worker) < 1e-12
    assert abs(exp["usd_sub"] - sub) < 1e-12
    assert abs(exp["usd_total"] - (worker + sub + chat + watcher)) < 1e-12
    assert set(exp["usd_by_source"]) == {"worker", "sub:gsd-executor", "chats", "watcher:pace"}
    assert abs(exp["usd_by_source"]["chats"] - chat) < 1e-12
    assert abs(exp["usd_by_source"]["watcher:pace"] - watcher) < 1e-12
    assert exp["calls_by_rel"] == {f"{W}.jsonl": 3, f"{W}/subagents/agent-a1.jsonl": 2, "cccc2222.jsonl": 1,
                                   "pppp3333.jsonl": 1}
    assert exp["generated_until"].startswith("2026-09-29T10:00:41")


def test_trim_until_cuts_lines_and_expected(tmp_path):
    proj = make_project(tmp_path)
    out = tmp_path / "f.json.gz"
    run_trim(proj, out, "--until", _iso(25))
    data = load(out)
    assert all(r["t"] <= data_epoch(_iso(25)) for f in data["files"] for r in f["lines"])
    assert "pppp3333.jsonl" in {f["rel"] for f in data["files"]}  # kept, with no lines
    assert data["expected"]["usd_by_source"].get("watcher:pace") is None


def data_epoch(iso: str) -> float:
    return tokens._epoch(iso)


def _snapshot(d: Path):
    return sorted((str(p.relative_to(d)), p.stat().st_mtime_ns, p.stat().st_size) for p in d.rglob("*"))


def test_trim_is_deterministic_and_read_only(tmp_path):
    proj = make_project(tmp_path)
    before = _snapshot(proj)
    out1, out2 = tmp_path / "o" / "a.json.gz", tmp_path / "o" / "b.json.gz"
    run_trim(proj, out1)
    run_trim(proj, out2)
    assert out1.read_bytes() == out2.read_bytes()
    assert _snapshot(proj) == before
    assert not any(str(p).startswith(str(proj)) for p in (out1, out2))


def test_trim_refuses_output_inside_the_source(tmp_path):
    proj = make_project(tmp_path)
    r = run_trim(proj, proj / "f.json.gz", check=False)
    assert r.returncode != 0 and not (proj / "f.json.gz").exists()


def test_trim_refuses_long_strings(tmp_path):
    proj = make_project(tmp_path)
    meta = proj / W / "subagents" / "agent-a1.meta.json"
    m = json.loads(meta.read_text())
    m["description"] = "d" * 500
    meta.write_text(json.dumps(m))
    out = tmp_path / "f.json.gz"
    r = run_trim(proj, out, check=False)
    assert r.returncode != 0
    assert not out.exists()


# -- 18-01 (T-18-01): the privacy checks run over every committed fixture -----------------------------------------

import re  # noqa: E402

import pytest  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "efficiency"
FIXTURE_FILES = sorted(p.name for p in FIXTURE_DIR.glob("*.json.gz"))
_META_KEYS = {"agentType", "description", "requestNonInteractive", "requestShape", "spawnDepth", "toolUseId"}
_LINE_KEYS = {"a": {"e", "id", "k", "m", "s", "t", "tu", "u", "v"}, "u": {"k", "r", "t"}, "o": {"k", "t", "y"}}
_KEY10 = re.compile(r"[0-9a-f]{10}")
_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,60}")


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _strings(v)


def test_there_are_at_least_three_fixtures():
    assert {"06ea6a04.json.gz", "a7d292d8.json.gz", "ed11ba3c.json.gz"} <= set(FIXTURE_FILES)


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_fixture_is_content_free_and_self_consistent(name):
    raw = gzip.decompress((FIXTURE_DIR / name).read_bytes())
    doc = json.loads(raw)
    assert set(doc) == {"expected", "files", "job", "worker_session"}
    assert all(len(s) <= 200 for s in _strings(doc))  # the trimmer's own abort threshold
    text = raw.decode()
    assert "/home/" not in text and "/tmp/" not in text and "SECRET" not in text
    rels = {f["rel"] for f in doc["files"]}
    assert sum(1 for f in doc["files"] if f["kind"] == "worker") == 1
    assert any(f["kind"] == "worker" and f["rel"] == f"{doc['worker_session']}.jsonl" for f in doc["files"])
    for f in doc["files"]:
        assert set(f) == {"kind", "lines", "marker", "meta", "rel"}
        assert f["kind"] in ("worker", "subagent", "toplevel") and set(f["meta"]) <= _META_KEYS
        assert f["marker"] in ("", ) or f["marker"].startswith("You are a Steward custom watcher named ")
        for r in f["lines"]:
            assert set(r) == _LINE_KEYS[r["k"]]  # an allow-listed schema: numbers, ids, hashes and tool names only
            if r["k"] == "a":
                for t in r["tu"]:
                    assert _TOOL_NAME.fullmatch(t[1]) and all(x == "" or _KEY10.fullmatch(x) for x in t[2:4])
    exp = doc["expected"]
    assert set(exp) == {"calls_by_rel", "generated_until", "usd_by_source", "usd_sub", "usd_total", "usd_worker"}
    assert set(exp["calls_by_rel"]) == rels
    assert abs(exp["usd_total"] - sum(exp["usd_by_source"].values())) < 1e-9
    assert exp["usd_worker"] + exp["usd_sub"] <= exp["usd_total"] + 1e-9
    assert exp["usd_total"] > 0
