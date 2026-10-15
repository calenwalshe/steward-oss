from pathlib import Path

from steward import coordinator_link as cl


def test_state_dir_default_follows_steward_home_and_env_overrides(steward_home, tmp_path, monkeypatch):
    monkeypatch.delenv("STEWARD_COORDINATOR_STATE", raising=False)
    assert cl.state_dir() == Path(steward_home).parent / "steward-coordinator"
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path / "s"))
    assert cl.state_dir() == tmp_path / "s" and cl.kill_file() == tmp_path / "s" / "KILL"


def test_heartbeat_round_trip_and_freshness(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path))
    assert cl.read_heartbeat() is None and not cl.heartbeat_fresh(now=1000.0)
    cl.write_heartbeat(cursor=7, head="abc", root="/r", now=1000.0)
    hb = cl.read_heartbeat()
    assert hb["cursor"] == 7 and hb["head"] == "abc" and hb["root"] == "/r" and hb["ts"] == 1000.0 and "pid" in hb
    assert cl.heartbeat_fresh(now=1119.0) and not cl.heartbeat_fresh(now=1121.0)
    assert not list(tmp_path.glob("*.tmp"))


def test_killed(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path))
    assert not cl.killed()
    (tmp_path / "KILL").write_text("")
    assert cl.killed()


def test_component_states(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path))
    assert (cl.component(now=1.0).ok, cl.component(now=1.0).detail) == (None, "never started")
    cl.write_heartbeat(cursor=3, head="aaa", root="/r", now=1000.0)
    same = lambda root: "aaa"
    c = cl.component(head_fn=same, now=1010.0)
    assert c.ok is True and "cursor 3" in c.detail
    c = cl.component(head_fn=same, now=1200.0)
    assert c.ok is False and "silent for 200s" in c.detail
    c = cl.component(head_fn=lambda root: "bbb", now=1010.0)
    assert c.ok is False and c.problems
    (tmp_path / "KILL").write_text("")
    c = cl.component(head_fn=same, now=1010.0)
    assert c.ok is None and "KILL" in c.detail
