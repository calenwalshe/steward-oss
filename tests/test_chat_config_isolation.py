"""ensure_trusted() honors $STEWARD_CLAUDE_JSON, so a test that exports a job (export.py -> ensure_trusted) without
patching chat.CLAUDE_CONFIG never writes the owner's real ~/.claude.json (it once left ~2,100 trust entries there)."""
import json

from steward import chat as chat_module
from steward.chat import claude_config_path, ensure_trusted


def test_env_override_is_used_when_constant_unpatched(tmp_path, monkeypatch):
    cfg = tmp_path / "claude.json"
    cfg.write_text("{}")
    monkeypatch.setenv("STEWARD_CLAUDE_JSON", str(cfg))
    assert claude_config_path() == cfg
    ensure_trusted("/work/x")
    assert json.loads(cfg.read_text())["projects"]["/work/x"]["hasTrustDialogAccepted"] is True


def test_conftest_isolation_points_away_from_real_home(tmp_path):
    from pathlib import Path

    assert claude_config_path() != Path.home() / ".claude.json"  # the autouse fixture sets STEWARD_CLAUDE_JSON


def test_patched_constant_still_wins(tmp_path, monkeypatch):
    patched = tmp_path / "patched.json"
    patched.write_text("{}")
    monkeypatch.setenv("STEWARD_CLAUDE_JSON", str(tmp_path / "env.json"))
    monkeypatch.setattr(chat_module, "CLAUDE_CONFIG", patched)
    ensure_trusted("/work/y")
    assert "/work/y" in json.loads(patched.read_text())["projects"]
    assert not (tmp_path / "env.json").exists()


def test_no_env_falls_back_to_home(monkeypatch):
    from pathlib import Path

    monkeypatch.delenv("STEWARD_CLAUDE_JSON", raising=False)
    assert claude_config_path() == Path.home() / ".claude.json"
