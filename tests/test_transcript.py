"""Worker transcript view: parsing, redaction, paging, CLI, MCP tool, phone screen."""
from __future__ import annotations

import io
import json

import pytest

from steward import cli, mcp_auth, mcp_server, phone, transcript
from steward.api import StewardAPI
from steward.chat import project_slug, worktree_path
from steward.models import JobState
from steward.store import Store

FAKE = ["sk-ant-api03-abcdefghijklmnopqrstuvwx", "ghp_abcdefghijklmnopqrstuvwxyz0123", "password=hunter2"]


def _ts(n):
    return f"2026-09-29T10:00:{n:02d}.000Z"


def _lines():
    return [
        {"type": "user", "timestamp": _ts(0), "message": {"role": "user", "content": "Build the thing"}},
        {"type": "assistant", "timestamp": _ts(1), "message": {"id": "m1", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "Starting " + FAKE[0]}]}},
        {"type": "assistant", "timestamp": _ts(1), "message": {"id": "m1", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "Starting " + FAKE[0]}]}},
        {"type": "assistant", "timestamp": _ts(2), "message": {"id": "m1", "content": [{"type": "thinking", "thinking": ""}]}},
        {"type": "assistant", "timestamp": _ts(3), "message": {"id": "m1", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo " + FAKE[1]}}]}},
        {"type": "user", "timestamp": _ts(4), "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "\n".join(f"line{i}" for i in range(20)) + "\n" + FAKE[2]}]}},
        {"type": "assistant", "timestamp": _ts(5), "message": {"id": "m2", "content": [{"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "/a/b.py", "old_string": "x", "new_string": "y"}}]}},
        {"type": "user", "timestamp": _ts(6), "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": [{"type": "text", "text": "no match"}]}]}},
        {"type": "assistant", "timestamp": _ts(7), "message": {"id": "m3", "content": [{"type": "tool_use", "id": "t3", "name": "Write", "input": {"file_path": "/c.py", "content": "a\nb\nc\n"}}]}},
        {"type": "assistant", "timestamp": _ts(8), "message": {"id": "m4", "content": [{"type": "tool_use", "id": "t4", "name": "Grep", "input": {"pattern": "foo"}}]}},
        {"type": "user", "timestamp": _ts(9), "message": {"content": "[rev=7] please use pytest"}},
    ]


@pytest.fixture
def env(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    proj = tmp_path / "projects"
    d = proj / project_slug(worktree_path(store, job.job_id))
    d.mkdir(parents=True)

    def write(name, rows):
        (d / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return d / f"{name}.jsonl"

    return store, job.job_id, proj, write


def _act(env, **kw):
    store, jid, proj, write = env
    return transcript.worker_activity(store, jid, projects_dir=proj, **kw)


def test_kinds_tools_results_and_redaction(env):
    store, jid, proj, write = env
    write("s1", _lines())
    store.kv_set(jid, "worker_session_id", "s1")
    a = _act(env, last=50)
    kinds = [t["kind"] for t in a["turns"]]
    assert kinds == ["prompt", "said", "tool", "result", "tool", "error", "tool", "tool", "steer"]  # dup + empty thinking dropped
    texts = [t["text"] for t in a["turns"]]
    assert texts[2].startswith("Bash: echo ") and "[redacted-token]" in texts[2]
    assert texts[4] == "Edit: /a/b.py" and texts[6] == "Write: /c.py (3 lines)"
    assert texts[7] == 'Grep: {"pattern":"foo"}'
    assert texts[8].startswith("[rev=7]")
    assert len(texts[3].splitlines()) == 9 and "(+13 lines)" in texts[3]
    blob = json.dumps(a)
    assert not any(s in blob for s in ["sk-ant-api03", "ghp_abc", "hunter2"])
    assert a["session_id"] == "s1" and a["model"] == "claude-sonnet-5-5" and a["turn_count"] == 9
    assert a["last_activity_ts"] == _ts(9)
    assert "waiting" in a["now"] and "Grep" in a["now"]


def test_result_password_redacted_when_visible(env):
    store, jid, proj, write = env
    write("s1", [{"type": "user", "timestamp": _ts(0), "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok\npassword=hunter2"}]}}])
    a = _act(env)
    assert a["turns"][0]["text"] == "ok\npassword=[redacted]"


def test_waiting_now_and_paging(env):
    store, jid, proj, write = env
    write("s1", _lines()[:5])
    a = _act(env, last=2)
    assert [t["i"] for t in a["turns"]] == [2, 3] or len(a["turns"]) == 2
    assert "waiting" in a["now"] and "Bash" in a["now"]
    cur = a["next_since"]
    assert _act(env, since=cur)["turns"] == []
    write("s1", _lines())
    more = _act(env, since=cur, last=50)
    assert [t["i"] for t in more["turns"]] == list(range(cur, 9))
    iso = _act(env, since=_ts(6), last=50)
    assert [t["kind"] for t in iso["turns"]] == ["tool", "tool", "steer"]


def test_only_own_session_and_fallback_discovery(env):
    store, jid, proj, write = env
    write("live", _lines()[:2])
    write("fork", _lines())
    store.kv_set(jid, "worker_session_id", "live")
    assert _act(env)["turn_count"] == 2
    store.kv_set(jid, "worker_session_id", "")
    # fallback: newest file that is not a watcher run
    import os
    p = write("watcher", [{"type": "user", "message": {"content": "You are a Steward custom watcher named x"}}])
    os.utime(p, (9e9 - 1, 9e9 - 1))
    a = _act(env)
    assert a["session_id"] in ("fork", "live") and a["turn_count"] >= 2


def test_no_transcript(env):
    a = _act(env)
    assert a["turns"] == [] and a["session_id"] is None


def test_total_cap(env):
    store, jid, proj, write = env
    write("s1", [{"type": "assistant", "timestamp": _ts(i % 60), "message": {"id": f"m{i}", "content": [{"type": "text", "text": "x" * 500 + str(i)}]}} for i in range(60)])
    a = _act(env, last=60)
    assert sum(len(t["text"]) for t in a["turns"]) <= 8000 + 500
    assert a["truncated"] and a["turns"][-1]["i"] == 59


def test_cli_render_and_json(env, monkeypatch, capsys):
    store, jid, proj, write = env
    write("s1", _lines())
    out = io.StringIO()
    transcript.run(store, jid, last=20, out=out, projects_dir=proj)
    s = out.getvalue()
    assert "said   Starting" in s and "tool   Bash:" in s and "→ ERR  no match" in s and "STEER rev 7 please use pytest" in s
    assert "\x1b" not in s
    out = io.StringIO()
    transcript.run(store, jid, last=3, as_json=True, out=out, projects_dir=proj)
    assert len(json.loads(out.getvalue())["turns"]) == 3
    assert "\x1b[" in transcript.render_turn(_act(env)["turns"][0], color=True)


def test_cli_follow_stops_when_terminal(env):
    store, jid, proj, write = env
    write("s1", _lines()[:3])
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.STOPPED.value, jid))
    out = io.StringIO()
    assert transcript.run(store, jid, last=5, follow=True, out=out, sleep=lambda s: None, projects_dir=proj) == 0


def test_cli_command_unknown_job(steward_home, capsys):
    assert cli.main(["transcript", "nope"]) == 1


def test_mcp_tool(env, steward_home, monkeypatch):
    store, jid, proj, write = env
    write("s1", _lines())
    store.kv_set(jid, "worker_session_id", "s1")
    monkeypatch.setattr(transcript.Path, "home", classmethod(lambda c: proj.parent.parent))
    monkeypatch.setattr(transcript, "worker_file", lambda s, j, projects_dir=None: proj / project_slug(worktree_path(s, j)) / "s1.jsonl")
    tools = {t.name: t for t in mcp_server.build_tools(StewardAPI(store))}
    t = tools["get_worker_activity"]
    assert t.required_scope == "runner:read" and t.annotations == {"readOnlyHint": True}
    out = t.handler({"job_id": jid, "last": 4}, None)
    assert len(out["turns"]) == 4 and out["text"].startswith("now: ") and "STEER rev 7" in out["text"]
    with pytest.raises(mcp_server.ToolError):
        t.handler({"job_id": "nope"}, None)


def test_phone_transcript_screen_40_cols(env, monkeypatch):
    store, jid, proj, write = env
    write("s1", _lines())
    store.kv_set(jid, "worker_session_id", "s1")
    monkeypatch.setattr(transcript, "worker_file", lambda s, j, projects_dir=None: proj / project_slug(worktree_path(s, j)) / "s1.jsonl")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (JobState.RUNNING.value, jid))
    p = phone.Phone(store, run_cli=lambda a: (0, ""), tokens_fn=lambda s: None)
    p.handle("1")
    p.handle("v")
    assert p.screen == "transcript"
    lines = p.lines(40, 24)
    text = [phone.plain(l) for l in lines]
    assert all(phone.vwidth(l.row) <= 40 for l in lines)
    assert text[0].startswith("WORKER ") and any("STEER 7" in x for x in text) and any("Grep" in x for x in text)
    p.handle("k")
    assert p.feed_off == 1
    p.handle("b")
    assert p.screen == "job"
