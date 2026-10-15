import time

from steward.coordinator import db, rules

RG = [{"rung": "false_red", "classification": "env_dependent"}]
DAY = 86400.0


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _close(cdb, outcome, now, rungs=RG):
    eid = cdb.open_episode("j", "t", 1, now=now)
    cdb.close(eid, outcome, "d", [], now=now + 1, rungs=rungs)


def test_novel_window(tmp_path):
    cdb = db.CoordDB(tmp_path / "c.db"); now = time.time()
    assert rules.novel(cdb, "false_red", "env_dependent", now)
    _close(cdb, "resolved", now)
    assert not rules.novel(cdb, "false_red", "env_dependent", now + 2)
    assert rules.novel(cdb, "false_red", "preexisting", now + 2)
    assert rules.novel(cdb, "other_rung", "env_dependent", now + 2)


def test_novel_ignores_asked_and_old(tmp_path):
    cdb = db.CoordDB(tmp_path / "c.db"); now = time.time()
    _close(cdb, "asked", now)
    assert rules.novel(cdb, "false_red", "env_dependent", now + 2)
    _close(cdb, "resolved", now - 40 * DAY)
    assert rules.novel(cdb, "false_red", "env_dependent", now + 2)


def test_file_argv_body_and_id():
    seen = {}

    def fake(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        path = argv[argv.index("--body-file") + 1]
        seen["path"] = path
        with open(path) as fh:
            seen["body"] = fh.read()
        return R(0, "filed #312 — rule: closeout rerun failing ids\n")

    ep = {"episode_id": "ep123", "job_id": "abcdef0123456789", "trigger": "park",
          "classification": "env_dependent", "diagnosis": "flaky env", "evidence_json": '{"x": 1}'}
    assert rules.file(ep, "closeout: rerun failing ids", run=fake) == "312"
    a = seen["argv"]
    assert a[:2] == ["tasks", "add"] and a[2].startswith("rule: closeout")
    assert a[a.index("--area") + 1] == "steward"
    assert [a[i + 1] for i, v in enumerate(a) if v == "--tag"] == ["rule-proposal", "steward-coordinator"]
    assert "ep123" in seen["body"] and "closeout: rerun failing ids" in seen["body"]
    assert "abcdef01" in seen["body"] and "absorbed" in seen["body"]
    import os
    assert not os.path.exists(seen["path"])
    assert seen["kw"].get("timeout") == 30


def test_file_failures_return_none():
    ep = {"episode_id": "e"}
    assert rules.file(ep, "x", run=lambda a, **k: R(1, "", "bad")) is None
    assert rules.file(ep, "x", run=lambda a, **k: R(0, "garbage")) is None

    def boom(argv, **kw):
        raise OSError("no tasks")
    assert rules.file(ep, "x", run=boom) is None


def test_absorbed():
    assert rules.absorbed("5", run=lambda a, **k: R(0, "title\n  Absorbed: abc123\n")) is True
    assert rules.absorbed("5", run=lambda a, **k: R(0, "title\nnot absorbed: no\n")) is False
    assert rules.absorbed("5", run=lambda a, **k: R(1, "absorbed: x")) is False

    def boom(argv, **kw):
        raise OSError
    assert rules.absorbed("5", run=boom) is False
