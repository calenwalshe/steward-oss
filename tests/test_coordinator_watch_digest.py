import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from steward import coordinator_link
from steward.coordinator import db, digest, watch
from steward.coordinator import __main__ as cmain

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_COORDINATOR_STATE", str(tmp_path))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))


def _sender():
    sent = []

    def send(text):
        sent.append(text)
        return True
    return sent, send


def _hb(ts):
    coordinator_link.write_heartbeat(cursor=1, head="h", root="r", now=ts)


def test_watch_alerts_once_then_recovers(tmp_path):
    sent, send = _sender()
    st = tmp_path / "watch.json"
    now = time.time()
    _hb(now - 700)
    assert watch.check(now=now, unit_active=True, send=send, state_path=st) == "alerted"
    assert sent[0].startswith("steward-coordinator: silent since ") and "(" in sent[0]
    assert json.loads(st.read_text())["alerted_at"] == now
    assert watch.check(now=now + 1, unit_active=True, send=send, state_path=st) == "already_alerted"
    assert len(sent) == 1
    _hb(now + 2)
    assert watch.check(now=now + 3, unit_active=True, send=send, state_path=st) == "recovered"
    assert sent[1].startswith("steward-coordinator: back after ") and sent[1].endswith(" min")
    assert not st.exists() or not json.loads(st.read_text())
    assert watch.check(now=now + 4, unit_active=True, send=send, state_path=st) == "ok"


def test_watch_tg_failure_retries_without_state(tmp_path):
    st = tmp_path / "w.json"
    now = time.time()
    _hb(now - 700)
    assert watch.check(now=now, unit_active=True, send=lambda t: False, state_path=st) == "tg_failed"
    assert not st.exists()
    sent, send = _sender()
    assert watch.check(now=now + 1, unit_active=True, send=send, state_path=st) == "alerted"


def test_watch_recovery_send_failure_keeps_state(tmp_path):
    st = tmp_path / "w.json"
    now = time.time()
    _hb(now - 700)
    watch.check(now=now, unit_active=True, send=lambda t: True, state_path=st)
    _hb(now + 1)
    assert watch.check(now=now + 2, unit_active=True, send=lambda t: False, state_path=st) == "tg_failed"
    assert st.exists()
    assert watch.check(now=now + 3, unit_active=True, send=lambda t: True, state_path=st) == "recovered"


def test_watch_killed_is_silent_and_clears_state(tmp_path):
    sent, send = _sender()
    st = tmp_path / "w.json"
    now = time.time()
    _hb(now - 9000)
    (tmp_path / "KILL").write_text("")
    assert watch.check(now=now, unit_active=False, send=send, state_path=st) == "killed"
    assert sent == [] and not st.exists()


def test_watch_disabled_in_config_is_killed(tmp_path):
    (tmp_path / "coordinator.toml").write_text("enabled = false\n")
    sent, send = _sender()
    assert watch.check(now=time.time(), unit_active=False, send=send, state_path=tmp_path / "w.json") == "killed"
    assert sent == []


def test_watch_unit_inactive_alerts(tmp_path):
    sent, send = _sender()
    now = time.time()
    _hb(now - 5)
    assert watch.check(now=now, unit_active=False, send=send, state_path=tmp_path / "w.json") == "alerted"
    assert "unit inactive" in sent[0]


def test_watch_no_heartbeat_unit_active_alerts_never_started(tmp_path):
    sent, send = _sender()
    assert watch.check(now=time.time(), unit_active=True, send=send, state_path=tmp_path / "w.json") == "alerted"
    assert "never started" in sent[0]


def test_watch_send_raising_is_tg_failed(tmp_path):
    def boom(text):
        raise RuntimeError("x")
    assert watch.check(now=time.time(), unit_active=True, send=boom, state_path=tmp_path / "w.json") == "tg_failed"


def _cdb(tmp_path):
    cdb = db.CoordDB(tmp_path / "c.db")
    now = time.time()
    e = cdb.open_episode("abcdef12-x", "needs_human:recovery_budget_exhausted", 1, now=now - 900)
    cdb.mark(e, unstuck_at=now - 300)
    cdb.merge_evidence(e, {"delivery": "delivered"})
    cdb.close(e, "resolved", "env-dependent ids <script>alert(1)</script>", ["approve", "steer"], now=now - 300,
              rule_proposal_id="312", rungs=[{"rung": "false_red", "classification": "env_dependent"}])
    e2 = cdb.open_episode("ffffffff-y", "orphan:RUNNING", 2, now=now - 100)
    cdb.close(e2, "asked", "no verb", [], now=now - 50)
    return cdb, now


def test_digest_renders_content(tmp_path):
    cdb, now = _cdb(tmp_path)
    cdb.spend(now, usd=1.5, codex_calls=2)
    html = digest.render(cdb, since=now - 86400, until=now, metrics={"touches_per_job": 0.2, "coordinator_actions": 7,
                         "asks": 1}, absorbed_fn=lambda tid: tid == "312")
    for needle in ("abcdef12", "false_red", "env_dependent", "resolved", "asked", "10 min", "312", "0.2", "delivered",
                   "<style>", "precision"):
        assert needle in html, needle
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "http" not in html


def test_digest_precision_and_absorbed(tmp_path):
    cdb, now = _cdb(tmp_path)
    eid = cdb.episodes_since(0)[0]["episode_id"]
    cdb.overturn(eid, "wrong", now)
    html = digest.render(cdb, since=now - 86400, until=now, metrics=None, absorbed_fn=lambda tid: True)
    assert "metrics unavailable" in html
    assert "0%" in html  # 1 overturn / 1 action
    assert "1 proposed" in html and "1 absorbed" in html


def test_digest_survives_bad_absorbed(tmp_path):
    cdb, now = _cdb(tmp_path)

    def boom(tid):
        raise RuntimeError("x")
    assert "312" in digest.render(cdb, since=0, until=now, metrics=None, absorbed_fn=boom)


def _fake_run(stdout="https://pixel.example/s/x/\n", rc=0, seen=None):
    def run(argv, **kw):
        if seen is not None:
            seen["argv"], seen["kw"] = argv, kw
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr="")
    return run


def test_publish_argv_url_and_file(tmp_path):
    seen = {}
    url = digest.publish("<html>x</html>", slug="slug1", run=_fake_run("done\nhttps://p.example/a/ ok\n", seen=seen))
    assert url == "https://p.example/a/"
    argv = seen["argv"]
    assert argv[:3] == ["pixel", "update", "slug1"] and argv[3] == str(tmp_path / "digest.html")
    assert argv[4] == "--title" and argv[5].startswith("Steward coordinator — ")
    assert seen["kw"]["timeout"] == 120 and seen["kw"]["capture_output"] is True
    assert (tmp_path / "digest.html").read_text() == "<html>x</html>"


def test_publish_none_cases():
    assert digest.publish("x", slug="s", run=_fake_run(rc=1)) is None
    assert digest.publish("x", slug="s", run=_fake_run(stdout="no url")) is None

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired("pixel", 1)
    assert digest.publish("x", slug="s", run=boom) is None


def test_main_digest_render_and_publish(tmp_path, capsys):
    cdb, now = _cdb(tmp_path)
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="https://p.example/d/\n", stderr="")
    assert cmain.main(["digest"], run=run, cdb=cdb, clock=lambda: now, settings=SimpleNamespace(
        digest_slug="sl")) == 0
    assert "abcdef12" in capsys.readouterr().out
    assert not any(c[0] == "pixel" for c in calls)
    assert cmain.main(["digest", "--publish"], run=run, cdb=cdb, clock=lambda: now,
                      settings=SimpleNamespace(digest_slug="sl")) == 0
    assert any(c[:3] == ["pixel", "update", "sl"] for c in calls)


def test_deploy_files():
    d = REPO / "deploy"
    wrapper = d / "run-steward-coordinator.sh"
    assert subprocess.run(["bash", "-n", str(wrapper)]).returncode == 0
    text = wrapper.read_text()
    assert "api-keys" not in text and ".env" not in text
    assert not any(ln.strip().startswith(("source ", ". ")) for ln in text.splitlines())
    assert 'exec env -u ANTHROPIC_API_KEY "$PYTHON" -m steward.coordinator "${1:-run}"' in text
    svc = (d / "steward-coordinator.service").read_text()
    assert "run-steward-coordinator.sh run" in svc and "Restart=on-failure" in svc and "RestartSec=10" in svc
    assert "NoNewPrivileges=yes" in svc
    assert not any(ln.startswith("EnvironmentFile=") for ln in svc.splitlines())
    w = (d / "steward-coordinator-watch.service").read_text()
    assert "Type=oneshot" in w and "run-steward-coordinator.sh watch" in w
    t = (d / "steward-coordinator-watch.timer").read_text()
    assert "OnUnitActiveSec=5min" in t and "OnBootSec=2min" in t and "WantedBy=timers.target" in t
